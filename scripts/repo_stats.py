#!/usr/bin/env python3
"""仓库统计：README 里的数字必须由这里算出来，不许手写。

用法：
    python scripts/repo_stats.py            # 打印统计
    python scripts/repo_stats.py --check    # 校验 README 是否与仓库一致

退出码：0 = 一致；1 = 有漂移（CI 会红）。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def count_lines(files: list[pathlib.Path]) -> int:
    return sum(len(read(p).splitlines()) for p in files)


def collect() -> dict[str, object]:
    py_files = [p for p in (ROOT / "backend").rglob("*.py") if "__pycache__" not in p.parts]
    core_files = sorted((ROOT / "backend" / "core").glob("*.py"))
    app_jsx = ROOT / "frontend" / "src" / "App.jsx"
    selftest = read(ROOT / "backend" / "selftest.py")
    registry = read(ROOT / "backend" / "core" / "source_registry.py")
    doc_files = [p for p in (ROOT / "docs").rglob("*.md")]
    return {
        "backend_files": len(py_files),
        "backend_lines": count_lines(py_files),
        "core_modules": len(core_files),
        "frontend_app_lines": len(read(app_jsx).splitlines()),
        "assertions": len(re.findall(r"^\s*def test_", selftest, re.M)),
        "source_specs": registry.count("SourceSpec("),
        "docs": len(doc_files),
    }


def render(s: dict[str, object]) -> str:
    return (
        f"backend .py files : {s['backend_files']}\n"
        f"backend lines     : {s['backend_lines']:,}\n"
        f"core modules      : {s['core_modules']}\n"
        f"frontend App.jsx  : {s['frontend_app_lines']:,} lines\n"
        f"offline assertions: {s['assertions']}\n"
        f"source specs      : {s['source_specs']}\n"
        f"docs (markdown)   : {s['docs']}\n"
    )


def check(s: dict[str, object]) -> list[str]:
    """README 里出现的每个数字都必须与仓库一致。"""
    readme = read(ROOT / "README.md")
    expected = [
        f"{s['backend_files']} 个 `.py` / {s['backend_lines']:,} 行",
        f"`core/` 模块 | {s['core_modules']} 个",
        f"`App.jsx` {s['frontend_app_lines']:,} 行",
        f"离线断言 | {s['assertions']} 个",
        f"数据源注册表 | {s['source_specs']} 条",
        "审计包 | 8 个文件",
        "R1–R15",
    ]
    return [e for e in expected if e not in readme]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    stats = collect()
    print(render(stats))
    if not args.check:
        return 0

    missing = check(stats)
    if missing:
        print("\nREADME 与仓库不一致，以下数字需更新：", file=sys.stderr)
        for m in missing:
            print(f"  - 未找到：{m}", file=sys.stderr)
        return 1
    print("README 数字与仓库一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
