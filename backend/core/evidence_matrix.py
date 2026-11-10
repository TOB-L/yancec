"""研策 · 结论状态机 + 双源配对矩阵（Evidence Status & Dual-source Pairing）

## 为什么要有这一层

行业通病是"**有答案 → 给答案**"。专业研究不是这样：

    真正专业的是 —— 证据不足时，明确说"我没有足够证据"。

以及：同一个数字，**两个独立来源打架**时，不是选一个报出去，而是**呈现分歧并降置信**。

本模块把这两件事工程化：
1. **结论状态机**：每条结论落一个状态 —— CONFIRMED / LIKELY / UNCERTAIN / CONTESTED / INSUFFICIENT
   （不再是笼统的"高/中/低"——`CONTESTED` 和 `INSUFFICIENT` 是**敢承认不知道**的两个状态）
2. **双源配对矩阵**：按结论类型固定"验证对"（公司数字=披露+第三方；行业数字=官方+行研），
   逐条核对是否满足；不满足 → 显式标出缺哪一侧，**不假装已交叉验证**

## 诚实边界
- 状态由**已有证据的客观属性**推导（信源层级 / 独立域名数 / 是否含冲突），不引入新判断。
- 不满足配对**不阻断交付**，只产生可见的记录 —— 缺口要看得见，不许静默"通过"。
- 与 `core/confidence.py` 共用同一套信源判定，**不另立口径**（避免两处结论互相打脸）。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.confidence import distinct_source_domains, grade_evidence_confidence
from core.models import Evidence, SourceLevel

# ============================================================
# 结论状态机
# ============================================================
# 五态取值（对齐业界研究交付惯例）：
#   CONFIRMED    已确证  —— 一手/权威源 + 多独立来源，可直接决策引用
#   LIKELY       较可信  —— 有权威源但独立来源不足，或来源权威度中等
#   UNCERTAIN    不确定  —— 仅二手/弱源，或口径不明
#   CONTESTED    存争议  —— 多来源**数据打架**（不静默取一个）
#   INSUFFICIENT 证据不足 —— 一手源缺失且无可信二手交叉；**敢于不下结论**
STATUS_META: dict[str, tuple[str, str]] = {
    "CONFIRMED": ("✅ 已确证", "一手/权威源 + 多独立来源，可直接引用"),
    "LIKELY": ("🟢 较可信", "有权威源但独立来源不足，建议落地前补第二来源"),
    "UNCERTAIN": ("🟡 不确定", "以二手/弱源为主或口径不明，仅作线索"),
    "CONTESTED": ("⚔️ 存争议", "多来源数据打架，已并列呈现、不下单方结论"),
    "INSUFFICIENT": ("❓ 证据不足", "一手源缺失且无可信交叉，**不宜据此决策**"),
}
_STATUS_ORDER = ("CONFIRMED", "LIKELY", "CONTESTED", "UNCERTAIN", "INSUFFICIENT")


@dataclass
class StatusVerdict:
    code: str = "UNCERTAIN"
    label: str = ""
    reason: str = ""

    def mark(self) -> str:
        return STATUS_META.get(self.code, ("", ""))[0]


def judge_status(ev: Evidence) -> StatusVerdict:
    """给一条结论判定状态（五态）

    判定顺序（先判"打架"和"没证据"，再判好坏）：
    1. 有冲突标记 → CONTESTED（**冲突优先于一切**：数据打架时"高置信"是假象）
    2. 无来源 / 无真实URL / 无 L1-L3 权威源 → INSUFFICIENT（宁说不足，不给假确定）
    3. 置信度=高 → CONFIRMED；=中 → LIKELY；=低 → UNCERTAIN
    """
    label0 = STATUS_META["UNCERTAIN"][0]
    # 1. 冲突优先
    if getattr(ev, "is_conflicting", False) or str(getattr(ev, "conflict_detail", "") or "").strip():
        return StatusVerdict("CONTESTED", STATUS_META["CONTESTED"][0], "多来源数据存在冲突，已并列呈现")

    sources = ev.sources or []
    domains = distinct_source_domains(ev)
    best = max((s.level for s in sources), key=_level_rank) if sources else None

    # 2. 证据不足：没有可点来源，或最高来源连 L3 都不到（L4 社媒/L5 推测撑不起结论）
    if not sources or not domains:
        return StatusVerdict("INSUFFICIENT", STATUS_META["INSUFFICIENT"][0], "无可点开的真实来源页")
    if best in (SourceLevel.L4_USER, SourceLevel.L5_SPECULATION):
        return StatusVerdict(
            "INSUFFICIENT", STATUS_META["INSUFFICIENT"][0],
            f"最高信源仅 {best.value}（{'用户/社媒' if best == SourceLevel.L4_USER else '推测'}），不足以支撑结论",
        )

    # 3. 按置信度映射
    try:
        c = grade_evidence_confidence(ev)
    except Exception:
        return StatusVerdict("UNCERTAIN", label0, "置信度无法判定")
    if c.level == "high":
        return StatusVerdict("CONFIRMED", STATUS_META["CONFIRMED"][0], "；".join(c.reasons[:2])[:60])
    if c.level == "medium":
        return StatusVerdict("LIKELY", STATUS_META["LIKELY"][0], "；".join(c.reasons[:2])[:60])
    return StatusVerdict("UNCERTAIN", label0, "；".join(c.reasons[:2])[:60])


def _level_rank(lv: SourceLevel) -> int:
    """信源等级排序用（L1 最强 → L5 最弱）"""
    return {
        SourceLevel.L1_OFFICIAL: 5,
        SourceLevel.L2_SEMI_OFFICIAL: 4,
        SourceLevel.L3_INDUSTRY: 3,
        SourceLevel.L4_USER: 2,
        SourceLevel.L5_SPECULATION: 1,
    }.get(lv, 0)


def status_distribution(evidences: list[Evidence]) -> dict[str, int]:
    """全部结论的状态分布（供披露附录）"""
    dist: dict[str, int] = {k: 0 for k in _STATUS_ORDER}
    for ev in evidences or []:
        try:
            dist[judge_status(ev).code] = dist.get(judge_status(ev).code, 0) + 1
        except Exception:
            continue
    return dist


# ============================================================
# 双源配对矩阵
# ============================================================
@dataclass
class PairRow:
    claim_label: str
    pair_required: str        # 该类型要求的验证对
    gott: str                 # 实际拿到哪几侧
    ok: bool                  # 来源是否**不单一**（异侧≥2 或独立域名≥2）
    n: int                    # 该类结论条数
    pair_ok: bool = False     # 是否**真的满足**上面那一列写明的验证对
    missing: str = ""         # pair_ok=False 时，缺的是哪一侧


# `pair_required` 字面 → 必须出现的来源侧。**机械映射，只认字面**（不做语义推断）。
# 键是 requirement 文本里出现的词，值是 `_side_of()` 会产出的侧别。
# ⚠️ `_side_of()` 只产出三种值（一手-官方 / 第三方-机构 / 第三方-媒体），
# 所以「企业侧（披露/官网）」在这里只能落到「一手-官方」——
# **这是近似，必须让读者看见**（`missing` 会照实写"缺 一手-官方"），不许假装精确。
_REQ_SIDE: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("法定披露", "部委原文", "官方统计", "总量（一手）", "（一手）", "一手"), ("一手-官方",)),
    (("企业侧",), ("一手-官方",)),
    (("第三方", "研报", "机构", "媒体", "解读", "权威媒体"), ("第三方-机构", "第三方-媒体")),
    (("社媒", "评价", "现象"), ("第三方-媒体",)),
)


def _required_sides(pair_required: str) -> list[tuple[str, tuple[str, ...]]]:
    """验证对文本 → [(要求原文, 可满足它的来源侧)]。取不到 → 空（= 不判，不猜）。"""
    out: list[tuple[str, tuple[str, ...]]] = []
    _t = str(pair_required or "")
    _i = _t.find("——")          # 破折号后是该要求的用途说明，不是要求本身
    if _i > 0:
        _t = _t[:_i]
    for _label in (x.strip() for x in _t.replace("＋", "+").split("+")):
        if not _label:
            continue
        for _keys, _sides in _REQ_SIDE:
            if any(k in _label for k in _keys):
                out.append((_label, _sides))
                break
    return out


def pair_matrix(evidences: list[Evidence], limit: int = 8) -> list[PairRow]:
    """按结论类型核对"双源配对"（每一类最多返回一行，按条数降序）

    **两把尺，两列，都印出来**（2026-09-18 修，来自外部审计 A1）：
    - `ok`       —— 来源是否**不单一**：异侧 ≥2，或独立域名 ≥2。这是"有没有交叉验证"。
    - `pair_ok`  —— 是否**真的满足**该行 `pair_required` 那一列写明的验证对。

    修之前只有第一把尺，而 `pair_required` **只被打印、不参与任何判定**，
    于是同一行可以一边印「要求：法定披露（一手）+ 第三方」、一边判 ✅，
    哪怕调用台账显示巨潮/SEC 本次**零调用**——**契约与判定不同源**，
    而"双源配对矩阵"恰恰是本产品对外主张可核对的那张表。
    两列并排后，"来源不单一"与"满足了你看到的那条要求"不再混为一谈。
    """
    try:
        from core.source_registry import CLAIM_ROUTES, route_claim
    except Exception:
        return []
    _by_key = {r.key: r for r in CLAIM_ROUTES}
    buckets: dict[str, list[Evidence]] = {}
    for ev in evidences or []:
        try:
            k = route_claim(str(getattr(ev, "claim", "") or "")).key
        except Exception:
            continue
        buckets.setdefault(k, []).append(ev)

    rows: list[PairRow] = []
    for k, evs in buckets.items():
        r = _by_key.get(k)
        if r is None:
            continue
        sides: set[str] = set()
        doms: set[str] = set()
        for e in evs:
            sides |= _side_of(e)
            doms |= distinct_source_domains(e)
        ok = len(sides) >= 2 or len(doms) >= 2
        # 第二把尺：逐条要求核。**取不到要求 → 不判**（pair_ok 保持 False 但 missing 写清
        # "要求无法机械解析"），宁可显示"无法判定"，也不显示一个假的 ✅。
        _reqs = _required_sides(r.pair)
        _missing: list[str] = []
        for _label, _ok_sides in _reqs:
            if not (sides & set(_ok_sides)):
                _missing.append(_label)
        if not _reqs:
            _pair_ok = False
            _missing = ["（该行验证对无法机械解析，规则层不判）"]
        else:
            _pair_ok = not _missing
        rows.append(
            PairRow(
                claim_label=r.label,
                pair_required=r.pair,
                gott="＋".join(sorted(sides)) if sides else "无可点来源",
                ok=ok,
                n=len(evs),
                pair_ok=_pair_ok,
                missing="；".join(_missing),
            )
        )
    rows.sort(key=lambda x: (-x.n, x.claim_label))
    return rows[:limit]


def _side_of(ev: Evidence) -> set[str]:
    """一条结论的**来源侧**：一手(企业/官方原文) vs 第三方(研报/媒体)

    判定同 `route_source` 口径：
    - URL 命中 TIER1(政府/交易所/巨潮) → 一手-官方
    - 命中 TIER2 且分组=研究报告 → 第三方-机构
    - 其余(媒体/门户/社媒) → 第三方-媒体
    """
    sides: set[str] = set()
    try:
        from core.source_router import route_source
    except Exception:
        return sides
    for s in ev.sources or []:
        u = (s.url or "").strip()
        if not u.startswith("http"):
            continue
        try:
            tier, grp = route_source(u, s.publisher or "")
        except Exception:
            continue
        if tier <= 1:
            sides.add("一手-官方")
        elif tier == 2 and grp == "研究报告":
            sides.add("第三方-机构")
        else:
            sides.add("第三方-媒体")
    return sides


def describe() -> str:
    return (
        "结论状态机（CONFIRMED/LIKELY/UNCERTAIN/CONTESTED/INSUFFICIENT）+ 双源配对矩阵："
        "① 每条结论落一个**敢承认不知道**的状态（冲突标 CONTESTED、一手源缺失标 INSUFFICIENT，"
        "不硬给一个数字）；② 按结论类型列出验证对，并**用两把尺分别核**——"
        "「来源不单一」（异侧 ≥2 或独立域名 ≥2）与「满足该行写明的验证对」（逐侧点名核对）。"
        # ⚠️ 2026-09-18 审计 A1 修的就是这里，改判定后**同一轮**改了这句话：
        # 此前 `pair_required` 那一列**只被打印、不参与判定**，于是一行可以一边印
        # 「要求：法定披露（一手）+第三方」、一边判 ✅，哪怕台账显示巨潮/SEC 本次零调用。
        # 现在 `pair_ok` 逐条核该要求的每一侧，**缺哪一侧写在明面上**（`missing`）。
        "**边界（必读）**：`pair_ok` 的映射是**机械的、只认字面**——"
        "`_side_of()` 只会产出「一手-官方 / 第三方-机构 / 第三方-媒体」三种值，"
        "所以要求里写的「企业侧（披露/官网）」只能近似落到「一手-官方」；"
        "若某行的验证对无法机械解析，该行显式标注**不判**（`pair_ok=False` + missing 说明），"
        "不显示假的 ✅。**这一层不阻断交付，只让缺口可见。**"
    )
