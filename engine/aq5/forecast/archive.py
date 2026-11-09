"""Encrypted immutable forecast history; compact only after confirmed remote push.

No plaintext history is written to disk. The live SQLite rows keep their original
hash and all evaluation/execution metadata. Rehydration verifies both ciphertext
and the exact original JSON record before returning it to training or the UI.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import threading
import urllib.request
from collections import OrderedDict
from pathlib import Path

from .. import cloud, db, vault
from ..config import DATA, now
from ..publisher import _git
from .calendar import iso, stamp

BRANCH = "forecast-history"
MAX_CIPHER_BYTES = 20 * 1024 * 1024
MAX_PLAIN_BYTES = 32 * 1024 * 1024
MAX_CACHE_BYTES = 96 * 1024 * 1024
MAX_DECODED_BYTES = 64 * 1024 * 1024
MAX_CACHE_PARTITIONS = 2
MAX_RECORDS_PER_RUN = 50000
ROOT = DATA / "forecast-history"
_compact_lock = threading.Lock()
_cache_lock = threading.RLock()
_decoded = OrderedDict()
_disk_partitions = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_archives(
 id TEXT PRIMARY KEY, partition TEXT NOT NULL, repository TEXT NOT NULL,
 commit_sha TEXT NOT NULL, path TEXT NOT NULL, cipher_hash TEXT NOT NULL,
 payload_hash TEXT NOT NULL, key_fingerprint TEXT NOT NULL,
 record_count INTEGER NOT NULL, plain_bytes INTEGER NOT NULL, cipher_bytes INTEGER NOT NULL,
 created_at TEXT NOT NULL);
"""


class ArchiveError(RuntimeError):
    pass


def init():
    with db.tx() as conn:
        conn.executescript(SCHEMA)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _fingerprint(key):
    return hmac.new(key.encode() if isinstance(key, str) else key, b"aq5-forecast-history-key-v1", hashlib.sha256).hexdigest()[:24]


def _root():
    root = Path(ROOT).resolve()
    if os.name == "nt" and root.drive.upper() == "C:":
        raise ArchiveError("archive_requires_non_system_data_directory")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(directory, name):
    result = (directory / name).resolve()
    if not result.is_relative_to(_root()):
        raise ArchiveError("archive_path_outside_data")
    return result


def _command(args, cwd, *, timeout=120, allowed=(0,)):
    code, output = _git(args, cwd, timeout)
    if code not in allowed:
        # Do not return git stderr: credentials and private remote URLs can occur.
        raise ArchiveError("archive_git_" + args[0] + "_failed")
    return code, output


def _head(output):
    lines = [line.split() for line in output.splitlines() if line.strip()]
    for parts in lines:
        if len(parts) == 2 and parts[1] == "refs/heads/" + BRANCH and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return parts[0]
    raise ArchiveError("archive_remote_head_invalid")


