#!/usr/bin/env python3
"""Validate the public skills-only source; no network or third-party packages."""
from pathlib import Path
from urllib.parse import unquote, urlsplit
import ast
import hashlib
import json
import re
import sys

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = {
    "ashare-analysis", "ashare-fundamental-research", "ashare-position-t",
    "ashare-session-briefing", "ashare-strategy-validation", "ashare-swing-screen",
    "ashare-trade-review",
}
errors = []

def check(condition, message):
    if not condition:
        errors.append(message)

def read_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))

def parse_frontmatter(text):
    # This package uses a flat YAML mapping of two strings. Reject other YAML
    # constructs rather than silently interpreting a richer schema incorrectly.
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing YAML opening delimiter")
    end = lines.index("---", 1)
    result = {}
    for line in lines[1:end]:
        match = re.fullmatch(r"(name|description): (.+)", line)
        if not match or match[1] in result:
            raise ValueError("expected unique name/description string fields")
        key, value = match.groups()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'"):
            if not value.endswith("'"):
                raise ValueError("unterminated quoted scalar")
            value = value[1:-1].replace("''", "'")
        elif ": " in value or " #" in value or value[0] in "|>&*!{[":
            raise ValueError("unsupported YAML scalar; use a quoted string")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("empty or non-string YAML field")
        result[key] = value
    if set(result) != {"name", "description"}:
        raise ValueError("missing required metadata")
    return result

def main():
    root = read_json("plugin.json")
    legacy = read_json(".codex-plugin/plugin.json")
    provenance = read_json("SOURCE_PROVENANCE.json")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check(root.get("$schema") == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "root schema")
    check(root.get("name") == legacy.get("name") == "ashare-analysis-companion", "plugin identity")
    check(root.get("version") == legacy.get("version") == version == provenance["public"]["version"], "version mismatch")
    check(bool(re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-public\.(0|[1-9]\d*)", version)), "public derivative SemVer")
    check(provenance["public"]["byte_identical_to_source_release"] is False, "derivative boundary")
    check(provenance["public"]["repository"] == "https://github.com/Suqi854/-A", "repository boundary")
    check(not ({"skills", "mcpServers", "apps", "interface"} & set(root)), "portable manifest fields")
    interface = root["extensions"]["com.openai"]["interface"]
    check(interface == legacy["interface"], "manifest presentation mismatch")
    check(len(interface["shortDescription"]) <= 30, "subtitle length")
    check(isinstance(interface["defaultPrompt"], (str, list)), "default prompt type")
    check(isinstance(interface["capabilities"], list), "capabilities type")

    component_paths = {row["path"] for row in provenance["files"] if row["status"] != "omitted"}
    omitted = {row["path"] for row in provenance["files"] if row["status"] == "omitted"}
    hashes = provenance["component_sha256"]
    check(len(provenance["files"]) == provenance["source"]["file_count"] == 52, "source inventory count")
    check(len(component_paths) == provenance["public"]["component_file_count"] == 31, "component inventory count")
    check(len(omitted) == provenance["summary"]["omitted"] == 21, "excluded inventory count")
    check(set(hashes) == component_paths, "hash inventory differs from component inventory")
    check(sum(row["status"] == "modified" for row in provenance["files"]) == provenance["summary"]["modified"], "modified count")
    check(sum(row["status"] == "unchanged_text" for row in provenance["files"]) == provenance["summary"]["unchanged_text"], "retained count")
    support = {"README.md", "COMPATIBILITY.md", "VERSION", "SOURCE_PROVENANCE.json", "scripts/validate_plugin.py"}
    expected = component_paths | support
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.relative_to(ROOT).parts}
    check(actual == expected, "unexpected or missing files: " + repr(sorted(actual ^ expected)))
    check(not (actual & omitted), "excluded private files present")
    check(not any(p.startswith(".github/") for p in actual), "workflow file present")
    for path in ROOT.rglob("*"):
        check(not path.is_symlink(), "symlink: " + str(path.relative_to(ROOT)))
    found_skills = set()
    for relative in sorted(expected):
        path = ROOT / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if relative in hashes:
            check(hashlib.sha256(raw).hexdigest() == hashes[relative], "hash mismatch: " + relative)
        if relative.endswith(".json"):
            json.loads(text)
        if relative.endswith("/SKILL.md"):
            metadata = parse_frontmatter(text)
            skill_name = Path(relative).parent.name
            check(metadata["name"] == skill_name, "skill directory/name: " + relative)
            check(skill_name not in found_skills, "duplicate skill name")
            found_skills.add(skill_name)
            check(len(raw) <= 1024 * 1024, "oversized skill")
        if relative.endswith(".md"):
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if urlsplit(target).scheme or target.startswith("#"):
                    continue
                destination = (path.parent / unquote(target.split("#", 1)[0])).resolve()
                check(destination.is_relative_to(ROOT), "link outside package: " + relative)
                check(destination.is_file(), "broken link in " + relative + ": " + target)
        if relative in component_paths or relative in {"README.md", "COMPATIBILITY.md"}:
            rules = {
                "absolute local path": r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]",
                "private service URL": r"https?://[^\s)]+\.chatgpt\.site",
                "private runtime implementation": r"\ba_share_quant\b",
                "private research variant": r"\b(?:FAST|SLOW)\b",
                "private case reference": r"daily-review-case|DR-20\d{6}",
                "credential-shaped token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
                "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                "credential assignment": r"(?im)^\s*(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9/+_.=-]{12,}",
            }
            for name, pattern in rules.items():
                check(re.search(pattern, text) is None, name + ": " + relative)
    check(found_skills == EXPECTED_SKILLS, "seven-skill inventory")
    ast.parse((ROOT / "scripts/validate_plugin.py").read_text(encoding="utf-8"))
    if errors:
        for error in errors:
            print("FAIL: " + error)
        return 1
    print(json.dumps({
        "status": "PASS", "version": version, "source_version": provenance["source"]["version"],
        "files": len(expected), "components": len(component_paths), "skills": len(found_skills),
        "excluded_source_files": len(omitted), "sha256": "all component files verified",
        "scope": "source/package checks; no runtime, notification or profitability claim"
    }, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print("FAIL: " + str(exc))
        sys.exit(1)
