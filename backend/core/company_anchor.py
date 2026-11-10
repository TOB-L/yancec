"""研策 · 公司级数字口径锚（P0-4）—— 治"同报告跨章打脸"（瑞幸市占32.5% vs 18.5%、门店17,242 vs 18,590、
星巴克单店投入45/98/120–180万三套并存）

病案（报告(6)验收）：主章与深入章由 _integrate 并行独立成文、互不可见，各自信不同来源 →
同一家公司/同一指标/同一时间在同一份报告里出现两套权威值，复核者/投资人一翻头对头表就穿帮。
根因：各写作者没有任何"同报告数字口径锚"；规则层 R1 又刻意不覆盖市占/门店数这类总量词。

方案（确定性下沉，纯只读，不改任何写回路径，沿用 facts_kb / 行业卡护栏先例）：
1. data/company_anchors.json：按 industry 存放本行业"公司级口径锚"，人工核验后登记。
   锚分三类：hard（同报告必须采用或并列标注冲突）、ownership（口径归属锁定，防把星巴克写成加盟）、
   disclosure（披露规则：经营数须带样本口径、无背书标待核）。
2. build_anchor_block(industry) -> str：确定性渲染成写给写作者的锚块（无该行业锚 → 空串）。
   锚块与 {viewpoint} 同一插槽注入 _integrate 的 human 消息 + TAM/单位经济参数抽取器 —— 一处生成、全章节可见。
3. 任何异常返回 "" 不阻断；无 LLM、无网络、无写盘。

验证：python core/company_anchor.py（离线自测，无网络）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ANCHOR_PATH = Path(__file__).resolve().parents[1] / "data" / "company_anchors.json"

# kind → 写给写作者的一句话约束
#
# ⚠️ `ownership` 这句里原先有个举例「如把直营说成加盟」——**2026-09-18 删掉**。
# 病案：星巴克中国 2025-11 官宣、2026 年春交割（博裕 60%、约 8,000 家直营转特许），
# 于是这条锚的**值**改成了"已不再是 100% 直营"，而**标签**还在教写作者"把直营说成加盟是错的"——
# 标签与值当场互斥。这正是本仓库第 N 次出现的「**审计层的话与判定不同源**」：
# 一句话是上一版世界写下的，另一句话是这一版世界写下的，两句话印在同一段里。
# 教训：**标签不许举具体实体的例子**——例子会随事实过期，而标签没人会想起来改。
_KIND_LABEL = {
    "hard": "【口径锚·须采用】同一指标只能用一个权威值；与锚冲突的来源只能并列标注，禁止另取一个数与锚或其它章打架",
    "ownership": "【口径归属锁定】主体、经营模式与口径归属一律以本条为准：同一年份/同一主体只允许一套说法，禁止把 A 口径的数值安到 B 主体头上，禁止沿用本条已标注为『已失效』的旧表述",
    "disclosure": "【披露规则】经营类数字须带样本口径；无权威背书的按(估算/待核)标注，禁止当已核事实",
    # 2026-09-18 新增第三类。病案：第十跑正文以「星巴克 100% 直营、加盟未开放」
    # 「县域门店仅 127 家、占比 1.8%」「截至 2024 年中未公布下沉量化目标」为前提——
    # 三条都不是"数字写错了"，是**报告拿 2024 年的世界做 2026 年的决策**。
    # 前面三类都假定"锚里给的值是对的、照用即可"，装不下"这一条本身已作废"。
    # 硬塞进 disclosure 会让它顶着"须带样本口径"的标签印出来——**标签与值不同源**，
    # 正是同一天在 ownership 上刚修掉的那个病。所以给它一个自己的类。
    "expired": "【已失效前提·禁止沿用】本条列出的事实前提已经过期：禁止再作为当前现状引用；确需引用必须显式写明『<年份> 口径，现已失效』并给出更新后的现状",
}


def _load() -> dict[str, Any]:
    try:
        if _ANCHOR_PATH.exists():
            return json.loads(_ANCHOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def build_anchor_block(industry: str) -> str:
    """按行业渲染数字口径锚块；无该行业登记 → 返回空串（不干扰其它行业）。"""
    ind = (industry or "").strip()
    if not ind:
        return ""
    try:
        entries = _load().get(ind) or []
        if not entries:
            return ""
        lines = [
            "【本报告数字口径锚（跨章一致性基准）】",
            "主章与深入章并行写作且互不可见，本锚让全报告用同一套公司级数字。写本章时：",
        ]
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind") or "hard")
            ent = str(e.get("entity") or "").strip()
            val = str(e.get("value") or "").strip()
            per = str(e.get("period") or "").strip()
            src = str(e.get("source") or "").strip()
            _ctx = f"（{per}）" if per else ""
            _src = f"，来源:{src}" if src else ""
            _lab = _KIND_LABEL.get(kind, "")
            lines.append(f"{i}. {ent}{_ctx}：{val}{_src} —— {_lab}")
            _note = str(e.get("note") or "").strip()
            if _note:
                # note 常含"禁止…""必须注明口径…"等写作者的绑定约束，必须随锚一起注入（否则形同虚设）
                lines.append(f"   （约束：{_note}）")
        lines.append(
            "若你检索到同一指标更新/更高权威（官方财报/政府统计）来源，可采用更新值但必须显式写明年份时点，"
            "并在其它冲突出现处并列标注——同一报告内禁止出现两套互不认账的权威值。"
        )
        return "\n".join(lines)
    except Exception:
        return ""


def list_industries() -> list[str]:
    try:
        return list(_load().keys())
    except Exception:
        return []


# ============================================================
# 离线自测（无网络；python core/company_anchor.py）
# ============================================================
def _selftest() -> int:
    import sys

    ok = 0

    def _check(name: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            print(f"[{name}] ✗ 断言失败")
            sys.exit(1)
        ok += 1
        print(f"[{name}] ✓")

    b = build_anchor_block("咖啡")
    _check("咖啡锚块非空", bool(b))
    _check("含瑞幸门店硬锚", "18,590家" in b)
    # ── 2026-09-18：星巴克经营模式锚从「100%直营不加盟」改成「已转特许」 ──
    # 这三条锁的是**事实更新有没有真的走到渲染出来的这段字里**——
    # 只改 JSON 不改锁，下次谁把 JSON 改回旧值不会有任何东西响。
    _check("星巴克已转特许（博裕）", "博裕" in b and "特许经营" in b)
    _check("旧『100%直营、不开放加盟』不再是断言形式", "100%直营、不开放加盟" not in b)
    _check("旧前提被显式判失效", "已失效" in b)
    # 2026-09-18：新增 expired 类 + 三条失效前提登记（外部审计对第十跑正文的判定）
    _check("失效前提登记进锚块", "禁止再作现状前提使用" in b)
    _check("失效前提带自己的类标签", "【已失效前提·禁止沿用】" in b)
    _check("三条失效前提都点了名", "不开放加盟" in b and "127 家" in b and "未公布下沉量化目标" in b)
    _check("从沉默推否证被显式点名", "从沉默推否证" in b)
    _check("含披露规则", "样本口径" in b and "待核" in b)
    _check("含统一纪律句", "同一报告内禁止出现两套" in b)
    # ownership 标签不许再举具体实体例子（例子会随事实过期，标签没人会想起来改）
    _check("ownership 标签不举实体例子", "如把直营说成加盟" not in b)
    _check("无登记行业→空串", build_anchor_block("不存在行业xyz") == "")
    _check("空行业→空串", build_anchor_block("") == "")

    print(f"\n数字口径锚自测：{ok} 项全过（离线，无网络）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