def _publish(shards, cfg):
    """Append to the dedicated encrypted branch; never force overwrite history."""
    root = _root() / "git"
    root.mkdir(exist_ok=True)
    if not (root / ".git").exists():
        _command(["init", "-q"], root)
        _command(["checkout", "-q", "-b", BRANCH], root)
    _command(["config", "user.name", "aq5-forecast-history"], root)
    _command(["config", "user.email", "aq5-forecast-history@users.noreply.github.com"], root)
    _, remotes = _command(["remote"], root)
    _command(["remote", "set-url" if "origin" in remotes.split() else "add", "origin", cfg["remote"]], root)
    _command(["config", "remote.origin.promisor", "true"], root)
    _command(["config", "remote.origin.partialclonefilter", "blob:none"], root)
    _command(["config", "core.sparseCheckout", "true"], root)
    _command(["config", "core.sparseCheckoutCone", "false"], root)
    info = root / '.git' / 'info'
    info.mkdir(parents=True, exist_ok=True)
    (info / 'sparse-checkout').write_text(''.join('days/' + day + '/*.bin\n' for day in sorted({s['partition'] for s in shards})), encoding='ascii')
    exists, remote = _command(["ls-remote", "--exit-code", "--heads", "origin", "refs/heads/" + BRANCH], root, allowed=(0, 2))
    if exists == 0:
        baseline = _head(remote)
        _command(["fetch", "-q", "--filter=blob:none", "--depth=1", "origin", "refs/heads/" + BRANCH], root)
        # This is an app-owned repository containing only encrypted .bin files.
        # Preserve the actual fetched history, discard only an unpublished index.
        _, fetched = _command(["rev-parse", "FETCH_HEAD"], root)
        if fetched.strip() != baseline:
            raise ArchiveError("archive_remote_changed_during_fetch")
        _command(["checkout", "-q", "-f", "-B", BRANCH, "FETCH_HEAD"], root)
    else:
        # A local commit without a remote is a previously unconfirmed upload;
        # retaining it is safe because its only content is encrypted history.
        pass
    paths = []
    for shard in shards:
        target = _path(root, shard["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and _hash(target.read_bytes()) != shard["cipher_hash"]:
            raise ArchiveError("immutable_archive_path_conflict")
        target.write_bytes(shard["cipher"])
        paths.append(shard["path"])
    _command(["add", "--", *paths], root)
    _command(["commit", "-q", "-m", "Encrypted forecast history " + now().date().isoformat()], root)
    _, commit = _command(["rev-parse", "HEAD"], root)
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ArchiveError("archive_commit_invalid")
    _command(["push", "-q", "origin", "HEAD:refs/heads/" + BRANCH], root, timeout=180)
    _, confirmation = _command(["ls-remote", "--exit-code", "--heads", "origin", "refs/heads/" + BRANCH], root)
    if _head(confirmation) != commit:
        raise ArchiveError("archive_push_unconfirmed")
    return commit


def _records(cutoff):
    tables = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    result = []
    if "forecast_predictions" in tables:
        for row in db.q("SELECT id,asof,body,hash FROM forecast_predictions WHERE substr(asof,1,10)<? "
                        "AND json_valid(body) AND json_extract(body,'$._archive_ref') IS NULL ORDER BY asof,id LIMIT ?",
                        (cutoff, MAX_RECORDS_PER_RUN)):
            body = json.loads(row["body"])
            if _hash(body) != row["hash"]:
                raise ArchiveError("prediction_original_hash_mismatch")
            result.append({**row, "table": "forecast_predictions", "partition": row["asof"][:10]})
    if "forecast_structures" in tables and len(result) < MAX_RECORDS_PER_RUN:
        for row in db.q("SELECT s.id,s.asof,s.body FROM forecast_structures s WHERE substr(s.asof,1,10)<? "
                        "AND json_valid(s.body) AND json_extract(s.body,'$._archive_ref') IS NULL "
                        "AND EXISTS(SELECT 1 FROM forecast_structures n WHERE n.code=s.code AND n.timeframe=s.timeframe "
                        "AND (n.asof>s.asof OR n.asof=s.asof AND n.id>s.id)) ORDER BY s.asof,s.id LIMIT ?",
                        (cutoff, MAX_RECORDS_PER_RUN-len(result))):
            result.append({**row, "hash": _hash(json.loads(row["body"])), "table": "forecast_structures", "partition": row["asof"][:10]})
    return result


def _build_shards(records, key, *, max_cipher_bytes=MAX_CIPHER_BYTES, max_plain_bytes=MAX_PLAIN_BYTES):
    """Bound raw memory as well as encrypted object size; deterministic plain hash."""
    shards = []

    def seal_group(group):
        if not group:
            return
        partition = group[0]["partition"]
        payload = {"schema": 1, "partition": partition, "records": {
            r["table"] + ":" + r["id"]: {"hash": r["hash"], "body": r["body"]} for r in group}}
        plain = _json(payload).encode()
        if len(plain) > max_plain_bytes:
            if len(group) == 1:
                raise ArchiveError("single_archive_record_exceeds_limit")
            middle = len(group) // 2
            seal_group(group[:middle]); seal_group(group[middle:]); return
        cipher = vault.seal(gzip.compress(plain, compresslevel=6, mtime=0), key)
        if len(cipher) > max_cipher_bytes:
            if len(group) == 1:
                raise ArchiveError("single_archive_record_exceeds_limit")
            middle = len(group) // 2
            seal_group(group[:middle]); seal_group(group[middle:]); return
        digest = _hash(cipher)
        shards.append({"id": digest, "partition": partition, "path": f"days/{partition}/{digest}.bin",
                       "cipher_hash": digest, "payload_hash": _hash(plain), "cipher": cipher,
                       "plain_bytes": len(plain), "cipher_bytes": len(cipher), "records": group})

    group, partition, estimated = [], None, 0
    for record in sorted(records, key=lambda r: (r["partition"], r["table"], r["id"])):
        size = len(record["body"].encode()) * 2 + 256
        if group and (partition != record["partition"] or estimated + size > max_plain_bytes):
            seal_group(group); group, estimated = [], 0
        partition = record["partition"]
        group.append(record); estimated += size
    seal_group(group)
    return shards


def _compact_body(record, ref):
    original = json.loads(record["body"])
    if record["table"] == "forecast_predictions":
        body = {k: v for k, v in original.items() if k not in (
            "features", "coverage", "feature_coverage", "feature_evidence", "structures", "source_bars")}
    else:
        body = {k: v for k, v in original.items() if k not in ("features", "structures", "bars")}
        body["structures"] = {k: [] for k in ("klines", "fractals", "strokes", "segments", "centers", "buy_sell_points", "revisions")}
    body["_archive_ref"] = ref
    return _json(body)


def compact(asof=None):
    """Archive completed natural days. Failure never compacts unconfirmed records."""
    t = stamp(asof or now())
    if t is None:
        raise ValueError("invalid archive asof")
    if not _compact_lock.acquire(blocking=False):
        return {"ok": True, "skipped": "archive_already_running"}
    try:
        cfg, key = cloud._cfg(), cloud._key()
        repo = cfg.get("repo") or cloud._repo_of(cfg.get("remote", ""))
        if not key or not cfg.get("remote") or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            return {"ok": False, "skipped": "archive_key_or_repository_missing", "archived_records": 0}
        init()
        records = _records(t.date().isoformat())
        if not records:
            return {"ok": True, "archived_records": 0, "shards": 0, "before_bytes": 0, "after_bytes": 0}
        shards = _build_shards(records, key, max_cipher_bytes=MAX_CIPHER_BYTES, max_plain_bytes=MAX_PLAIN_BYTES)
        commit = _publish(shards, cfg)
        fingerprint = _fingerprint(key)
        count, before, after = 0, 0, 0
        with db.tx() as conn:
            for shard in shards:
                conn.execute("INSERT OR IGNORE INTO forecast_archives VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (shard["id"], shard["partition"], repo, commit, shard["path"], shard["cipher_hash"],
                              shard["payload_hash"], fingerprint, len(shard["records"]), shard["plain_bytes"],
                              shard["cipher_bytes"], iso(t)))
                for record in shard["records"]:
                    if record["table"] == "forecast_structures":
                        latest = conn.execute("SELECT n.id FROM forecast_structures n JOIN forecast_structures s "
                                              "ON n.code=s.code AND n.timeframe=s.timeframe WHERE s.id=? "
                                              "ORDER BY n.asof DESC,n.id DESC LIMIT 1", (record["id"],)).fetchone()
                        if latest and latest["id"] == record["id"]:
                            continue
                    ref = {"schema": 1, "archive_id": shard["id"], "partition": shard["partition"],
                           "repository": repo, "commit": commit, "path": shard["path"],
                           "cipher_hash": shard["cipher_hash"], "payload_hash": shard["payload_hash"],
                           "key_fingerprint": fingerprint, "record_key": record["table"] + ":" + record["id"],
                           "record_hash": record["hash"]}
                    new_body = _compact_body(record, ref)
                    changed = conn.execute("UPDATE " + record["table"] + " SET body=? WHERE id=? AND body=?",
                                           (new_body, record["id"], record["body"])).rowcount
                    count += changed
                    if changed:
                        before += len(record["body"].encode()); after += len(new_body.encode())
        cache_error = None
        for shard in shards:
            try:
                _store_cipher(shard["cipher_hash"], shard["partition"], shard["cipher"])
            except OSError:
                cache_error = 'local_archive_cache_unavailable'
        return {"ok": True, "archived_records": count, "shards": len(shards), "before_bytes": before,
                "after_bytes": after, "cipher_bytes": sum(s["cipher_bytes"] for s in shards), "commit": commit,
                "cache_warning": cache_error}
    except (ArchiveError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        return {"ok": False, "archived_records": 0, "error": str(exc) if isinstance(exc, ArchiveError) else "archive_error:" + type(exc).__name__}
    finally:
        _compact_lock.release()


def _validate_ref(ref):
    if not isinstance(ref, dict) or ref.get("schema") != 1:
        raise ArchiveError("invalid_archive_reference")
    for name in ("archive_id", "cipher_hash", "payload_hash", "record_hash"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get(name, ""))):
            raise ArchiveError("invalid_archive_reference")
    if (not re.fullmatch(r"[0-9a-f]{40}", str(ref.get("commit", "")))
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(ref.get("repository", "")))
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(ref.get("partition", "")))
            or ref.get("path") != f"days/{ref['partition']}/{ref['cipher_hash']}.bin"
            or ref.get("archive_id") != ref.get("cipher_hash")
            or not str(ref.get("record_key", "")).startswith(("forecast_predictions:", "forecast_structures:"))):
        raise ArchiveError("invalid_archive_reference")


