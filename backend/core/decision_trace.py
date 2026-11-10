"""研策 · 自主决策链——把"智能体做过的每一次取舍"变成可读、可核对的东西

## 为什么需要它（这是本模块唯一的存在理由）

我们是"超级智能体"赛道。而这条赛道上最常见的一句质疑是：

> **"你的智能体在哪里自主决策了？拓扑不是你写死的吗？"**

对此最无力的回答是"我们的架构很先进"。最有用的回答是**把每一次取舍摆出来**：
面对什么处境、有哪些选项、选了哪个、凭什么选它。

而真正的问题是：**我们的自主决策其实大量存在，只是从来没有被结构化过。**
下面这些今天就已经在发生，全部是真实的判断，全部是模型或规则在**当时**做的选择：

| 决策点 | 谁在做 | 今天长什么样 |
|---|---|---|
| 问题类型判定（因果/描述/探索） | Router | 一个字符串，进了计划对象，没进 trace |
| MECE 四分支拆解 | Router（LLM） | 一棵树，展示为"规划"而非"决策" |
| 源层级硬路由（该结论必须用什么源） | `CLAIM_ROUTES` | 规则表，从未出现在报告里 |
| 质检返工触发 | `quality_check` | 日志一行"质检未通过，返工修复(1/2)" |
| 冲突仲裁（并列 / 取舍） | `check_conflict` | 冲突日志有结果，没有过程 |
| Red Team 高风险 → 就地降级 | `deliver` | 报告里出现"（RedTeam待核）"，但没说这是**一个决定** |
| 质检 BLOCK → 人工接管 | `quality_check` | 停止卡片，没说这是**主动停止的判断** |

于是复核者看到的是一个"跑得很顺的流水线"，而不是一个"一直在做判断的智能体"。
**看不见的自主性 = 不存在的自主性。**

## 设计原则

1. **只记真决策，不记工序**。"子任务[st_0] → industry" 是工序，不是决策；
   "该结论属政策监管类，必须 L1 官方源，因此把国家统计局列为必需源" 才是决策。
2. **每个决策必须带「选项」与「依据」**。没有选项的不是决策（是流程），
   没有依据的决策不可核对（是黑箱）。这两条把"决策链"和"日志"区分开。
3. **如实记录，包括没得选的**。若某一步只有一条路可走，写"唯一可行（原因）"，
   不编造不存在的选项来撑场面——**编造的选项比没有选项更伤可信度**。
4. **不重复报告正文已有的结论**。这里记的是**过程**（怎么选的），不是**结果**（选了什么）。

## 与 trace_events 的分工

- `trace_events`：**执行轨迹**（哪个节点在跑、跑完没有）→ 驱动进度条与思考流
- `decisions`（本模块）：**取舍轨迹**（为什么这么跑）→ 驱动「🧠 决策链」面板与报告附录

两者互补，不合并：合并后前端只能二选一渲染，而它们是两种完全不同的信息。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# 决策类型 → 展示元信息（图标 + 中文名）。顺序即报告的推荐排布顺序。
KINDS: dict[str, tuple[str, str]] = {
    "classify": ("🧭", "问题定性"),
    "decompose": ("🌳", "问题拆解"),
    "route": ("🗂️", "源层级路由"),
    "select_tool": ("🧰", "工具选择"),
    "replan": ("🔄", "计划调整"),
    "recall": ("📚", "经验召回"),
    "rework": ("↩️", "返工判定"),
    "arbitrate": ("⚖️", "冲突仲裁"),
    "degrade": ("⬇️", "风险降级"),
    "stop": ("🛑", "主动停止"),
    "fallback": ("🧯", "降级兜底"),
}

# 触发方式（诚实标注这一步是"智能体自己判的"还是"规则判的"）
TRIGGERS = {
    "llm": "模型判断",
    "rule": "确定性规则",
    "hybrid": "模型提议 + 规则校验",
    "human": "人工确认",
}


def record(
    state: dict[str, Any],
    kind: str,
    situation: str,
    options: list[str],
    chosen: str,
    why: str,
    evidence: str = "",
    trigger: str = "rule",
) -> None:
    """记一次决策。失败静默（决策链不能阻断交付）。

    参数刻意做成"必须交代清楚"的形状——`options` 与 `why` 都没有默认值，
    写调用时想偷懒也偷不掉：**逼调用方把取舍讲明白，是这一层能有价值的唯一保证。**
    """
    try:
        if state is None:
            return
        opts = [str(o)[:120] for o in (options or []) if str(o).strip()]
        state.setdefault("decisions", []).append(
            {
                "kind": kind,
                "icon": KINDS.get(kind, ("•", kind))[0],
                "label": KINDS.get(kind, ("•", kind))[1],
                "situation": str(situation)[:220],
                "options": opts[:6],
                # 「唯一可行」与「多选一」在展示上完全不同：前者要说明为什么没得选，
                # 后者要说明为什么没选别的。混在一起会让读者以为所有决策都有备选。
                "chosen": str(chosen)[:180],
                "why": str(why)[:320],
                "evidence": str(evidence)[:220],
                "trigger": TRIGGERS.get(trigger, trigger),
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        )
    except Exception:
        pass


def summarize(decisions: list[dict[str, Any]] | None) -> dict[str, Any]:
    """按类型聚合（报告附录与前端角标用）"""
    ds = [d for d in (decisions or []) if isinstance(d, dict)]
    by_kind: dict[str, int] = {}
    by_trigger: dict[str, int] = {}
    for d in ds:
        k = str(d.get("kind") or "?")
        by_kind[k] = by_kind.get(k, 0) + 1
        t = str(d.get("trigger") or "?")
        by_trigger[t] = by_trigger.get(t, 0) + 1
    return {
        "total": len(ds),
        "by_kind": by_kind,
        "by_trigger": by_trigger,
        "llm_judged": by_trigger.get("模型判断", 0) + by_trigger.get("模型提议 + 规则校验", 0),
    }


def render_appendix_md(decisions: list[dict[str, Any]] | None) -> str:
    """决策链 → 报告附录（**没有决策就一个字不加**，不印空壳）"""
    ds = [d for d in (decisions or []) if isinstance(d, dict)]
    if not ds:
        return ""
    s = summarize(ds)
    lines = [
        "\n\n---\n\n# 🧠 自主决策链",
        f"本次调研过程中，智能体共做出 **{s['total']}** 次取舍"
        f"（其中 **{s['llm_judged']}** 次由模型判断、其余为确定性规则或人工）。"
        "下面把每一次都摊开——**面对什么处境、有哪些选项、选了哪个、凭什么**。",
        "",
        "> 为什么单独列这一节：一次调研里真正决定报告质量的是**取舍**（该查什么源、该不该返工、"
        "冲突该并列还是该取舍），而这些取舍在传统产品里是看不见的。"
        "**看不见的判断无法被检验，也无法被信任。**",
    ]
    # 按 KINDS 的顺序分组展示（比按时间顺序更易读：读者关心"哪类决策做了几次"）
    for kind, (_icon, label) in KINDS.items():
        group = [d for d in ds if d.get("kind") == kind]
        if not group:
            continue
        lines.append("")
        lines.append(f"### {_icon} {label}（{len(group)} 次）")
        for d in group:
            lines.append("")
            lines.append(f"- **处境**：{d.get('situation','')}")
            if d.get("options"):
                _opts = " ｜ ".join(d["options"])
                lines.append(f"  - **选项**：{_opts}")
            lines.append(f"  - **选择**：{d.get('chosen','')}")
            lines.append(f"  - **理由**：{d.get('why','')}")
            if d.get("evidence"):
                lines.append(f"  - **依据**：{d.get('evidence','')}")
            lines.append(f"  - _（{d.get('trigger','')}）_")
    lines.append("")
    lines.append(
        "> **这一节不证明什么**：它证明的是「取舍被记录且可核对」，"
        "**不证明每个取舍都是对的**。判错的取舍同样会列在上面——"
        "若某次判断事后被质检或 Red Team 推翻，那条决策仍留在链上（处置结果见对应附录）。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "自主决策链：把一次调研里每一次真实取舍（问题定性 / 拆解 / 源路由 / 工具选择 / "
        "计划调整 / 返工 / 仲裁 / 降级 / 主动停止）结构化为"
        "「处境 → 选项 → 选择 → 理由 → 依据」，进报告附录与前端决策链面板。"
        "与执行轨迹（trace）互补：trace 答「跑到哪了」，决策链答「为什么这么跑」。"
    )
