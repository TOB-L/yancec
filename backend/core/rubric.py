"""研策 · 报告 Rubric 自评器（大赛评分对标）

对齐 正大杯报告评审七维度（报告占国赛初赛 70% 分）：
  1 研究选题  2 文献/来源  3 方案设计(最重28)  4 调查实施(20)
  5 分析与结论(20)  6 报告文本  7 研究创新
交付前自动对报告打分（0-100），最弱维度在报告末尾披露。
维度与权重公开可核对 → "复核者视角"的产品化（对应用户看到的评分维度）。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def _has(report: str, *pats: str) -> bool:
    return any(p in (report or "") for p in pats)


# 研究报告通用方法论框架（Rubric"方案设计"维度计分同源；方法章节据实引用）
_FRAMEWORKS = ("PEST", "波特五力", "TAM", "SAM", "STP", "单位经济", "SWOT", "BCG")


def extract_frameworks(descriptions: list[str]) -> list[str]:
    """从子任务描述里提取实际用到的分析框架名（仅提取、不编造，供"研究方法"章节）
    例：`调研『市场规模测算』（TAM）` → TAM；`调研『竞争格局』（波特五力）` → 波特五力
    """
    seen: list[str] = []
    for d in descriptions or []:
        d = d or ""
        for fw in _FRAMEWORKS:
            if fw in d and fw not in seen:
                seen.append(fw)
        # 方法论括号里自定义框架（RFM/回归/渗透率模型…）也纳入，仅在确含于子任务时
        for mt in re.findall(r"[（(]([^（）()]{2,12})[)）]", d):
            if mt in seen or mt.startswith("调研") or re.match(r"^[0-9]+$", mt):
                continue
            if any(c in mt for c in (":", "：", "，", ",", "、", "方法论")):
                continue  # 跳过"方法论：…"这类引导语，只要纯框架名
            seen.append(mt)
    return seen[:6]


def score_report(state: dict, report: str) -> dict[str, Any]:
    """按七维度给报告打分（0-100）→ {total, level, breakdown, lowest}"""
    plan = state.get("plan") or {}
    subtasks = getattr(plan, "subtasks", None) or []
    evidences = state.get("evidences") or []
    conflicts = state.get("conflict_results") or []
    decision = state.get("decision_sop") or ""
    tree = getattr(plan, "problem_tree", None) or []

    breakdown: list[dict[str, Any]] = []

    # 1. 研究选题（10）：聚焦度（子任务 3-8 聚焦）+ MECE 结构
    topic, topic_reasons = 0, []
    n_st = len(subtasks)
    if 3 <= n_st <= 8:
        topic += 6; topic_reasons.append(f"子任务{n_st}个，题目聚焦")
    elif 1 <= n_st < 3:
        topic += 3; topic_reasons.append(f"子任务仅{n_st}个，覆盖偏薄")
    else:
        topic += 4; topic_reasons.append(f"子任务{n_st}个，偏多需收敛")
    if len(tree) >= 3:
        topic += 4; topic_reasons.append(f"MECE 问题树{len(tree)}分支")
    breakdown.append({"dimension": "研究选题", "max": 10, "score": topic, "reason": "；".join(topic_reasons)})

    # 2. 来源与文献（10）：独立域名数 + 官方(L1)权威
    src, src_reasons, domains, tier1 = 0, [], set(), False
    for ev in evidences:
        if ev is None or not hasattr(ev, "sources"):
            continue
        for s in (ev.sources or []):
            if s is None:
                continue
            u = (getattr(s, "url", "") or "").strip()
            if u.startswith("http"):
                try:
                    domains.add(urlparse(u).netloc)
                except Exception:
                    pass
            if str(getattr(s, "level", "")).startswith("L1"):
                tier1 = True
    src += min(len(domains), 8)
    src_reasons.append(f"独立来源域名{len(domains)}个")
    if tier1:
        src += 2; src_reasons.append("含官方(L1)权威来源")
    breakdown.append({"dimension": "来源与文献", "max": 10, "score": min(src, 10), "reason": "；".join(src_reasons)})

    # 3. 方案设计（28，最重）：方法论框架 + MECE + 置信度方法
    design, design_reasons = 0, []
    frameworks = [k for k in ("PEST", "波特五力", "TAM", "SAM", "STP", "单位经济", "SWOT", "BCG") if k in report]
    design += min(len(frameworks) * 4, 12)
    design_reasons.append(f"方法论框架{len(frameworks)}个（{'/'.join(frameworks[:4]) or '—'}）")
    if len(tree) >= 3:
        design += 8; design_reasons.append("方案结构：MECE 问题树")
    if _has(report, "置信度"):
        design += 8; design_reasons.append("置信度分级方法")
    breakdown.append({"dimension": "方案设计", "max": 28, "score": min(design, 28), "reason": "；".join(design_reasons)})

    # 4. 调查实施（20）：一手验证方案 + 证据/数据源量
    survey, survey_reasons = 0, []
    if _has(report, "一手验证方案", "问卷", "深访"):
        survey += 12; survey_reasons.append("含一手验证方案（问卷/深访/实地）")
    survey += int(min(len(evidences) * 1.5, 8))
    survey_reasons.append(f"证据/数据源{len(evidences)}条")
    breakdown.append({"dimension": "调查实施", "max": 20, "score": min(survey, 20), "reason": "；".join(survey_reasons)})

    # 5. 分析与结论（20）：置信度披露 + 冲突仲裁 + 反证 + 对抗校验 + 决策
    analysis, analysis_reasons = 0, []
    if _has(report, "结论置信度"):
        analysis += 4; analysis_reasons.append("置信度披露")
    if conflicts:
        analysis += 4; analysis_reasons.append(f"冲突仲裁{len(conflicts)}处")
    if _has(report, "【反方】", "反方证据"):
        analysis += 4; analysis_reasons.append("反证检索")
    if _has(report, "对抗性数字校验"):
        analysis += 4; analysis_reasons.append("对抗性校验")
    if decision:
        analysis += 4; analysis_reasons.append("决策SOP")
    breakdown.append({"dimension": "分析与结论", "max": 20, "score": min(analysis, 20), "reason": "；".join(analysis_reasons)})

    # 6. 报告文本（6）：结构完整（执行摘要/数据说明/附录）
    text, text_reasons, structure = 0, [], []
    if _has(report, "执行摘要", "核心指标"): structure.append("执行摘要")
    if _has(report, "数据与方法说明"): structure.append("数据说明")
    if _has(report, "引文核验", "数据缺口"): structure.append("附录")
    text = min(len(structure) * 2, 6)
    text_reasons.append(f"章节结构：{'/'.join(structure) or '—'}")
    breakdown.append({"dimension": "报告文本", "max": 6, "score": text, "reason": "；".join(text_reasons)})

    # 7. 研究创新（6）：反共识/反证洞察
    innovation, innovation_reasons = 0, []
    if _has(report, "【反方】", "反共识", "可能被低估", "风险被低估"):
        innovation += 6; innovation_reasons.append("反共识/反证洞察")
    else:
        innovation += 2; innovation_reasons.append("创新点待加强（缺反共识视角）")
    breakdown.append({"dimension": "研究创新", "max": 6, "score": innovation, "reason": "；".join(innovation_reasons)})

    total = sum(b["score"] for b in breakdown)
    level = "优秀" if total >= 85 else "良好" if total >= 70 else "达标" if total >= 55 else "需补强"
    lowest = sorted(breakdown, key=lambda b: b["score"] / max(b["max"], 1))[0]
    return {"total": total, "level": level, "breakdown": breakdown, "lowest": lowest}


def format_rubric_md(result: dict[str, Any]) -> str:
    """Rubric 自评 → markdown（追加到报告末尾）"""
    bd = result.get("breakdown", [])
    lines = [
        "\n\n---\n\n# 📝 Rubric 系统自检清单 · 报告质量（大赛评分对标）",
        f"**自检合计：{result.get('total')}/100（{result.get('level')}）** —— 对齐正大杯报告评审七维度（报告占国赛初赛 70% 分）；仅作交付前自查，非得分证明",
        "",
        "| 维度 | 自检得分 | 满分 | 核心理由 |",
        "|---|---|---|---|",
    ]
    for b in bd:
        lines.append(f"| {b['dimension']} | **{b['score']}** | {b['max']} | {b['reason']} |")
    low = result.get("lowest") or {}
    if low:
        lines.append("")
        lines.append(f"> ⚠️ 最需补强：**{low.get('dimension')}**（{low.get('score')}/{low.get('max')}）—— {low.get('reason', '')}")
    lines.append(
        "> ⚠️ 本自检为交付前自查清单，仅度量 结构/来源/方案完整度，**不度量事实与算术正确性**"
        "（规则层 R1/R4/R5/R6 与 Red Team 发现项见上文附录）；不构成得分证明，正式评分以复核者为准。"
    )
    return "\n".join(lines)


# ============================================================
# 目标大赛五维自检（双轨：正大杯评"报告质量"，目标大赛评"Agent 能力"）
# AI杭州·码动未来 · AI+超级智能体赛道 初赛评分（5×20=100）：
#   1 技术可行性  2 市场可行性  3 综合创新性  4 AI大模型结合
#   5 赛道维度评定（①任务理解与自主规划 ②工具调用与环境交互 ③结果交付与可验收 ④场景价值与端到端闭环）
# ============================================================
def competition_selfcheck(state: dict, report: str) -> dict[str, Any]:
    """按目标大赛 5×20 评分给 Agent 自检 → {total, breakdown}"""
    plan = state.get("plan") or {}
    subtasks = getattr(plan, "subtasks", None) or []
    tree = getattr(plan, "problem_tree", None) or []
    evidences = state.get("evidences") or []
    decision = state.get("decision_sop") or ""
    breakdown: list[dict[str, Any]] = []

    # 1. 技术可行性（20）：全链路可跑 + 离线降级 + 校验闭环
    tech, tech_reasons = 0, []
    if report and len(report) > 500:
        tech += 8; tech_reasons.append("报告全链路跑通")
    if _has(report, "引文核验"):
        tech += 6; tech_reasons.append("URL/证书存活核验+域名回退")
    if _has(report, "置信度", "缺口"):
        tech += 6; tech_reasons.append("置信度分级+缺口披露")
    breakdown.append({"dimension": "技术可行性", "max": 20, "score": min(tech, 20), "reason": "；".join(tech_reasons)})

    # 2. 市场可行性（20）：场景价值 + 商业模式 + 落地
    mkt, mkt_reasons = 0, []
    if _has(report, "决策清单", "该不该", "建议"):
        mkt += 6; mkt_reasons.append("数字员工场景价值（决策导向）")
    if _has(report, "一手验证方案"):
        mkt += 6; mkt_reasons.append("落地路径（一手验证方案）")
    mkt += 8; mkt_reasons.append("商业模式：档位+积分计量（预付费+按工作量）")
    breakdown.append({"dimension": "市场可行性", "max": 20, "score": min(mkt, 20), "reason": "；".join(mkt_reasons)})

    # 3. 综合创新性（20）：概念 + 技术
    inn, inn_reasons = 0, []
    if _has(report, "【反方】", "反方证据"):
        inn += 6; inn_reasons.append("反证检索/反共识洞察")
    if _has(report, "Rubric 自评"):
        inn += 6; inn_reasons.append("Rubric 自评器（复核者视角）")
    if _has(report, "对抗性数字校验"):
        inn += 8; inn_reasons.append("对抗性校验（敢挑战结论）")
    breakdown.append({"dimension": "综合创新性", "max": 20, "score": min(inn, 20), "reason": "；".join(inn_reasons)})

    # 4. AI 大模型结合（20）：检索/整合/决策/校验全环节 + 工具调用
    llm, llm_reasons = 0, []
    if _has(report, "数据与方法说明"):
        llm += 8; llm_reasons.append("检索LLM+整合LLM+决策LLM+质检LLM（全环节）")
    if _has(report, "引文核验", "URL"):
        llm += 6; llm_reasons.append("工具调用：联网检索/网页核验")
    if _has(report, "一手验证方案"):
        llm += 6; llm_reasons.append("问卷/深访设计（LLM 生成）")
    breakdown.append({"dimension": "AI大模型结合", "max": 20, "score": min(llm, 20), "reason": "；".join(llm_reasons)})

    # 5. 赛道维度评定（20）：①任务理解 ②工具调用 ③结果交付 ④场景闭环
    track = 0
    track_parts = []
    # ① 任务理解与自主规划：MECE 树 + 问题类型
    if len(tree) >= 3:
        track += 5; track_parts.append("①任务理解：MECE问题树+问题类型分流")
    # ② 工具调用与环境交互（区别"问答机器人"的关键）
    if evidences and sum(
        1
        for ev in (evidences or [])
        for s in ((getattr(ev, "sources", None) or []) if ev is not None else [])
        if s is not None and (getattr(s, "url", "") or "").startswith("http")
    ) >= 2:
        track += 5; track_parts.append("②工具调用：真实检索+URL溯源")
    # ③ 结果交付与可验收性
    if decision or _has(report, "决策清单"):
        track += 5; track_parts.append("③结果交付：决策SOP+报告")
    # ④ 场景价值与端到端闭环
    if _has(report, "引文核验") and _has(report, "置信度"):
        track += 5; track_parts.append("④场景闭环：检索→证据→分析→交付")
    breakdown.append({"dimension": "赛道维度评定", "max": 20, "score": track, "reason": "；".join(track_parts) or "四子项信号不足"})

    total = sum(b["score"] for b in breakdown)
    return {"total": total, "breakdown": breakdown}


def format_competition_md(result: dict[str, Any]) -> str:
    """目标大赛五维自检 → markdown（达标/缺口清单，无得分——自评分数不随交付件外发）"""
    bd = result.get("breakdown", [])
    lines = [
        "\n\n---\n\n# 🏆 目标大赛五维自检清单（对照评分制的达标/缺口自查，无得分）",
        "逐维对照初赛评分制（5×20：技术可行性 / 市场可行性 / 综合创新性 / AI大模型结合 / 赛道维度评定）。",
        "本清单只列达标项与待补项、不给分——得分以评审为准；仅度量 Agent 能力覆盖信号，"
        "不度量事实与算术正确性（后者由规则层 R1/R4/R5/R6 与 Red Team 承担）。",
        "",
        "| 维度 | 自查 | 依据 |",
        "|---|---|---|",
    ]
    for b in bd:
        _ratio = (b.get("score") or 0) / max(b.get("max") or 1, 1)
        _mark = "达标" if _ratio >= 0.7 else "有缺口"
        lines.append(f"| {b['dimension']} | {_mark} | {b.get('reason', '')} |")
    lines.append(
        "> 赛道维度评定含四子项：①任务理解与自主规划 ②工具调用与环境交互（区别'问答机器人'的关键）"
        "③结果交付与可验收性 ④场景价值与端到端闭环。"
    )
    return "\n".join(lines)
