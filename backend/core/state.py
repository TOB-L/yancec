"""研策 · LangGraph 状态定义

LangGraph 状态机用 TypedDict 定义整个调研流程的共享状态。
每个节点读写这个状态，形成"自主规划→执行→质检→交付"的闭环。

关键节点（对应 v3 方案 §3.3）：
- human_confirm：研究计划人类确认
- route_skill：按任务路由到 Skill
- 并行子 Agent：行业扫描/竞争格局/用户洞察/数据统计
- check_conflict：证据冲突检测 + 仲裁
- quality_check：质检 + 返工（限2次）
- budget_check：预算控制（超80%征询）
- checkpoint：长任务暂停恢复
"""

from __future__ import annotations

import operator

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from core.models import Evidence, ResearchPlan, Source


def _merge_lists(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """LangGraph 并行合并：多个节点同时写同一 list 键时，拼接而非覆盖"""
    return (left or []) + (right or [])


def _merge_dict(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """并行合并 dict（如 subtask_results）"""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _merge_or(left: bool | None, right: bool | None) -> bool:
    """并行合并 bool：任一路为真即为真。

    病案（v0.9.75 实跑）：`asset_reused` 由 4 个并行子 Agent 节点各自判定
    （每个子任务都可能命中行业资产），直接返回裸 bool 会被 LangGraph 拒收——
    **"At key 'asset_reused': Can receive only one value per step"**。
    语义上"命中过即算命中"正是「或」，所以用 or 合并而不是随便挑一个。
    """
    return bool(left) or bool(right)


class TaskState(TypedDict):
    """整个调研任务的状态"""

    # --- 用户输入 ---
    raw_question: str                    # 原始提问
    clarified_context: dict[str, str]    # 需求澄清结果
    plan: ResearchPlan                   # 研究计划
    plan_override: ResearchPlan | None   # 用户编辑后的计划（有则直接用，跳过重新生成）

    # --- 执行阶段 ---
    subtask_results: Annotated[dict[str, Any], _merge_dict]  # 各子任务结果
    evidences: Annotated[list[Evidence], _merge_lists]       # 证据（并行子Agent合并）
    trace_events: Annotated[list[dict[str, Any]], _merge_lists]  # 执行轨迹
    conflict_results: Annotated[list[dict[str, Any]], _merge_lists]  # 仲裁卡片数据
    # 冲突检测的**分母**（trunk_matched / pairs_compared / criterion）。
    # 报告顶部那行「N 条证据 · M 处仲裁」在 M=0 时缺了它就没法读——
    # 读者分不清"查过没有"和"根本没查"。**必须是 TaskState 的真键**：
    # 只写在节点返回 dict 里会被 LangGraph 静默丢弃（`decision_chain` 踩过这个坑），
    # 于是同步路径拿到空、SSE 路径侥幸正常——同一个函数两条路径两种真相。
    conflict_scan: dict[str, Any]
    search_debug: Annotated[list[tuple[str, str, int]], _merge_lists]  # 检索诊断（子任务id/检索词/证据数），前端trace可见
    source_trace: Annotated[list[dict[str, Any]], _merge_lists]  # 本次调用的数据源台账（哪个源回了几条/哪些回空），进交付附录
    # 自主决策链：每一次**真实取舍**（问题定性/拆解/源路由/工具选择/计划调整/返工/仲裁/降级/主动停止）
    # 结构化为「处境 → 选项 → 选择 → 理由 → 依据」，进报告附录与前端决策链面板。
    # 与 trace_events 的分工：trace 答「跑到哪了」（驱动进度），decisions 答「为什么这么跑」（驱动信任）。
    decisions: Annotated[list[dict[str, Any]], _merge_lists]

    # --- 质检阶段 ---
    report_draft: str                    # 报告初稿
    quality_report: dict[str, Any] | None  # 质检结果
    rework_count: int                    # 返工次数（限2次）
    quality_passed: bool                 # 是否通过质检
    stop_card: dict[str, Any] | None     # 人工接管卡片（BLOCK时生成，供前端展示）

    # --- 预算控制 ---
    budget_estimate: float               # 预估成本
    # 已花费。**必须带 add reducer**（2026-09-14 审计）：
    # 原先是裸 float，而 `_run_sub_agent` 只对 `state[...]` 做原地赋值、从不随节点返回
    # → LangGraph 永远不提交这个写 → `node_budget_check` 读到的恒为 0.0
    # → "预算超 80% 暂停征询"这条产品能力**从未触发过一次**，
    #   SSE 的 budget 事件永不推送、落进账本的成本永远 0.0（上线演示上会显示"预计新增 0.0 元"）。
    # 加 reducer 后各节点返回**增量**，同超级步的多个节点相加。
    budget_spent: Annotated[float, operator.add]
    budget_flag: bool                    # 是否触发80%预警

    # --- 交付 ---
    report_final: str                    # 最终报告（专业研报=干净版，无参赛痕迹）
    competition_report: str              # 参赛对照版（目标大赛五维达标自检·无得分 + AI透明度，默认不展示）
    citations: Annotated[list[dict[str, Any]], _merge_lists]  # 报告引用清单（角标弹层数据源）
    decision_sop: str                    # 决策清单（决策型交付）
    validation_tasks: list[dict[str, Any]]  # 结构化验证任务（Action Layer）
    monitor_conditions: list[str]        # 持续监控触发条件（Market Monitor）
    decision_verdict: str                # Go / No-Go / Validate（决策记忆）
    # 决策完整性闸门的结论（v0.9.82 新增，2026-09-16 补进 schema）。
    # **病案：这个键在返回 dict 里活了整整一批，却不在 schema 里**——
    # `node_deliver` 一直返回 `{"integrity_gate": {...}}`，而 LangGraph 丢弃 schema 外的键。
    # 于是又是同一道分叉：
    #   · SSE（/api/execute-stream）取流式 updates → 侥幸正常，前端角标能显示降级；
    #   · 同步（/api/execute、run_task、_fulltest）走 graph.invoke 最终状态 → **丢失**，
    #     报告存档里只能存到空 dict，"这份报告有没有被闸门拦过、拦的是哪几条"无法判定。
    # **下面那段注释（decision_chain 病案）已经把这个坑写清楚了，而它照样复发了一次。**
    # 所以纪律再收紧一层：**新增 `node_deliver` 返回键时，同一次提交里必须改本文件。**
    integrity_gate: dict[str, Any]
    # 按合规姿态**没去敲门**的地址（≠ 死链）。2026-09-16 与 `integrity_gate` 同批发现
    # 也不在 schema 里——同一个坑里的第二个键。
    # 消费方：`app.py:1527`（API 响应）与 `graph.py:3762` 都读 `result["unprobed_urls"]`，
    # 而两处都写了 `.get(..., [])` 兜底 —— 于是**丢键不会报错，只会静默变成"0 个未探测"**，
    # 而报告正文里那一节列着真实条数。**正文说 7 条、接口说 0 条**，正是本仓库反复在治的那种不一致。
    # 兜底默认值在这里起了反作用：它把"丢键"伪装成了"值为空"。
    unprobed_urls: list[str]
    metrics: list[dict[str, Any]]        # 结构化核心指标（交付物头部/作品页用，从指标表解析）
    # ---- 交付节点产出、必须**随结果返回给调用方**的键 ----
    # 病案（2026-09-14 实跑）：这些键原先只写在 `node_deliver` 的 return dict 里，
    # **而 LangGraph 会丢弃节点返回的、不在本 schema 里的键**。后果按路径分叉：
    #   · SSE（/api/execute-stream）直接取流式 updates → 侥幸正常；
    #   · 同步（/api/execute、run_task、_fulltest）走 graph.invoke 的最终状态 → **全部丢失**。
    # 实测证据：同一份报告，附录里"自主决策链"渲染出 2 条，而 result["decision_chain"] 是 0 条；
    # 前几轮 `citation_report` 也一直是 None（当时误判为"_result_json 没透传"，其实是更上游被丢了）。
    # 所以：**凡是调用方要读的交付产物，都必须是 schema 里的真键**，不能靠"返回个额外键"。
    # 这些键**不加 reducer**（单一写者 = 交付节点，末次写覆盖）——
    # 加了 add/merge reducer 反而会像 source_trace 那样把同一份内容复制 N 倍。
    decision_chain: list[dict[str, Any]]  # 自主决策链（交付用快照；累积日志见 decisions）
    bad_urls: list[str]                  # 本次核验出的坏 URL（前端不渲染可点链接）
    url_fixes: dict[str, str]            # 失效页 → 官网根域回退映射
    citation_report: dict[str, Any] | None  # 引文核验报告（链接层通过率等）
    entailment: dict[str, Any]
    citation_pass_rate: float
    # 交付降级标记（v0.9.75）：决策清单若因任何原因走了兜底模板，这里记下原因，
    # 由交付节点在**报告头**显式告警。理由见 agents/decision.py 顶部病案：
    # 兜底此前是静默的，报告其余部分正常、只有"决策清单"变成空壳，无人会发现。
    sop_fallback: dict[str, str]
    # 资产复用（v0.9.75）：本次调研是否命中已沉淀的行业卡/研究经验。
    # 「有效复用率」的分子——没有这一位，资产飞轮就只是叙事，拿不出复用次数的账。
    # 带 `_merge_or`：4 个并行取证节点各自都会判一次（任一子任务命中即算命中）。
    asset_reused: Annotated[bool, _merge_or]
    deliverables: Annotated[list[dict[str, Any]], _merge_lists]  # 交付物
    status: str                          # draft/confirmed/running/review/done/failed

    # --- 人机协同 ---
    messages: Annotated[list, add_messages]  # 与用户的对话


def init_state(
    raw_question: str,
    clarified_context: dict[str, str] | None = None,
    plan_override: ResearchPlan | None = None,
) -> TaskState:
    """初始化一个空任务状态"""
    return {
        "raw_question": raw_question,
        "clarified_context": clarified_context or {},
        "plan": plan_override or ResearchPlan(raw_question=raw_question, clarified_context=clarified_context or {}),
        "plan_override": plan_override,
        "subtask_results": {},
        "evidences": [],
        "trace_events": [],
        "conflict_results": [],
        "conflict_scan": {},
        "search_debug": [],
        "source_trace": [],
        "decisions": [],
        "report_draft": "",
        "quality_report": None,
        "rework_count": 0,
        "quality_passed": False,
        "stop_card": None,
        "budget_estimate": 0.0,
        "budget_spent": 0.0,
        "budget_flag": False,
        "report_final": "",
        "competition_report": "",
        "citations": [],
        "decision_sop": "",
        "validation_tasks": [],
        "monitor_conditions": [],
        "decision_verdict": "",
        "integrity_gate": {},
        "unprobed_urls": [],
        "metrics": [],
        "decision_chain": [],
        "bad_urls": [],
        "url_fixes": {},
        "citation_report": None,
        "entailment": {},
        "citation_pass_rate": 0.0,
        "sop_fallback": {},
        "asset_reused": False,
        "deliverables": [],
        "status": "draft",
        "messages": [],
    }
