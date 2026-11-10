"""研策 · 核验器假阳性率校准 CLI（现场可跑、可留档）

用法（PowerShell，backend 目录）：
  ..\\venv\\Scripts\\python.exe _calibrate_entailment.py            # 跑一次并存档
  ..\\venv\\Scripts\\python.exe _calibrate_entailment.py --no-save  # 只看结果，不覆盖存档
  ..\\venv\\Scripts\\python.exe _calibrate_entailment.py --show     # 只读现有存档，不跑

为什么是一个独立 CLI：
校准测的是"核验官会不会把错的判成对的"，而**核验官是模型**——
换模型、改判定提示词、调温度，这个数都会变。
所以它必须能被单独、反复、随时重跑，而不是藏在某次报告运行里。
每次改动 core/entailment.py 的判定提示词后，**都应重跑一次**。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from core.calibration import TTL_DAYS, load, run, save  # noqa: E402


def _print(rep: dict) -> None:
    print(f"正例 {rep['n_pos']} 条（应判 support）｜反例 {rep['n_neg']} 条（绝不能判 support）")
    print(f"核验时点 {rep['ran_at']}")
    print("-" * 72)
    for x in rep["rows"]:
        _ok = (x["group"] == "match") == (x["verdict"] == "support")
        _mark = "✔" if _ok else "✘ 判错"
        print(f"  {_mark}  {x['gid']}  [{x['axis']}]  {x['verdict']}")
        print(f"        主张：{x['claim'][:66]}")
        print(f"        理由：{x['why'][:80]}")
    print("-" * 72)
    print(f"严格假阳性率（反例被判 support）：{rep['fpr'] * 100:.1f}%   ← 污染支撑率的那个数")
    print(f"宽松假阳性率（support 或 partial）：{rep['fpr_loose'] * 100:.1f}%")
    print(f"真阳性率（正例被判 support）    ：{rep['tpr'] * 100:.1f}%")
    print()
    print(f"提醒：{rep['caveat']}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:]
    print("=" * 72)
    print("研策 · 核验器假阳性率校准（16 个答案已知的样本，与生产同一条判定函数）")
    print("=" * 72)
    if "--show" in args:
        _r = load(max_age_days=3650)
        if not _r:
            print("没有校准存档。跑一次：python _calibrate_entailment.py")
            return 0
        _print(_r)
        _fresh = load()
        print(f"\n存档有效性：{'在有效期内' if _fresh else f'**已过期**（TTL {TTL_DAYS} 天）'}——"
              "过期的校准结果不得用于给当前支撑率背书。")
        return 0
    rep = run()
    if not rep:
        print("本次未执行（无可用模型，或全部判定失败）。**未执行 ≠ 假阳性率为 0**。")
        return 1
    _print(rep)
    if "--no-save" not in args:
        save(rep)
        print("\n已存档 → data/entailment_calibration.json（有效期 %d 天）" % TTL_DAYS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
