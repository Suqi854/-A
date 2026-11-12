"""Current financial-response industry projection; no network or historical taxonomy claim."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import decision_emotion as em
from .config import DATA

SCHEMA = "financial-response-industry-1"
LABEL = "财务接口行业名称分组"
NOTE = "当前公开财务接口 INDUSTRY_NAME 观测；非已核实官方申万分类，不代表穷尽题材，不回溯财报期行业归属。"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _code(value):
    return em.market.norm_code(str(value))


def project(universe, growth):
    """Verify raw financial responses and select latest reported row per official-scope code."""
    rows = universe.get("rows", [])
    codes = [_code(r.get("code", "")) for r in rows]
    scope = set(codes)
    failures, candidates, receipts, outside = [], {}, {}, set()
    scope_valid = bool(universe.get("verified") is True and scope and len(scope) == len(codes)
                       and all(re.fullmatch(r"(?:sh|sz)\d{6}", c) and em.market.is_main_board(c) for c in scope))
    if not scope_valid:
        failures.append({"reason": "official_scope_unverified_or_duplicate"})
    for source in growth.get("sources", []):
        url = source.get("source_url", "")
        query = parse_qs(urlsplit(url).query)
        if query.get("reportName") != ["RPT_DMSK_FN_INCOME"]:
            continue
        digest = source.get("raw_sha256", "")
        if digest in receipts:
            continue
        try:
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid_source_hash")
            at = em._at(source["retrieved_at"])
            # Checkpoint relocations reconstruct the content-addressed path first.
            path = Path(DATA) / "decision-sources" / "growth" / (digest + ".json")
            if not path.exists():
                path = Path(source.get("raw_path", ""))
            raw = path.read_bytes()
            if len(raw) > 20 * 1024 * 1024 or _sha(raw) != digest:
                raise ValueError("financial_raw_integrity_failed")
            obj = json.loads(raw)
            data = obj.get("result", {}).get("data")
            if obj.get("success") is not True or not isinstance(data, list):
                raise ValueError("financial_response_invalid")
            receipt = {k: source[k] for k in ("source_url", "raw_sha256", "retrieved_at")}
            receipt.update(provider="东方财富公开转载", official_verified=False, method=SCHEMA)
            receipts[digest] = receipt
            for row in data:
                code = _code(row.get("SECURITY_CODE", ""))
                if code not in scope:
                    outside.add(code); continue
                period = str(row.get("REPORT_DATE", ""))[:10]
                datetime.fromisoformat(period)
                if period > str(at.date()):
                    raise ValueError("future_financial_period")
                industry = str(row.get("INDUSTRY_NAME") or "").strip()
                candidates.setdefault(code, []).append((period, industry, digest))
        except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
            failures.append({"raw_sha256": digest, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    mapping, unknown = {}, {}
    for code in sorted(scope):
        values = candidates.get(code, [])
        if not values:
            unknown[code] = "missing_financial_industry"; continue
        latest = max(v[0] for v in values)
        current = [v for v in values if v[0] == latest]
        names = {v[1] for v in current}
        if len(names) != 1 or not next(iter(names)):
            unknown[code] = "latest_period_industry_conflict" if len(names) > 1 else "empty_latest_industry"
            continue
        refs = sorted({v[2] for v in current})
        mapping[code] = {"industry_name": next(iter(names)), "selected_report_date": latest,
                         "classification_time_basis": "current_response_observation_not_report_date",
                         "observed_at": max(receipts[h]["retrieved_at"] for h in refs),
                         "source_receipts": [receipts[h] for h in refs]}
    groups = {}
    for code, row in mapping.items():
        groups.setdefault(row["industry_name"], []).append(code)
    complete = scope_valid and len(mapping) == len(scope) and not failures and not outside
    return {"schema": SCHEMA, "label": LABEL, "note": NOTE, "complete": complete,
            "observed_at": max((r["retrieved_at"] for r in receipts.values()), default=None),
            "directory_asof": universe.get("asof"), "groups": groups, "codes": mapping,
            "unknown": unknown, "outside_scope": sorted(outside), "failures": failures,
            "sources": list(receipts.values()), "source_hashes": sorted(receipts),
            "coverage": {"universe_count": len(scope), "mapped_count": len(mapping), "group_count": len(groups),
                         "unknown_count": len(unknown), "outside_scope_count": len(outside), "complete": complete}}


def _histories(universe, emotion):
    run_id = emotion.get("run_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("invalid_emotion_run_id")
    root = Path(em.DATA) / "decision-public" / "emotion" / "full" / run_id
    manifest = json.loads((root / "manifest.json").read_bytes())
    codes = sorted(_code(r["code"]) for r in universe["rows"])
    if (manifest.get("scope_hash") != em._sha(em._json_bytes(codes))
            or em._at(manifest["asof"]) != em._at(emotion["asof"])
            or manifest.get("version") != emotion.get("version")):
        raise ValueError("emotion_checkpoint_scope_or_cutoff_mismatch")
    reader = object.__new__(em._FullRun)
    reader.root = root
    histories = {}
    for code in ["sh000300", *codes]:
        key = "history:" + code
        value = reader.read(key)
        if value and value.get("status") == "ok":
            try:
                receipt = value["value"][1]
                digest = receipt["raw_sha256"]
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("invalid_history_hash")
                raw = (Path(em.DATA) / "decision-public" / "emotion" / (digest + ".json")).read_bytes()
                if _sha(raw) != digest:
                    raise ValueError("history_raw_integrity_failed")
            except (OSError, ValueError, KeyError, TypeError):
                value = None
        histories[key] = value
    return histories


def augment(universe, growth, emotion):
    """Add one independent grouping, retaining original Sina coverage and coherent contexts."""
    result = copy.deepcopy(emotion)
    projection = project(universe, growth)
    result["financial_industry"] = projection
    result.setdefault("coverage", {})["financial_industry"] = projection["coverage"]
    result["sina_coverage"] = copy.deepcopy(emotion.get("sina_coverage", emotion.get("coverage", {})))
    result["sina_unmapped_codes"] = list(emotion.get("sina_unmapped_codes", emotion.get("unmapped_codes", [])))
    result["note"] = str(emotion.get("note", "")) + " " + NOTE
    if not projection["complete"]:
        return result
    try:
        histories = _histories(universe, emotion)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        projection["failures"].append({"stage": "history_checkpoint", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
        projection["evaluation_status"] = "UNKNOWN"
        return result
    at = em._at(emotion["asof"])
    anchor = histories.get("history:sh000300")
    anchor_day = anchor["value"][0][-1]["date"] if anchor and anchor.get("status") == "ok" and anchor["value"][0] else None
    if not anchor_day or not 0 <= (at.date() - datetime.fromisoformat(anchor_day).date()).days <= 5:
        anchor_day = None
    records, contexts, code_context = [], {}, {}
    for name, members in sorted(projection["groups"].items()):
        context = "financial-industry:" + _sha(name.encode())[:20]
        refs = {r["raw_sha256"]: r for code in members for r in projection["codes"][code]["source_receipts"]}
        membership = {"board": {"id": context, "name": name, "kind": "industry", "scope": LABEL},
                      "target_members": members, "source_total": len(members), "outside_universe_mainboard": [],
                      "non_mainboard_count": 0, "receipts": list(refs.values()),
                      "enumeration_method": "verified_official_scope_financial_response_projection"}
        new, proof = em._full_context(membership, histories, anchor_day, at)
        proof.update(classification_schema=SCHEMA, classification_observed_at=projection["observed_at"], classification_note=NOTE)
        proof["proof_sha256"] = em._sha(em._json_bytes({k: v for k, v in proof.items() if k != "proof_sha256"}))
        context_times = [at, *(em._at(r["retrieved_at"]) for r in refs.values())]
        context_times += [em._at(histories["history:" + c]["value"][1]["retrieved_at"])
                          for c in members if histories.get("history:" + c) and histories["history:" + c].get("status") == "ok"]
        context_available = max(context_times).isoformat(timespec="seconds")
        for record in new:
            history_receipt = histories["history:" + record["code"]]["value"][1]
            receipts = projection["codes"][record["code"]]["source_receipts"] + [history_receipt]
            record["source_url"] = receipts[0]["source_url"]
            record["available_at"] = context_available
            record["provenance"].update(method=NOTE, classification_schema=SCHEMA, classification_observed_at=projection["observed_at"],
                                         source_url=receipts[0]["source_url"], retrieved_at=max(r["retrieved_at"] for r in receipts),
                                         raw_receipts=receipts, raw_sha256=em._sha(em._json_bytes([r["raw_sha256"] for r in receipts])),
                                         context_evidence_sha256=proof["proof_sha256"])
        records += new; contexts[context] = proof
        code_context.update({c: context for c in members})
    # Idempotent repeated augmentation cannot retain stale financial contexts.
    original = [r for r in result.get("records", []) if not r.get("provenance", {}).get("context_id", "").startswith("financial-industry:")]
    selected, alternatives = em.select_context(original + records)
    result["records"], result["selected_contexts"] = selected, alternatives
    result.setdefault("contexts", {}).update(contexts)
    chosen = {}
    for record in selected:
        chosen.setdefault(record["code"], {})[record["kind"]] = record["value"]
    financial_kinds = {}
    for record in records:
        financial_kinds.setdefault(record["code"], set()).add(record["kind"])
    for code, outcome in result.get("code_outcomes", {}).items():
        if code not in code_context:
            continue
        outcome.setdefault("sina_industry_mapping", outcome.get("industry_mapping"))
        outcome.setdefault("sina_membership_covered", outcome.get("membership_covered"))
        outcome["financial_industry_mapping"] = "mapped"
        outcome["financial_industry_context"] = code_context[code]
        outcome["financial_industry_evaluated"] = {"mainline", "core", "cooling"} <= financial_kinds.get(code, set())
        outcome["membership_covered"] = True
        outcome["industry_mapping"] = "mapped_financial_response" if not outcome.get("industry_contexts") else outcome["industry_mapping"]
        outcome["industry_contexts"] = sorted(set(outcome.get("industry_contexts", []) + [code_context[code]]))
        for kind in ("mainline", "core", "cooling"):
            outcome[kind] = chosen.get(code, {}).get(kind, "UNKNOWN")
    outcomes = result.get("code_outcomes", {})
    result["unmapped_codes"] = [c for c, o in outcomes.items() if not o.get("membership_covered")]
    result["unresolved_codes"] = [c for c, o in outcomes.items() if o.get("history") != "qualified" or not o.get("membership_covered")
                                  or any(o.get(k, "UNKNOWN") == "UNKNOWN" for k in ("mainline", "core", "cooling"))
                                  or o.get("lhb") not in ("listed_details_complete", "not_listed_in_complete_window")]
    result["coverage"].update(membership_covered=sum(bool(o.get("membership_covered")) for o in outcomes.values()),
                              membership_unmapped_count=len(result["unmapped_codes"]), evidence_evaluated_count=len(chosen),
                              unresolved_count=len(result["unresolved_codes"]))
    # Preserve source completeness, pending tasks and original failure status.
    projection["evaluation_status"] = "evaluated_with_existing_thresholds"
    result["sources"] = list({r["raw_sha256"]: r for r in result.get("sources", []) + projection["sources"]}.values())
    return result
