"""研策 · 自主性图谱——把「这个智能体到底自主在哪」摊开成可被审的一张表

## 为什么要有它

赛道叫「自主智能体」，而我们对拓扑的诚实描述是：**固定 15+ 节点的 LangGraph 状态机**。
当复核者问"你的智能体在哪里自主决策了？拓扑不是你写死的吗？"——最无力的回答是
"我们的架构很先进"，最好的回答是**把每个节点的自主程度、影响面与护栏逐条列出来，
让他自己看谁在决定什么**。

这件事在 2026 年有两条权威依据（见 `describe()` 里的引文）：
1. **自主性不是一个二值属性**。LangChain《What is an AI agent?》（2026-07）给出自主性光谱
   Code → LLM Call → Chain → Router → State Machine → Autonomous。
   **但我们不据此自称落在哪一档**——各档判据需逐字对照原文，而我们的结构本来就不是
   为"落在哪一档"设计的。我们只直述自己的形态：**有界自主（bounded autonomy）**，
   每步输出由模型决定，而"下一步走哪"多数由固定边与规则决定（见 render_md 内的说明）。
2. **《Measuring AI Agent Autonomy》主张：自主性可以从编排代码本身被打分**，
   评估维度是 **impact（影响）+ oversight（监督）**，不需要把 agent 跑起来。

于是本模块做三件事：把每个节点标注 `决定什么 / 谁在决定 / 影响面 / 有没有护栏 /
可不可逆`，算出它在 impact–oversight 平面上的位置，并把"可逆性"单列——
**不可逆动作是自主性真正的代价所在**。

## 三条纪律

1. **不美化**。`谁在决定` 只有三种如实选项：`模型` / `规则` / `人`。
   凡是"模型提议 + 规则裁决"的写成 `混合`，不写成"模型自主决策"。
2. **不可逆的必须点名**。整条链路里真正不可逆的只有"交付"——
   报告一旦发出就无法收回。它必须是 oversight 最高的节点。
3. **表不能漂**。`_fulltest` 断言本表恰好覆盖 `build_graph()` 的全部节点，
   漏一个就红——一张会过期的自主性表比没有表更糟。
"""
from __future__ import annotations

from typing import Any

