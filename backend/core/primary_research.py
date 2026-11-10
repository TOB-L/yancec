"""研策 · 一手验证方案设计（AI 深访/问卷 —— 差异化护城河）

解决行业公害"只有二手数据"：把报告末尾"建议补一手验证"升级为
"已经帮你设计好怎么做一手验证"——问卷（对象/样本/问法）+ 深访（访谈谁/问什么）
+ 实地（踩点计划/观察清单）+ 成本周期。

输出直接可执行，这是"数字员工=能落地的员工"而非"只会写报告"的关键差异点。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_PROMPT = """你是研策的市场研究经理。基于这份报告，为用户设计一份【可直接执行的一手验证方案】（问卷 + 深访 + 实地）。

背景：报告基于公开二手数据，以下结论置信度不足或存在数据缺口，需要一手验证：
{gaps}

输出 JSON（只输出 JSON，不要其他文字）：
{{
  "objective": "一手验证的核心目标（1-2句）",
  "survey": {{
    "target": "问卷对象（谁，如 目标商圈18-35岁上班族）",
    "sample_size": "建议样本量与置信度（如 300份，95%置信度±5.7%）",
    "method": "投放方式（线上问卷/门店拦截/社群投放）",
    "questions": ["核心问题1（含具体问法）", "核心问题2", "核心问题3", "核心问题4", "核心问题5"]
  }},
  "interviews": {{
    "targets": ["访谈对象1（如 已开店老板×3）", "访谈对象2（供应商/批发商）", "访谈对象3（商场招商/物业）"],
    "guide": ["访谈核心问题1", "访谈核心问题2", "访谈核心问题3"]
  }},
  "field": {{
    "plan": "实地暗访计划（踩点哪些店、看什么）",
    "checklist": ["观察项1", "观察项2", "观察项3", "观察项4"]
  }},
  "estimated_cost": "预估成本与周期（如 ¥3,000 预算 + 2 周）"
}}
"""


def scan_gaps(report: str) -> list[str]:
    """扫描报告里的数据缺口/低置信表述 → 待一手验证的点"""
    gaps: list[str] = []
    patterns = [
        "暂缺", "未检索到", "缺乏可靠", "无公开", "未覆盖", "一手验证",
        "待验证", "置信度上限为中", "建议补充第二来源", "低置信", "估算",
    ]
    lines = (report or "").split("\n")
    for ln in lines:
        s = ln.strip()
        if any(p in s for p in patterns) and len(s) > 6:
            gaps.append(s[:60])
    if not gaps:
        gaps = ["市场规模/渗透率等关键参数以估算为主，需一手问卷验证"]
    # 去重截断
    return list(dict.fromkeys(gaps))[:5]


def design_primary_research(report: str, gaps: list[str] | None = None) -> dict[str, Any]:
    """生成一手验证方案（LLM 优先，无 LLM/失败 降级模板——始终给出可执行方案）"""
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm() or not report:
            return _fallback_design()
        llm = get_main_llm(temperature=0.3, max_tokens=1500)
        gaps_txt = "；".join(gaps or scan_gaps(report))
        resp = llm.invoke(
            [
                ("system", _PROMPT.format(gaps=gaps_txt)),
                ("human", f"报告（节选）：\n{report[:3000]}"),
            ]
        )
        content = (resp.content or "").strip()
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return _fallback_design()
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) and data.get("survey") else _fallback_design()
    except Exception as e:
        logger.warning("一手验证方案生成失败: %s", e)
        return _fallback_design()


def _fallback_design() -> dict[str, Any]:
    """模板兜底（无 LLM / 失败时也能给可执行方案）"""
    return {
        "objective": "验证市场规模与消费频次等关键假设（报告以估算为主）",
        "survey": {
            "target": "目标商圈潜在消费者",
            "sample_size": "300 份（95% 置信度，±5.7%）",
            "method": "门店周边拦截 + 线上社群投放",
            "questions": [
                "您每月购买现制咖啡的频次？（过去30天实际次数）",
                "您常去的咖啡品牌与单次消费金额？",
                "您选择咖啡店最看重的三个因素（口味/价格/位置/环境/服务）？",
                "如果附近新开一家店，您愿意尝试的可能性（0-10分）？",
                "您平均每周在该区域消费咖啡的次数与时段？",
            ],
        },
        "interviews": {
            "targets": ["已开店老板×3", "供应商/批发商×1", "商场招商/物业×1"],
            "guide": [
                "单店真实月营收与成本结构（房租/人力/原料占比）？",
                "开业至今的盈亏曲线与回本周期？",
                "最痛的经营问题（流量/复购/竞争/成本）？",
            ],
        },
        "field": {
            "plan": "踩点目标商圈 5 家头部店，分时段记录客流与消费",
            "checklist": ["工作日上午/下午客流", "客单价与产品结构", "外卖单量", "竞品定价与活动"],
        },
        "estimated_cost": "约 ¥3,000 预算 + 2 周（问卷¥500 + 访谈¥1,500 + 实地¥1,000）",
    }


_SELF_DOMAIN_TERMS = (
    "GB 45438", "GB45438", "人工智能生成合成内容标识", "生成合成内容标识",
    "AIGC-YC", "《人工智能生成合成内容标识办法》",
)
"""**引擎自身的合规词汇**——它们绝不该出现在"给被调研行业设计的一手方案"里。

