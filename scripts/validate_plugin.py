#!/usr/bin/env python3
"""校验插件包（Claude Code / Codex 通用）。只用标准库、不联网。

检查：各清单版本与名称一致；每个技能的 frontmatter；Markdown 相对链接；
敏感信息特征；引擎 Python 源码可解析；继承自 1.13.3 公开整理版的技能文件哈希未被意外改动。
"""
from pathlib import Path
from urllib.parse import unquote, urlsplit
import ast
import hashlib
import json
import re
import sys

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
NAME = "ashare-analysis-companion"
errors = []


def check(ok, msg):
    if not ok:
        errors.append(msg)


def js(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


def frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("缺少 frontmatter")
    end = lines.index("---", 1)
    out = {}
    for line in lines[1:end]:
        m = re.fullmatch(r"(name|description): (.+)", line)
        if not m:
            raise ValueError("frontmatter 只允许 name/description 两个字段: " + line[:40])
        v = m[2]
        out[m[1]] = json.loads(v) if v.startswith('"') else v
    return out


def main():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    manifests = {p: js(p) for p in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json")}
    for p, m in manifests.items():
        check(m.get("name") == NAME, "名称不一致: " + p)
        check(m.get("version") == version, "版本不一致: " + p)
    check(manifests["plugin.json"]["extensions"]["com.openai"]["interface"] == manifests[".codex-plugin/plugin.json"]["interface"],
          "Codex 界面信息与根清单不一致")
    check(len(manifests[".codex-plugin/plugin.json"]["interface"]["shortDescription"]) <= 30, "副标题过长")
    cm = js(".claude-plugin/marketplace.json")
    check(cm["plugins"][0]["name"] == NAME and cm["plugins"][0]["source"] == "./", "Claude marketplace")
    am = js(".agents/plugins/marketplace.json")
    check(am["plugins"][0]["name"] == NAME and am["plugins"][0]["source"]["path"] == "./", "Codex marketplace")

    skills = sorted(p for p in (ROOT / "skills").glob("*/SKILL.md"))
    check(len(skills) >= 9, "技能数量不足")
    for p in skills:
        meta = frontmatter(p.read_text(encoding="utf-8"))
        check(meta.get("name") == p.parent.name, "技能名与目录不一致: " + p.parent.name)
        check(len(meta.get("description", "")) >= 20, "技能描述过短: " + p.parent.name)

    files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.relative_to(ROOT).parts]
    check(not any(p.relative_to(ROOT).parts[:2] == ("engine", "data") for p in files), "engine/data 不应进入仓库")
    rules = {
        "token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bsk-[A-Za-z0-9]{20,}\b|\bSCT[A-Za-z0-9]{20,}\b",
        "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "private service": r"https?://[^\s)]+\.chatgpt\.site",
    }
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix in (".png", ".ico"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for name, pat in rules.items():
            check(re.search(pat, text) is None, f"{name}: {rel}")
        if p.suffix == ".json":
            json.loads(text)
        if p.suffix == ".py":
            ast.parse(text)
        if p.suffix == ".md" and not rel.startswith("engine/"):
            check(re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", text) is None, "绝对本机路径: " + rel)
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if urlsplit(target).scheme or target.startswith("#"):
                    continue
                dest = (p.parent / unquote(target.split("#", 1)[0])).resolve()
                check(dest.is_file() or dest.is_dir(), f"失效链接 {rel}: {target}")

    prov = ROOT / "SOURCE_PROVENANCE.json"
    if prov.exists():
        hashes = json.loads(prov.read_text(encoding="utf-8")).get("component_sha256", {})
        for rel, h in hashes.items():
            f = ROOT / rel
            if f.is_file() and rel.startswith("skills/"):
                check(hashlib.sha256(f.read_bytes()).hexdigest() == h, "继承技能文件被改动: " + rel)

    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    print(json.dumps({"status": "PASS", "version": version, "skills": [p.parent.name for p in skills],
                      "files": len(files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as exc:
        print("FAIL: " + str(exc))
        sys.exit(1)
