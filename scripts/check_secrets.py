#!/usr/bin/env python3
"""提交前密钥扫描（只用标准库，CI 与本地同一套规则）。

用法：
    python scripts/check_secrets.py            # 扫描仓库（跳过 .git / node_modules / .venv）
    python scripts/check_secrets.py --all      # 连被 .gitignore 排除的文件也扫（用于自查）

退出码：0 = 干净；1 = 命中。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", ".pytest_cache"}
TEXT_SUFFIX = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".yml", ".yaml", ".env", ".toml", ".ini", ".cfg", ".ps1", ".sh", ".bat"}

PATTERNS = [
    ("OpenAI/DeepSeek 风格 Key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("私钥块", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("疑似赋值型密钥", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]")),
]

# 这些是模板/示例，不视为泄露
ALLOW = re.compile(r"(sk-xxxxxxxx|<your-|example|placeholder|REDACTED|\.\.\.)")


def iter_files(include_ignored: bool):
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIX:
            continue
        if p.name == ".env" and not include_ignored:
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="包含 .env 等通常被忽略的文件")
    args = ap.parse_args()

    hits: list[str] = []
    for path in iter_files(args.all):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS:
                m = rx.search(line)
                if m and not ALLOW.search(m.group(0)):
                    rel = path.relative_to(ROOT)
                    hits.append(f"{rel}:{lineno}: {name}")

    if hits:
        print("发现疑似密钥：", file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        print("\n请删除并轮换该 Key；不要只删文件，历史里仍然在。", file=sys.stderr)
        return 1

    print("未发现密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
