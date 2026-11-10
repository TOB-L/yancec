"""研策 · 结论置信度分级（可信度护城河：让每条结论的可靠性一目了然）

把"证据可信度合成分"升级为对决策者友好的 高/中/低 置信度，
并给出可解释理由（信源等级 / 独立来源数 / 时效 / 是否推测）。
对齐 P0 项：
- 置信度分级：每个关键结论标注 高/中/低 + 理由
- 关键论断 2+ 来源：单源结论置信度强制降级
- 与 4 维证据仲裁模型共用一套评分（权重公开可核对，不是黑箱）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from core.models import Evidence, SourceLevel, evidence_credibility_score

_LABEL_MAP = {"high": "高", "medium": "中", "low": "低"}


@dataclass
class Confidence:
    """一条证据的置信度结论"""

    level: str = "low"            # high / medium / low
    label: str = "低"             # 高 / 中 / 低
    score: float = 0.0            # 0-1 合成分
    reasons: list[str] = field(default_factory=list)  # 可解释理由
    downgraded: bool = False      # 是否因证据不足被强制降级


def distinct_source_domains(evidence: Evidence) -> set[str]:
    """该证据的独立来源域名数（去重 + www/端口归一；http(s) 链接才算独立来源）

    同机构 www.xxx.cn 与 xxx.cn 视为同一独立来源——否则同一家官网的两页会被
    误判成 2 个独立来源，虚高"多源佐证≥2"，把单源结论错标高置信。
    """
    # 用**可注册根域**而不是裸 hostname（2026-09-14 审计）。
    # 原实现只去掉 `www.`，于是 `finance.sina.com.cn` 与 `www.sina.com.cn` 被算成
    # **两个独立来源**——同一家媒体的两个栏目凑出"多源佐证≥2"，把单源结论抬成高置信。
    # 这正是本函数 docstring 自己警告的那件事，只是原来没真正做到。
    # `source_util.registrable_root` 会正确处理 `.com.cn`/`.gov.cn` 这类多段后缀。
    from core.source_util import registrable_root

    domains: set[str] = set()
    for s in evidence.sources:
        u = (s.url or "").strip()
        if u.startswith("http"):
            try:
                host = registrable_root(u)
                if host:
                    domains.add(host)
            except Exception:
                pass
    return domains


def grade_evidence_confidence(evidence: Evidence) -> Confidence:
    """给一条证据打置信度（高/中/低）+ 可解释理由。

    规则（公开、可核对，复核者可逐条对账）：
    1. 基准分 = 最高信源的可信度合成分（evidence_credibility_score）
    2. 推测(L5) / is_speculation → 强制"低"
    3. 多源判定 = 独立来源域名 ≥2 个 **或** 最高信源佐证数 ≥2（llm_search 整合多来源）
       —— 单源结论 → 上限"中"（降级，P0 关键论断 2+ 来源）
    4. 无真实URL（无法点开溯源）→ 上限"中"
    5. 其余按合成分：≥0.75 高 / ≥0.5 中 / 其余 低
    """
    if not evidence.sources:
        return Confidence("low", "低", 0.0, ["无来源支撑"], downgraded=True)

    best = max(evidence.sources, key=lambda s: evidence_credibility_score(s))
    base = round(evidence_credibility_score(best), 3)
    domains = distinct_source_domains(evidence)
    corr = max((s.corroboration_count for s in evidence.sources), default=0)
    # 高置信的前提是"真实独立来源域名 ≥2"。corr(佐证页数)可≥2但同一域名不算独立。
    # 例外：L1 官方权威(统计/监管)单域 + 佐证≥2（如两页同属统计局相互印证）仍可高；
    #      L2 品牌官网/媒体单域永远不算独立多源（防"用官网主页撑市场级结论"名实不符）。
    multi_source = len(domains) >= 2 or (
        len(domains) == 1 and corr >= 2 and best.level == SourceLevel.L1_OFFICIAL
    )

    level_reason = {
        SourceLevel.L1_OFFICIAL: "官方来源(L1)",
        SourceLevel.L2_SEMI_OFFICIAL: "半官方来源(L2)",
        SourceLevel.L3_INDUSTRY: "行业来源(L3)",
        SourceLevel.L4_USER: "用户来源(L4)",
        SourceLevel.L5_SPECULATION: "推测(L5)",
    }
    reasons: list[str] = [f"最高信源：{level_reason.get(best.level, best.level.value)}"]
    reasons.append(f"独立来源域名：{len(domains)} 个")

    downgraded = False
    # 推测 → 强制低
    if evidence.is_speculation or best.level == SourceLevel.L5_SPECULATION:
        level, label = "low", "低"
        reasons.append("含推测参数 → 置信度上限为低")
        downgraded = True
    # 无真实URL → 无法点开溯源，上限中
    elif not domains:
        level, label = "medium", "中"
        reasons.append("无真实URL → 无法点开溯源，上限为中")
        downgraded = True
    # 独立来源不足（单域，即便同域多页）→ 降级到中（P0：关键论断需 2+ 独立来源）
    elif not multi_source:
        level, label = "medium", "中"
        reasons.append("独立来源不足（<2 个域名）→ 置信度上限为中，需补充第二独立来源交叉验证")
        if corr >= 2:
            reasons.append(f"同域 {corr} 个来源页不构成独立多源")
        downgraded = True
    elif base >= 0.75:
        level, label = "high", "高"
        if len(domains) >= 2:
            reasons.append("多源佐证：≥2 个独立来源域名")
        else:
            reasons.append("L1 官方权威来源（统计/监管）+ 佐证≥2，官方口径可信")
    elif base >= 0.5:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"

    return Confidence(level, label, base, reasons, downgraded)


def confidence_distribution(evidences: list[Evidence]) -> dict[str, int]:
    """报告全部证据的置信度分布（高/中/低计数，供披露附录）"""
    dist: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for ev in evidences or []:
        c = grade_evidence_confidence(ev)
        dist[c.level] = dist.get(c.level, 0) + 1
    return dist
