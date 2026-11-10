"""研策 · 核心数据模型

定义整个系统共享的类型：研究计划、子任务、证据、信源分级、报告等。
这些模型贯穿 LangGraph 状态机、MCP 数据源、报告生成、前端展示。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ============================================================
# 信源分级（L1-L5）—— 证据链核心
# ============================================================
class SourceLevel(str, Enum):
    """信源等级：官方 / 半官方 / 行业 / 用户 / 推测"""

    L1_OFFICIAL = "L1_OFFICIAL"      # 官方：政府/统计局/官方公告
    L2_SEMI_OFFICIAL = "L2_SEMI_OFFICIAL"  # 半官方：企业官网/权威媒体
    L3_INDUSTRY = "L3_INDUSTRY"      # 行业：行业报告/专业机构
    L4_USER = "L4_USER"              # 用户：社媒/评价/论坛
    L5_SPECULATION = "L5_SPECULATION"  # 推测：模型推断，必须标注


class Source(BaseModel):
    """一条证据来源（可溯源）"""

    title: str = Field(description="来源标题")
    url: str = Field(default="", description="来源链接（空=无链接，如内部文档）")
    level: SourceLevel = Field(description="信源等级 L1-L5")
    publisher: str = Field(default="", description="发布方（如'国家统计局'）")
    publish_date: str = Field(default="", description="发布日期 YYYY-MM-DD")
    excerpt: str = Field(default="", description="原文摘录（溯源弹层展示）")
    retrieved_at: str = Field(default="", description="检索时间")

    # --- 来源血缘（治"把转述当独立佐证"）---
    # 这一页里的数字**真正的出处**（"据艾瑞咨询" → 艾瑞咨询）。空 = 没写明出处。
    origin: str = Field(default="", description="数字的原始出处（显式归属才填，不推断）")
    # 非空表示这一页是**转述**（它引用了别人的数字），值是那个出处的名字。
    # 三条媒体转述同一家机构 = 一个源头 + 三条回声，不能算三个独立佐证。
    via: str = Field(default="", description="转述自谁（空=自己就是一手出处）")

    # --- 可解释证据评分维度（评审命门1：做深的技术点）---
    # 4 维评分，用于证据冲突仲裁
    timeliness_score: float = Field(
        default=0.5, ge=0, le=1, description="时效性分（越新越高）"
    )
    independence_score: float = Field(
        default=0.5, ge=0, le=1, description="独立性分（是否独立来源）"
    )
    corroboration_count: int = Field(
        default=0, description="多源佐证数（其他独立来源佐证此结论的次数）"
    )

    @property
    def credibility_score(self) -> float:
        """证据可信度合成分（可解释证据仲裁模型的核心）"""
        return evidence_credibility_score(self)


class Evidence(BaseModel):
    """一条证据（结论 + 来源 + 信源分级）"""

    claim: str = Field(description="结论/论断")
    query: str = Field(
        default="",
        description="本条证据来自哪次联网检索（检索词）；供前端「来自检索」展示与工具调用取证（环节3）",
    )
    sources: list[Source] = Field(default_factory=list, description="支撑来源")
    is_speculation: bool = Field(
        default=False, description="是否推测（L5），推测必须明确标注"
    )
    is_conflicting: bool = Field(
        default=False, description="是否存在冲突（多来源数据打架）"
    )
    conflict_detail: str = Field(default="", description="冲突说明（并列展示/仲裁）")
    arbitration: dict[str, Any] | None = Field(
        default=None, description="仲裁结果（可解释仲裁卡片数据）"
    )
    # --- 结论置信度分级（P0 可信度护城河）---
    confidence: str = Field(
        default="", description="置信度标签：高/中/低（供前端证据卡与披露附录展示）"
    )
    confidence_level: str = Field(
        default="", description="置信度机器码：high/medium/low（供样式）"
    )
    confidence_reasons: list[str] = Field(
        default_factory=list, description="置信度核心理由（可核对，非黑箱）"
    )


# ============================================================
# 可解释证据仲裁模型（评审命门1 · 做深的技术点）
# ============================================================
# 权重表：公开可查、可解释 —— 不是黑箱
CREDIBILITY_WEIGHTS: dict[str, float] = {
    "source_level": 0.40,   # 信源等级 40%
    "timeliness": 0.20,     # 时效性 20%
    "independence": 0.15,   # 独立性 15%
    "corroboration": 0.25,  # 多源佐证 25%
}

# 信源等级基准分（L1 最高 → L5 最低）
SOURCE_LEVEL_BASE_SCORE: dict[SourceLevel, float] = {
    SourceLevel.L1_OFFICIAL: 1.0,
    SourceLevel.L2_SEMI_OFFICIAL: 0.8,
    SourceLevel.L3_INDUSTRY: 0.65,
    SourceLevel.L4_USER: 0.4,
    SourceLevel.L5_SPECULATION: 0.15,
}


def evidence_credibility_score(source: Source) -> float:
    """证据可信度合成分 = Σ(维度分 × 权重)

    权重与基准分全部公开可查，仲裁时 UI 弹出"为什么采信A不信B"。
    这是回应院士追问"这不就是加权打分吗？权重怎么定的？"的关键——
    权重可解释、过程可验证。
    """
    level_score = SOURCE_LEVEL_BASE_SCORE[source.level]
    # 佐证分：0 个佐证 = 0.3，1 个 = 0.6，2+ 个 = 1.0
    corr_score = min(source.corroboration_count / 2.0, 1.0) * 0.7 + 0.3

    score = (
        CREDIBILITY_WEIGHTS["source_level"] * level_score
        + CREDIBILITY_WEIGHTS["timeliness"] * source.timeliness_score
        + CREDIBILITY_WEIGHTS["independence"] * source.independence_score
        + CREDIBILITY_WEIGHTS["corroboration"] * corr_score
    )
    return round(max(0.0, min(1.0, score)), 3)


def arbitrate_conflict(
    evidence_a: Evidence,
    evidence_b: Evidence,
) -> dict[str, Any]:
    """冲突仲裁：比较两个证据的可信度，产出可解释的仲裁卡片数据。

    仲裁卡片 UI 展示：
    - A、B 各自的 4 维得分 + 权重
    - 合成分对比
    - 采纳结论 + 理由
    让"为什么采信A不信B"一目了然、可核对。
    """
    _sa = list(evidence_a.sources or [])
    _sb = list(evidence_b.sources or [])
    score_a = max((s.credibility_score for s in _sa), default=0)
    score_b = max((s.credibility_score for s in _sb), default=0)
    # **4 维拆解必须取自"贡献了合成分的那个来源"**（2026-09-16 全量审计）。
    # 病案：合成分取 `max(所有来源)`，而拆解固定取 `sources[0]`——
    # 证据 A 的来源是 [L2媒体, L1官方] 时，分数按 L1 算（≈1.0），
    # 拆解却印 L2 的四个维度（加权和 ≈0.405）。**这张卡的全部卖点是"可核对"**：
    # 读者拿拆解乘权重，得到的数与合成分对不上——**当场自证不可核对**。
    _best_a = max(_sa, key=lambda s: s.credibility_score) if _sa else None
    _best_b = max(_sb, key=lambda s: s.credibility_score) if _sb else None

    # 若任一证据有 L1 官方来源且分差显著，倾向采信；否则并列展示
    has_official_a = any(s.level == SourceLevel.L1_OFFICIAL for s in _sa)
    has_official_b = any(s.level == SourceLevel.L1_OFFICIAL for s in _sb)

    _rule = ""
    if abs(score_a - score_b) >= 0.15:
        winner = "A" if score_a > score_b else "B"
        verdict = evidence_a.claim if winner == "A" else evidence_b.claim
        decision = "adopt_winner"  # 采信高分者
        _rule = f"可信度分差距 {abs(score_a - score_b):.2f} ≥ 0.15"
    elif has_official_a and not has_official_b:
        winner, verdict, decision = "A", evidence_a.claim, "adopt_winner"
        _rule = "分差不足 0.15，但**证据A含 L1 官方来源**而证据B没有"
    elif has_official_b and not has_official_a:
        winner, verdict, decision = "B", evidence_b.claim, "adopt_winner"
        _rule = "分差不足 0.15，但**证据B含 L1 官方来源**而证据A没有"
    else:
        winner, verdict, decision = "", "", "show_both"  # 并列展示，不替用户下结论
        _rule = "两证据分差<0.15 且均无 L1 官方来源"

    def _breakdown(src):
        if src is None:
            return {"source_level": 0, "timeliness": 0, "independence": 0, "corroboration": 0}
        return {
            "source_level": SOURCE_LEVEL_BASE_SCORE[src.level],
            "timeliness": src.timeliness_score,
            "independence": src.independence_score,
            "corroboration": min(src.corroboration_count / 2.0, 1.0) * 0.7 + 0.3,
        }

    return {
        "decision": decision,          # adopt_winner | show_both
        "winner": winner,
        "adopted_claim": verdict,
        "score_a": score_a,
        "score_b": score_b,
        "weights": CREDIBILITY_WEIGHTS,
        # 拆解的三个来源标注：读者据此知道"这张拆解对应的是哪一条来源"，
        # 也才可能自己乘一遍权重验算（**合成分 = Σ(拆解×权重)**，这是可核对的定义）
        "breakdown_a": _breakdown(_best_a),
        "breakdown_b": _breakdown(_best_b),
        "breakdown_a_source": (_best_a.title or "")[:80] if _best_a else "",
        "breakdown_b_source": (_best_b.title or "")[:80] if _best_b else "",
        "reason": (
            f"采信{'证据A' if winner == 'A' else '证据B'}：{_rule}"
            if winner
            else f"{_rule} → 并列展示，不替用户下结论"
        ),
    }


# ============================================================
# 研究计划 / 子任务
# ============================================================
class DepthLevel(str, Enum):
    """四档调研深度"""

    FREE = "free"               # 免费 ¥0：市场概览
    STANDARD = "standard"       # 标准 ¥39：市场全景
    DEEP = "deep"               # 深入 ¥99：+竞争 +用户
    CUSTOM = "custom"           # 定制 ¥299：+决策SOP +私有数据


class SubTask(BaseModel):
    """一个子任务（由 Router 主 Agent 拆解）"""

    id: str
    agent_type: str = Field(description="负责的子Agent类型：industry/competition/user/statistics")
    description: str = Field(description="子任务描述")
    search_queries: list[str] = Field(default_factory=list, description="检索词")
    # **章节归属：这条子任务属于哪个调研维度。**（2026-09-19 补）
    #
    # 病案：在这之前，章节归属是 `graph._chapter_key` **从描述文本里用 `『』` 猜**的。
    # 模板兜底计划恰好每条描述都写了 `调研『维度名』`，于是猜得中；而 LLM 自主拆解
    # 写的是自由文本（"检索三四线城市咖啡消费相关的政策支持…"），猜不中 →
    # 基础轮自成一章、深入轮又按维度名出一章 → **同一维度两章、同一指标两套数**
    # （本项目自称最严重的历史缺陷）。
    #
    # 现在归属是**结构化字段**，由规划层显式写死：模板兜底直接取模板的维度名，
    # LLM 路径由 `_resolve_subtask_dimensions` 逐条核定（核定不了就整份退回模板）。
    # `_chapter_key` 优先用它，描述解析只作为读老存档时的兜底。
    dimension: str = Field(default="", description="该子任务所属的调研维度（章节归属键）")
    depends_on: list[str] = Field(default_factory=list, description="依赖的子任务id")
    budget_estimate: float = Field(default=0, description="预估token成本(元)")
    status: str = Field(default="pending", description="pending/running/done/failed")


class ResearchPlan(BaseModel):
    """研究计划（需求澄清 + Router 生成）"""

    id: str = ""
    raw_question: str = Field(description="用户原始提问")
    clarified_context: dict[str, str] = Field(
        default_factory=dict, description="需求澄清结果（如 目标=进入市场/经营诊断, 预算=10万内）"
    )
    dimensions: list[str] = Field(
        default_factory=list, description="调研维度（如 市场全景/竞争格局/消费人群/开店成本/进入策略）"
    )
    depth: DepthLevel = DepthLevel.STANDARD
    price_estimate: float = Field(default=0, description="预估消耗（单位=档位积分，与 core/billing.MODE_POINTS 同源；非人民币）")
    subtasks: list[SubTask] = Field(default_factory=list)
    status: str = Field(default="draft", description="draft/confirmed/running/done")
    # MECE 问题树（P1 咨询级自主规划）：[{branch, icon, question, subtask_ids}]
    # 需求侧/供给侧/资源侧/财务侧 四分支，互相独立、合并穷尽 → 计划有方法论结构
    problem_tree: list[dict[str, Any]] = Field(
        default_factory=list, description="MECE 问题树分支（每个子任务归属一个分支）"
    )
    # 研究问题类型（② 假设驱动按问题类型分流，不全报告强制 H1-H3）
    # exploratory 议题树 / descriptive 统计描述+TAM / causal 假设检验+因果方法
    question_type: str = Field(
        default="exploratory", description="研究问题类型：exploratory/descriptive/causal"
    )
    # 仅 causal 模式生成研究假设（H1-H3），报告内"假设验证"章节逐条支持/反证
    hypotheses: list[str] = Field(default_factory=list, description="因果类问题的研究假设")
    # 计划**实际**由谁产出：`llm` = 模型自主拆解；`fallback` = 规则模板兜底。
    #
    # 2026-09-18 审计补。此前没有任何字段记录这件事，于是无论走哪条路，
    # 自主性图谱都印「拆解由模型生成而非套模板」+ `_（模型判断）_`——
    # 而实测 LLM 分支因提示词花括号未转义**每次都抛异常**，所有计划都来自模板。
    # **交付物在替一个从未运行过的能力背书**，印在专门证明自主性的那一节里。
    # 现在：写入点记真相，展示点按真相印（公理六·降级必须告警）。
    plan_source: str = Field(
        default="", description="llm/fallback——计划实际由模型生成还是规则模板兜底；空=未标注"
    )
