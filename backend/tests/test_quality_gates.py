"""两个"仓库级"契约测试：一个跑全量离线断言，一个验示例审计包的完整性。

它们不重复实现断言，只回答两个问题：
1. 这套代码在当前环境里跑，失败项是不是 0；
2. 仓库里那份示例审计包，换行/编码有没有被 Git 改写（改写就会让 verify.py 失效）。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


@pytest.mark.skipif(not (BACKEND / "selftest.py").exists(), reason="selftest.py 不在")
def test_offline_suite_reports_zero_failures():
    env = {**os.environ, "YANCEC_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "selftest.py"],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, out[-3000:]
    m = re.search(r"失败\s*(\d+)\s*项", out)
    assert m, f"没读到结果汇总：\n{out[-1500:]}"
    assert m.group(1) == "0", out[-3000:]


def test_example_bundle_matches_manifest_hashes():
    bundle = REPO / "examples" / "mixue-vs-heytea" / "audit_bundle"
    manifest = bundle / "manifest.json"
    if not manifest.exists():
        pytest.skip("示例审计包不在")

    data = json.loads(manifest.read_text(encoding="utf-8"))
    mismatched = []
    for item in data["files"]:
        path = bundle / item["path"]
        if not path.exists():
            mismatched.append((item["path"], "missing"))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            mismatched.append((item["path"], "hash"))
    assert not mismatched, f"示例审计包与 manifest 不一致：{mismatched}"