# node → (这一步在决定什么, 谁在决定, impact 1-10, oversight 1-10, 可逆?, 护栏/依据)
# impact：这一步判错，会不会改变用户最终拍板的那句话
# oversight：交付前有没有机制能拦住它（规则硬闸 / 模型复核 / 人工介入）
NODES: dict[str, tuple[str, str, int, int, bool, str]] = {
    "router": (
        "问题定性（因果/描述/探索）+ MECE 四分支拆解 + 假设树",
        "模型", 9, 4, True,
        "产物进计划对象并全链路可见；拆解偏了会在质检与缺口披露里暴露",
    ),
    "human_confirm": (
        "澄清提问的取舍：问什么、问到什么程度才算清楚；以及是否采纳/修改研究计划",
        "人", 8, 6, True,
        # 病案（2026-09-14 二次审计）：此处原写"计划在开跑前**必须**由人确认"、监督强度给到 10。
        # 但 `node_human_confirm` 是**纯透传**（只 `_trace` 一行 + `return {"status": "confirmed"}`，
        # 无 interrupt、无门）；真正的确认发生在 API 层（`/api/confirm` 与 `ExecuteRequest.plan`），
        # 而**不确认也能直接跑通**——实测 `POST /api/execute {question}` 全程无人工介入即交付。
        # 所以这是**软门不是强制门**：监督强度从 10 降到 6，并把话说准。
        # **这张表是我们用来证明"连自己弱点都公开"的东西，它自己不准确，就什么都没了。**
        "**人在这里可以介入，但不是强制门**：可在开跑前确认/改写计划（`/api/confirm` + "
        "`ExecuteRequest.plan`），但**不带计划直接执行同样跑通**（实测无人工介入即交付）。"
        "「人」这一档目前是**可选监督**，不是审批卡点",
    ),
    "route_skill": (
        "该结论必须用什么层级的源（硬路由）+ 经验召回 + 本次打哪些源（工具选择）",
        "混合", 9, 6, True,
        "CLAIM_ROUTES 硬路由是规则、模型选漏了也强制并集；每次选择都进决策链",
    ),
    "industry": ("行业盘子与规模的取证与成章", "模型", 7, 5, True, "来源分层 + 引文核验 + 规则层自检"),
    "competition": ("竞争格局的取证与成章", "模型", 7, 5, True, "同上；跨章口径锚防自相矛盾"),
    "user": ("目标用户与需求的取证与成章", "模型", 7, 5, True, "消费者声音类不套官方源，避免口径错配"),
    "statistics": ("定量测算与指标核对", "规则", 6, 7, True, "确定性引擎（Python 计算），不交给模型算数"),
    "check_conflict": (
        "同一指标多处数值不同时：并列展示还是取舍？取谁？",
        "混合", 8, 6, True,
        "四维权重（信源40/时效20/独立15/佐证25）公开可对账；期间差异不算打架",
    ),
    "replan": (
        "发现缺口后要不要补一轮、补几个子任务",
        "混合", 7, 6, True,
        "触发靠规则（缺口有定义）、内容靠模型、**裁决靠规则**（档位上限）；补了几条进决策链",
    ),
    "summarize": ("把并行成章合并为一份报告", "模型", 8, 5, True, "合并后仍走全量质检与核验"),
    "budget_check": ("预算是否越线、要不要停下来征询", "规则", 4, 9, True, "阈值确定，无模型参与"),
    "quality_check": (
        "三层质检判定 PASS/WARN/BLOCK；BLOCK 时**主动停止并交还人工**",
        "混合", 10, 9, True,
        "规则硬闸门 + LLM 复核双轨；**主动停止是产品能力**，不是故障（见复购看板的接管率）",
    ),
    "rework_fix": ("质检不通过时返什么、返几轮", "规则", 8, 7, True, "返工次数上限 2，防死循环；每次返工留痕"),
    "decision_sop": (
        "决策判定 Go / No-Go / Validate + 验证计划 + 风险对策",
        "模型", 10, 7, True,
        "证据不足一律 Validate（规则约束）；**降级为模板时报告头必须告警**，不许静默",
    ),
    "asset_sink": (
        "哪些结论值得沉淀成行业资产、哪些必须剔除",
        "规则", 6, 8, True,
        "落库前确定性剔除 RedTeam 高/中风险 + 低信源 + 无源数（防自腐护栏）",
    ),
    "deliver": (
        "交付装配：URL 核验与坏链清洗、法定 AI 标识、审计附录、降级告警",
        "规则", 10, 8, False,
        "**全链路唯一不可逆的一步**（报告发出即无法收回）→ oversight 必须最高："
        "核验不通过就如实标注、降级就置顶告警，绝不静默",
    ),
}


def _level(trigger: str) -> str:
    return {
        "模型": "LLM 自主",
        "混合": "模型提议 + 规则裁决",
        "规则": "确定性代码",
        "人": "人类决定",
    }.get(trigger, trigger)


def entries() -> list[dict[str, Any]]:
    """按 impact 降序的节点清单（最该被盯的排最前）。"""
    out = []
    for name, (what, who, impact, oversight, rev, guard) in NODES.items():
        out.append(
            {
                "node": name,
                "decides": what,
                "who": who,
                "who_label": _level(who),
                "impact": impact,
                "oversight": oversight,
                "reversible": rev,
                "guard": guard,
            }
        )
    out.sort(key=lambda x: (-x["impact"], -x["oversight"]))
    return out


def summary() -> dict[str, Any]:
    """自主性画像：谁在决定、不可逆动作有几个、护栏覆盖率。"""
    es = entries()
    n = len(es) or 1
    by_who: dict[str, int] = {}
    for e in es:
        by_who[e["who"]] = by_who.get(e["who"], 0) + 1
    covered = sum(1 for e in es if e["oversight"] >= 6)
    irrev = [e["node"] for e in es if not e["reversible"]]
    # 高影响 + 低监督 = 真正的风险位（这张表存在的意义就是让它们无处可藏）
    risky = [e["node"] for e in es if e["impact"] >= 8 and e["oversight"] < 6]
    return {
        "nodes": len(es),
        "by_who": by_who,
        "llm_share": round(by_who.get("模型", 0) / n, 3),
        "hybrid_share": round(by_who.get("混合", 0) / n, 3),
        "rule_share": round((by_who.get("规则", 0) + by_who.get("人", 0)) / n, 3),
        "guard_coverage": round(covered / n, 3),
        "irreversible": irrev,
        "high_impact_low_oversight": risky,
        # 病案（2026-09-14 二次审计）：此处原写 "State Machine（LLM 决定每步输出、图决定下一步走哪…）"
        # 并用 LangChain 光谱第五档为它背书。但**那句"定义"是我们自己的转述、不是来源原话**；
        # 独立核验员读原文后指出：第五档的判据里"下一步走哪"由**模型**决定（开发者代码只决定
        # "有哪些步可选"）——与我们转述的正好相反。若属实，按该光谱我们根本不落在第五档。
        # 网络检索额度耗尽，**此条未能独立复核**——所以采取在两种读法下都安全的写法：
        # **直述我们自己的结构，不锚定档位自称**。宁可少一个标签，也不留一句可能被当场推翻的引文。
        "self_level": "有界自主（bounded autonomy）——每步输出由模型决定；下一步走哪多数由固定边与规则决定、"
        "少数由模型在预定义分支内选择；开发者决定有哪些步",
    }


