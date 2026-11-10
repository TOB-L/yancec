"""研策 · AI 透明度报告（双模式）

对齐 ICC/ESOMAR 2025 AI 条款 + 正大杯 AI 工具使用规范：
- 学术/比赛模式（competition，默认）：完整 AI 使用附录——工具/模型/方法/来源/置信度/局限/交互记录
  （"AI 影响原创度是评审重点"，透明度=加分）
- 企业/商用模式（enterprise）：专业研报样式，突出"可审计"而非"AI 披露"——不附 AI 附录，避免对 B 端客户"自爆"

通过请求 context.transparency_mode 切换（默认 competition）。
"""

from __future__ import annotations

from typing import Any

# 本次研策使用的模型/工具（随版本更新）
_LLM_STACK = "通义千问 qwen-plus（检索/整合/决策/质检）+ DeepSeek（降级兜底）"
_TOOLS = [
    "联网检索与取证（自研数据源适配层：博查/Bing Web API/Bing 结果页解析 + 真实网页抓取；千问仅理解结果、不编链接，含类型黑名单过滤）",
    "网页抓取与引文核验（URL 存活 + 证书可信 + 域名回退）",
    "Source Router 白名单（官方/研究机构分级）",
    "Python 测算引擎（TAM/SAM/SOM、单位经济、置信度分级）",
    "反证检索（决策/风险维度主动找反方证据）",
    "对抗性数字校验（Red Team Review）",
    "一手验证方案设计（问卷/深访/实地）",
    "报告质量自检器（交付前自查：结构/来源/方案完整度，非得分证明）+ 目标大赛五维达标自查（无得分）",
]
_METHODS = [
    "MECE 问题树（需求/供给/资源/财务四分支）",
    "假设驱动（因果类问题 H1-H3 + 支持/反证证据表）",
    "TAM/SAM/SOM 三层测算（人口基数由知识库注入）",
    "波特五力 / PEST / STP / 单位经济",
    "关键数字 2+ 独立来源交叉验证",
    "冲突证据不静默覆盖（并列/仲裁）",
]


def build_transparency_md(mode: str = "competition", report_len: int = 0) -> str:
    """生成 AI 透明度附录（双模式）"""
    mode = mode or "competition"
    if mode == "enterprise":
        return ""  # 企业模式：不附 AI 附录（突出专业研报样式）
    lines = [
        "\n\n---\n\n# 🔧 AI 使用与透明度报告",
        "> 对齐 ICC/ESOMAR 国际准则与大赛 AI 工具使用规范：如实披露 AI 参与环节，接受人工核验。",
        "",
        "**使用的大模型**：" + _LLM_STACK,
        "",
        "**使用的工具/能力**：",
    ]
    for t in _TOOLS:
        lines.append(f"- {t}")
    lines.append("")
    lines.append("**研究方法**：")
    for m in _METHODS:
        lines.append(f"- {m}")
    lines.append("")
    lines.append(
        "**AI 参与边界（诚实声明）**：AI 负责 检索→证据核验→分析→初稿生成→自评；"
        "关键决策结论与口径需人工复核。本报告基于公开二手数据，一手问卷/访谈/实地方案已设计，"
        "真实采集待执行（见『一手验证方案』）。"
    )
    return "\n".join(lines)


def transparency_disclosure() -> dict[str, Any]:
    """透明度信息（供方案书/说明材料）"""
    out = {
        "llm_stack": _LLM_STACK,
        "tools": _TOOLS,
        "methods": _METHODS,
        "modes": ["competition（完整 AI 附录）", "enterprise（专业研报样式，不附 AI 附录）"],
    }
    # 法定标识与"AI 透明度附录"是两件事：前者两种模式都必须有，后者企业模式不附。
    # 放在同一份披露里，是为了让材料侧一眼看到两者的边界（免得说明时被问住）。
    try:
        from core.ai_label import label_summary

        out["ai_label"] = label_summary()
    except Exception:
        pass
    return out