def _store_cipher(digest, partition, cipher):
    if len(cipher) > MAX_CIPHER_BYTES or _hash(cipher) != digest:
        raise ArchiveError("archive_cipher_hash_mismatch")
    root = _root() / "cache"
    root.mkdir(exist_ok=True)
    target = _path(root, partition + '/' + digest + ".bin")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(cipher)
    with _cache_lock:
        _disk_partitions[digest] = partition
        _prune_disk(root, keep=digest)


def _prune_disk(root, keep):
    # Only generated hash-named files under the resolved archive cache are removed.
    files = [p for p in root.glob("*/*.bin") if not p.is_symlink() and re.fullmatch(r"[0-9a-f]{64}\.bin", p.name)
             and re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.parent.name)]
    files.sort(key=lambda p: (p.stem == keep, p.stat().st_mtime), reverse=True)
    total, partitions = 0, set()
    for path in files:
        part = path.parent.name
        size = path.stat().st_size
        if path.stem != keep and (total + size > MAX_CACHE_BYTES or part and part not in partitions and len(partitions) >= MAX_CACHE_PARTITIONS):
            _path(root, part + '/' + path.name).unlink()
            _disk_partitions.pop(path.stem, None)
            continue
        total += size
        if part:
            partitions.add(part)


def _remote_cipher(ref):
    url = f"https://api.github.com/repos/{ref['repository']}/contents/{ref['path']}?ref={ref['commit']}"
    headers = {"User-Agent": "aq5-forecast-history", "Accept": "application/vnd.github.raw"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            cipher = response.read(MAX_CIPHER_BYTES + 1)
    except Exception as exc:
        raise ArchiveError("archive_download_failed:" + type(exc).__name__) from None
    if len(cipher) > MAX_CIPHER_BYTES:
        raise ArchiveError("archive_cipher_exceeds_limit")
    return cipher


def _payload(ref, key):
    fingerprint = _fingerprint(key)
    if fingerprint != ref.get("key_fingerprint"):
        raise ArchiveError("archive_key_fingerprint_mismatch")
    identity = (ref["cipher_hash"], fingerprint)
    with _cache_lock:
        hit = _decoded.get(identity)
        if hit:
            if hit['payload_hash'] != ref['payload_hash'] or hit['partition'] != ref['partition']:
                raise ArchiveError('archive_cached_reference_mismatch')
            _decoded.move_to_end(identity)
            return hit["payload"]
    path = _path(_root() / "cache", ref['partition'] + '/' + ref["cipher_hash"] + ".bin")
    if path.exists():
        if path.stat().st_size > MAX_CIPHER_BYTES:
            raise ArchiveError("archive_cipher_exceeds_limit")
        cipher = path.read_bytes()
    else:
        cipher = _remote_cipher(ref)
    if _hash(cipher) != ref["cipher_hash"]:
        raise ArchiveError("archive_cipher_hash_mismatch")
    try:
        compressed = vault.unseal(cipher, key)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            plain = stream.read(MAX_PLAIN_BYTES + 1)
        if len(plain) > MAX_PLAIN_BYTES:
            raise ArchiveError("archive_plaintext_exceeds_limit")
        if _hash(plain) != ref["payload_hash"]:
            raise ArchiveError("archive_payload_hash_mismatch")
        payload = json.loads(plain)
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError("archive_decode_failed:" + type(exc).__name__) from None
    if payload.get("schema") != 1 or payload.get("partition") != ref["partition"] or not isinstance(payload.get("records"), dict):
        raise ArchiveError("archive_payload_contract_mismatch")
    _store_cipher(ref["cipher_hash"], ref["partition"], cipher)
    with _cache_lock:
        _decoded[identity] = {"payload": payload, "bytes": len(plain), "partition": ref["partition"], 'payload_hash':ref['payload_hash']}
        _decoded.move_to_end(identity)
        while (sum(x["bytes"] for x in _decoded.values()) > MAX_DECODED_BYTES or
               len({x["partition"] for x in _decoded.values()}) > MAX_CACHE_PARTITIONS):
            _decoded.popitem(last=False)
    return payload


def read_record(body):
    """Return the verified original dictionary, or fail visibly; no partial fallback."""
    item = json.loads(body) if isinstance(body, str) else copy.deepcopy(body)
    if not isinstance(item, dict):
        raise ArchiveError("archive_record_not_object")
    ref = item.get("_archive_ref")
    if ref is None:
        return item
    _validate_ref(ref)
    key = cloud._key()
    if not key:
        raise ArchiveError("archive_key_unavailable")
    payload = _payload(ref, key)
    entry = payload["records"].get(ref["record_key"])
    if not isinstance(entry, dict) or entry.get("hash") != ref["record_hash"]:
        raise ArchiveError("archive_record_reference_mismatch")
    try:
        original = json.loads(entry["body"])
    except (ValueError, TypeError, KeyError):
        raise ArchiveError("archive_original_record_invalid") from None
    if not isinstance(original, dict) or _hash(original) != ref["record_hash"]:
        raise ArchiveError("archive_original_record_hash_mismatch")
    if (str(original.get('id')) != ref['record_key'].split(':', 1)[1]
            or item.get('id') != original.get('id')):
        raise ArchiveError('archive_record_identity_mismatch')
    return original