病案（2026-09-17 元气森林跑，Claude A1 / GPT §16 同时点名）：
    本模块拿**整份报告**当上下文，而报告顶部就有研策自己的 AI 标识声明
    （「依《人工智能生成合成内容标识办法》（2025-09-01 施行）及 GB 45438-2025…」）。
    于是模型把它当成了**被调研行业的一条法规**，在一手方案的深访对象里写出
    「核实 GB 45438-2025 及标签新规带来的单瓶包材成本实际涨幅」。
    **GB 45438-2025 是《网络安全技术 人工智能生成合成内容标识方法》，与食品标签、
    包材成本毫无关系。** 复核者席是技术内行——一份饮料报告里出现 AI 标识标准，
    读者不会想"这里有个小错"，而会想"它的所有编号还可信吗"。
    **它用一个错误污染了整份报告最值钱的资产：引用可信度。**

这不是"模型偶尔幻觉"，是**系统自身的合规任务泄漏进了用户的研究任务**
（GPT 给的命名：Domain Contamination）。修法放在**出口**而不是提示词：
出口过滤是确定性的、可核对的，且不会动 `prompt_set`（改提示词会让核验器校准判废）。
"""


def _strip_self_domain(lines: list[str]) -> tuple[list[str], int]:
    """摘掉含引擎自身合规词的整行 → (新行, 摘掉几行)。

    **整行摘，不做子串替换**：把 `GB 45438-2025` 抠掉之后剩下的是半句话，
    比留着更容易被误读。摘掉要**如实说**（公理六：降级必须告警，不许悄悄兜底）。
    """
    kept, dropped = [], 0
    for ln in lines:
        if any(t in ln for t in _SELF_DOMAIN_TERMS):
            dropped += 1
            continue
        kept.append(ln)
    return kept, dropped


def format_md(design: dict[str, Any]) -> str:
    """一手验证方案 → markdown 展示"""
    if not design:
        return ""
    lines = ["# 🔬 一手验证方案（AI 深访/问卷设计）", ""]
    if design.get("objective"):
        lines.append(f"**核心目标**：{design['objective']}")
        lines.append("")
    surv = design.get("survey") or {}
    if surv:
        lines.append("## 📋 问卷设计")
        lines.append(f"- **对象**：{surv.get('target', '—')}")
        lines.append(f"- **样本**：{surv.get('sample_size', '—')}")
        lines.append(f"- **投放**：{surv.get('method', '—')}")
        for q in surv.get("questions", [])[:6]:
            lines.append(f"- Q：{q}")
        lines.append("")
    iv = design.get("interviews") or {}
    if iv:
        lines.append("## 🎙 深访设计")
        for t in iv.get("targets", [])[:5]:
            lines.append(f"- **{t}**")
        for g in iv.get("guide", [])[:4]:
            lines.append(f"  - 问：{g}")
        lines.append("")
    fd = design.get("field") or {}
    if fd:
        lines.append("## 🚶 实地计划")
        lines.append(f"- {fd.get('plan', '')}")
        for c in fd.get("checklist", [])[:5]:
            lines.append(f"- 观察：{c}")
        lines.append("")
    if design.get("estimated_cost"):
        lines.append(f"## 💰 成本与周期\n- {design['estimated_cost']}")
    lines.append("")
    lines.append("> 该方案可直接执行：问卷用问卷星/腾讯问卷投放，访谈按对象约人，实地按清单踩点。研策可协助数据分析。")
    lines, _dropped = _strip_self_domain(lines)
    if _dropped:
        lines.append("")
        lines.append(
            f"> ⚠️ **已摘除 {_dropped} 行**：这些行把**研策自身的交付合规词汇**"
            "（如 AI 生成合成内容标识标准 GB 45438-2025）误写成了**被调研行业的法规**——"
            "两者毫无关系。整行摘除而非局部替换，是因为抠掉编号后剩下的半句话更易被误读。"
            "**本次摘除即本节的一处降级，如实列出，不静默。**"
        )
    return "\n".join(lines)
