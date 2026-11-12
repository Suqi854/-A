"""Encrypted research-only snapshots/checkpoints on a separate pinned Git branch.

Never calls account-state pack/unpack, never writes main/data/state. Reuses the
application's authenticated envelope, with ciphertext and archive bounds.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from . import vault
from .config import DATA, now

REPO = "Suqi854/-A"
BRANCH = "research-cache"
FORMAT = "aq5-research-snapshot-1"
CHUNK = 8 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_CIPHER = 128 * 1024 * 1024
ROOTS = {"decision-sources", "decision-public", "decision-full", "decision-universe"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()


def _password():
    password = os.environ.get("AQ5_KEY") or vault.get("mobile_password")
    if not password:
        raise RuntimeError("research_encryption_key_unavailable")
    return password


def api(path, method="GET", payload=None, *, accept="application/vnd.github+json"):
    token = os.environ.get("GITHUB_TOKEN", "")
    if method != "GET" and not token:
        raise RuntimeError("research_publish_token_unavailable")
    headers = {"User-Agent":"aq5-full-research", "Accept":accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else _json(payload)
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/"+path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request,timeout=120) as response:
        raw = response.read(32*1024*1024+1)
    if len(raw)>32*1024*1024:
        raise ValueError("github_response_exceeds_bound")
    return json.loads(raw)


def head():
    try:
        return api("git/ref/heads/"+BRANCH)["object"]["sha"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def read_file(commit, name):
    if not re.fullmatch(r"[a-f0-9]{40}", str(commit)) or not re.fullmatch(r"(?:manifest\.json|(?:summary|checkpoint)-\d{4}\.bin)",name):
        raise ValueError("research_snapshot_reference_invalid")
    value = api(f"contents/{name}?ref={commit}", accept="application/vnd.github.object+json")
    if value.get("encoding") != "base64" or not isinstance(value.get("content"), str):
        # Git Contents omits content above 1 MiB; retrieve the same pinned blob.
        blob = value.get("sha")
        if not re.fullmatch(r"[a-f0-9]{40}",str(blob)):
            raise ValueError("research_blob_reference_missing")
        value = api("git/blobs/"+blob)
    raw = base64.b64decode(value["content"],validate=False)
    if len(raw)>CHUNK+1024*1024:
        raise ValueError("research_chunk_too_large")
    return raw


def manifest(commit=None):
    commit = commit or head()
    if not commit:
        return None, None
    raw = read_file(commit,"manifest.json")
    if len(raw)>1024*1024:
        raise ValueError("research_manifest_too_large")
    value=json.loads(raw)
    if value.get("format")!=FORMAT or value.get("branch")!=BRANCH:
        raise ValueError("research_manifest_invalid")
    return commit,value


def decode_part(commit, item, password):
    if (not isinstance(item,dict) or not isinstance(item.get("parts"),list) or not 1<=len(item["parts"])<=32
            or not isinstance(item.get("size"),int) or not 69<=item["size"]<=MAX_CIPHER):
        raise ValueError("research_payload_manifest_invalid")
    if (any(not isinstance(r,dict) or type(r.get("size")) is not int or not 1<=r["size"]<=CHUNK
            or not re.fullmatch(r"(?:summary|checkpoint)-\d{4}\.bin",str(r.get("path")))
            or not re.fullmatch(r"[a-f0-9]{64}",str(r.get("sha256"))) for r in item["parts"])
            or sum(r["size"] for r in item["parts"])!=item["size"]):
        raise ValueError("research_chunk_manifest_invalid")
    parts=[];names=set()
    for row in item["parts"]:
        name=row["path"]
        if name in names:
            raise ValueError("duplicate_research_chunk")
        names.add(name)
        if row.get("blob"):
            if not re.fullmatch(r"[a-f0-9]{40}",row["blob"]):raise ValueError("invalid_research_blob")
            value=api("git/blobs/"+row["blob"])
            raw=base64.b64decode(value["content"],validate=False)
            if len(raw)>CHUNK:raise ValueError("research_chunk_too_large")
        else:
            raw=read_file(commit,name)
        if len(raw)!=row["size"] or sha(raw)!=row["sha256"]:
            raise ValueError("research_chunk_integrity_failure")
        parts.append(raw)
    cipher=b"".join(parts)
    if len(cipher)!=item["size"] or sha(cipher)!=item["sha256"]:
        raise ValueError("research_payload_integrity_failure")
    return vault.unseal(cipher,password)


def read_summary(password=None):
    commit,meta=manifest()
    if not meta or not meta.get("summary"):
        return {"status":"unavailable", "commit":commit, "progress":(meta or {}).get("progress",{})}
    compressed=decode_part(commit,meta["summary"],password or _password())
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        plain=stream.read(8*1024*1024+1)
    if len(plain)>8*1024*1024:
        raise ValueError("research_summary_too_large")
    result=json.loads(plain)
    if result.get("kind")!="research_preview" or not isinstance(result.get("routes"),dict):
        raise ValueError("research_summary_contract_invalid")
    return {"status":"ready", "commit":commit,"summary":result,"progress":meta.get("progress",{}),"updated_at":meta.get("updated_at")}


def archive(directory=DATA):
    directory=Path(directory).resolve();buffer=io.BytesIO();total=0
    with tarfile.open(fileobj=buffer,mode="w:gz") as tar:
        for name in sorted(ROOTS):
            folder=directory/name
            if not folder.exists():continue
            for path in sorted(folder.rglob("*")):
                if not path.is_file() or path.name.endswith((".tmp",".lock")) or ".tmp-" in path.name:continue
                if path.is_symlink() or not path.resolve().is_relative_to(directory):
                    raise ValueError("research_cache_path_escape")
                size=path.stat().st_size
                if size>64*1024*1024:raise ValueError("research_cache_file_too_large")
                total+=size
                if total>MAX_ARCHIVE:raise ValueError("research_archive_too_large")
                tar.add(path,arcname=path.relative_to(directory).as_posix(),recursive=False)
    return buffer.getvalue()


def restore(directory=DATA,password=None):
    commit,meta=manifest()
    if not meta or not meta.get("checkpoint"):return {"restored":False,"commit":commit}
    raw=decode_part(commit,meta["checkpoint"],password or _password())
    directory=Path(directory).resolve();total=0
    # Bound the entire uncompressed tar, including PAX headers, before parsing.
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        plain=stream.read(MAX_ARCHIVE+1)
    if len(plain)>MAX_ARCHIVE:raise ValueError("research_archive_too_large")
    with tarfile.open(fileobj=io.BytesIO(plain),mode="r:") as tar:
        validated=[];destinations=set()
        for member in tar:
            name=Path(member.name)
            dst=(directory/name).resolve()
            allowed_root=directory/name.parts[0] if name.parts else directory
            if (not member.isfile() or name.is_absolute() or not name.parts or name.parts[0] not in ROOTS or ".." in name.parts
                    or not dst.is_relative_to(allowed_root.resolve()) or ":" in member.name or "\\" in member.name
                    or any(p.is_symlink() for p in [allowed_root,directory/name,*list((directory/name).parents)] if p.is_relative_to(directory))):
                raise ValueError("unsafe_research_archive_member")
            total+=member.size
            if total>MAX_ARCHIVE or member.size>64*1024*1024:raise ValueError("research_archive_too_large")
            if dst in destinations or len(validated)>=100000:raise ValueError("research_archive_duplicates_or_excessive")
            destinations.add(dst);validated.append((member,dst))
        if any(parent in destinations or parent.is_file() for _,dst in validated for parent in dst.parents if parent.is_relative_to(directory)):
            raise ValueError("research_archive_parent_conflict")
        # Validate every member before writing even the first one.
        for member,dst in validated:
            data=tar.extractfile(member).read(member.size+1)
            if len(data)!=member.size:raise ValueError("research_archive_size_mismatch")
            dst.parent.mkdir(parents=True,exist_ok=True)
            temp=dst.with_name(dst.name+".restore.tmp");temp.write_bytes(data);temp.replace(dst)
    return {"restored":True,"commit":commit,"bytes":total}


def publish(*,summary=None,checkpoint=None,progress=None,password=None):
    """Fast-forward-only CAS: a concurrent branch change cannot be overwritten."""
    password=password or _password();commit,old=manifest();meta=dict(old or {})
    entries=[]
    for kind,plain in (("summary",gzip.compress(_json(summary)) if summary is not None else None),("checkpoint",checkpoint)):
        if plain is None:continue
        cipher=vault.seal(plain,password)
        if len(cipher)>MAX_CIPHER:raise ValueError("research_cipher_too_large")
        parts=[]
        for i,start in enumerate(range(0,len(cipher),CHUNK)):
            raw=cipher[start:start+CHUNK];path=f"{kind}-{i:04d}.bin"
            blob=api("git/blobs","POST",{"encoding":"base64","content":base64.b64encode(raw).decode()})
            entries.append({"path":path,"mode":"100644","type":"blob","sha":blob["sha"]})
            parts.append({"path":path,"size":len(raw),"sha256":sha(raw),"blob":blob["sha"]})
        meta[kind]={"size":len(cipher),"sha256":sha(cipher),"parts":parts}
    meta.update(format=FORMAT,branch=BRANCH,updated_at=now().isoformat(),progress=progress or {})
    entries.append({"path":"manifest.json","mode":"100644","type":"blob","content":_json(meta).decode()})
    base=api("git/commits/"+commit)["tree"]["sha"] if commit else None
    tree=api("git/trees","POST",{"tree":entries,**({"base_tree":base} if base else {})})["sha"]
    new=api("git/commits","POST",{"message":"Update encrypted full-mainboard research snapshot","tree":tree,"parents":[commit] if commit else []})["sha"]
    if commit:
        api("git/refs/heads/"+BRANCH,"PATCH",{"sha":new,"force":False})
    else:
        api("git/refs","POST",{"ref":"refs/heads/"+BRANCH,"sha":new})
    return {"commit":new,"updated_at":meta["updated_at"],"summary_updated":summary is not None}