def render_md() -> str:
    """自主性图谱 → markdown（进方案书/说明稿/报告附录）。"""
    es = entries()
    s = summary()
    lines = [
        "# 🧭 自主性图谱（每个节点：谁在决定 / 影响多大 / 有没有护栏）",
        "",
        f"本系统共 **{s['nodes']}** 个编排节点。按**自主等级**分布："
        f"模型自主 {s['by_who'].get('模型', 0)} · 模型提议+规则裁决 {s['by_who'].get('混合', 0)} · "
        f"确定性规则 {s['by_who'].get('规则', 0)} · 人类决定 {s['by_who'].get('人', 0)}。"
        f"**护栏覆盖率 {s['guard_coverage'] * 100:.0f}%**"
        f"（监督强度 ≥6 的节点占比）。",
        "",
        "> 本系统自陈：**" + s["self_level"] + "**。",
        "> 依据（**不锚定档位**）：LangChain《What is an AI agent?》（2026-07）给出自主性光谱"
        " Code → LLM Call → Chain → Router → State Machine → Autonomous；"
        "**我们不据此自称落在哪一档**——各档判据需逐字对照原文，且我们的结构本就不是为「落在哪一档」设计的。",
        "> 真正支撑本设计的是：MAP《Measuring Agents in Production》（arXiv:2512.04123，ICML Oral）——"
        "**80% 生产案例用结构化工作流、68% 的 agent 在人工介入前 ≤10 步**；以及 Anthropic / Google / "
        "Microsoft 三家 2026 年官方架构指南的共同主张：**能用确定性流水线，就不要让模型编排流程**。",
        "",
        "| 节点 | 这一步在决定什么 | 谁在决定 | 影响 | 监督 | 可逆 | 护栏 / 依据 |",
        "|---|---|---|---:|---:|:--:|---|",
    ]
    for e in es:
        lines.append(
            f"| `{e['node']}` | {e['decides']} | {e['who_label']} | {e['impact']} | "
            f"{e['oversight']} | {'是' if e['reversible'] else '**否**'} | {e['guard']} |"
        )
    lines += [
        "",
        f"- **不可逆动作**：{('、'.join('`' + x + '`' for x in s['irreversible'])) or '无'}"
        "——不可逆是自主性真正的代价所在，因此它们的监督强度必须最高。",
        f"- **高影响 × 低监督**：{('、'.join('`' + x + '`' for x in s['high_impact_low_oversight'])) or '无'}"
        "（影响 ≥8 且监督 <6 的节点；为空表示没有「判错了也没人拦」的位置）。",
        "",
        "> **这张表证明什么**：它证明「自主」在本系统里**不是一个形容词，而是一组可核对的标注**——"
        "每个节点谁在决定、判错的影响面、以及交付前谁能拦住它。",
        "> **它不证明什么**：① impact/oversight 的取值是**自评**，不是第三方测得的；"
        "② 覆盖率不等于正确率，护栏存在也不保证护栏判对；"
        "③ 它描述的是**编排代码**，不含模型自身的内部行为。",
    ]
    return "\n".join(lines)


def describe() -> str:
    return (
        "自主性图谱：把编排层每个节点标注「决定什么 / 谁在决定（模型·混合·规则·人）/ "
        "影响面 / 监督强度 / 可逆性」，算出护栏覆盖率与不可逆动作清单，"
        "按 impact–oversight 二维给每个节点自评自主性；**不锚定光谱档位自称**。"
        "依据 LangChain 2026-07 的六档自主性光谱与《Measuring AI Agent Autonomy》"
        "的「自主性可从编排代码打分」方法论。"
    )
