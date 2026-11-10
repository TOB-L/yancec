"""研策 · LangGraph 状态机（核心编排）

赛题能力：自主规划 + 工具调用 + 结果交付 + 人机协同。
v3 方案 §3.3 的关键节点全部实现。

状态流转：
  router → [human_confirm] → route_skill → 并行子Agent → check_conflict
         → 汇总 → [budget_check] → quality_check → 资产沉淀 → 交付

返工控制：check_conflict / quality_check 各限 2 次，防死循环烧预算。
可观测性：每个节点写 trace_events，Trace 面板实时渲染。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from langgraph.graph import END, StateGraph

# LangGraph Checkpoint：支持"暂停后继续"（用户打断长任务后再恢复，从断点续跑而非从头）
# MemorySaver = 进程内存储（演示够用；后端重启即丢）。import 失败则降级为"不可恢复"。
try:
    from langgraph.checkpoint.memory import MemorySaver

    _CHECKPOINTER = MemorySaver()
except Exception:  # pragma: no cover - langgraph 旧版本无 checkpoint 模块
    _CHECKPOINTER = None

from core.models import Evidence, ResearchPlan, SourceLevel
from core.state import TaskState, init_state

logger = logging.getLogger(__name__)

MAX_REWORK = 2  # 返工上限（防死循环）


def _engine_version() -> str:
    """引擎版本（与 app.py /health 同源，见 core/version.py 病案）"""
    try:
        from core.version import ENGINE_VERSION

        return ENGINE_VERSION
    except Exception:
        return "见 /health"


# ============================================================
# 工具函数
# ============================================================
def _trace(state: TaskState, node: str, detail: str) -> None:
    """写入执行轨迹（Trace 面板数据源）"""
    state["trace_events"].append(
        {
            "node": node,
            "detail": detail,
            "ts": datetime.now().isoformat(),
            "status": "running",
        }
    )


def _finalize_event(state: TaskState, node: str, ok: bool = True) -> None:
    """标记节点事件完成"""
    for ev in reversed(state["trace_events"]):
        if ev["node"] == node and ev["status"] == "running":
            ev["status"] = "done" if ok else "failed"
            break


def _risk_high(risk: Any) -> bool:
    """RedTeam 风险等级归一：'高/高风险/高危/high…' 都判为高（防 LLM 标签漂移使置顶红条静默失效）"""
    r = str(risk or "").strip().lower()
    return r.startswith("高") or r.startswith("high")


def _risk_med(risk: Any) -> bool:
    """RedTeam 风险等级归一：'中/中风险/medium…' 判为中（供行业卡护栏等下游按标注拦截）"""
    r = str(risk or "").strip().lower()
    return r.startswith("中") or r.startswith("med")


# ============================================================
# 节点 1：Router 主 Agent（意图解析 + 计划生成）
# ============================================================
def node_router(state: TaskState) -> dict[str, Any]:
    """Router 主 Agent（意图解析 + 计划生成）

    若用户在前端编辑过研究计划（plan_override），直接采用用户计划，
    不再重新生成——"人在回路"的体现：用户可以改子任务再执行。
    """
    from core.decision_trace import record as _dec

    if state.get("plan_override"):
        plan = state["plan_override"]
        plan.status = "confirmed"
        _trace(state, "router", f"采用用户编辑后的计划（{len(plan.subtasks)} 子任务）")
        _dec(
            state,
            "decompose",
            "用户在前端编辑过研究计划——是否覆盖 Router 自己生成的计划？",
            ["按 Router 生成的计划执行", "按用户编辑后的计划执行"],
            "按用户编辑后的计划执行",
            "人在回路优先：计划是用户对研究范围的直接表达，"
            "覆盖它会让我们研究一个用户并不想要的问题——**研究方向的最终决定权在人**。",
            evidence=f"用户计划含 {len(plan.subtasks)} 个子任务",
            trigger="human",
        )
    else:
        from agents.router import confirm_plan

        _trace(state, "router", f"解析提问: {state['raw_question']}")
        plan = confirm_plan(state["raw_question"], state["clarified_context"])
        plan.status = "confirmed"
        # —— 决策①：问题定性（决定后面走哪条分析路径）——
        _QT = {
            "causal": ("因果类", "触发假设驱动（H1-H3 支持/反证证据表）——回答『为什么』"),
            "descriptive": ("描述类", "走事实盘点路径（规模/格局/结构），不做因果断言"),
            "exploratory": ("探索类", "走机会扫描路径（MECE 四分支发散），不预设结论方向"),
        }
        _qt = str(getattr(plan, "question_type", "") or "")
        _qt_cn, _qt_act = _QT.get(_qt, (_qt or "未分类", "未匹配到已知类型 → 走通用路径"))
        _dec(
            state,
            "classify",
            f"提问「{state['raw_question'][:60]}」属于哪一类研究问题？",
            ["因果类（为什么/是否有效）", "描述类（多大/什么格局）", "探索类（有什么机会）"],
            f"{_qt_cn} → {_qt_act}",
            "问题类型决定分析路径与证据要求，判错的代价是**用描述性数据回答因果问题**"
            "（相关当因果，是本行业最常见的硬伤）。所以这一步先定性，再拆解。",
            evidence=f"question_type={_qt or '（未标注）'}；计划维度={len(getattr(plan, 'dimensions', []) or [])} 项",
            # `question_type` 全程由 `agents/router.py::_detect_question_type` 的关键词表判定
            # （`any(w in q for w in _CAUSAL_WORDS)`），没有任何模型参与。
            # 此前标 `llm` → 报告里印「_（模型判断）_」，且被 `decision_trace` 计入
            # 「其中 N 次由模型判断」——**自主性图谱的模型判断次数被这条虚增**。
            trigger="rule",
        )
        # —— 决策②：MECE 拆解（决定研究边界）——
        _tree = getattr(plan, "problem_tree", []) or []
        _branches = [str(b.get("branch") or "") for b in _tree if isinstance(b, dict)]
        if _branches:
            # 计划的**实际**来源：`llm` = 模型自主拆解；其余（含空）= 规则模板兜底。
            #
            # 2026-09-18 审计病案：此前这段理由串**写死**「拆解由模型生成而非套模板」并标
            # `trigger="llm"`——而实测 LLM 分支因提示词花括号未转义**每次都抛异常**，
            # 所有计划都来自 `_SKILL_TEMPLATES`。于是交付物**在替一个从未运行过的能力背书**，
            # 而且印在专门用来证明自主性的那一节里（《🌳 问题拆解》）。
            # 现在：写入点记真相（`plan.plan_source`），展示点按真相印（公理六）。
            _psrc = str(getattr(plan, "plan_source", "") or "")
            _by_model = _psrc == "llm"
            _dec(
                state,
                "decompose",
                "把这个问题拆成哪几个互斥穷尽的子问题？",
                _branches,
                "全部纳入，拆成 " + "、".join(_branches) + f" 共 {len(_branches)} 个分支",
                # 「四类」改成与分支数同源：分支表可由技能骨架扩展，
                # 写死"四类"会在只有三支时自相矛盾（本报告实测过 3 支配"这四类"）。
                "这几类是市场研究的**互斥穷尽**切法（需求/供给/资源/财务）："
                "缺任何一支都会得出片面结论（只看需求=盲目乐观，只看供给=错过机会）。"
                + (
                    "拆解由模型生成而非套模板，**问题变了拆法就变**。"
                    if _by_model
                    else "**本次拆解来自规则模板兜底**（模型规划路径未产出可用计划）——"
                    "骨架按档位截断，**问题变了拆法不一定变**。这是降级，不是自主拆解。"
                ),
                evidence=f"由 {len(getattr(plan, 'subtasks', []) or [])} 个子任务承接，"
                         f"档位 {getattr(getattr(plan, 'depth', None), 'value', '') or '标准'}"
                         + ("" if _by_model else "，计划来源=规则模板兜底"),
                trigger="llm" if _by_model else "rule",
            )
        # —— 决策③：假设驱动是否启用（因果题专属）——
        _hyps = getattr(plan, "hypotheses", []) or []
        if _qt == "causal" and _hyps:
            _dec(
                state,
                "decompose",
                "因果类问题要不要先立假设？",
                ["直接检索罗列事实", "先立 H1-H3 假设再找支持/反证两组证据"],
                f"立 {len(_hyps)} 条假设，正反两组分别取证",
                "不立假设就会**只找支持自己的证据**（确认偏误）。"
                "先立假设、再强制找反证，是让『下沉增速放缓』这类结论"
                "存在被推翻可能的唯一办法。",
                evidence="；".join(str(h)[:40] for h in _hyps[:3]),
                # 假设**不是模型立的**：`_build_hypotheses` 是模板句（`H1：『{topic}』的…（待验证）`），
                # 主语由 `_subject_of` 规则截取。标 `llm` 会让自主性图谱多记一次"模型判断"。
                trigger="rule",
            )
    return {
        "plan": plan,
        "budget_estimate": plan.price_estimate,
        "status": "confirmed",
    }


# ============================================================
# 节点 2：human_confirm（人类确认研究计划）
# ============================================================
def node_human_confirm(state: TaskState) -> dict[str, Any]:
    """人机协同：用户确认/编辑计划后进入执行。
    这是"数字员工"而非"全自动黑箱"的关键——人在回路。
    """
    _trace(state, "human_confirm", "等待用户确认研究计划")
    # 真实场景：这里由前端展示计划 → 用户点击确认 → 触发执行
    # 当前：确认状态已由调用方写入 plan.status，这里只记录
    return {
        "status": "confirmed",
    }


# ============================================================
# 节点 3：route_skill（按任务路由到 Skill + 分配子 Agent）
# ============================================================
def node_route_skill(state: TaskState) -> dict[str, Any]:
    """自主规划：把计划子任务路由给对应类型的子 Agent"""
    plan = state["plan"]
    logging.getLogger(__name__).info(
        "应用计划 depth=%s: %s",
        plan.depth,
        [(st.id, st.agent_type, st.description[:26]) for st in plan.subtasks],
    )
    routed = {}
    for st in plan.subtasks:
        # 按 agent_type 分组（行业扫描/竞争格局/用户洞察/数据统计）
        routed.setdefault(st.agent_type, []).append(st.id)
        _trace(state, "route_skill", f"子任务[{st.id}] → {st.agent_type}")
    # —— 决策：源层级路由（这类结论**必须**用什么层的源）——
    # 这是"取证前定策略"而不是"事后解释"：结论类型决定必需源层，
    # 取不到就如实记缺口，**不许拿低层级源顶上冒充**。
    # 同一 claim 类型只记一次——N 个子任务命中同一类型就记 N 条，只会把决策链刷成噪音。
    try:
        from core.decision_trace import record as _dec
        from core.source_registry import LAYERS, route_query

        _by_key: dict[str, Any] = {}
        _ids_by_key: dict[str, list[str]] = {}
        for st in plan.subtasks:
            # `route_query` 返回的是 **RouteDecision**（字段 claim_key/claim_label），
            # 不是 ClaimRoute（字段 key/label）。踩过：读 `_r.key` → AttributeError
            # → 被下面那个宽 except 吞掉 → **路由此决策静默从未记录**（实测 0 条）。
            # 教训：给"不影响主流程"的旁路写宽 except 时，必须让它**吼一声**——
            # 静默的旁路失效与"这个功能本来就没有"在结果上无法区分。
            _r = route_query(
                state.get("raw_question", "") or "",
                st.description or "",
                list(st.search_queries or []),
            )
            _ck = getattr(_r, "claim_key", "") or ""
            if not _ck:
                continue
            _by_key[_ck] = _r
            _ids_by_key.setdefault(_ck, []).append(st.id)
        for _key, _r in _by_key.items():
            _ids = _ids_by_key.get(_key) or []
            _req = getattr(_r, "required_layer", "") or ""
            _dec(
                state,
                "route",
                f"子任务 {'/'.join(_ids[:3])} 的主结论是「{getattr(_r, 'claim_label', '') or _key}」"
                "——这类结论该用什么层级的源？",
                [f"{k} {v}" for k, v in list(LAYERS.items())[:5]],
                f"必需 {_req} {LAYERS.get(_req, '')}".strip()
                + (f"（配对：{_r.pair}）" if getattr(_r, "pair", "") else ""),
                "不同结论类型对源的要求完全不同：公司经营数字只有法定披露（巨潮/SEC）算数，"
                "行业规模要官方统计或行业机构，用户声音则 L1 政府源根本不适用。"
                "**先定必需层、再取证**，取不到就记缺口——"
                "而不是先抓一堆链接、再回头挑几个看起来权威的当依据。",
                evidence=f"触发词命中 → claim 类型 {_key}；承接子任务 {len(_ids)} 个",
                trigger="rule",
            )
    except Exception as _e:
        logging.getLogger(__name__).warning("源路由决策记录失败（不影响执行）: %s", _e, exc_info=True)
    # —— 决策：要不要用历史经验调整这次的路数 ——
    try:
        from agents.researchers import _extract_industry
        from core.decision_trace import record as _dec
        from core.exp_memory import format_brief as _eb2, recall as _exp_recall

        _ind = _extract_industry(state.get("raw_question", "") or "")
        _exp_items = _exp_recall(_ind) if _ind else []
        if _exp_items:
            _brief = _eb2(_ind)
            _dec(
                state,
                "recall",
                f"本行业（{_ind}）过往调研里，有 {len(_exp_items)} 个源的调用命中率偏低"
                "——这次要不要据此调整检索路数？",
                [
                    "不参考历史：每个源每次都正常尝试（最保险，但会重复走已知走不通的路）",
                    "把历史统计当作**提示**注入检索上下文（不跳过任何源，只提示换角度）",
                    "把历史统计升级成**规则**，命中率低的源直接不打（最快，但会自我锁死）",
                ],
                "作为提示注入，**不跳过任何源**",
                "源在某类问题上没返回，往往是**检索角度**的问题，不是源本身没用——"
                "把统计升级成规则，等于让一次偶发失败**永久锁死一条路**，"
                "而且我们再也发现不了它其实换个问法就能用。"
                "所以经验只用来**提醒换角度**，不用来**决定打不打**。"
                "（与工具选择的分工：那个决定打不打，依据是**本次子任务需要什么**，不是历史。）",
                evidence="；".join(
                    f"{it['name']} {it['hits']}/{it['calls']}（{it['hit_rate'] * 100:.0f}%）"
                    for it in _exp_items[:3]
                ),
                trigger="rule",
            )
    except Exception as _e:
        logging.getLogger(__name__).warning("研究经验决策记录失败（不影响执行）: %s", _e)

    return {"subtask_results": {"_routing": routed}}


# ============================================================
# 节点 4-7：并行子 Agent（行业扫描/竞争格局/用户洞察/数据统计）
# ============================================================
def _run_sub_agent(state: TaskState, agent_type: str, st_ids: list[str], rework_hint: str = "") -> dict[str, Any]:
    """运行一个子 Agent，返回 {evidences, search_debug}

    real 模式：调用大模型做信息整合 → 产出带引用的模块报告
    优化：节点内子任务线程池并行（检索受信号量限流，但整合与其他子任务检索重叠）
    rework_hint：质检返工时的致命项，透传给子Agent要求其修正表述
    """
    import concurrent.futures

    from agents.researchers import run_researcher, _build_context_query

    subtasks = [
        next((s for s in state["plan"].subtasks if s.id == sid), None)
        for sid in st_ids
    ]
    subtasks = [s for s in subtasks if s]
    # —— 同维度合并（方案 A）：写作权归该维度的**最终版**独占 ——
    # 被取代的轮次（同章节键里靠前的那个）**只检索、不写作**，其原始资料并入最终版。
    # 为什么这样切：
    #   ① 从源头消灭"同一指标两套数"——全报告每个维度只有一个写作者，不可能自相矛盾；
    #   ② 不丢证据——基础轮检索到的东西照旧进证据池、照旧并入最终版的写作资料池与引文；
    #   ③ 反而更快——每个维度少一次整合 LLM 调用（长报告 700s+ 的档位很吃这个）。
    _finals = _final_subtask_ids(list(state["plan"].subtasks))
    _superseded = [s for s in subtasks if s.id not in _finals]   # 只检索
    _writers = [s for s in subtasks if s.id in _finals]          # 检索 + 写作
    # 被取代轮次的检索成果，按**章节键**暂存 → 交给同键的最终版
    _pool_raw: dict[str, list] = {}
    # 行业锚定：把用户提问里的行业词传给子Agent，防止内容串台到其他行业
    industry = _extract_industry(state["raw_question"])
    # 澄清约束（跨境/对象/地域/口径）随检索词传导，防任务漂移（P3）
    # 关键：把原始问题注入 context → _build_context_query 强制锚定行业词，
    #       否则 LLM 生成的子任务描述可能不含"咖啡"等关键词 → 检索0证据。
    ctx = dict(state.get("clarified_context") or {})
    ctx["raw_question"] = state["raw_question"]
    # —— 本次数据源调用台账（source_trace）——
    # 这里放的是**一个 list 对象**，各子任务拿到的 `_c = dict(ctx)` 复制的是**同一个引用**，
    # 所以并发线程里的追加天然汇总到一处（list.append 在 GIL 下是原子的）。
    # 之所以不改成"每个子任务各记一份再合并"：那样必然会漏掉某个线程的失败路径，
    # 而台账漏记 = 报告里声称"这个源没用过"，我们其实用过——**台账的完整性就是它的全部价值**。
    _mini_trace: list = []
    ctx["_health_trace"] = _mini_trace
    # 行业基线注入（数据护城河）：复用上次调研沉淀的行业卡片，做增量更新与对比
    # 命中即记 `asset_reused`（v0.9.75）：这是「有效复用率」唯一的分子来源。
    # 不记的话，资产飞轮就只是一个叙事——讲"越用越懂这个行业"却拿不出复用次数的账。
    _asset_reused = False
    try:
        from core.industry_kb import format_industry_brief

        _brief = format_industry_brief(industry)
        if _brief:
            ctx["industry_brief"] = _brief
            _asset_reused = True
    except Exception as e:
        logging.getLogger(__name__).warning("行业基线注入失败: %s", e)
    # 研究经验注入（core/exp_memory）：本行业过往**可核对的调用统计**——哪条路常走不通。
    # 措辞是"提示"不是"规则"：源失败往往是检索角度问题，不是源本身没用。
    try:
        from core.exp_memory import format_brief as _exp_brief

        _eb = _exp_brief(industry)
        if _eb:
            ctx["exp_brief"] = _eb
            _asset_reused = True
    except Exception as e:
        logging.getLogger(__name__).warning("研究经验注入失败: %s", e)
    # 已归档指标锚（指标时序库）：本行业既往调研确立的指标口径 → 同一指标跨报告锁值。
    # 与行业基线（散文）互补：这里给的是**结构化指标**，写作者要么沿用、要么写明"修正：X→Y"。
    try:
        from core.metric_ledger import locked_anchor

        _anchor = locked_anchor(industry)
        if _anchor:
            ctx["metric_anchor"] = _anchor
    except Exception as e:
        logging.getLogger(__name__).warning("指标锚注入失败: %s", e)
    # 估算每次子 Agent 执行的**档位积分**消耗（LLM 调用 + 检索）。
    # ⚠️ **单位是档位积分，不是人民币**（2026-09-15 外部审计）：
    # 这个数一路流进 `budget_spent`，而它在三处被叫成三种东西——
    # 台账抽屉印「1.4 积分」、`node_budget_check` 印「1.4 元」、停止卡片印「0.7 元」，
    # 而计费引擎同一笔实扣 50 积分。**同一个数三种单位**，交叉核对当场对不上。
    # 更硬的一条：本仓库的 K17 明写「未配单价就不报钱数」，而 `cost_cny` 因未配单价恒为 None
    # ——**一边返回 None 拒绝报价，一边在别处印"X 元"**，是自己打自己。
    # 现在统一：全链路只用「档位积分」，人民币只在 `cost_cny` 非空时出现。
    BUDGET_POINTS_PER_SUBTASK = 0.2

    evidences: list[Evidence] = []
    search_debug: list[tuple[str, str, int]] = []

    # 工具选择结果（子任务级）：线程内收集，节点末尾汇总成**一条**决策，
    # 而不是每个子任务记一条——N 条同类决策只会把决策链刷成噪音。
    _tool_picks: dict[str, Any] = {}

    def _run_one(subtask, write):
        _trace(state, agent_type, f"执行子任务[{subtask.id}]: {subtask.description}")
        try:
            q = _build_context_query(subtask, ctx)
            # 每个子任务用**自己的 context 副本**：同一节点里并发跑的子任务原先共用一个 dict，
            # `_route`（源路由）这种"每子任务一份"的键会被后写的覆盖掉。
            _c = dict(ctx)
            if write:
                # 把被取代轮次检索到的原始资料交给本章的**唯一写作者**
                _c["_merge_raw"] = list(_pool_raw.get(_chapter_key(subtask)) or [])
            result = run_researcher(
                subtask, agent_type, industry=industry, context=_c,
                rework_hint=rework_hint, write=write,
            )
            n = len(result.get("evidences") or []) if result else 0
            # 诊断：检索词+结果收进 search_debug → 经 trace 直接显示在浏览器思考过程
            search_debug.append((subtask.id, q, n))
            return subtask, result
        except Exception as e:
            search_debug.append((subtask.id, _build_context_query(subtask, ctx), 0))
            _trace(state, agent_type, f"检索[{subtask.id}] 异常: {e}")
            return subtask, {"evidences": [], "module_report": f"## {subtask.description}\n（异常: {e}）"}

    def _wave(items, write):
        """跑一波子任务。write=False 的那一波只检索、不出章（见 _final_subtask_ids）"""
        if not items:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_run_one, st, write) for st in items]
            for fut in futures:
                try:
                    subtask, result = fut.result()
                except Exception as e:
                    # 子Agent线程内未捕获异常：打堆栈（不静默），便于定位
                    logging.getLogger(__name__).error(
                        "子Agent(%s) 线程未捕获异常: %s", agent_type, e, exc_info=True
                    )
                    continue
                # 预算消耗**不再在这里原地累加**（2026-09-14）：`budget_spent` 现在是
                # `Annotated[float, operator.add]` 通道，本函数末尾按"跑了几个子任务"
                # 返回**增量**。这里再改一次会与之重复计费。
                if not result:
                    continue
                evidences.extend(result["evidences"])
                # 来源质量门拦下的页 → 写进思考过程。用户看到"来源少了"时，
                # 界面上必须能直接回答"是拦了还是没搜到、拦的是什么"。
                _rej = result.get("rejected") or []
                if _rej:
                    _why = "；".join(f"{u}（{r}）" for u, r in _rej[:3])
                    _trace(
                        state,
                        agent_type,
                        f"来源质量门[{subtask.id}] 拦下 {len(_rej)} 条：{_why}",
                    )
                _tp = result.get("tool_plan")
                if _tp and _tp.get("tools"):
                    _tool_picks[subtask.id] = _tp
                _mr = result.get("module_report")
                # 被取代的轮次不出章 → 不写 module_report：写了也没人读，
                # 还会让下游"取不到结果就跳过"的判断失真
                if _mr:
                    state["subtask_results"][subtask.id] = _mr
                # 引用清单：按子任务分组存到 subtask_results（报告角标数据源）
                state["subtask_results"][f"{subtask.id}__citations"] = result.get("citations", [])
                # 源路由审计记录（问答→源硬路由）：存纯 dict，供交付期"数据源体系"附录复算
                _rt = result.get("route")
                if _rt is not None:
                    try:
                        state["subtask_results"].setdefault("_routes", {})[subtask.id] = _rt.to_dict()
                    except Exception:
                        pass
                # 引擎侧层级越界（TAM/SAM/SOM 不严格递减）：子研究员自己算出来的确定性事实。
                # **落在 `subtask_results` 而不是新开一个 TaskState 键**：并行 reducer 下
                # 多章可能各报一次，这里按子任务号累积，交付期一次收全（与 `_routes` 同一姿势）。
                _sv = str(result.get("sizing_violation") or "").strip()
                if _sv:
                    state["subtask_results"].setdefault("_sizing_violation", {})[subtask.id] = _sv
                # 原始取证记录（可复算包原料）：瘦身后入 state——存的是检索层**真实返回**，
                # 只截断长度、不改内容，第三方才能从原始记录复算到报告结论。
                try:
                    from core.audit_bundle import _trim_raw

                    _raw_full = result.get("raw") or []
                    if _mr:
                        _raw = _trim_raw(_raw_full)
                        if _raw:
                            state["subtask_results"][f"{subtask.id}__raw"] = _raw
                    else:
                        # 被取代的轮次：原始记录**原样**留在内存里并入最终版。
                        # 不能用 _trim_raw——它砍掉 sources/urls，最终版就抽不出真实来源页，
                        # 引文会莫名变少。
                        _pool_raw.setdefault(_chapter_key(subtask), []).extend(_raw_full)
                except Exception:
                    pass
            _finalize_event(state, agent_type)

    # 顺序**必须**是"先只检索的一轮，再出章的一轮"：最终版要拿到前序轮次的原始资料才能统一成文
    _wave(_superseded, write=False)
    if _superseded and _writers:
        _trace(
            state,
            agent_type,
            f"同维度合并：{len(_superseded)} 个前序轮次只做检索、不单独成章，资料并入 {len(_writers)} 个最终版章节",
        )
    _wave(_writers, write=True)
    # —— 决策：工具选择（本次实际打哪几个源）——
    # 按**子任务**汇总成一条决策（每子任务记一条会把决策链刷成噪音），
    # 且只在本节点真的选过工具时记（`_tool_picks` 为空 = 未执行/fail-open，那不算取舍）。
    if _tool_picks:
        try:
            from core.decision_trace import record as _dec

            _lines = []
            _forced_all: list[str] = []
            _why_all: list[str] = []
            _tool_union: list[str] = []
            for sid, tp in list(_tool_picks.items())[:4]:
                _lines.append(f"{sid}→{'/'.join(tp.get('tools') or [])}")
                _forced_all.extend(tp.get("forced") or [])
                if tp.get("why"):
                    _why_all.append(str(tp["why"]))
                for t in tp.get("tools") or []:
                    if t not in _tool_union:
                        _tool_union.append(t)
            _dec(
                state,
                "select_tool",
                f"{agent_type} 节点有 {len(_tool_picks)} 个子任务要取证"
                "——这次的检索该打哪些数据源？",
                [
                    "全部已接入源都打一遍（稳，但有很多请求是白打的）",
                    "按子任务挑：只打能回答这个问题的源",
                ],
                "按子任务挑：" + "；".join(_lines),
                "每个源都有自己的**能提供什么**与**边界**（注册表里写明了）。"
                "打一个用不上的源不只是浪费请求——它会把**不相关的条目混进证据池**，"
                "稀释真正有用的来源，还让调用台账变成噪音。"
                + (
                    f" 另有 {len(set(_forced_all))} 个源是**规则强制并集**进来的"
                    f"（{'/'.join(sorted(set(_forced_all)))}）：这类结论按规则必须有该层级的源，"
                    "**模型选漏了也不许降级**。"
                    if _forced_all
                    else ""
                ),
                evidence="；".join(_why_all)[:200]
                or f"最终工具集：{'/'.join(_tool_union[:8])}",
                trigger="hybrid" if _forced_all else "llm",
            )
        except Exception as _e:
            logging.getLogger(__name__).warning("工具选择决策记录失败（不影响执行）: %s", _e)
    # ⚠️ **只返回本节点的增量**，不要先把通道里的 list 取出来 extend 再交回 reducer。
    # 病案（2026-09-14 审计）：原来 `_acc = state.get("source_trace"); _acc.extend(...)`
    # 原地改了通道里那个对象，又把它当返回值交回 `_merge_lists`（`(left or []) + (right or [])`）
    # → 4 个并行子 Agent 节点在同一超级步各写一次，同一份台账被自己拼自己 =
    # **整表复制 5 倍**。报告附录于是把"合计 20 次调用"写成"合计 100 次调用"——
    # 而那段附录的意义正是"让用了什么源可核对"，等于在最强调诚实的地方自造假数。
    # `_mini_trace` 是本节点私有的 list，与通道无别名关系，交给 reducer 去拼接。
    return {
        "evidences": evidences,
        "search_debug": search_debug,
        "source_trace": list(_mini_trace),
        # 预算：返回**增量**（reducer 是 operator.add，见 core/state.py）
        "budget_spent": round(BUDGET_POINTS_PER_SUBTASK * (len(_superseded) + len(_writers)), 2),
        # 资产复用（v0.9.75）：本次是否命中了已沉淀的行业资产 → 「有效复用率」的分子
        "asset_reused": bool(_asset_reused),
    }


def node_industry(state: TaskState) -> dict[str, Any]:
    routing = state["subtask_results"].get("_routing", {})
    return _run_sub_agent(state, "industry", routing.get("industry", []))


def node_competition(state: TaskState) -> dict[str, Any]:
    routing = state["subtask_results"].get("_routing", {})
    return _run_sub_agent(state, "competition", routing.get("competition", []))


def node_user(state: TaskState) -> dict[str, Any]:
    routing = state["subtask_results"].get("_routing", {})
    return _run_sub_agent(state, "user", routing.get("user", []))


def node_statistics(state: TaskState) -> dict[str, Any]:
    routing = state["subtask_results"].get("_routing", {})
    return _run_sub_agent(state, "statistics", routing.get("statistics", []))


# ============================================================
# 节点 8：check_conflict（证据冲突检测 + 可解释仲裁）
# ============================================================
def node_check_conflict(state: TaskState) -> dict[str, Any]:
    """证据冲突检测 + 仲裁（评审命门1 的技术钩子）

    自动检测：同一维度内，不同来源对同一指标给出显著不同的数值（如
    "门店5000家" vs "门店8000家"）→ 自动仲裁，产出仲裁卡片数据。
    仲裁卡片 UI 展示"为什么采信A不信B"（4维评分 + 权重 + 理由）。
    """
    from core.models import arbitrate_conflict

    evidences = state["evidences"]
    conflicts = []
    handled = set()
    # **"0 处仲裁"必须带上分母**（2026-09-15，GPT 评审：这是复核者会直接问的一处）。
    # 报告顶部写着「57 条证据 · 0 处仲裁」，而正文明明列着 5 组口径冲突——
    # 没有分母时读者无法判断"0"是"查过没有"还是"根本没查"。
    # 本仓库在链接核验、遗漏层上已经各栽过一次（0% vs 未测不可区分），这里是第三处。
    _pairs_examined = 0      # 主干相同、进入数值比对的证据对
    _trunk_matched = 0       # 去掉数字后主干完全相同的证据对

    for i, ev in enumerate(evidences):
        if i in handled:
            continue
        if not ev.sources:
            continue
        # 维度分组：去掉claim中的数字，比较语义主干（如"杭州咖啡门店数量约"）
        import re as _re

        # **归一化必须吃掉空白**（2026-09-15 实跑）。原实现只把数字与中文标点换成 `·`，
        # 空格原样保留 —— 于是 `门店 5000 家` → `门店 · 家`、`门店5000家` → `门店·家`，
        # **差一个空格就不比较了**。而证据是不同来源、不同子任务产出的，
        # 排版差异（数字两侧加不加空格、有没有全角空格）几乎是必然存在的。
        # 后果：这条判据实际要求两条证据**逐字相同**，冲突检测近乎永不触发 ——
        # 实跑报告里「0 处仲裁」与「规则层 5 组口径冲突」并排出现，就是这么来的。
        # 空白不是语义，把它归一掉只可能让匹配更正确。
        def _trunk(s: str) -> str:
            t = _re.sub(r"[\d，。\.、]+", "·", str(s or ""))
            return _re.sub(r"\s+", "", t).strip("·")

        ev_core = _trunk(ev.claim)
        for j in range(i + 1, len(evidences)):
            other = evidences[j]
            if j in handled or not other.sources:
                continue
            other_core = _trunk(other.claim)
            if other_core != ev_core:
                continue  # 不同语义主干不比较
            _trunk_matched += 1

            # 数值冲突检测：提取两 claim 中的数字（如 5000 vs 8000）
            nums_ev = [float(n) for n in _re.findall(r"(\d+(?:\.\d+)?)", ev.claim)]
            nums_other = [float(n) for n in _re.findall(r"(\d+(?:\.\d+)?)", other.claim)]
            if not nums_ev or not nums_other:
                continue
            _pairs_examined += 1   # 主干相同 + 两边都带数字 → 真正进了数值比对
            if max(nums_ev) > 0 and abs(max(nums_ev) - max(nums_other)) / max(nums_ev) > 0.2:
                # 两数值差异>20% → 判定冲突
                card = arbitrate_conflict(ev, other)
                conflicts.append(
                    {
                        "dimension": ev_core[:20],
                        "evidence_a": {"claim": ev.claim, "score": card["score_a"]},
                        "evidence_b": {"claim": other.claim, "score": card["score_b"]},
                        "card": card,
                    }
                )
                _trace(
                    state,
                    "check_conflict",
                    f"冲突仲裁[{ev_core[:20]}]: {ev.claim[:18]}... vs {other.claim[:18]}... → {card['decision']}",
                )
                # **回写冲突标记**（2026-09-14 审计）。没有这一步，`Evidence.is_conflicting`
                # 全仓无一处被写为 True → 结论状态机的 `CONTESTED`（存争议）**结构上不可达**，
                # 而它正是本项目"敢承认不知道"卖点里点名的两个关键态之一。
                # 连带后果更刺眼：报告末尾会先断言「所有结论均…无来源间数据冲突」，
                # 紧接着列出《数据冲突日志》——同一份交付物自己打自己。
                try:
                    ev.is_conflicting = True
                    ev.conflict_detail = str(card.get("decision") or "")[:200]
                    other.is_conflicting = True
                    other.conflict_detail = str(card.get("decision") or "")[:200]
                except Exception:
                    pass
                handled.add(i)
                handled.add(j)
                break

    return {
        "conflict_results": conflicts,
        "rework_count": state["rework_count"],
        # **仲裁读数的分母**（2026-09-15）：报告顶部写的是「N 条证据 · M 处仲裁」，
        # 而 M=0 时读者无从判断这是"查过没有"还是"根本没查"。
        # 把两个分母一并带出去，那一行才能写成「检测了 X 对同主干证据，Y 对进入数值比对，M 处判定冲突」——
        # **一个带分母的 0 才是有信息的 0**。同型病案本仓库已修过两次（链接核验 0% vs 未测、遗漏层 claims=0 vs 崩溃）。
        "conflict_scan": {
            "trunk_matched": _trunk_matched,     # 去掉数字后主干完全相同的证据对
            "pairs_compared": _pairs_examined,   # 两边都带数字、真正进了数值比对的对数
            "conflicts": len(conflicts),
            "criterion": "同语义主干（去数字去空白后逐字相同）且最大值相差 >20%",
        },
    }


# ============================================================
# 节点 9：汇总（生成报告初稿）
# ============================================================
# 章节叙事顺序（宏观→中观→微观→决策，防并列罗列、保证递进）
_DIMENSION_ORDER = [
    "宏观环境", "PEST", "市场全景", "市场规模", "细分赛道", "细分",
    "竞争格局", "竞品", "目标市场", "消费人群", "消费洞察", "用户",
    "进入壁垒", "竞争优势", "单位经济", "成本测算", "决策建议",
    "趋势", "风险",
]


def _dim_order_key(st) -> int:
    """子任务在报告中的顺序：按维度递进排序，未匹配维度放最后"""
    desc = st.description or ""
    for i, kw in enumerate(_DIMENSION_ORDER):
        if kw in desc:
            return i
    return len(_DIMENSION_ORDER)


# ============================================================
# 同维度合并（方案 A）：一个维度全报告**只出一章**
# ============================================================
# 病案（报告(8) 实拆）：同一维度同时有"基础轮"和"深入『X』"两个子任务，两章并列 →
# 同一份报告里 TAM 写了两遍（6,562 亿 vs 7,738 亿）、SAM 1,088/1,395 亿、SOM 131/167 亿、
# 单店初期投资 120 万/315 万、星巴克中国市占率 18.5%/12.5%、库迪营收 95.0/60.0 亿……
# 规则层自己报了 8 项口径冲突、RedTeam 报 4 项高风险，却**只披露、不收敛**——
# 检测到位而把矛盾原样交给用户，等于把"严谨"做成了免责声明。这才是"不像专业调研
# 交付物"的根本原因（比缺图严重得多）。
# 用户拍板方案 A：同维度只出一章（深入轮取代基础轮，不再并列）。
def _chapter_key(st) -> str:
    """子任务的**章节键**。同键 = 同一章。

    **优先取结构化字段 `st.dimension`**（2026-09-19 收口）——它由规划层显式写死：
    模板兜底直接取模板维度名，LLM 路径由 `router._resolve_subtask_dimensions`
    逐条核定（核定不了就整份退回模板）。**归属不再从描述文本里猜。**

    为什么必须改成这样：从前这里是拿 `『』` 从描述里抠维度名的。模板计划的描述
    恰好写成 `调研『市场规模测算』…`，所以抠得中；而 LLM 自主拆解写的是自由文本
    （"检索三四线城市咖啡消费相关的政策支持、居民可支配收入变化情况"）——
    抠不到 → 基础轮自成一章、深入轮又按维度名出一章 → **同一维度两章、同一指标
    两套数**，正是本产品自称最严重的历史缺陷。**靠措辞约定来保证结构正确，
    等于把结构交给模型自由发挥。**

    描述解析保留为兜底：读的是**旧存档**（那时 `dimension` 字段还不存在），
    以及少数不参与维度归并的子任务（如定制档的决策建议）。兜底逻辑原样不动。
    """
    import re as _re

    _dim = str(getattr(st, "dimension", "") or "").strip()
    if _dim:
        return _dim

    t = _clean_section_title(st.description or "")
    t = _re.sub(r"[（(][^）)]*[）)]", "", t)                                # 去括号补充
    t = _re.sub(r"(深入|深化|补充|细化|进阶|扩展|加强|第二轮|二次|其二)$", "", t.strip())
    return t.strip() or (st.id or "")


def _final_subtask_ids(subtasks: list) -> set[str]:
    """哪些子任务是**该维度的最终版**（唯一出章者）。其余只检索、不出章。

    同键多轮时留哪一个，看末轮是不是"**指定的**最终版"：
    - 是（id 带 `_deep`，或描述以"深入『X』"开头＝深入/定制档的正规第二轮）→ 留末轮。
      末轮已按"完整最终版"写作（见 router 的写作要求 + _PLAN_SYSTEM_PROMPT 规则 5）。
    - 否（＝LLM 没守 standard/free 档"每维度一个子任务"而多拆的**意外重复维度**）→ 留**首轮**。
      那一轮才是按该维度正规写的；留末轮会得到一章残篇（它以为自己是"补充"）。
    两种情况都只出一章 —— 差别只在留哪一章。
    """
    import re as _re

    groups: dict[str, list] = {}
    for st in subtasks:
        groups.setdefault(_chapter_key(st), []).append(st)
    finals: set[str] = set()
    for items in groups.values():
        if len(items) == 1:
            finals.add(items[0].id)
            continue
        last = items[-1]
        _designated = bool(last.id.endswith("_deep")) or bool(
            _re.search(r"深入\s*[『「]", last.description or "")
        )
        finals.add((last if _designated else items[0]).id)
    return finals


def _chapter_subtasks(state: TaskState) -> list:
    """报告章节清单：同维度只保留最终版，按维度递进排序。

    node_summarize 与 node_deliver **必须**共用本函数——正文 <ref> 角标编号与交付
    引文列表本来就靠"同一排序 + 同一配额"对齐，一侧少一章就会整体错位。
    """
    subs = list(state["plan"].subtasks)
    finals = _final_subtask_ids(subs)
    return [st for st in sorted(subs, key=_dim_order_key) if st.id in finals]


def _apply_report_citation_quotas(
    ordered_lists: list[list[dict]],
    *,
    gov_cap: int = 3,
    site_cap: int = 2,
    media_ratio: float | None = None,
) -> list[list[dict]]:
    """报告级引用配额：同一**可注册根域**在全报告（跨所有子任务模块）最多贡献 site_cap 条
    （政府/教育 gov_cap 条）；**同一篇文章的镜像双链（m.163/www.163、sohu 带 pvid）跨模块
    只保留最先出现的一条**——根域配额 + 规范键去重 + owner 视角独立计数，杜绝"整篇只剩 3
    个域名"或"同一篇反复出现"。

    **第二道：来源结构配额（2026-09-10 新增）**——根域配额只挡"同一家刷屏"，挡不住
    "新浪+搜狐+36氪+网易+凤凰…合起来占大半"。实测一笔跑题 14 条引文里门户/汽车媒体占 9 条
    （用户原话："数据源全是以微博、新浪为主"）。所以在根域配额之后再封一道**门户/财经媒体/
    社媒类引用的全报告占比上限**（默认 50%，`CITATION_MEDIA_RATIO` 可覆盖），超出部分
    从**后出现的媒体项**裁掉（先到先得：排在前面的维度保留）。裁的是"第 N 个可点来源"，
    正文只在段末挂 <ref> 角标块、不逐句内联引用，故不会产生悬空引用。

    引用在正文里按模块顺序编号（<ref> 角标，见 node_summarize），所以裁剪只能从
    "后出现的重复项"里裁（先到先得），**返回与 ordered_lists 等长的裁剪后列表**，
    保证 node_summarize 编号与 node_deliver 交付的引文列表 1:1 不错位。
    无真实 URL 的纯文字引文不占配额（不挤占可点来源的多样性名额）。
    """
    from urllib.parse import urlparse

    from core.source_util import canonical_key, registrable_root

    kept_lists: list[list[dict]] = []
    counts: dict[str, int] = {}
    seen_ck: set[tuple[str, str]] = set()  # 跨模块规范键去重（同文镜像只留最先一条）
    for lst in ordered_lists:
        kept: list[dict] = []
        for c in lst or []:
            if not isinstance(c, dict):
                continue
            u = (c.get("url") or "").strip()
            if u.startswith("http"):
                ck = canonical_key(u)
                if ck[0]:
                    if ck in seen_ck:
                        continue  # 同文镜像（m./www./pvid 双链）跨模块只留一条
                    seen_ck.add(ck)
                try:
                    h = (urlparse(u).netloc or "").lower().split(":")[0].split("@")[-1]
                except Exception:
                    h = ""
                gov = h.endswith("gov.cn") or ".gov" in h or h.endswith("edu.cn")
                root = registrable_root(u)
                if root:
                    cap = gov_cap if gov else site_cap
                    if counts.get(root, 0) >= cap:
                        continue  # 该根域已达报告配额 → 裁掉后出现的重复项
                    counts[root] = counts.get(root, 0) + 1
            kept.append(c)
        kept_lists.append(kept)

    # —— 第二道：门户/媒体占比封顶（治"来源清一色新浪/搜狐/36氪"）——
    if media_ratio is None:
        try:
            import os as _os

            media_ratio = float(_os.environ.get("CITATION_MEDIA_RATIO", "0.5"))
        except Exception:
            media_ratio = 0.5
    _MEDIA_GROUPS = ("财经媒体", "媒体报道", "社媒/论坛")
    _is_url = lambda c: str((c or {}).get("url") or "").startswith("http")  # noqa: E731
    _is_media = lambda c: str((c or {}).get("group") or "") in _MEDIA_GROUPS  # noqa: E731
    total_url = sum(1 for lst in kept_lists for c in lst if _is_url(c))
    media_have = sum(1 for lst in kept_lists for c in lst if _is_url(c) and _is_media(c))
    if total_url and media_have:
        ratio = max(0.0, min(1.0, media_ratio))
        # 地板：媒体裁剪最多把可点来源总数压到 60%（**不为了一个比例把来源清单掏空**——
        # 达不到目标比例时接受现状并如实披露，而不是删到只剩几行"好看"的构成）
        floor = max(4, int(round(total_url * 0.6)))
        excess = 0
        _t, _m = total_url, media_have
        while _t > floor and _m > 0 and (_m / _t) > ratio:
            _m -= 1
            _t -= 1
            excess += 1
        if excess > 0:
            # 从后往前裁（后面的维度先让位），保持模块内相互顺序
            for li in range(len(kept_lists) - 1, -1, -1):
                if excess <= 0:
                    break
                rest: list[dict] = []
                for c in reversed(kept_lists[li]):
                    if excess > 0 and _is_url(c) and _is_media(c):
                        excess -= 1
                        continue
                    rest.append(c)
                kept_lists[li] = list(reversed(rest))
    return kept_lists


def _build_method_chapter(state) -> str:
    """「研究方法与框架」章节（确定性生成，进 report_draft）

    让方案设计透明可见（Rubric"方案设计"维度 + 大赛"任务理解/自主规划"叙事）：
    问题拆解(MECE) + 实际用到的分析框架 + 问题类型/假设 + 证据与结论方法。
    只列真实发生的方法，不编造。
    """
    try:
        from core.rubric import extract_frameworks

        plan = state.get("plan")
        subtasks = getattr(plan, "subtasks", None) or []
        tree = getattr(plan, "problem_tree", None) or []
        qt = getattr(plan, "question_type", "") or ""
        hyps = getattr(plan, "hypotheses", None) or []
        descs = [getattr(st, "description", "") or "" for st in subtasks]
        fw = extract_frameworks(descs)
        lines = ["## 研究方法与框架", ""]
        if tree:
            parts = [
                f"**{br.get('icon', '')}{br.get('branch', '')}**（{len(br.get('subtask_ids') or [])} 问）"
                for br in tree[:4]
            ]
            lines.append(f"- **问题拆解（MECE）**：{'　'.join(parts)}")
        else:
            lines.append(f"- **问题拆解**：{len(subtasks)} 个子问题（互斥穷尽）")
        if fw:
            lines.append(f"- **分析框架**：{'、'.join(fw)}（各子任务对应框架产出结论）")
        if qt == "causal" and hyps:
            lines.append(f"- **假设驱动（因果问题）**：{'；'.join(hyps[:3])}")
        elif qt:
            _desc = {"causal": "因果 → 假设-证据验证", "descriptive": "描述 → 统计口径核验", "exploratory": "探索 → 议题树分层"}.get(qt, qt)
            lines.append(f"- **问题类型**：{qt}（{_desc}）")
        lines.append("- **证据方法**：实时检索 + 官方/研究机构源分级（L1-L5）；URL 存活核验 + 失效域名回退")
        lines.append("- **结论方法**：多源交叉 → 置信度分级（高/中/低）；反证检索与对抗性校验抑制乐观偏差")
        return "\n".join(lines)
    except Exception:
        return ""


def node_replan(state: TaskState) -> dict[str, Any]:
    """动态重规划：审视「目标 vs 已获证据 vs 缺口」→ **自己决定**要不要补一轮定向调查。

    节点位置刻意选在 `check_conflict` 之后、`summarize` 之前：
    此时第一轮证据已全部落袋、冲突已检出，正是"回头看还缺什么"的唯一正确时点——
    放在取证前叫预设，放在成稿后叫返工，放在这里才叫**重规划**。

    分工（见 `core/replan.py` 模块头）：
      触发 = 确定性规则（缺口是客观事实，不需要判断力，也不该烧预算去猜）
      内容 = 模型提议（"该换什么角度查"才需要判断力）
      裁决 = 规则校验（类型白名单 / 检索词去重 / 不撞章 / 数量上限）

    三态如实记录，**包括"不补"**：
      · 无缺口 → 只记 trace（不占决策链，那不是取舍）
      · 有缺口但不补 → 记一条决策并写明理由（这是真取舍）
      · 有缺口且补 → 记一条决策 + 执行 + 把执行结果如实带回来（补不到也要说）
    """
    from core.decision_trace import record as _dec
    from core.replan import run as _replan_run

    _trace(state, "replan", "动态重规划：审视已获证据与缺口")
    try:
        r = _replan_run(state)
    except Exception as e:
        logging.getLogger(__name__).warning("动态重规划失败（不阻断交付）: %s", e, exc_info=True)
        return {}
    gaps = r.get("gaps") or []
    planned = r.get("planned") or []
    # 无缺口：这在决策链上不算取舍（"没发现问题所以不补"不是判断，是事实），只进 trace
    if not gaps:
        _trace(state, "replan", "缺口检测：未发现客观缺口，按首轮结果成稿")
        return {}
    _gap_txt = "；".join(g["label"] for g in gaps[:4])
    if not planned:
        # 有缺口但不补 —— 这是**真取舍**，必须记，且必须说明为什么
        _dec(
            state,
            "replan",
            f"缺口检测发现 {len(gaps)} 项客观缺口（{_gap_txt}）——要不要追加一轮定向调查？",
            [
                f"追加补充调查（档位 {r.get('depth')} 上限 {r.get('max_new')} 条）",
                "不追加：缺口如实写进『数据缺口披露』，由读者/下一步一手调研处理",
            ],
            "不追加，缺口如实披露",
            "这些缺口**不是靠换检索词能补上的**（需要付费库/企业内部数据/一手采集），"
            "或者模型建议的补充方向与首轮重复。硬补一轮只会：花掉预算、拿到同一批结果、"
            "再在报告里多一段没有信息量的章节。**承认查不到，比假装查过更有用。**",
            evidence=_gap_txt[:200],
            trigger="hybrid",
        )
        return {}

    # ---- 执行补充轮：复用既有子代理链路（不新写取证路径，避免两套行为漂移）----
    from core.models import SubTask as _ST

    _max_id = 0
    for _s in state.get("plan").subtasks or []:
        _m = __import__("re").match(r"st_(\d+)$", str(getattr(_s, "id", "") or ""))
        if _m:
            _max_id = max(_max_id, int(_m.group(1)))
    _new: list[Any] = []
    for i, p in enumerate(planned, 1):
        _new.append(
            _ST(
                id=f"st_{_max_id + i}_sup",
                agent_type=p["agent_type"],
                description=p["description"],
                search_queries=list(p["queries"]),
                status="pending",
            )
        )
    _dec(
        state,
        "replan",
        f"缺口检测发现 {len(gaps)} 项客观缺口（{_gap_txt}）——要不要追加一轮定向调查？",
        [
            "不追加：缺口写进『数据缺口披露』",
            f"追加 {len(_new)} 条定向补充调查（档位 {r.get('depth')} 上限 {r.get('max_new')} 条）",
        ],
        f"追加 {len(_new)} 条：" + "；".join(p["description"] for p in planned),
        "这些缺口是**换检索角度就能补上的**（首轮检索词没打中，或首轮只拿到转述、没拿到一手），"
        "而它们直接影响决策结论的可信度——**能补而不补，等于明知有问题还是交了**。"
        "补充方向由模型提、由规则裁决（去重/不撞章/限量），避免它借机扩大研究范围。",
        evidence="；".join(
            f"{p['description']}（{p['why']}；检索词 {'/'.join(p['queries'][:2])}）" for p in planned
        )[:220],
        trigger="hybrid",
    )
    # 计划落回 plan（读者/复核者在计划视图里能看到计划被改过）
    try:
        state["plan"].subtasks = list(state["plan"].subtasks or []) + _new
    except Exception:
        pass

    evidences: list = []
    search_debug: list = []
    _trace_all: list = []
    _spent = 0.0
    _by_type: dict[str, list[str]] = {}
    for st in _new:
        _by_type.setdefault(st.agent_type, []).append(st.id)
    for at, ids in _by_type.items():
        try:
            out = _run_sub_agent(state, at, ids)
            evidences.extend(out.get("evidences") or [])
            search_debug.extend(out.get("search_debug") or [])
            _trace_all.extend(out.get("source_trace") or [])
            _spent += float(out.get("budget_spent") or 0)
        except Exception as e:
            logging.getLogger(__name__).warning("补充调查[%s]执行失败（不阻断交付）: %s", at, e)
    _hit = sum(1 for e in evidences if getattr(e, "sources", None))
    _trace(
        state,
        "replan",
        f"补充调查完成：{len(_new)} 个子任务取回 {len(evidences)} 条证据"
        f"（其中 {_hit} 条带可点来源）",
    )
    if not _hit:
        # **补了但没补到** —— 同样要如实记，不许因为"我们努力过了"就说成已覆盖
        _dec(
            state,
            "fallback",
            f"补充调查 {len(_new)} 个子任务执行完毕——结果够不够，要不要再补一轮？",
            ["再补一轮", "到此为止，把未补上的缺口如实写进『数据缺口披露』"],
            "到此为止，未补上的缺口如实披露",
            "补充轮没有取回带可点来源的证据。**再补一轮大概率是同一个结果**"
            "（同一批检索通道、同一个知识边界），而预算和时间是确定的。"
            "该做的是把'查不到'本身讲清楚，而不是反复试到看起来努力。",
            evidence=f"补充轮取回 {len(evidences)} 条、带来源 0 条；原始缺口：{_gap_txt[:120]}",
            trigger="rule",
        )
    return {
        "evidences": evidences,
        "search_debug": search_debug,
        "source_trace": _trace_all,
        "budget_spent": round(_spent, 2),
        "plan": state.get("plan"),
    }


def node_summarize(state: TaskState) -> dict[str, Any]:
    """汇总 Agent：把子任务结果 + 证据整合成专业报告（章节递进排序）"""
    _trace(state, "summarize", "汇总各模块结果生成报告初稿")
    # 章节清单：**同一维度只出一章**（深入轮取代基础轮，不再并列成两章）——见 _chapter_key 病案
    ordered = _chapter_subtasks(state)
    # 先按"报告级根域配额"裁剪各模块引文，再编号角标——保证 <ref> 与交付引文 1:1 不错位
    mod_cits = [state["subtask_results"].get(f"{st.id}__citations", []) or [] for st in ordered]
    kept_lists = _apply_report_citation_quotas(mod_cits)
    sections = []
    # 全局引用编号（跨章节连续编号，与交付时的 citations 扁平列表对齐）
    cite_cursor = 0
    title_used: dict[str, int] = {}  # 同维度多子任务 → 标题加序号去重
    for i, st in enumerate(ordered):
        res = state["subtask_results"].get(st.id)
        if not res:
            # 补充调查**空手而归时不许静默跳过**（2026-09-14 实跑踩中）：
            # 动态重规划派出的补充轮跑了、缺口仍没补上，而 `continue` 让它从报告里彻底消失——
            # 读者看到的是一份"没有这一章"的报告，而真相是"我们试过了、没查到"。
            # 「试过没查到」是信息，「没提」不是。所以补一小节如实说明。
            _is_sup = str(getattr(st, "description", "") or "").startswith("补充调查")
            if not _is_sup:
                continue
            _res_txt = (
                "> **本轮补充调查未取得可用内容。** 首轮缺口检测判定该项存在客观缺口，"
                "补充轮按换角度检索词重取，但未获得可引用的公开来源——"
                "**该缺口仍然存在**，已记入『数据缺口披露』与『自主决策链』，"
                "请勿把本节的存在理解为「已补齐」。"
            )
            res = _res_txt
        # 章节标题：清洗掉"调研『XX』"等前缀，用干净的维度名
        title = _clean_section_title(st.description)
        # 同维度已在上游合并成**唯一一章**（_chapter_subtasks），所以不再有"· 深入"并列章；
        # 这里只保留"章节键不同但显示名撞车"的兜底序号。
        if title in title_used:
            title_used[title] += 1
            title = f"{title}（二）" if title_used[title] == 2 else f"{title}（{title_used[title]}）"
        else:
            title_used[title] = 1
        # 在每段末追加本模块的引用角标（编号全局连续，前端可点击查看来源详情）
        kept = kept_lists[i] if i < len(kept_lists) else []
        suffix = ""
        if kept:
            refs = "".join(
                f"<ref>{cite_cursor + j + 1}</ref>" for j in range(len(kept))
            )
            cite_cursor += len(kept)
            suffix = f"\n\n<span class='cite-group'>{refs}</span>"
        sections.append(f"## {title}\n\n{res}{suffix}")
    draft = "\n\n".join(sections) if sections else "（暂无模块结果）"
    # 「研究方法与框架」章节放在正文最前（透明方案设计：MECE+框架+假设+证据/结论方法）
    method = _build_method_chapter(state)
    if method and sections:
        draft = f"{method}\n\n{draft}"
    return {"report_draft": draft}


def _clean_section_title(desc: str) -> str:
    """清洗子任务描述为干净的章节标题：只保留维度名（去掉方法论括号）"""
    import re

    t = desc.strip()
    # 取"调研『XX』"里的维度名（新格式：调研『市场规模测算』（方法论：...））
    m = re.search(r"[『「]([^』」]+)[』」]", t)
    if m:
        return m.group(1)[:30]
    # 兜底：去掉"调研/分析/评估"前缀和方法论括号
    t = re.sub(r"（[^）]*）", "", t)
    for pre in ["调研『", "调研「", "分析『", "分析「", "评估『", "评估「", "调研", "分析", "评估"]:
        t = t.replace(pre, "")
    t = t.replace("』", "").replace("」", "").replace("」", "")
    return t[:30] or desc


# ============================================================
# 节点 10：budget_check（预算控制）
# ============================================================
# 成本预算 = 报价的一个固定比例（**毛利护栏**）。
# 为什么不是"直接比报价"（2026-09-14 审计）：`budget_estimate` 是 `plan.price_estimate`，
# 即**本档位的档位积分**（免费10 / 标准50 / 深入150 / 定制400，与计费引擎同源）；
# 而 `budget_spent` 是本次已消耗的积分（每子任务 `BUDGET_POINTS_PER_SUBTASK` × 子任务数）。
# 两者**必须同单位**，比例比较才成立——2026-09-15 之前它们一个被当"元"一个被当"积分"，
# 这条护栏的比较本身是无效的（现在是有效的，但仍极少触发：见下方注释）。
# 正确的问法是："这次烧的积分是不是已经吃掉了报价的一大块？"——超过这个比例就该停下来问用户。
#
# ⚠️ **如实记录**：即使单位修对了，这条护栏在实测中**仍未触发过**
# （触发需要子任务数 ≈ 报价×0.35÷0.2，标准档约 88 个子任务）。
# 演示/说明时**不要把"预算停止"讲成已生效的能力**（见裁决 v2 §7 第 9 问）。
_COST_BUDGET_RATIO = 0.35


def node_budget_check(state: TaskState) -> dict[str, Any]:
    """成本护栏：本次成本吃掉报价的 35% 以上 → 置 flag，暂停征询用户"""
    price = state["budget_estimate"]
    spent = state["budget_spent"]
    cap = price * _COST_BUDGET_RATIO
    flag = spent > cap if price > 0 else False
    if flag:
        _trace(
            state, "budget_check",
            f"本次预算已用 {spent:.1f} 积分（本档位 {price:.0f} 积分的 {spent / max(price, 1e-9) * 100:.0f}%，"
            f"护栏 {cap:.1f} 积分）→ 征询用户是否继续",
        )
    return {"budget_flag": flag}


# ============================================================
# 节点 11：quality_check（质检 + 返工）
# ============================================================
def node_quality_check(state: TaskState) -> dict[str, Any]:
    """质检 Agent：事实核查/幻觉检测/信源完整度

    规则：BLOCK（致命项）→ 挂人工接管卡片 + 触发返工（次数<2）
    """
    from agents.quality import quality_check as run_quality, build_stop_card
    from core.decision_trace import record as _dec

    _trace(state, "quality_check", "质检: 事实核查/信源完整度")
    report = run_quality(state["report_draft"], state["evidences"])
    # Rubric 质量门 + 返工闭环（大赛评分对标）：方案设计<60% → 定位缺什么 → 强制一轮定向返工
    try:
        from core.rubric import _FRAMEWORKS, score_report

        _r = score_report(state, state.get("report_draft") or "")
        _design = next((b for b in _r.get("breakdown", []) if b["dimension"] == "方案设计"), None)
        if _design and _design["score"] < _design["max"] * 0.6:
            _draft = state.get("report_draft") or ""
            _missing = [fw for fw in _FRAMEWORKS if fw not in _draft]
            _branches = len(getattr(state.get("plan"), "problem_tree", None) or [])
            _fix = (
                f"Rubric质量门：方案设计 {_design['score']}/{_design['max']}（<60%）。"
                f"请在对应维度补上方法论框架结论并写明依据（现缺：{('、'.join(_missing)) or '—'}；"
                f"MECE分支{_branches}个），关键口径处写'置信度分级'。"
            )
            report["rule_issues"] = (report.get("rule_issues") or []) + [_fix]
            if report.get("level") != "BLOCK":
                report["level"] = "FLAG"
            # 闭环：真 LLM + 首轮质检 → 强制返工一次让 Agent 真正补齐（而非只披露）。上限一次，防死循环。
            try:
                from core.llm import has_main_llm

                _llm = has_main_llm()
            except Exception:
                _llm = False
            if _llm and state.get("rework_count", 0) == 0 and report.get("passed") is not False:
                report = dict(report)
                report["passed"] = False
                report["_rubric_rework"] = _fix
                _trace(state, "quality_check", f"Rubric 质量门触发定向返工（方案设计{_design['score']}/{_design['max']} <60%）")
    except Exception:
        pass
    passed = report.get("passed", False)

    # 【演示触发器】提问含"演示拦截"或上下文 force_block=true → 第一次质检强制 BLOCK 一次，
    # 用于稳定展示"主动停止 + 人工接管"（演示高光：异常→护栏→人工接管）。
    # 首次返工后 rework_count>=1，不再强制 → 正常质检放行。
    demo_trigger = (
        "演示拦截" in (state.get("raw_question") or "")
        or str(state.get("clarified_context", {}).get("force_block", "")).lower() == "true"
    )
    if demo_trigger and state.get("rework_count", 0) == 0:
        report = dict(report)
        report["level"] = "BLOCK"
        report["passed"] = False
        report["fatal_issues"] = (report.get("fatal_issues") or []) + [
            "【演示】质检演示拦截：模拟一处关键数据来源冲突（人工确认后可继续返工）"
        ]
        passed = False
        _trace(state, "quality_check", "【演示模式】质检强制 BLOCK → 展示人工接管卡片")

    # 【口径一致性护栏】规则层(零成本)先拦"同概念跨章节多值打架"(单店杯量/盈亏平衡杯量/
    # 客单价/现磨渗透率/人均咖啡消费量/关店率·闭店率·关闭率)。命中 → 规则披露(报告附录由
    # deliver 追加) + 口径统一指令挂进 quality_report：若本车另有返工(RedTeam高/Rubric)会一并
    # 注入；但**口径冲突本身不触发整轮 LLM 返工**——多值常因不同样本/价格带本就合法，实测重跑
    # 一轮 4 模块后同组冲突原样保留，只白烧成本。
    try:
        from core.rule_lint import lint_report as _lint2

        _cc = [v for v in _lint2(state.get("report_draft") or "") if v.get("kind") == "metric_conflict"]
        if _cc:
            _cc_msgs = [
                f"口径一致性：『{v.get('metric')}』在报告多处取值 {('、'.join(v.get('values') or []))}"
                f"（跨度 {v.get('spread')}×）。若为不同样本/价格带/年份请逐处标注口径"
                f"（全量门店/头部连锁/单城样本/价格带列举 vs 计算均价）；同一口径的裸数字不得互相矛盾："
                f"{('；'.join(v.get('snippets') or [])[:150])}"
                for v in _cc[:4]
            ]
            report = dict(report)
            report["rule_issues"] = (report.get("rule_issues") or []) + _cc_msgs
            if report.get("level") != "BLOCK":
                report["level"] = "FLAG"
            report["_consistency_rework"] = "；".join(_cc_msgs)[:700]
    except Exception:
        pass
    # 【Red Team 高风险 → 定向返工】仅"深入/定制档 首次质检 且 未被 Rubric/口径护栏先拦"时触发。
    # v0.9.44 放宽：不再限 question_type=='causal'（可行性题"能不能做/值不值得"常被判 exploratory
    # 而漏检——closure-audit P1-E）。免费/标准档语义高风险仍改为交付末尾披露+顶部红条。
    # 依据：语义性 RedTeam 高危多为"事实类幻觉"，其真相不在检索源里，全模块重跑修不掉、只白烧成本；
    # 根治靠注入层(口径锁/现实锚)；深入/定制档保留返工 = 高价值题+演示"自我纠错"闭环。
    try:
        from core.adversarial import adversarial_review as _adv3
        from core.llm import has_main_llm as _hllm3

        _dv = getattr(getattr(state.get("plan"), "depth", None), "value", "") or ""
        if (
            _hllm3()
            and _dv in ("deep", "custom")
            and report.get("passed") is not False
            and not demo_trigger
            and state.get("rework_count", 0) == 0
        ):
            _adv_issues = _adv3(state.get("report_draft") or "", state.get("raw_question", ""))
            _hi = [i for i in (_adv_issues or []) if _risk_high(i.get("risk"))]
            if _hi:
                report = dict(report)
                _red = "RedTeam高风险触发返工：" + "；".join(
                    f"“{i.get('issue','')}”→修正建议：{i.get('suggestion','')}" for i in _hi[:3]
                )
                report["fatal_issues"] = (report.get("fatal_issues") or []) + [_red[:700]]
                report["_consistency_rework"] = (report.get("_consistency_rework") or "") + "；" + _red[:700]
                report["level"] = "FLAG"
                report["passed"] = False
                _trace(state, "quality_check", f"RedTeam 高风险 {len(_hi)} 项 → 定向返工(重核关键数字)")
    except Exception:
        pass
    passed = report.get("passed", False) is not False and report.get("level") != "BLOCK"

    updates: dict[str, Any] = {
        "quality_report": report,
        "quality_passed": passed,
        "rework_count": state["rework_count"] + (0 if passed else 1),
    }
    # BLOCK → 生成人工接管卡片（培训信号：主动停止=产品能力）
    if report.get("level") == "BLOCK":
        updates["stop_card"] = build_stop_card(
            state["report_draft"],
            state["evidences"],
            report.get("fatal_issues") or [],
            state.get("budget_spent", 0),
        )
        _trace(state, "quality_check", f"发现致命项 {len(report.get('fatal_issues') or [])} 条 → 需人工接管")
        # **「主动停止」是决策链里唯一一个声明了却从没记过的决策**（2026-09-16 全量审计）。
        # 机械核对：`core.decision_trace.KINDS` 声明 11 类，`graph.py` 实际记录 10 类，
        # 缺的正是 `stop`——于是《自主决策链》附录里**永远没有"主动停止"这一节**，
        # 而前端类型表也为它留了位置（和当年 conflict_scan 一个形状：**声明了、渲染位也留了、
        # 就是没有生产者**）。偏偏"主动停止、交给人工"是赛道培训明确点名要展示的能力。
        # 注意：这里记的是**质检层**的主动停止；预算停止另有一条（尚未触发过，见说明十问 Q9）。
        _dec(
            state,
            "stop",
            f"质检判定 BLOCK（致命项 {len(report.get('fatal_issues') or [])} 条）",
            ["继续交付并标注风险", "整份作废重跑", "停下来交还人工"],
            "停下来交还人工",
            "致命项触及证据链本身（不是措辞问题），继续交付等于把不可靠的结论包装成可交付的——"
            "**主动停止是产品能力，不是故障**；接管率已进最小复购看板，可核对。",
            trigger="hybrid",
        )
    return updates


def route_after_quality(state: TaskState) -> str:
    """质检后的路由：通过→决策SOP+沉淀交付；不通过且未超限→返工修复；超限→降级交付"""
    from core.decision_trace import record as _dec

    _qr = state.get("quality_report") or {}
    _lvl = _qr.get("level") or ""
    _issues = list(_qr.get("rule_issues") or _qr.get("issues") or [])
    if state["quality_passed"]:
        return "finalize"
    if state["rework_count"] < MAX_REWORK:
        _trace(state, "quality_check", f"质检未通过，返工修复({state['rework_count']}/{MAX_REWORK})")
        _dec(
            state,
            "rework",
            f"质检未通过（{_lvl or 'FLAG'}，{len(_issues)} 项待处理）——退回重做，还是带着问题交付？",
            [
                "带问题直接交付（快，但读者会把未修的问题当结论）",
                f"退回相关子 Agent 定向重跑（第 {state['rework_count'] + 1}/{MAX_REWORK} 轮）",
                "无限返工直到通过（质量最高，但可能不收敛）",
            ],
            f"定向返工第 {state['rework_count'] + 1}/{MAX_REWORK} 轮",
            "返工是**有代价**的（时间 + 成本），所以设上限而不追求完美；"
            "但第一轮几乎总能修掉真问题，直接交付则等于把已知缺陷印给读者。"
            "两害相权：**先修一轮，修不动就如实披露**——而不是假装没检出。",
            evidence="；".join(str(i)[:50] for i in _issues[:3]) or "（未列出具体项）",
            trigger="rule",
        )
        return "rework"
    _trace(state, "quality_check", "返工达上限，降级交付（保留已完成模块，并在报告末尾如实披露致命项）")
    _dec(
        state,
        "degrade",
        f"返工已达上限（{MAX_REWORK} 轮）仍未通过——继续返工，还是降级交付？",
        [
            f"再返工一轮（超出上限，仍有 {len(_issues)} 项待处理）",
            "降级交付 + 在报告末尾如实披露未解决的致命项",
        ],
        "降级交付，并把未解决的致命项写进报告",
        "继续返工会**不收敛**（同一个问题修两轮没修掉，第三轮大概率也修不掉），"
        "而时间成本是确定的。选择降级交付的前提是：**缺陷必须对读者可见**，"
        "所以质检披露会在报告里明写『以下致命项仍未解决，请勿直接采用』。",
        evidence="；".join(str(i)[:50] for i in _issues[:3]) or "（未列出具体项）",
        trigger="rule",
    )
    return "finalize"


# ============================================================
# 节点 11.4：rework_fix（质检返工修复——不再空转重汇总）
# ============================================================
def node_rework_fix(state: TaskState) -> dict[str, Any]:
    """质检返工修复：把致命项注入相关子Agent重跑，而非简单重汇总。

    旧实现：返工只是回到 summarize 重排一次，测算参数错误不会被修正。
    新实现：
    1. 读取 quality_report.fatal_issues；
    2. 重跑"市场测算"相关子Agent（industry=市场规模测算 / statistics=单位经济），
       注入 rework_hint 让子Agent按质检意见修正表述（L5标注/口径统一/冲突并列）；
    3. 返回空 dict（不追加 evidences，避免并行reducer重复；模块报告已被覆盖）。
    之后由 summarize 用更新后的模块报告重新汇总 → budget_check → quality_check 复检。
    """
    q = state.get("quality_report") or {}
    fatal = q.get("fatal_issues") or []
    rubric = q.get("_rubric_rework") or ""      # Rubric 质量门定向补强指令（方案设计<60%）
    consistency = q.get("_consistency_rework") or ""  # 口径一致性护栏指令（同概念多值打架）
    if not fatal and not rubric and not consistency:
        _trace(state, "rework_fix", "无致命项，跳过修复直接重汇总")
        return {}
    hint = "；".join(x for x in [rubric, consistency] + fatal if x)[:700]
    _trace(state, "rework_fix", f"返工修复：致命项{len(fatal)}条 + Rubric补强{bool(rubric)} + 口径护栏{bool(consistency)} → 重跑相关子Agent")
    routing = state["subtask_results"].get("_routing", {})
    # 致命项 → 重跑 测算/行业；Rubric 方案设计 或 口径护栏 → 全维度重跑（同概念冲突常跨章节）
    agents = ("industry", "statistics") if not (rubric or consistency) else ("industry", "competition", "user", "statistics")
    _rw_trace: list = []
    _rw_spent = 0.0
    for at in agents:
        ids = routing.get(at, [])
        if ids:
            _trace(state, "rework_fix", f"重跑子Agent[{at}]（{len(ids)} 个子任务）")
            _r = _run_sub_agent(state, at, ids, rework_hint=hint) or {}
            # 返工轮**同样要记台账与预算**：`_run_sub_agent` 现在返回的是本节点增量，
            # 不接住就等于"返工跑过的那几轮从台账里消失"（原先靠原地 extend 的副作用记上，
            # 改成返回局部 list 后必须显式接住——否则修一个静默失效会造出另一个）。
            _rw_trace.extend(_r.get("source_trace") or [])
            _rw_spent += float(_r.get("budget_spent") or 0.0)
    return {"source_trace": _rw_trace, "budget_spent": round(_rw_spent, 2)}


# ============================================================
# 节点 11.5：决策SOP（决策型交付，v3 §6.7）
# ============================================================
def node_decision_sop(state: TaskState) -> dict[str, Any]:
    """决策型交付：基于证据链生成"该不该做/怎么做/预算/风险"决策清单

    这是"市场可行性"和"赛道维度·可验收性"的实锤——
    交付的不只是报告，是可执行、可查证的决策清单。
    同时生成结构化验证任务（Action Layer）：Task/Owner/Deadline/Budget/KPI/Status。
    """
    from agents.decision import (
        generate_decision_sop,
        build_validation_tasks,
        extract_verdict,
        build_monitor_conditions,
        sop_status,
    )

    _trace(state, "decision_sop", "生成决策清单（判定+验证计划+行动/成本/风险）")
    vt = build_validation_tasks(
        raw_question=state["raw_question"],
        evidence_count=len(state.get("evidences", [])),
    )
    # v0.9.43 一致性：先建结构化 vt 清单，把清单原文喂给决策 LLM，
    # 让"最小验证计划"只写执行顺序、禁止另造人数/预算（根治 30人/¥5000 vs 5-10人/¥500 双清单打架）。
    _vt_sum = (
        "\n".join(
            f"- {v.get('id', '')}｜{v.get('task', '')}｜负责人:{v.get('owner', '')}｜截止:{v.get('deadline', '')}"
            f"｜预算:{v.get('budget', '')}｜KPI:{v.get('kpi', '')}｜停止:{v.get('stop_condition', '')}"
            for v in vt
        )
        if vt
        else ""
    )
    sop = generate_decision_sop(
        raw_question=state["raw_question"],
        report=state["report_draft"],
        evidences=state.get("evidences", []),
        vt_summary=_vt_sum,
    )
    # Decision Memory + Market Monitor：判定 → 存档 + 监控条件
    verdict = extract_verdict(sop)
    industry = _extract_industry(state["raw_question"])
    conditions = build_monitor_conditions(industry, verdict)
    try:
        from core.memory import record_decision

        record_decision(
            question=state["raw_question"],
            industry=industry,
            verdict=verdict,
            monitor_conditions=conditions,
        )
    except Exception:
        pass  # 记忆写入失败不阻断交付
    _trace(state, "decision_sop", f"决策判定：{verdict} · 已存档决策记忆 + {len(conditions)} 条监控条件")
    # 降级不静默（v0.9.75）：兜底原因必须随状态走，交付层据此在报告头告警。
    _sop_st = sop_status() or {}
    _sop_fb = (
        {"reason": _sop_st.get("reason", ""), "detail": _sop_st.get("detail", "")}
        if _sop_st.get("reason")
        else {}
    )
    if _sop_fb:
        _trace(state, "decision_sop", f"⚠️ 决策清单降级为兜底模板（原因：{_sop_fb['reason']}）")
        try:
            from core.decision_trace import record as _rec

            _rec(
                state,
                "fallback",
                "决策清单生成环节未能走通真实模型路径",
                ["用兜底模板交付（快，但结论不是基于证据链的个性化判定）", "中止交付并报错"],
                "用兜底模板交付，同时在报告头显式告警",
                _sop_fb.get("detail") or _sop_fb.get("reason", ""),
                evidence="兜底输出在报告内自带『未调用大模型』说明，不冒充个性化判定",
                trigger="rule",
            )
        except Exception:
            pass
    return {
        "decision_sop": sop,
        "validation_tasks": vt,
        "monitor_conditions": conditions,
        "decision_verdict": verdict,
        "sop_fallback": _sop_fb,
    }


# ============================================================
# 节点 12：资产沉淀 + 交付
# ============================================================
def node_asset_sink(state: TaskState) -> dict[str, Any]:
    """资产沉淀：把证据/结论写入行业档案库（跨任务复用）

    资产飞轮：每次调研的证据入库，下次同行业调研先查资产库复用，
    越用越懂该行业（护城河）。
    """
    from core.rag import add_asset

    _trace(state, "asset_sink", "沉淀证据到行业档案库")

    # 行业标签：**认不出就不落卡**（2026-09-15 外部审计）。
    # 此前这里有个"取提问前 4 个汉字"的兜底，产出了 `市场目前` 这种键——
    # 它既不是行业，也永远不会被检索侧命中。**编一个没人搜得到的键，等于把资产丢了还记账。**
    # 认不出时的正确行为是：不沉淀，并留一条日志（可核对"这次没落卡是因为没认出行业"）。
    industry = _extract_industry(state["raw_question"])
    if not industry:
        __import__("logging").getLogger(__name__).info(
            "资产沉淀跳过：未能从提问识别行业（不编造行业键）"
        )
        return {"assets_added": 0, "asset_industry": "", "asset_skipped": "industry_unrecognized"}

    added = 0
    _seen_assets: set[tuple[str, str]] = set()  # 同任务查重：同文镜像/同正文不再重复入库
    try:
        from core.source_util import canonical_key as _asset_ck
    except Exception:
        _asset_ck = None
    for ev in state.get("evidences", []):
        src = ev.sources[0] if ev.sources else None
        if not src:
            continue
        # 沉淀前清洗：只存真实内容，不残留"历史资产/联网检索"标签与查询词回显
        # （防止脏资产二次检索污染后续报告）
        from agents.researchers import _strip_data_labels, _strip_query_echo

        excerpt_clean = _strip_query_echo(_strip_data_labels(src.excerpt))
        if len(excerpt_clean) < 30:
            continue  # 无有效正文的资产不入库
        # 查重键：规范URL（m./www./pvid 同文）；URL 无效时用正文前60字兜底
        _key: tuple[str, str] = ("", "")
        if _asset_ck and src.url:
            _key = _asset_ck(src.url)
        if not _key[0]:
            _key = ("", excerpt_clean[:60])
        if _key in _seen_assets:
            continue  # 同一文章（镜像双链/多模块重复命中）本次只入库一次
        _seen_assets.add(_key)
        if add_asset(
            industry=industry,
            content=excerpt_clean,
            source_type="evidence",
            source_url=src.url,
            source_title=_strip_data_labels(src.title),
        ):
            added += 1

    _trace(state, "asset_sink", f"已沉淀 {added} 条证据到「{industry}」行业档案")
    return {"status": "done"}


def _extract_industry(raw_question: str) -> str:
    """从提问提取行业关键词（简版：取首个行业词）

    完整版应由 LLM 提炼，此处做规则兜底。
    """
    import re

    # 常见行业词表（可扩展）
    # **唯一实现**：不在本文件另抄一份词表（2026-09-15 外部审计）。
    # 病案：这里原本抄了第二份行业词表，并带一个兜底——"取提问前 4 个汉字"。
    # 那个兜底把「2026年国内现制咖啡连锁在下沉市场还有多少空间」的前 4 个汉字
    # 取成了 **「市场目前」**，于是 `industry_kb.json` 里长出了
    # `市场目前` / `万级电车` / `星巴克中` 这类垃圾键；
    # 而**检索侧用的是 13 词白名单** → 这些卡沉下去之后**永远搜不到**。
    # 一处沉淀、一处检索，两个函数各自演化 = 资产飞轮空转。
    # 现在收口：两边都走 `agents.researchers._extract_industry`，
    # **认不出就返回空串**（而不是编一个"看起来像行业"的键）。
    try:
        from agents.researchers import _extract_industry as _canon_ind

        return _canon_ind(raw_question) or ""
    except Exception:
        return ""


def _build_methodology_note(state: TaskState, body: str = "") -> str:
    """报告末尾"数据与方法说明"：诚实标注一手数据缺失 + 调研方法论

    企业级专业度的体现：主动交代数据边界与建议，不假装一手调研。

    `body`：**本节之前已经成稿的正文**。只为算假设集指纹用（见 `core/assumption_set`）——
    指纹必须覆盖《市场规模测算》与《单位经济》两节，而那两节在这节**之前**写好了。
    不传 body 就退回不打印指纹行（**不印一句"取不到"的空话**）。
    """
    from core.skills import get_skill_templates, framework_info

    skill = ""
    templates = get_skill_templates(skill)
    used_frameworks = []
    for st in state["plan"].subtasks:
        # 从子任务描述里取框架名（含完整括号，如"市场规模测算（TAM/SAM/SOM）"）
        import re

        m = re.search(r"（方法论：(.+?)，", st.description)
        if m:
            fw = m.group(1).strip()
            if fw and fw not in used_frameworks:
                used_frameworks.append(fw)
    frameworks_txt = "、".join(used_frameworks) if used_frameworks else "PEST / 波特五力 / STP / TAM-SAM-SOM / 单位经济"
    # 假设集指纹（2026-09-18 第十跑）：`prompt_set` 管判据，它管假设——两件事。
    # 九跑与十跑 `prompt_set` 都是 72c514f9，而 BEP 从 222 跳到 242、TAM 差 28.6%。
    # 没有这一行，"同版本两套数"在报告里**一个字都看不出来**。
    _afp = ""
    try:
        from core.assumption_set import describe as _as_desc
        from core.assumption_set import latest_recorded as _as_prev

        # 传账本里最近一笔的指纹进去：**只印"本跑是这一套"没用**，
        # 真实读者只拿到一份报告，得在这一份里就告诉他"这跟上一笔不是同一套"。
        _afp = _as_desc(body or "", _as_prev())
    except Exception:
        _afp = ""
    _afp_block = (f"\n{_afp}\n" if _afp else "")
    return f"""

---

# 📊 数据与方法说明

**调研方法论**：本报告采用咨询级分析框架 —— {frameworks_txt}，数据经多源交叉验证并标注来源（L1 官方统计/监管 → L2 法定披露/交易所 → L3 行业机构/协会 → L4 市场发现/媒体 → L5 用户声音/社媒）。

**数据边界（诚实声明）**：
- 本报告基于公开二手数据（政府统计、行业报告、媒体报道、企业财报）生成，**未包含一手调研（问卷/访谈/实地）**；
- 市场规模等测算为"估算值"，供决策参考，落地前建议补充一手验证；
- 若需进一步精准，建议执行：**① 目标客群问卷（样本≥300） ② 竞品实地暗访 ③ 供应链访谈** —— 研策可协助设计并分析。

**可验证性（边界，D-7）**：关键结论可溯源到真实来源并支持点开核验；被标记"待核"的数字如实就地标注、不进入决策锚；冲突数据已自动仲裁并列。不承诺报告内每个数字均可点。
{_afp_block}"""


_SUMMARY_METRICS_PROMPT = """你是"研策"的首席分析师。基于下面这份行业调研报告，输出两部分（只输出这两部分 markdown，不要其他内容）：

## 📌 执行摘要（一页纸，麦肯锡 SCR 结构）
- **第一句直接给核心结论**（Situation：市场现状 1-2 句 → Complication：变化/痛点 1-2 句 → Resolution：该不该做+怎么做，占主体）
- 用不超过 300 字，给决策者讲清：这个市场能不能做、机会在哪、最大风险是什么
- 必须包含至少 3 个关键数字（标注来源机构）
- 给 3 条**行动式建议**（每条=一个完整句子的结论，如"建议优先选址社区/办公楼下店，承接 4.1 次/周的高频刚需"而非"考虑选址问题"），每条标注"依据：报告[章节名]"

## 📊 核心指标一览表
- 从报告中提取最核心的 5-8 个指标，输出 markdown 表格，列为：指标 | 数值 | 来源机构 | 数据级别 | 年份 | 备注
- 数据级别用 **与数据源注册表同一套定义**（`core/source_registry.LAYERS`）标注：L1官方统计/监管 · L2法定披露/交易所 · L3行业机构/协会 · L4市场发现/媒体 · L5用户声音/社媒
- **来源机构列只能写本次实际取回的来源里出现过的机构名或域名 + 对应年份**（本次清单见 system 末尾的「实际取回」一节）；**清单里没有的机构一律写「待核」**——不要凭记忆填。**禁止使用**"宏观经济数据/行业数据/行业基线/联网搜索/行业观察/综合整理"等泛化标签——拿不到机构名的指标不要放进表，或来源写"待核"

数值规范（最重要，违反即为错误）：
1. 所有数值与信源级别必须**逐字来自报告原文**，禁止改写、四舍五入、重新估算、换算单位
2. 报告原文没有的指标不要放进来；拿不准数值的来源不要写
3. 摘要与指标表必须与正文完全一致，禁止出现正文没有的数字
4. 正文关键数字若没有任何机构背书，摘要/指标表里必须标注"(估算/待核)"，不得冒充已核数据
5. 同概念数字（单店日均杯量/渗透率/客单价/盈亏平衡杯量/人均咖啡消费量/关店率）若正文多处出现，须与正文口径一致，禁止摘要拿一个、正文拿另一个
6. **口径修正纪律**：摘要不得出现"原口径偏差，自行修正/校准为…"这类自我修正叙事；机构口径不一致时应并列两值并注明差异与采信理由，否则取保守口径并标注
8. **来源资格纪律（企业微观数据不得挂政府来源）**：政府网站/统计局/部委只发布**宏观、政策与统计口径**，
   绝不会发布某家企业的门店数、单店日销、客单价、市占率等**经营微观数据**。此类数字的来源机构只能写
   企业法定披露（巨潮/SEC/年报）、券商研报或行业协会；**拿不到就写"待核"**，严禁写成 gov.cn / 国家统计局
   并标 L1官方——那比无来源更危险（评审一眼看穿）。
7. **法规资格断言纠偏（防"冷食专间式"误读）**：报告正文若称"现制/自制饮品门店被强制要求独立冷食专间、约X%商铺无法过审"——此为法规误读（《食品经营许可和备案管理办法》中"自制饮品制售"与"冷食类食品制售"是并列独立许可项目，纯现制饮品仅需专用操作区域；冷食专间只对兼售凉菜/现切果品等冷食的经营提出）。摘要与指标表**不得采信或传播该断言**，也不得把"43%商铺无法过审"之类派生结论写进行动建议/关键指标；若正文含此误读，摘要应略去该结论，指标表中该项来源标"待核"而非机构名。
9. **取数纪律（给你的摘录已覆盖全文）**：摘录已遍历**全部章节**，`[章节名]` 前缀标明该行出自哪一章。
   请优先从「市场规模测算」「竞争格局」「目标市场」「单位经济」这类**分析章**的表格里取指标，
   不要只从开头的摘要/方法段落里凑数；执行摘要里的每一个数字都必须在摘录中找得到出处。

"""


# 决策链占位符（2026-09-17）：渲染要等**所有 record 完成**，但这一节在报告里的**位置**
# 必须留在附录链的原地——所以先插槽、后回填。见 `node_deliver` 里"渲染早于记录"那段病案。
_DEC_SLOT = "\n\n<!--YANCEC-DECISION-CHAIN-SLOT-->\n\n"


def _inject_subject_tension(md: str) -> str:
    """把「主体级反证」提醒从报告头部**再钉一份到结论处**（2026-09-17）。

    病案（元气森林跑）：补检抓到《元气森林 2025 年业绩同比增长 26%》，
    而正文结论正建立在"元气森林线下失速（-6.3%，2024Q1）"之上。
    提醒确实印出来了——但印在**报告头部**，而读者（和复核者）真正会读的是
    **执行摘要那句结论**。两者相距几百行，**结构上没人会把它们连起来**。
    这不是措辞问题，是位置问题：同一份报告里"结论说跌、头部说涨"，
    中间没有任何一句话把它们对上。

    做法与决策链占位符同款：`core.freshness` 命中主体级反证时打一个哨兵，
    这里把它换成一句话，插在「## 📌 执行摘要」标题之后、正文之前。
    纯字符串操作、幂等（哨兵换掉就没了）、失败原样返回。
    """
    text = md or ""
    try:
        from core.freshness import SUBJECT_TENSION_SENTINEL as _SENT

        if _SENT not in text:
            return text
        _lines = [ln for ln in text.split("\n") if "🔺🔺" in ln]
        text = text.replace(_SENT, "")
        _head = "## 📌 执行摘要"
        _i = text.find(_head)
        if _i < 0:
            return text
        _e = text.find("\n", _i)
        if _e < 0:
            return text
        _note = (
            "\n> ⚠️ **本结论的前提可能已被近一年动态改变（系统级提示，非模型判断）**："
            "本报告的时效补检检索到**直接涉及本案主体的近期线索**，"
            "而那次检索发生在正文成稿之后——**这些线索不可能已经进入下面的分析与结论**。"
            "**引用本结论前，请先点开下列线索确认前提是否仍然成立**：\n"
            + "\n".join(
                f">\n> {ln.strip().lstrip('>').strip()}" for ln in _lines[:3]
            )
            + "\n"
        )
        return text[:_e + 1] + _note + text[_e + 1:]
    except Exception as _e2:  # 绝不阻断交付
        logging.getLogger(__name__).warning("主体级反证提醒注入失败（不阻断）: %s", _e2)
        return md


def _clip_number_label(s: str, limit: int = 12) -> str:
    """把 RedTeam 的数字标签裁到横幅里放得下的长度——**但不许从数字中间切断**。

    2026-09-17 实跑病案：原写法是裸 `_nm[:12]`，把 `28.3% vs 12.7%` 切成
    「**28.3% vs 12.**」、把 `527万辆 vs 327.6万辆` 切成「**527万辆 vs 327**」——
    **正好切在数字中间**，读者看到的是一个不存在的数（"12." 和 "327"）。
    横幅是给人 30 秒扫一眼用的，它上面的数字必须**要么完整、要么明确省略**。

    判据（按优先级）：
      ① 放得下 → 原样
      ② 含 ` vs ` 的复合项 → 保留第一个完整组件 + ` vs …`（复合项的信息在"有冲突"这件事上）
      ③ 其余 → 按长度裁，但**回退到最后一个完整数字的末尾**（不留下 `12.` 这种半截）
    """
    t = str(s or "").strip()
    if len(t) <= limit:
        return t
    if " vs " in t:
        head = t.split(" vs ", 1)[0].strip()
        return f"{head} vs …" if len(head) + 5 <= limit else _clip_number_label(head, limit - 5) + " vs …"
    cut = t[:limit]
    # 回退：结尾若是数字的一部分（数字/小数点/千分位逗号），一路退到它前面
    m = re.search(r"[\d.,]+$", cut)
    if m and m.start() > 0:
        cut = cut[: m.start()]
    cut = cut.rstrip("，,、：:；;·-— ")
    return (cut + "…") if cut and cut != t else (cut or t[:limit])


def _layers_legend() -> dict[str, str]:
    """信源层级图例 —— **唯一出处是 `core.source_registry.LAYERS`**。

    2026-09-17 新增。此前报告正文的图例是手抄的，且抄的是**另一套词**
    （L2 写成"媒体与咨询"），而同一份报告随附的可复算审计包印的是注册表那套
    （L2 = 法定披露/交易所）——**同一份交付物里同一个标签两个意思**。
    这里改成派生，是为了让"两处写同一件事"这件事**结构上不可能再发生**：
    以后要改层级定义，只改 `LAYERS` 一处，报告与审计包同时跟上。
    """
    try:
        from core.source_registry import LAYERS

        return dict(LAYERS)
    except Exception:
        # 拿不到注册表时**不编一套**——宁可少印图例，也不印一套可能与审计包打架的词。
        return {}


def _retrieved_source_brief(state: TaskState, cap: int = 40) -> str:
    """本次**真的取回来**的来源清单（给摘要/指标表提示词用）。

    这是"来源落地净化"的**根因侧**：`_SUMMARY_METRICS_PROMPT` 里那条
    「来源机构列只能写报告正文里出现的可核验机构名（如 艾瑞咨询、欧睿国际…）」
    ——它要求"可核验"，却**没给模型一份"本次到底取回了哪些"的清单**，
    于是模型只能凭记忆填机构名。实测（2026-09-18）：一份报告的表里 8 行有 6 行写成
    `Euromonitor International` / `艾瑞咨询`，而全文检索来源里
    `euromonitor` / `iresearch` 出现 **0 次**。
    **规则写了但没给执行条件 = 没规则**（本仓库第 N 次踩同型）。
    """
    import re as _re

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for k, v in (state.get("subtask_results") or {}).items():
        if not str(k).endswith("__citations") or not isinstance(v, list):
            continue
        for c in v:
            if not isinstance(c, dict):
                continue
            u = str(c.get("url") or "")
            _m = _re.search(r"https?://([^/]+)", u)
            _dom = _m.group(1) if _m else ""
            _name = str(c.get("source") or c.get("publisher") or "").strip()
            _title = str(c.get("title") or "").strip()[:44]
            _key = (_name or _dom or _title).lower()
            if not _key or _key in seen:
                continue
            seen.add(_key)
            rows.append((f"{_name}（{_dom}）" if _name and _dom else (_name or _dom), _title))
            if len(rows) >= cap:
                break
        if len(rows) >= cap:
            break
    if not rows:
        return ""
    _lines = "\n".join(f"- {a}｜{b}" for a, b in rows if a or b)
    return (
        "\n\n【本次**实际取回**的来源清单（来源机构列只能从这里选）】\n"
        f"{_lines}\n"
        "**硬约束**：核心指标表的「来源机构」列，只能填**上面清单里出现过的**机构名或域名。\n"
        "清单里没有的机构，**一律写「待核」**——即使你在训练数据里见过它、即使它看起来很权威。\n"
        "把你想写但不在清单里的机构名，放到「备注」列并注明「（来源未取回）」。\n"
        "理由：读者会拿这一列去核。填一个本次从没访问过的机构名，就是伪造可核验性。\n"
    )


def _build_summary_and_metrics(
    report: str, question: str, ctx: dict | None = None, sources_brief: str = ""
) -> str:
    """生成"一页纸执行摘要 + 核心指标一览表"（报告开头，提升决策阅读体验）

    一次 LLM 调用生成两块；失败回退为空（不影响主报告交付）。
    ctx：澄清上下文（用于决策视角锁定，防止摘要把主体写偏）。
    sources_brief：本次实际取回的来源清单（见 `_retrieved_source_brief`）——
        不给它，模型就会凭记忆填机构名。
    """
    if not report:
        return ""
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm():
            return ""
        # 决策视角锁定（v0.9.42）：摘要与各章节共用同一决策主体，
        # 防"摘要=高端品牌口吻、章节=新进入者视角"的主体漂移。
        _vp = ""
        try:
            from core.viewpoint import viewpoint_block

            _vp = viewpoint_block(question, ctx)
        except Exception:
            _vp = ""
        system = _SUMMARY_METRICS_PROMPT
        if sources_brief:
            system += sources_brief
        if _vp:
            system += "\n\n【研究视角锁定（执行摘要与指标表必须遵守，正文各章节同一视角）】\n" + _vp
        llm = get_main_llm(temperature=0.3, max_tokens=2200)
        # 全文「关键数字摘录」（core.report_html.build_number_digest）：
        # 原来只喂 `report[:4500]`，而正文实测 124,000 字符 → 模型只看到 **3.6%**
        # （方法章 + 第一章开头），却被要求"从报告中提取最核心的 5-8 个指标"。
        # 摘要与指标表的数字因此缺正文依据——这是"同一指标两套数"在摘要侧的入口。
        try:
            from core.report_html import build_number_digest

            _view = build_number_digest(report, budget=14000)
        except Exception as _e:
            logging.getLogger(__name__).warning("关键数字摘录失败，退回开头截断: %s", _e)
            _view = report[:4500]
        resp = llm.invoke(
            [
                ("system", system),
                (
                    "human",
                    f"用户问题：{question}\n\n"
                    f"调研报告全文的关键数字摘录（**已覆盖全部章节**，含各章表格；"
                    f"`[章节名]` 前缀表示该行所属章节）：\n{_view}",
                ),
            ]
        )
        content = (resp.content or "").strip()
        content = _fix_metric_source_qualification(content)
        # 正文背书校验：指标表里的数值必须在正文里**真的出现过**。
        # 提示词早就写了"所有数值必须逐字来自报告原文"，但写了规则没人检查 = 没规则
        # （和"政府来源背书企业微观数据"是同一类）。这里把它变成执行机制：找不到同值的行
        # 如实标「待核（正文未见同值数字）」，不擅自改数值、不改来源。
        try:
            from core.report_html import annotate_unbacked_metrics

            content, _unbacked = annotate_unbacked_metrics(content, report)
            if _unbacked:
                logging.getLogger(__name__).info(
                    "核心指标表：%d 项在正文中找不到同值数字，已标待核 → %s",
                    len(_unbacked), "、".join(_unbacked[:6]),
                )
        except Exception as _e:
            logging.getLogger(__name__).warning("指标表正文背书校验跳过: %s", _e)
        return content if len(content) > 60 else ""
    except Exception:
        return ""


# 泛化/内部标签：写得像来源，其实无从核验（提示词早就禁止，但此前**没有执行机制**）
# 源路由审计表最多列几行。**超出的必须自陈**（见 `_build_source_appendix` 里的省略行）——
# 病案（2026-09-15 深访实跑）：抬头写"16 个维度 / 未命中 2"，表里却只有 12 行、
# 数得出 1 个 ⚠️，第二个未命中躲在被切掉的行里。**读者数不出来 = 这个数无法被核对。**
_ROUTE_ROWS = 12
# 仲裁结论的枚举 → 人话。**这一列是"规则层公开可对账"那句话的证据位**，
# 印 `adopt_winner` 等于让读者去看我们的变量名（2026-09-16）。
# 取值来源：`core/models.py` 的 `arbitrate`（自测扫它，漏一个就红）。
_ARBITRATION_ZH = {
    "adopt_winner": "取信源更强者（四维权重 40/20/15/25 可对账）",
    "show_both": "并列展示、不替用户下结论（分歧如实标注）",
}
_GENERIC_SRC = (
    # ⚠️ **「反方检索」这一族必须收全词形**（2026-09-17 实跑病案）：
    # 原表只有「反方检索**数据**」，而报告里出现的是「反方检索**资料**」——
    # **差一个字，整条漏过**，那张表上就印着「县域门店盈亏平衡安全阈值月销 | 28台 | 反方检索资料」，
    # 而同一张表下面还挂着一条"来源标注修正"提示、举的例子恰恰是「反方检索数据」。
    # **提示在说自己修了一类它其实没修的东西。**
    # 本仓库第三次踩"只收一种词形＝没收"（前两次：`节\b` 中文词边界、`_SEC_CNT` 只收「章节」）。
    # 这次连**另一张表**（`core/industry_kb._LOW_TRUST`）都早就收全了——同一件事三张表、三种内容。
    "反方检索资料", "反方检索数据", "反方检索", "反方观点", "反方",
    "实时检索数据", "实时检索", "网络检索", "联网搜索", "检索数据",
    "行业数据", "宏观经济数据", "行业基线", "历史基线", "基线数据",
    "行业观察", "综合整理", "数据整理",
    "测算结果", "本报告测算", "内部假设", "内部估算", "L5估算", "系统清单",
)
# 其中属于"我们自己算的"那一类：坦白说「本报告测算」，比挂一个假机构名诚实
_SELF_COMPUTED = ("测算结果", "本报告测算", "内部假设", "内部估算", "L5估算")


def _annotate_ungrounded_metric_sources(md: str, citations: list | None) -> str:
    """核心指标表**来源落地净化**：点名的机构若不在本次**真的取回来**的来源里 → 就地标注。

    病案（2026-09-18 实跑）：《核心指标一览表》8 行里 6 行的来源机构是
    `Euromonitor International` 与 `艾瑞咨询`，而这份报告全文的检索来源只有 11 个域名，
    `euromonitor` / `iresearch` 出现 **0 次**。
    根因在**生成侧**：`_build_summary_and_metrics` 的提示词写着
    「来源机构列只能写报告正文里出现的可核验机构名（如 艾瑞咨询、国家统计局、欧睿国际…）」
    —— 它要求"可核验"，却**没给模型一份"本次到底取回了哪些"的清单**，
    于是模型凭记忆填机构名。**规则说了，但没有执行机制**（本仓库第 N 次踩同型）。

    这里做**确定性就地标注**（不重跑 LLM）：把机构名保留、加一句「本次未取回」，
    同时把数据级别降为「待核」——理由同类 #1：**挂着一个本次从没访问过的机构名，
    比老实标「待核」更危险**，因为它看起来是可核验的。

    ⚠️ **它不判定"这个机构是编的"**——机器分不清两种可能：
      ① 数字其实来自某个已取回页面**转述**了该机构（如界面新闻转引艾瑞）——名字填错了，数字可追溯；
      ② 机构名只是**模型记忆**——那是来源幻觉。
    所以标注用词是中性的「本次未取回」，并由下方提示语把两种可能**都告诉读者**。
    这也是它没有阻断权（`core.rule_lint` R12 是 `warn`）的同一个理由。
    """
    try:
        import re as _re

        from core.rule_lint import _FILING_HOSTS, _FILING_WORDS, _ORG_DOMAINS, _ORG_NON_SOURCE
        from core.rule_lint import _retrieved_corpus as _corpus_of
    except Exception:
        return md
    _corpus = _corpus_of(citations)
    if not _corpus:
        return md  # 没有台账就无从核 —— **不猜、不动**
    try:
        m = _re.search(r"^##\s*📊\s*核心指标一览表\s*$(.*?)(?=^##\s|\Z)", md or "", _re.M | _re.S)
        if not m:
            return md
        _block = m.group(1)
        _new: list[str] = []
        _hits: list[str] = []
        # 来源已是「待核」而级别仍写着 L1–L5 的行（见下方病案）——单独记，单独披露。
        _level_fixed: list[str] = []
        # **表头行必须跳过**（2026-09-18 首次运行就踩中）：「来源机构」「数据级别」这两个**列名**
        # 与"点名了一个没取回的机构"在结构上完全同形——不跳，表头会被改成
        # `| 指标 | 数值 | 来源机构（⚠️本次未取回） | 待核 | …`，整张表的列名当场作废。
        _HEADER_WORDS = ("指标", "数值", "来源机构", "来源", "数据级别", "级别", "年份", "备注", "说明")
        for ln in _block.split("\n"):
            s = ln.strip()
            if not s.startswith("|"):
                _new.append(ln)
                continue
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            if len(cells) < 4 or set("".join(cells[:2])) <= set(":- ") or any(
                c in _HEADER_WORDS for c in cells[:4]
            ):
                _new.append(ln)
                continue
            _o = cells[2]
            # **幂等**：已标注过的直接跳过。不跳的话第二次跑会叠成
            # 「艾瑞咨询（⚠️本次未取回）（⚠️本次未取回）」——本仓库对"非幂等修正器"有前科。
            if not _o or "本次未取回" in _o or any(k in _o for k in _ORG_NON_SOURCE):
                # ── 2026-09-18 第十跑病案：**来源已是「待核」，级别却还写着 L5** ──
                # 原实现到这里整行跳过 —— 把级别列一起跳过了。实测后果：
                #   `| 下沉市场单店日均杯量中位数 | 92 杯 | 待核 | **L5用户声音/社媒** | 2026 |`
                # 而同一行在第九跑写的是 `L3`。同一份基线数据，跨跑换了两个层级，
                # 而两个都不是核对出来的——**来源既然没有，就没有任何依据说它有层级。**
                # 这与本节开头那条理由同源（挂一个看起来可核验的层级，比老实写"待核"更危险），
                # 只是入口不同：那条防的是"机构名编的"，这条防的是"层级编的"。
                # **不是判它错**，是判它**不可能被核对**。
                if len(cells) >= 4 and _re.match(r"^\s*L[1-5]", cells[3] or ""):
                    cells[3] = "待核"
                    _new.append("| " + " | ".join(cells) + " |")
                    _level_fixed.append(cells[0])
                    continue
                _new.append(ln)
                continue
            _ol = _o.lower()
            if _ol in _corpus:
                _new.append(ln)
                continue
            if any(w.lower() in _ol for w in _FILING_WORDS) and any(h in _corpus for h in _FILING_HOSTS):
                _new.append(ln)
                continue
            _grounded = False
            for _k, _doms in _ORG_DOMAINS.items():
                if _k.lower() in _ol:
                    _grounded = any(d in _corpus for d in _doms)
                    break
            if _grounded:
                _new.append(ln)
                continue
            cells[2] = f"{_o}（⚠️本次未取回）"
            cells[3] = "待核"
            _new.append("| " + " | ".join(cells) + " |")
            if _o not in _hits:
                _hits.append(_o)
        if not _hits and not _level_fixed:
            return md
        _note = (
            f"\n\n> ⚠️ **来源落地修正（{len(_hits)} 个机构）**：上表中有机构的名称"
            f"**不在本次实际取回的来源清单里**（本次命中：「{'」「'.join(_hits[:3])}」），"
            "已就地标注并降级为「待核」。"
            "**这不等于该机构是编的**——两种可能：① 数字来自某个已取回页面**转述**了该机构，"
            "那是**名字填错了**，应改填你真正打开的那一页；"
            "② 该机构名只是**模型记忆**里的，本次从未访问过。"
            "**报告不印一个自己说不清来路的来源机构**：请按上述两种可能之一改写，"
            "或补一次该机构的定向检索把真实来源页并进台账。"
            if _hits else ""
        )
        if _level_fixed:
            _note += (
                f"\n>\n> ⚠️ **数据级别修正（{len(_level_fixed)} 行）**：这些行的来源列本来就已经是"
                f"「待核」，**级别列却仍写着 L1–L5**（本表：「{'」「'.join(_level_fixed[:3])}」）。"
                "来源既然没有，就没有任何依据说它有层级——**级别已一并降为「待核」**。"
                "（这不是断言原层级写错了，是断言它**不可能被核对**："
                "挂着 `L5 用户声音/社媒` 却没有任何一个社媒来源，读者无从校验，"
                "而它看起来比「待核」可信得多。）"
            )
        # 提示语**必须紧贴在表的最后一行之后**（2026-09-18 首次运行就踩中两次）：
        # 第一次挂到了全文末尾（读者在表上看到「⚠️本次未取回」，翻三千行才找到解释）；
        # 第二次挂在"这一节的末尾"，而节末还可能隔着 `---` 与下一节标题。
        # 正确的落点只有一个：**表格数据行的下一行**。
        _last = max((i for i, l in enumerate(_new) if l.lstrip().startswith("|")), default=-1)
        if _last < 0:
            return md
        _new.insert(_last + 1, _note)
        return (md or "")[: m.start(1)] + "\n".join(_new) + (md or "")[m.end(1):]
    except Exception:
        return md


def _fix_metric_source_qualification(md: str) -> str:
    """核心指标表**来源资格净化**：政府/宏观来源不得背书企业微观经营数据 → 就地改「待核」

    病案（2026-09-11 真实报告）：核心指标表出现
        | 三线城市星巴克门店平均日销 | 8,200元 | gov.cn | L1官方 | 2026 |
    中国政府网**不可能**发布某家企业的门店日销——挂着"L1官方"比无来源更危险，
    复核者里任何懂行的人一眼看穿。归因要么是检索内容被错挂到政府域名，要么是模型编的，两者都必须拦。

    这里做**确定性就地修正**（不重跑 LLM）：命中即把来源与级别改成「待核」，
    并在表下如实说明——宁可让这一行显得"没来源"，也不能让它冒充 L1 官方。
    """
    try:
        from core.rule_lint import _GOV_SRC, _MICRO_OPS
    except Exception:
        return md
    lines = (md or "").split("\n")
    out: list[str] = []
    n_fixed = 0
    n_generic = 0
    # 记下**实际命中**的词，供提示语引用（2026-09-17）。
    # 原先提示语里的例子是**硬编码散文**（「反方检索数据」「行业数据」「联网搜索」），
    # 于是出现一个难看的局面：**提示在举一个它其实没在查的例子**——
    # 那一跑的表里明明写着「反方检索资料」，却因为词表只收了"数据"而漏过。
    # 例子从命中里取，提示就不可能再比实际做的多。
    _hits_seen: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if not (s.strip().startswith("|") and s.count("|") >= 4):
            out.append(s)
            continue
        cells = [c.strip() for c in s.strip().strip("|").split("|")]
        if len(cells) < 4:
            out.append(s)
            continue
        _metric = " ".join(cells[:2])
        _src = cells[2] if len(cells) > 2 else ""
        if any(g in _src for g in _GOV_SRC) and any(k in _metric for k in _MICRO_OPS):
            cells[2] = "待核（政府来源不覆盖企业微观数据）"
            if len(cells) > 3:
                cells[3] = "待核"
            out.append("| " + " | ".join(cells) + " |")
            n_fixed += 1
            continue
        # 第二类：**泛化/内部标签**当来源机构。
        # 实测报告(10)：来源机构列写着「反方检索数据」——那是我们内部的**取证通道名**，
        # 不是机构名；和「RedTeam待核」是同一类泄露：读者看到的是工序，不是出处。
        # 提示词早就禁止泛化标签（"宏观经济数据/行业数据/联网搜索…"），但**没有执行机制**。
        # 只在**短单元格**上判：`国家统计局行业数据库` 这种正经机构名含"行业数据"子串，
        # 不做长度限制就会把真机构当成泛化标签误改成"待核"。
        _hit = next((g for g in _GENERIC_SRC if g in _src), None) if len(_src) <= 14 else None
        if _hit:
            # 自己算出来的就老实说"本报告测算"，别伪装成机构；其余一律"待核"
            cells[2] = "本报告测算" if _hit in _SELF_COMPUTED else "待核"
            if len(cells) > 3 and _hit not in _SELF_COMPUTED:
                cells[3] = "待核"
            out.append("| " + " | ".join(cells) + " |")
            n_generic += 1
            if _hit not in _hits_seen:
                _hits_seen.append(_hit)
            continue
        out.append(s)
    res = "\n".join(out)
    if n_generic:
        # 例子**从实际命中里取**，不写死。写死过一次，结果提示举的那个例子
        # 恰是词表没覆盖的写法（"反方检索数据" vs 表里的"反方检索资料"）。
        _eg = "」「".join(_hits_seen[:3]) or "内部通道名"
        res += (
            f"\n\n> ⚠️ **来源标注修正（{n_generic} 项）**：上表中有 {n_generic} 项的「来源机构」"
            f"原写的是**内部通道名或泛化标签**（本次实际命中：「{_eg}」）——"
            "那不是机构名，读者无从核验。已改为「待核」；本报告自行测算的指标改标「本报告测算」"
            "（坦白是自己算的，比挂一个假机构名诚实）。"
        )
    if n_fixed:
        res += (
            f"\n\n> ⚠️ **来源资格修正（{n_fixed} 项）**：上表中有 {n_fixed} 项「企业微观经营数据」"
            "（门店数/单店日销/客单价等）原被挂到**政府来源**下。政府网站与统计部门只发布宏观、"
            "政策与统计口径，**不会发布某家企业的经营微观数据**——已就地改为「待核」。"
            "此类数字的正确来源是企业法定披露（巨潮/SEC/年报）、券商研报或行业协会。"
        )
    return res


def _build_quality_note(state: TaskState) -> str:
    """报告末尾"质检与可验证性"披露（质控层：发布前返工清单的公开结果，分级 FLAG/PASS）"""
    q = state.get("quality_report") or {}
    if not q:
        return ""
    level = q.get("level", "FLAG")
    ev_count = q.get("evidence_count", 0)
    rule_issues = q.get("rule_issues", [])
    url_missing = [i for i in rule_issues if "无真实URL" in i]
    others = [i for i in rule_issues if "无真实URL" not in i]
    # **LLM 复核的结论此前一个字都没披露**（2026-09-15 外部审计）：
    # `_llm_check` 产出了 `llm_issues`，它只经 `all_issues` 把等级抬成 FLAG，
    # 而 `_build_quality_note` 全程只渲染 `rule_issues` ——
    # 于是用户看到的是"共 N 条证据；M 项提示"（M 只数规则层），
    # **LLM 实际检出的问题全部不可见**。这属于本仓库反复出现的
    # 「记录 → 消费 → 披露」断链：算了、存了、没人看。
    llm_issues = q.get("llm_issues") or []
    _cov = q.get("llm_coverage") or {}
    lines = ["\n\n---\n\n# ✅ 质检与可验证性"]
    if level == "PASS":
        lines.append(f"**自动质检**：通过（共 {ev_count} 条证据；关键结论均带可溯源来源，被标记“待核”项已如实就地标注）。")
    elif level == "BLOCK":
        # 返工达上限后降级交付：必须如实披露致命项，不得伪装成"通过"
        fatal = q.get("fatal_issues") or []
        lines.append(
            "**自动质检：未完全通过（返工已达上限，以下致命项仍未解决，相关结论请勿直接采用）**"
        )
        for f in fatal[:6]:
            lines.append(f"- ⚠️ {f}")
        lines.append(
            "> 处理建议：补充/更正对应数据源后重新发起调研，或人工修正上述口径后重测。"
        )
    else:
        lines.append(
            f"**自动质检**：共 {ev_count} 条证据；规则层 {len(rule_issues)} 项提示（多为估算类，不阻断决策，供人工复核）。"
        )
        if url_missing:
            lines.append(f"- 数字缺独立可点击链接：{len(url_missing)} 项（已按规则视为估算值）")
        for issue in others[:3]:
            lines.append(f"- {issue}")
        if len(others) > 3:
            lines.append(f"- …另有 {len(others) - 3} 项同类提示")
        lines.append(
            "> 数字缺来源 = 该数值来自行业估算或联网检索内容，正式使用前建议补齐可点击链接，或维持'估算'标注。"
        )
    # LLM 语义复核的检出（此前**完全不披露**）+ **它的覆盖率**（两者必须一起给）
    if llm_issues:
        lines.append(f"- **LLM 语义复核**另检出 {len(llm_issues)} 项（与规则层相互独立）：")
        for _x in llm_issues[:5]:
            lines.append(f"  - {_x}")
        if len(llm_issues) > 5:
            lines.append(f"  - …另有 {len(llm_issues) - 5} 项")
    # 覆盖率**无论过没过都要印**：不印的话，"质检通过"会被读成"全文都查过了"
    if _cov.get("chars"):
        _pct = round(float(_cov.get("coverage") or 0) * 100)
        _tail = "" if _pct >= 99 else "（**未覆盖的部分未经 LLM 复核**，只经规则层扫描）"
        lines.append(
            f"- LLM 语义复核覆盖正文 {_cov.get('covered_chars', 0):,} / {_cov['chars']:,} 字"
            f"（{_pct}%，分 {_cov.get('windows', 1)} 段）；规则层为全文扫描。{_tail}"
        )
    lines.append(
        "**标注释义**：正文与指标表中的 **「（存疑·待核）」** 表示该数字经对抗性数字校验判定为高风险"
        "（反方证据未消解/与常识或同源数据互斥），已就地降级、不作为决策锚使用；"
        "具体校验过程与反方证据见下文《对抗性数字校验》一节。"
    )
    lines.append(
        "**可验证性（边界，D-7）**：关键结论可溯源到真实来源并支持点开核验；数据标注信源级别"
        # ⚠️ **图例必须从 `LAYERS` 派生，不许手抄**（2026-09-17 实跑发现）。
        #
        # 病案：这里此前**硬编码**了「L1官方/L2媒体与咨询/L3行业/L4用户/L5推测」，
        # 而 `core/source_registry.LAYERS`（数据源体系页、`/api/source-registry`、
        # 以及**每份报告随附的可复算审计包** `audit_bundle.py` 印的源层定义）是
        # 「L1官方统计/监管 · L2法定披露/交易所 · L3行业机构/协会 · L4市场发现/媒体 · L5用户声音/社媒」。
        # **同一个 `L2`，报告正文说"媒体与咨询"、报告自己的审计包说"法定披露/交易所"。**
        # 第三方拿报告对着它自己的审计包核，第一步就撞上一个自相矛盾——
        # 而"可核对"正是这份报告卖的那个点。
        # 提示词那侧（`_SUMMARY_METRICS_PROMPT` 的核心指标表）已同步改成同一套词，
        # 这里改成**从唯一出处派生**，两处再也不会各走各的。
        "（" + " · ".join(f"{_k}{_v}" for _k, _v in _layers_legend().items()) + "）；"
        "冲突数据已自动仲裁并列；"
        "被标记“待核”的数字如实就地标注、不进入决策锚；**不承诺报告内每个数字均可点**。"
        "人口基数等事实参数由系统知识库注入，测算数值由 Python 引擎计算；"
        "市场规模等测算为估算值，落地前建议补充一手验证。"
    )
    return "\n".join(lines)


def _build_confidence_appendix(state: TaskState) -> str:
    """结论置信度披露 + 证据链台账（P0 可信度护城河 + P0 Evidence Ledger）

    每条关键结论：置信度(高/中/低) + 核心理由 + 溯源域名 → 可核对、非黑箱。
    规则公开：L1/L2且多源→高；单源→上限中；含L5推测→低。
    """
    from urllib.parse import urlparse as _urlparse

    evs = state.get("evidences") or []
    if not evs:
        return ""
    try:
        from core.confidence import confidence_distribution, grade_evidence_confidence
    except Exception:
        return ""
    dist = confidence_distribution(evs)
    rows = []
    for ev in evs[:20]:
        try:
            c = grade_evidence_confidence(ev)
        except Exception:
            continue
        mark = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}.get(c.level, c.label)
        reason = "；".join(c.reasons[:3])[:60]
        domain = ""
        for _s in ev.sources:
            if _s.url and _s.url.startswith("http"):
                try:
                    domain = _urlparse(_s.url).netloc
                    break
                except Exception:
                    pass
        rows.append(f"| {mark} | {ev.claim[:34]} | {reason} | {domain or '纯文字'} |")
    if not rows:
        return ""
    lines = [
        "\n\n---\n\n# 📊 结论置信度披露 · 证据链台账",
        f"本报告 {len(evs)} 条证据：🟢 高置信度 {dist.get('high', 0)} · 🟡 中 {dist.get('medium', 0)} · 🔴 低 {dist.get('low', 0)}",
        "",
        "> 置信度规则（公开可核对）：L1/L2 权威来源且 ≥2 个独立来源 → 高；单源结论 → 上限中；含 L5 推测参数 → 低。理由逐条列出。",
        "> 边界：本置信度度量的是**来源可追溯性**（信源层级 × 独立来源数），**不含事实/算术正确性**——",
        "> 后两者由规则层（口径 R1 / 背书 R4 / 算术 R5 / 伪量化 R6）与 Red Team、人工复核承担；",
        "> 若某数字命中上述任一层疑点，即使此处标『高』，引用前也须先人工核验。",
        "",
        "| 置信度 | 结论 | 核心理由 | 溯源域名 |",
        "|---|---|---|---|",
    ]
    lines.extend(rows)
    # **截断必须自陈**：抬头写"本报告 57 条证据"、表里只有 20 行，中间那 37 条去哪了
    # 读者无从知道——而"表里数不出抬头那个数"会让整节的读数都变得不可核对。
    # 病案（2026-09-15 深访实跑）：抬头 57 条、表 20 行、无任何说明。
    if len(evs) > len(rows):
        lines.append(
            f"| … | 另有 **{len(evs) - len(rows)}** 条证据未列出（台账上限 {len(rows)} 行，"
            f"按取证顺序） | 完整清单见可复算包 `claims.csv` | — |"
        )
    lines.append(
        "> 低置信度结论请作为'线索'参考，落地决策前建议补一手验证；高置信度结论可直接引用。"
    )
    return "\n".join(lines)


def _structural_unknowns(state: TaskState) -> list[str]:
    """Unknown 的**结构化**部分：本次运行确实缺了什么（可归因、可核对）。

    为什么要单列这一支：正文自陈固然要引，但「缺什么」在系统里本来就有**确定的答案**
    ——源路由审计记着哪个维度没拿到必选源层，注册表记着哪个源本机没启用。
    这些是**算出来的事实**，不是从散文里捞出来的印象。
    """
    out: list[str] = []
    try:
        routes = (state.get("subtask_results") or {}).get("_routes") or {}
        for _sid, r in routes.items():
            if not isinstance(r, dict) or r.get("satisfied"):
                continue
            _need = f"{r.get('required_layer', '')} {r.get('required_label', '')}".strip()
            out.append(
                f"**必选源层未命中**：维度 `{_sid}`（{r.get('claim_label', '')}）按规则需 {_need}，"
                f"本次未取得——该维度结论只能按降级口径读"
            )
    except Exception as e:
        logger.debug("缺口披露读取源路由失败: %s", e)
    try:
        from core.source_registry import blocked_runtime_sources

        for s, _why in blocked_runtime_sources():
            out.append(f"**数据源本机未启用**：{s.layer} · {s.name}——{_why}")
    except Exception as e:
        logger.debug("缺口披露读取未启用源失败: %s", e)
    # 构建性缺口：与本次检索无关，恒定存在——写死在这里，因为它不依赖任何一次运行。
    out.append("**无一手调研**：问卷/访谈/实地的采集方案已设计、**尚未执行**（见『数据与方法说明』）")
    return out


# 正文**自陈缺口**的判据（2026-09-17 蜜雪冰城跑重写）。
#
# 原实现是 `report.find(关键词)` 然后 `report[idx:idx+46]`——**从关键词本身起切 46 个字符**。
# 病案（同一次跑）：这一节印出的是
#     「未覆盖目标路径。 | | L1 · 官方统计/监管 | 世界银行开放数据 | structu」
#     「一手验证方案』人工复核。  > 🔍 **时效补检（新鲜度门禁）**：本报告决策型调研例行补检」
#     「待验证] | | **设备采购** | 6万 - 8万 | 自动化封口机…」
# ——跨句、跨节、跨表格，读者看到的是一堆**没有主语的碎片**；而这一节的标题正是
# 「数据缺口披露」，是全篇诚实性最显眼的那一块。根因两层：① 起点在关键词而非句首；
# ② 硬截 46 字符，不认句读。**同一个词表 + 朴素切片 = 产出无出处可言的文本**，
# 与 `excluded_plan_lines`／`_GENERIC_SRC` 是同一个家族。
#
# 新判据两条，都为了"每一行都得有主语、有出处"：
#   ① 只认**散文行**（表格行/标题/引用块/含制表符的行一律不取）里的**整句**；
#   ② 句内必须**同时**出现「缺口短语」与「自陈主语」——缺主语的是我们的解释文字
#      （如决策链里那句"取不到就记缺口"），不是这次调研缺了什么。
_GAP_PHRASES = (
    "未包含一手", "不含一手", "无一手", "尚未执行", "未执行", "未做一手",
    "未检索到", "暂缺", "缺乏可靠", "未覆盖", "建议补充一手", "待一手验证",
    "无法获取", "取不到", "未配置", "未取得",
)
_GAP_SUBJECTS = ("本报告", "本次", "本文", "研策")


def _declared_gap_clauses(report: str) -> list[str]:
    """正文里**自陈缺口**的整句（只取整句，绝不切字符片段）。"""
    out: list[str] = []
    for ln in (report or "").split("\n"):
        s = ln.strip()
        if not s or s[0] in "|#>`" or "\t" in s or s.count("|") >= 2 or len(s) < 8:
            continue
        for seg in re.split(r"(?<=[。！？])", s):
            t = seg.strip(" -·*")
            if not (8 <= len(t) <= 120):
                continue
            if any(k in t for k in _GAP_PHRASES) and any(k in t for k in _GAP_SUBJECTS):
                out.append(t if len(t) <= 100 else t[:100] + "…")
    return out


def _build_gap_disclosure(state: TaskState, report: str) -> str:
    """数据缺口披露（P1：Known / Estimated / Unknown）

    把"我们知道什么 / 在估算什么 / 不知道什么"摊开——企业级诚实，
    与置信度披露互补（置信度看可靠度，缺口看覆盖度）。
    """
    evs = state.get("evidences") or []
    if not evs:
        return ""
    try:
        from core.confidence import confidence_distribution

        dist = confidence_distribution(evs)
    except Exception:
        return ""
    # Unknown：**结构化事实**（源层未命中 / 源未启用）+ **正文自陈缺口**（按句读取）。
    unknown = _structural_unknowns(state) + _declared_gap_clauses(report)
    if unknown:
        unknown = list(dict.fromkeys(unknown))[:6]
    lines = [
        "\n\n---\n\n# 🗺️ 数据缺口披露（Known / Estimated / Unknown）",
        f"- **✅ Known（已知）**：{dist.get('high', 0)} 项高置信结论，可直接引用",
        f"- **🟡 Estimated（估算）**：{dist.get('medium', 0) + dist.get('low', 0)} 项中/低置信结论（测算假设/单源），落地前需交叉验证",
    ]
    if unknown:
        lines.append("- **❓ Unknown（未知 / 待一手验证）**：")
        for u in unknown:
            lines.append(f"  - {u}")
    else:
        lines.append("- **❓ Unknown（未知）**：主要缺口见『数据与方法说明』（未含一手问卷/访谈/实地）")
    lines.append(
        "> 决策建议：高置信结论直接采用；估算项落地前补一手验证；未知项是下一轮调研/问卷/深访的方向。"
    )
    return "\n".join(lines)


def _build_source_system_appendix(state: TaskState, citations: list[dict]) -> str:
    """数据源体系 + 问答→源硬路由审计 + 数字护照（数据层可审计性的对外呈现）

    三段合一，回答三个问题：
      ① 我用的是什么数据源体系？（注册表：按权威梯度分层，已接入/规划中两栏诚实分列）
      ② 这个问题我**本该**用什么源、实际拿到没有？（源路由审计，未命中给降级说明）
      ③ 每个数字的数据期与口径是什么？（数字护照；抽不到就标"未标注"，不猜）
    """
    lines: list[str] = ["\n\n---\n\n# 🗂️ 数据源体系 · 源路由审计 · 数字护照"]
    # —— ① 注册表：已接入 / 规划中（诚实两栏，不把没接的写成已接入）——
    try:
        from core.source_registry import (
            LAYERS,
            blocked_runtime_sources,
            connected_sources,
            planned_sources,
        )

        conn = connected_sources()
        plan = planned_sources()
        # **第三栏：已登记接入、但本机运行时不可用**（2026-09-15 补）。
        # 不加这一栏，附录会把 SEC EDGAR 这类"登记了但 fail-closed 没在跑"的源
        # 混进"已接入 N 个源"里——披露层把已停用的源说成已接入，是**反向声明**。
        blocked = blocked_runtime_sources()
        _tail = f"，另有 **{len(blocked)} 个本机未启用**（见下）" if blocked else ""
        lines.append(
            f"**已接入 {len(conn)} 个源**{_tail}"
            f"（按权威梯度分层：{' → '.join(f'{k} {v}' for k, v in LAYERS.items())}）"
        )
        lines.append("")
        lines.append("| 层 | 数据源 | 形态 | 喂哪些维度 | 边界说明 |")
        lines.append("|---|---|---|---|---|")
        for s in conn:
            lines.append(
                f"| {s.layer} · {LAYERS.get(s.layer, '')} | {s.name} | {s.structure} | "
                f"{'、'.join(s.feeds[:3])} | {s.note[:52]} |"
            )
        if blocked:
            lines.append("")
            lines.append(
                f"**本机未启用 {len(blocked)} 个源**（已登记接入，但当前运行时不可用——"
                "**本次未从中取得任何数据**，如实列示）："
            )
            for s, _why in blocked:
                lines.append(f"- {s.layer} · {s.name}：{_why}")
        if plan:
            lines.append("")
            lines.append(f"**规划中 {len(plan)} 个源**（未接入，仅路线图）：")
            for s in plan:
                lines.append(f"- {s.layer} · {s.name}（{s.note[:60]}）")
    except Exception as e:
        lines.append(f"（注册表读取失败：{e}）")
    # —— ② 源路由审计：该用什么源层 / 实际命中没有 / 没命中的降级口径 ——
    try:
        routes = (state.get("subtask_results") or {}).get("_routes") or {}
        if routes:
            hit = sum(1 for r in routes.values() if r.get("satisfied"))
            lines.append("")
            lines.append(
                f"**问答→源硬路由审计**：{len(routes)} 个调研维度，"
                f"**命中必选源层 {hit} 个 / 未命中 {len(routes) - hit} 个**"
            )
            lines.append("")
            lines.append("| 维度 | 结论类型 | 必选源层 | 命中 | 实际拿到 |")
            lines.append("|---|---|---|---|---|")
            # **未命中的排前面**：这一节的用处是"哪些必选源层没拿到"，
            # 而按插入顺序取前 12 行时，未命中的行会被挤到看不见的地方——
            # 病案（2026-09-15 深访实跑）：抬头写"命中 14 / 未命中 2"，
            # 表里却只数得出 **1** 个 ⚠️（另一个在被切掉的 4 行里）。
            # **读者数不出来 = 这个数没法被核对**，而这一整节的意义就是可核对。
            _routed = sorted(routes.items(), key=lambda kv: bool(kv[1].get("satisfied")))
            _shown = _routed[:_ROUTE_ROWS]
            for _sid, r in _shown:
                mark = "✅" if r.get("satisfied") else "⚠️ 未命中"
                got = "/".join(r.get("hit_layers") or []) or "无"
                lines.append(
                    f"| {_sid} | {r.get('claim_label', '')} | "
                    f"{r.get('required_layer', '')} {r.get('required_label', '')} | {mark} | {got} |"
                )
            # **截断必须自陈**（本仓库铁律：静默截断读起来像"全覆盖"）。不写这一行，
            # 上表就成了"我们只跑了 12 个维度"的另一个说法，而抬头明明写着 16。
            if len(_routed) > len(_shown):
                lines.append(
                    f"| … | 另有 **{len(_routed) - len(_shown)}** 个维度未列出（表格上限 {_ROUTE_ROWS} 行；"
                    f"未命中的已全部排在上方，被省略的都是已命中项） | — | — | — |"
                )
            misses = [r for r in routes.values() if not r.get("satisfied")]
            if misses:
                lines.append("")
                lines.append("**未命中维度的降级口径（已按此表述，不假装有一手源）**：")
                seen_deg: set[str] = set()
                for r in misses[:4]:
                    d = str(r.get("degrade") or "")
                    if d and d not in seen_deg:
                        seen_deg.add(d)
                        lines.append(f"- 「{r.get('claim_label', '')}」：{d}")
    except Exception:
        pass
    # —— ③ 数字护照：每条引用的数据期 + 口径（抽不到标"未标注"，绝不推断）——
    try:
        pax = [
            c for c in (citations or [])
            if isinstance(c, dict) and (c.get("data_period") or c.get("caliber"))
        ]
        if pax:
            lines.append("")
            lines.append(
                f"**数字护照**（{len(pax)} 条可定位数据期的来源）——治的正是"
                "「数字真、口径不对」这类**正文里看不出来**的引用事故："
            )
            lines.append("")
            lines.append("| # | 来源 | 数据期 | 口径 |")
            lines.append("|---|---|---|---|")
            for c in pax[:16]:
                _i = (citations or []).index(c) + 1
                lines.append(
                    f"| {_i} | {str(c.get('source') or '')[:18]} | "
                    f"{c.get('data_period') or '**未标注**'} | {c.get('caliber') or '**未标注**'} |"
                )
            _n_un = sum(1 for c in (citations or []) if isinstance(c, dict) and not c.get("data_period"))
            if _n_un:
                lines.append("")
                lines.append(
                    f"> ⚠️ 另有 {_n_un} 条来源**未能定位数据期**（标题/摘录中无明确报告期）——"
                    "该项如实标为「未标注」，**不做推断填充**；引用时请点开原文核对。"
                )
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_evidence_status_appendix(state: TaskState) -> str:
    """结论状态机 + 双源配对矩阵（敢承认"不知道"，且不假装已交叉验证）

    - **状态机**：每条结论落 CONFIRMED/LIKELY/UNCERTAIN/CONTESTED/INSUFFICIENT。
      两个关键状态是 `CONTESTED`（来源打架 → 不下单方结论）与 `INSUFFICIENT`
      （一手源缺失 → **不硬给数字**）。专业研究不是"有答案就给答案"。
    - **配对矩阵**：按结论类型核对"验证对"（公司数字=披露+第三方；行业数字=官方+行研），
      不满足就显式标出缺哪一侧，**不假装已交叉验证**。
    """
    evs = state.get("evidences") or []
    if not evs:
        return ""
    try:
        from core.evidence_matrix import STATUS_META, judge_status, pair_matrix, status_distribution
    except Exception:
        return ""
    lines: list[str] = ["\n\n---\n\n# 🧭 结论状态 · 双源配对矩阵"]
    # —— 来源血缘（把"转述"从"独立佐证"里剥出来）——
    # 这是本报告最该被看到的一段：数字到处都是、看着互相印证，其实可能全都指向同一个原始出处。
    # 我们**主动做减法**：把 N 条转述收敛成 1 条原始 + (N-1) 条回声，而不是拿它当多源交叉验证。
    try:
        _echo_pairs: dict[str, set[str]] = {}
        _origin_by_domain: dict[str, set[str]] = {}
        for _ev in evs:
            for _s in getattr(_ev, "sources", []) or []:
                _via = (getattr(_s, "via", "") or "").strip()
                _uu = (_s.url or "").strip()
                if not _uu:
                    continue
                try:
                    from core.source_util import registrable_root as _rroot

                    _dom = _rroot(_uu) or _uu
                except Exception:
                    _dom = _uu.split("/")[2].lower().removeprefix("www.") if "//" in _uu and len(_uu.split("/")) > 2 else _uu
                if _via:
                    _echo_pairs.setdefault(_via, set()).add(_dom)
                _o = (getattr(_s, "origin", "") or "").strip()
                if _o:
                    _origin_by_domain.setdefault(_dom, set()).add(_o)
        _echo_n = sum(len(v) for v in _echo_pairs.values())
        if _echo_pairs:
            lines.append("")
            lines.append(
                f"**来源血缘**：本报告有 **{_echo_n}** 个来源页其实是在**转述同一家机构的数字**"
                f"（共 {len(_echo_pairs)} 个原始出处）。这些条目**不构成独立佐证**，"
                f"我们已按「出处」而非「域名」重算佐证数："
            )
            lines.append("")
            lines.append("| 原始出处 | 转述它的来源页 |")
            lines.append("|---|---|")
            for _origname, _doms in sorted(_echo_pairs.items(), key=lambda x: -len(x[1]))[:8]:
                lines.append(f"| {_origname} | {'、'.join(sorted(_doms)[:6])} |")
            lines.append("")
            lines.append(
                "> 为什么较真这一步：同一份原始数据被多家媒体各写一篇，看着像「多方印证」，"
                "实际上**那个出处一旦错了，所有「佐证」一起错**。把转述当成独立来源，"
                "是调研里最常见、也最不容易被发现的虚假确定性。"
            )
    except Exception as _e:
        logging.getLogger(__name__).warning("来源血缘附录生成失败: %s", _e)
    try:
        dist = status_distribution(evs)
        parts = [f"{STATUS_META[k][0]} {dist.get(k, 0)}" for k in ("CONFIRMED", "LIKELY", "CONTESTED", "UNCERTAIN", "INSUFFICIENT")]
        lines.append(f"本报告 {len(evs)} 条结论的状态分布：" + " · ".join(parts))
        lines.append("")
        lines.append("| 状态 | 含义 |")
        lines.append("|---|---|")
        for k in ("CONFIRMED", "LIKELY", "CONTESTED", "UNCERTAIN", "INSUFFICIENT"):
            lines.append(f"| {STATUS_META[k][0]} | {STATUS_META[k][1]} |")
        # 需要人注意的两类：存争议 / 证据不足
        flagged: list[tuple[str, str, str]] = []
        for ev in evs:
            try:
                v = judge_status(ev)
            except Exception:
                continue
            if v.code in ("CONTESTED", "INSUFFICIENT"):
                flagged.append((v.mark(), str(getattr(ev, "claim", ""))[:40], v.reason[:40]))
        if flagged:
            lines.append("")
            lines.append(f"**需要人工注意的 {len(flagged)} 条**（存争议 / 证据不足——不建议直接据此决策）：")
            lines.append("")
            lines.append("| 状态 | 结论 | 原因 |")
            lines.append("|---|---|---|")
            for m, c, r in flagged[:12]:
                lines.append(f"| {m} | {c} | {r} |")
            # **截断必须留痕**（2026-09-16 全量审计）：上面那行标题写的是**全量**条数
            # （`len(flagged)`），而表里只列前 12 条——于是"需要人工注意的 23 条"
            # 配一张 12 行的表，读者既不知道少了 11 条，也不知道去哪找。
            # 同文件另两处同类截断都写了"另有 N 条"（`_ROUTE_ROWS` / 图表行数），这一处漏了。
            if len(flagged) > 12:
                lines.append("")
                lines.append(
                    f"> 表中列前 **12** 条，**另有 {len(flagged) - 12} 条**"
                    "未展开（完整清单见上方证据链台账）。"
                )
        else:
            lines.append("")
            # **这句必须自带口径边界**（2026-09-15 深访实跑）。
            # 原话是「本次无『存争议』或『证据不足』结论：所有结论均有可点来源且**无来源间数据冲突**」，
            # 而同一份报告里：决策判定写着"关键财务指标存在**显著冲突**与风险"，
            # 规则层又列了 **5 条口径冲突**（回本周期跨度 6.5×、关店率 11.9×…）。
            # 三处都不算说谎——它们判的是三种不同的"冲突"：
            #   · 本行判的是**来源与来源之间**是否打架（同一结论的两条证据互斥）；
            #   · 规则层判的是**报告内部同一指标在不同章节取值不同**；
            #   · 决策判定那句话是分析师的行文。
            # 但读者只看得到"无冲突"三个字，然后再看到"5 条冲突"——**同一个词两种含义，
            # 而且结论相反**。所以这里把口径写在句子里，并指向另一类冲突的出处。
            lines.append(
                "> 本次无「存争议」或「证据不足」结论——**此处只判一件事：来源与来源之间是否打架**"
                "（同一结论的两条证据互相矛盾）。"
                "**报告内部「同一指标在不同章节取值不同」是另一类问题**，不计入本行，"
                "由规则层逐条列出（见下文《规则层自检》的口径冲突项）——**两类不互相抵消**，"
                "本行判「无」不代表另一类也是零。"
            )
    except Exception:
        pass
    # 双源配对矩阵
    try:
        rows = pair_matrix(evs)
        if rows:
            _ok = sum(1 for r in rows if r.ok)
            _pok = sum(1 for r in rows if r.pair_ok)
            lines.append("")
            lines.append(
                f"**双源配对矩阵**：{len(rows)} 类结论中 **{_ok} 类来源不单一**"
                f"（异侧三角 或 同侧多源），{len(rows) - _ok} 类**仅单侧来源**；"
                f"其中 **{_pok} 类真正满足**了它那一行写明的验证对。"
            )
            # **两把尺、两列**（2026-09-18 修）：`ok` 只看"来源是否单一"，
            # `pair_ok` 才核"你要的那两侧到齐没有"。此前只有第一列，
            # 于是同一行可以一边印「要求：法定披露（一手）+第三方」、一边判 ✅，
            # 哪怕调用台账显示巨潮/SEC 本次零调用——**契约与判定不同源**。
            lines.append("")
            lines.append("| 结论类型 | 要求验证对 | 实际拿到 | 来源不单一 | 满足该要求 | 条数 |")
            lines.append("|---|---|---|---|---|---|")
            for r in rows:
                _cell = "✅ 满足" if r.pair_ok else "❌ 未满足"
                if r.missing:
                    _cell += f"（缺 {r.missing[:28]}）"
                lines.append(
                    f"| {r.claim_label} | {r.pair_required[:40]} | {r.gott} | "
                    f"{'✅' if r.ok else '⚠️ 单侧'} | {_cell} | {r.n} |"
                )
            if _pok < _ok:
                lines.append("")
                lines.append(
                    "> ⚠️ **两列为什么不一样**：「来源不单一」只要求**两侧来源到齐**"
                    "（异侧 ≥2 或独立域名 ≥2）；「满足该要求」核的是**该行自己写明的那两侧**"
                    "（如『法定披露（一手）+ 第三方』必须真有一手法定披露页）。"
                    "**前者为 ✅ 而后者为 ❌，意思是：这份结论有交叉验证，但不是按这一类结论"
                    "应有的方式验证的**——请按最后一列指出的缺口补来源。"
                )
    except Exception:
        pass
    # —— 来源时效分布 + 出处集中度 ——
    # 这两块回答的是"来源结构"层面的问题，不是单条来源的问题：
    #   · 时效分布 → 这批来源整体有多新？（"引了 40 条，其中 22 条是 5 年前的"是个结构性问题，
    #     但逐条看每一条都不算违规——只有摊开分布才看得见）
    #   · 出处集中度 → 报告的独立信息源到底有几个？（名义上 24 条来源，可能只来自 3 家机构）
    try:
        import re

        from datetime import date as _date

        _yr: dict[str, int] = {}
        _undated = 0
        _origin_hits: dict[str, set[int]] = {}
        _src_seen = 0
        for _i, _ev in enumerate(evs):
            _ev_origins: set[str] = set()
            for _s in getattr(_ev, "sources", []) or []:
                _src_seen += 1
                _dt = str(getattr(_s, "publish_date", "") or "")
                _m = re.search(r"(19|20)\d{2}", _dt)
                if _m:
                    _yr[_m.group(0)] = _yr.get(_m.group(0), 0) + 1
                else:
                    _undated += 1
                _o = (getattr(_s, "origin", "") or "").strip()
                _via = (getattr(_s, "via", "") or "").strip()
                if _o:
                    _ev_origins.add(_o)
                elif _via:
                    _ev_origins.add(_via)
            for _o in _ev_origins:
                _origin_hits.setdefault(_o, set()).add(_i)
        if _yr or _undated:
            _now_y = _date.today().year
            _parts = []
            for _y in sorted(_yr, reverse=True):
                _tag = f"{_y} 年" + ("（本年）" if int(_y) == _now_y else "")
                _parts.append(f"{_tag} {_yr[_y]} 条")
            if _undated:
                _parts.append(f"日期不详 {_undated} 条")
            lines.append("")
            lines.append(f"**来源时效分布**（共 {_src_seen} 条来源页）：" + " · ".join(_parts))
            _old = sum(n for y, n in _yr.items() if _now_y - int(y) >= 3)
            if _src_seen and _old / max(1, _src_seen) >= 0.35:
                lines.append(
                    f"> ⚠️ 其中 **{_old}** 条（{_old * 100 // max(1, _src_seen)}%）发布于 3 年以前。"
                    "逐条看它们各自都未必违规（年份久的口径仍可能有效），但**整体偏旧**是结构性问题："
                    "市场类结论尤其要核对这批发旧来源支撑的是哪几句。"
                )
            if _src_seen and _undated / max(1, _src_seen) >= 0.5:
                lines.append(
                    f"> ⚠️ 另有 **{_undated}** 条来源**取不到发布日期**（部分政府栏目路径不含月日）。"
                    "我们**没有为了凑数给它们补一个猜测日期**——所以它们也**没能通过时效门**，"
                    "请按「日期不详」对待，不要当成新近资料。"
                )
        if _origin_hits:
            _tot_ev = sum(len(v) for v in _origin_hits.values())
            _top = sorted(_origin_hits.items(), key=lambda x: -len(x[1]))[:3]
            _txt = " · ".join(f"{n} {len(v)} 条" for n, v in _top)
            lines.append("")
            lines.append(
                f"**出处集中度**：{_tot_ev} 条有明确出处的结论，来自 "
                f"{len(_origin_hits)} 个独立原始出处。占比最高的几家：{_txt}"
            )
            _n0, _v0 = _top[0]
            if _tot_ev >= 5 and len(_v0) / _tot_ev >= 0.4:
                lines.append(
                    f"> ⚠️ **结构性依赖提示**：单一出处「{_n0}」占 {len(_v0)}/{_tot_ev}"
                    f"（{len(_v0) * 100 // _tot_ev}%）。这不是错误，但要知道它的含义——"
                    f"该机构一旦调整口径或修正数据，本报告**相当一部分数字会同时变动**，"
                    f"而它们看起来像是互相印证的多个来源。"
                )
    except Exception as _e:
        logging.getLogger(__name__).warning("来源结构附录生成失败: %s", _e)
    # —— 本次数据源调用台账（哪个源回了几条 / 哪些回空）——
    # 放在最后：这是"元信息"，读者先看结论与证据，再关心我们向哪些源发了请求。
    try:
        from core.source_health import render_trace_appendix

        _tl = render_trace_appendix(state.get("source_trace"))
        if _tl:
            lines.append(_tl)
    except Exception as _e:
        logging.getLogger(__name__).warning("调用台账附录生成失败: %s", _e)
    return "\n".join(lines) if len(lines) > 2 else ""


def _build_caliber_appendix(citations: list[dict[str, Any]] | None) -> str:
    """口径对照附录：报告**实际用到**的统计口径 → 一段释义表（没命中一字不加）

    独立成函数是为了**可测**。病案（2026-09-14 端到端实跑）：
    这段逻辑原来内联在 `node_deliver` 里、且用了 `re.split`，而 `graph.py` **没有在
    模块级 import re**（本文件历来在函数内部 `import re`）→ 每次交付都抛
    `NameError: name 're' is not defined`，被 `except` 吞成一行 warning，
    **整块口径对照在生产里从来没有出现过**。
    单测只测了 `caliber_kb.explain()` 本身，测不到"接线"，所以一直没发现。
    """
    import re

    from core.caliber_kb import explain as _cal_explain

    terms: list[str] = []
    for c in citations or []:
        v = str((c or {}).get("caliber") or "")
        # 护照里的口径可能是 "限额以上+零售额" 或 "范围:三线及以下；性质:预测值"，
        # 拆开逐段查词典（词典是精确术语表，整串丢进去匹配不上）
        for seg in re.split(r"[+；;、]", v):
            seg = seg.split(":")[-1].split("：")[-1].strip()
            if seg:
                terms.append(seg)
    return _cal_explain(terms)


def node_deliver(state: TaskState) -> dict[str, Any]:
    """交付：决策判定置顶 + 执行摘要 + 正文 + 数据说明 + 质检披露

    结构对齐"Decision First"（方案优化 §25）：
      ① 决策判定 + 14天验证计划（用户最关心的"该不该做/下一步做什么"）
      ② 执行摘要 + 核心指标表
      ③ 正文（证据支撑）
      ④ 数据与方法说明
      ⑤ 质检与可验证性
    —— 客户买的不是报告，是"是否该做 + 接下来做什么"；报告只是证明。
    """
    _trace(state, "deliver", "生成最终报告与交付物（决策置顶）")
    # D2 检索新鲜度门禁（Q0）：核心证据偏旧(≥2年无近源) → 补检近一年动态，块置顶展示。
    # 独立模块 + 全程 try：任何失败/离线都不阻断交付；无可用线索返回空串。
    _fresh_block = ""
    try:
        from core.freshness import build_freshness_block

        _fresh_block = build_freshness_block(
            state.get("raw_question", ""), state.get("evidences") or []
        )
        if _fresh_block:
            _trace(state, "deliver", "时效补检命中：核心来源偏旧，已补检近一年动态并置顶")
    except Exception as _e:
        logger.warning("时效补检跳过: %s", _e)
    # 决策清单：判定 + 验证计划 + 行动/成本/风险（Decision First，紧跟开头）
    decision_block = ""
    if state.get("decision_sop"):
        decision_block = "\n# 📋 决策清单\n\n" + state["decision_sop"] + "\n---\n\n"
    # 一页纸执行摘要 + 核心指标表
    # **必须先算出"本次真的取回了哪些来源"再交给模型**（2026-09-18）：
    # 提示词要求来源机构"可核验"，却从没给它可核验的清单 —— 模型只能凭记忆填。
    try:
        _src_brief = _retrieved_source_brief(state)
    except Exception:
        _src_brief = ""
    summary = _build_summary_and_metrics(
        state["report_draft"],
        state["raw_question"],
        ctx=state.get("clarified_context"),
        sources_brief=_src_brief,
    )
    # 结构化核心指标（交付物头部 CSS 条 / HTML 作品页共用）：从摘要的指标表解析，
    # 解析失败不阻断交付（宁空不崩）。
    metrics: list[dict] = []
    if summary:
        try:
            from core.report_html import extract_core_metrics

            metrics = extract_core_metrics(summary) or []
        except Exception:
            metrics = []
    parts = []
    if summary:
        parts.append(summary)
        parts.append("\n---\n")
    parts.append(decision_block)
    parts.append(state["report_draft"])
    final = "".join(parts)
    # 法定标识的内容编号：**在正文定型时算一次**，头尾共用。
    # 头与尾各自算一遍会得到两个编号（中间还插了附录）→ 一份报告两个编号，
    # 而"内容编号"的全部意义就是唯一可核对。算不出就退回逐处计算（不影响交付）。
    _aigc_cid = ""
    try:
        from core.ai_label import content_id as _aigc_cid_of

        _aigc_cid = _aigc_cid_of(final)
    except Exception:
        _aigc_cid = ""
    # 法定显式标识（末尾位置）：紧跟**正文末尾**，在任何诊断附录之前。
    # 落点踩过一次坑（2026-09-14 实跑）：原先插在"数据与方法说明"之前，
    # 但更靠前的「指标一致性核对」本身就是一个审计标题 → 前端 `splitAuditTail` /
    # 后端 `split_audit_tail` 从那里就切开，标识被折进"审计折叠"里 → **要点开才看得见**，
    # 而条文要的是读者可感知。放在正文段最后一行，两种切分法都切不到它。
    try:
        from core.ai_label import explicit_tail as _aigc_tail

        final += _aigc_tail(final, _aigc_cid)
    except Exception:
        logging.getLogger(__name__).warning("AI 标识（末尾）生成失败（不阻断交付）", exc_info=True)
    # ── 决策基线**自动登记**（2026-09-15 新增，来自一次外部对抗审计）──────────────
    # 病案：`data/outcome_baselines.json` 里的基线**全部是手工维护的**，而系统自己
    # 每一次交付都写下了 判定 / 监控条件 —— **却没有一条进入回访队列**。
    # 于是「员工会回来对账」这个叙事只对了一半：**对的是人写下的账，系统自己的账没进过队列。**
    # 审计原话：这一处对「数字员工」叙事的提升，远大于再加 5 个数据源。
    # 纪律：同题只登一次（复跑不刷屏）；登记失败绝不影响交付。
    #
    # **降级交付不登记**（2026-09-15 外部审计）：`outcome_track.register` 的 docstring
    # 明确写着「只在真交付时登记——无判定/降级交付不调」，而这里原先无条件调用。
    # 降级交付（`sop_fallback`：决策清单是规则兜底模板、不是基于本次证据链的个性化结论）
    # 如果也进回访队列，队列里就会出现**"没人真正做过"的判断**，
    # 而回访的全部意义正是"当初那句话今天还站不站得住"。
    # 自己写的契约自己不守，比没有契约更糟——所以这里显式挡掉并留日志（可核对跳过了几条）。
    _sf = state.get("sop_fallback") or {}
    if _sf:
        logging.getLogger(__name__).info(
            "决策清单为兜底模板（%s）→ 本次**不登记**决策基线（契约：降级交付不调）",
            str(_sf.get("reason") or "")[:40],
        )
    else:
        try:
            from core.outcome_track import register as _reg_baseline

            _ind = ""
            try:
                _p = state.get("plan")
                _ind = str(getattr(_p, "industry", "") or "")[:30]
            except Exception:
                _ind = ""
            _reg_baseline(
                question=str(state.get("raw_question") or ""),
                verdict=str(state.get("decision_verdict") or ""),
                monitor_conditions=list(state.get("monitor_conditions") or []),
                industry=_ind,
            )
        except Exception:
            logging.getLogger(__name__).warning("决策基线自动登记失败（不阻断交付）", exc_info=True)
    # 判断时点（v0.9.43 时效纪律：材料展示 & 读者判断都该知道这份报告是什么时候做的）
    try:
        from datetime import date as _d8

        _doc_date = _d8.today().isoformat()
        _head = (
            # 版本水印：与 app.py /health 同源（core.version.ENGINE_VERSION）——
            # 两处各写一份会漂移（实测：报告印 v0.9.56、服务已 v0.9.57），
            # 而复核者正是按报告水印的版本对照离线自测复现的。
            f"**研策调研报告**｜引擎基线 {_engine_version()}｜生成时点：{_doc_date}"
            "（数据截至检索当日公开来源，正式决策前请核验最新动态）"
        )
        # 法定显式标识（起始位置）：《人工智能生成合成内容标识办法》2025-09-01 已施行。
        # **两种模式都加**——`transparency` 附录（企业模式不附）是产品取舍，
        # 法定标识是义务。此前 enterprise 模式全仓零标识，客户拿去对外发布要担责。
        try:
            from core.ai_label import explicit_head as _aigc_head

            _head = f"{_head}\n\n{_aigc_head(final, _aigc_cid)}"
        except Exception:
            logging.getLogger(__name__).warning("AI 标识（起始）生成失败（不阻断交付）", exc_info=True)
        # v0.9.75 交付降级告警（与时效补检块同层）：凡走了兜底/降级的产物，必须在**报告头**
        # 说明。理由是 agents/decision.py 那条病案：正文其余部分一切正常时，读者不会去猜
        # 哪一节是模板 —— 埋在正文里的"说明"等于没有说明。**降级不静默是这一层唯一的价值。**
        _warn_block = ""
        _sop_fb = state.get("sop_fallback") or {}
        if _sop_fb.get("reason"):
            _warn_block += (
                "> ⚠️ **决策清单为兜底模板**（未走通模型生成路径，原因："
                f"{_sop_fb.get('reason')}）——下面「决策清单」的判定与行动建议来自"
                "**确定性规则模板**，不是基于本次证据链的个性化结论；"
                "证据链与核验部分不受影响。\n\n"
            )
        # D2 时效补检块紧跟时点行（30 秒可见，不埋文末）：
        # 核心来源偏旧时给"近一年重大动态线索"，块自带真实内链，走交付期统一 URL 核验与坏链清洗。
        if _fresh_block:
            final = f"{_head}\n\n{_warn_block}{_fresh_block}\n\n{final}"
        else:
            # D2 时效块"必须非空"验收门禁（decision-grade 支柱4）：
            # 决策题若补检未能返回任何可见产物（检索瞬断/两探针零返回，freshness 按
            # "宁缺勿假"返回空串）→ 时点下仍给一行诚实标记，杜绝 v0.9.46"时效块空被
            # 验收打回"病案在演示日重演。门=决策题+有证据+检索通道在位；描述/问答题
            # 不触发（措辞不误标）。注：本机 .env 含真实检索 key → 离线 _fulltest 的
            # 决策题 e2e 文本也可能带此标记，但 _fulltest 无断言依赖头部文本。
            _need_fresh_marker = False
            try:
                from core.freshness import is_decision_question, _search_configured as _fc
                _need_fresh_marker = (
                    bool(is_decision_question(state.get("raw_question", "")))
                    and bool(state.get("evidences"))
                    and bool(_fc())
                )
            except Exception:
                _need_fresh_marker = False
            if _need_fresh_marker:
                _warn_block += (
                    "> ⚠️ **时效补检未完成**：决策型报告例行补检本次未取得可用检索结果"
                    "（检索通道暂不可用或瞬时零返回）——结论时效以生成时点与既有证据为准，"
                    "正式决策前请人工定向核验近一年重大动态（出售/并购/政策变化）。\n\n"
                )
            final = f"{_head}\n\n{_warn_block}{final}"
    except Exception:
        pass
    # 注：质检结果不再在报告开头显示"未通过"警示（比赛演示避免负面观感），
    #     而是统一放到报告末尾的"✅ 质检与可验证性"如实披露提示项。
    # 指标一致性核对（核心/深入两章并列的根因已由"同维度合并"消除，这里是**兜底检出**：
    # 同一指标在同一章或跨章被写了两次而数值不同，只靠事后检出——那就检干净、写在明处）。
    # 必须放在各类附录之前：附录里的诊断表（配对矩阵/数据源体系）不是分析指标，
    # 进了台账会报出"✅满足=51 / 国家统计局=L1官方"这种捏造的冲突。
    try:
        from core.metric_ledger import build_ledger as _bl, find_conflicts as _fc, format_appendix as _fa

        _ledger = _bl(final)
        _conflicts = _fc(_ledger)
        if _conflicts:
            final += _fa(_conflicts)
            logging.getLogger(__name__).info(
                "指标一致性核对：%d 项指标在不同位置数值不同 → %s",
                len(_conflicts), "、".join(c["indicator"] for c in _conflicts[:6]),
            )
    except Exception as _e:
        logging.getLogger(__name__).warning("指标台账生成失败: %s", _e)
    # 追加"数据与方法说明"：诚实标注一手数据缺失 + 方法论（企业级专业度 + 落地性）
    # 传 `final`（本节之前的成稿）：假设集指纹要覆盖《市场规模测算》与《单位经济》两节，
    # **而那两节在 `final` 里、不在 `state` 里**。不传就等于永远不打印指纹。
    final += _build_methodology_note(state, final)
    # 自主决策链：方法说明讲"用了什么方法"，这一节讲"做了哪些取舍"——互补，紧邻放。
    #
    # ⚠️ **这里放的是占位符，不是最终渲染**（2026-09-17 实跑病案）。
    #
    # 病案：本跑报告头部徽章写「🧠 自主决策链 **19**」，正文却写「共做出 **17** 次取舍」
    # 并逐条列出恰好 17 条——**同一屏两个数**。
    # 根因是**渲染早于记录**：这一行在 `graph.py:2580` 就渲染了，
    # 而两条最该被看见的取舍——`arbitrate`（"要不要给被质疑方申辩权"）与
    # `degrade`（"改数字、删数字，还是标出来"）——记在 `:2798` 与 `:2862`，
    # **都在这一行之后**。于是附录缺 2 条，而 `decision_chain`（交付时取全量）有 19 条。
    # 讽刺的是：缺的正好是这两条**最有故事性**的（L19b 分镜要的就是它们）。
    #
    # 修法：这里只插占位符，等**所有 record 都完成**之后统一渲染一次再替换回去
    # （位置见下方"决策链占位符回填"）。这样既保住这一节在报告里的**原有位置**，
    # 又保证它的内容是**最终的**那一份。
    final += _DEC_SLOT
    # 追加"质检与可验证性"披露（质控层结果公开）
    final += _build_quality_note(state)
    # 追加"结论置信度披露"（P0：每条关键结论的置信度 + 理由 + 溯源域名，可核对）
    final += _build_confidence_appendix(state)
    # 追加"数据缺口披露"（P1：Known / Estimated / Unknown 摊开给决策者）
    final += _build_gap_disclosure(state, final)
    # 汇总全部引用清单（角标弹层数据源），按报告出现顺序排列。
    # 关键：与 node_summarize 用同一排序(_dim_order_key) + 同一报告级根域配额裁剪，
    #       否则正文<ref>序号与 citations 索引错位 / 交付面板又冒出同根域刷屏项。
    citations = []
    # 与 node_summarize 共用 _chapter_subtasks（同维度只出一章）——两侧任何一侧的
    # 章节集合不一致，正文 <ref> 序号与 citations 索引就会整体错位。
    _mods_ordered = _chapter_subtasks(state)
    _mod_cits = [state["subtask_results"].get(f"{st.id}__citations", []) or [] for st in _mods_ordered]
    for _lst in _apply_report_citation_quotas(_mod_cits):
        citations.extend(_lst)
    # 数据层三件套（2026-09-11 数据层深度优化）：数据源体系 + 源路由审计 + 数字护照
    # 放在引文核验**之前**——核验报告讲"链接活不活"，本段讲"该用什么源/数据期口径对不对"，互补。
    try:
        final += _build_source_system_appendix(state, citations)
    except Exception as _e:
        logging.getLogger(__name__).warning("数据源体系附录生成失败: %s", _e)
    # 口径对照：把报告里**实际用到**的统计口径摊开讲。
    # 数字护照回答"口径标注了没有"，这一节回答"这个口径到底指什么、最容易被当成什么"——
    # 报告里它们都被简写成"市场规模""门店数"，读者看不出差别，而混用就是硬伤。
    try:
        final += _build_caliber_appendix(citations)
    except Exception as _e:
        logging.getLogger(__name__).warning("口径对照附录生成失败: %s", _e)
    # 结论状态机 + 双源配对矩阵（敢承认不确定；不假装已交叉验证）
    try:
        final += _build_evidence_status_appendix(state)
    except Exception as _e:
        logging.getLogger(__name__).warning("结论状态附录生成失败: %s", _e)
    # 信任认证：引文核验报告（真实探测每条引用URL存活+证书可信，防毕马威式引用造假）
    cite_report = None
    _all_verdicts: dict[str, tuple[str, bool, str]] = {}
    try:
        from core.verification import build_citation_report, format_citation_report_md

        cite_report = build_citation_report(citations)
        if cite_report.get("total"):
            final += "\n\n---\n\n" + format_citation_report_md(cite_report)
        _all_verdicts = dict(cite_report.get("verdicts") or {})
    except Exception as e:
        logger.warning("引文核验报告生成失败: %s", e)
    # 引用蕴涵核验（claim 级）：上一节只证明"链接活着"，这一节证明"原文真的支持这句话"。
    # 数据源层**唯一一处真正的 LLM 环节**——"这句话和这段原文是不是一个意思"是语义判断，
    # 正则做不了。跑在引文核验之后（两节连读才构成完整引用可信度）。
    # 成本：按档位限对数（快速档不跑）。失败一律 None → 附录如实写"本次未执行"，
    # **绝不当成"全部通过"**（这是本模块存在的全部前提）。
    entail_report = None
    omission_report: dict[str, Any] | None = None
    calibration: dict[str, Any] | None = None
    try:
        # 档位取自 plan.depth（与质检等处同一取值路径：`depth` 是枚举，`.value` 才是字符串）
        _depth_e = getattr(getattr(state.get("plan"), "depth", None), "value", "") or "standard"
        # 键必须与 DepthLevel 的值逐字一致（free/standard/deep/custom）；
        # 踩过 `"quick": 0` → 免费档取不到 0，白跑一轮蕴含核验。
        _max_pairs = {"free": 0, "standard": 18, "deep": 28, "custom": 36}.get(_depth_e, 18)
        if _max_pairs:
            from core.entailment import format_appendix_md as _ent_md, run as _ent_run

            entail_report = _ent_run(state.get("evidences") or [], max_pairs=_max_pairs)
            final += _ent_md(entail_report)
        else:
            _trace(state, "deliver", "引用蕴涵核验：快速档跳过（省成本），附录如实标注未执行")
            from core.entailment import format_appendix_md as _ent_md

            final += _ent_md(None)
        # 遗漏层（v0.9.75）：链接层与内容层**只审已经存在的引用**，对"该引而没引"完全盲。
        # 而行业实测（AIO 98,020 条断言）显示主导失败模式恰恰是遗漏。
        #
        # ⚠️ **扫描推迟到最后**（2026-09-15 外部审计）：这段原先在这里就扫并追加附录，
        # 但它**跑在链接清理之前**（`_strip_bad_links` 与末尾的幂等兜底都会改写正文：
        # 坏链接降级为纯文字、裸 URL 整段删除）。于是同一个包的读数取决于"扫的是清理前还是清理后"——
        # 实测同一份报告重跑得 63 与 59 两个数，而材料引用了那个数。
        # **一个自己都不能复现的数，不该出现在报告里。**
        # 正确位置：在**全部文本改写完成之后**扫最终交付文本（见本函数末尾）。
        omission_report: dict[str, Any] | None = None
        # 核验器自校准（v0.9.77）：报出**假阳性率**——HALLMARK 指出这才是决定核验器
        # 能否部署的那个数（理由：**旗标一旦多为误报就会沦为噪声、不再被相信**）。
        # ⚠️ 方向不许说反：同篇摘要明写漏检（未被发现的伪造）才是对记录代价更高的错误。
        # 读的是**未过期**的存档；过期/没跑过就一个字不印（不拿旧数给今天的支撑率背书）。
        try:
            from core.calibration import format_appendix_md as _cal_md, load as _cal_load

            calibration = _cal_load()
            final += _cal_md(calibration)
        except Exception as _ce:
            logger.warning("校准结果读取/渲染失败: %s", _ce)
    except Exception as e:
        logger.warning("引用蕴涵核验失败（不阻断交付）: %s", e)
    # 检索重复度（2026-09-17）：Agent 有没有在同一个检索词上反复打转。
    # 判据是「同一检索词重复 ≥2 次 **且其中 ≥2 次无返回**」——因为本系统里
    # 反证检索与时效补检是**刻意设计的重复**，只按"重复"报会把设计说成缺陷。
    # 只报读数、不改执行行为（改变失败行为的改动，需一次真实运行验证后才许上线）。
    try:
        from core.repeat_guard import detect as _rg_detect
        from core.repeat_guard import format_md as _rg_md

        _rg = _rg_md(_rg_detect(state.get("search_debug")))
        if _rg:
            final += _rg
    except Exception as _rge:
        logger.warning("检索重复度统计失败（不阻断交付）: %s", _rge)
    # 冲突日志：数据冲突不静默覆盖（保留冲突值+来源+取舍理由）
    if state.get("conflict_results"):
        try:
            cf_lines = ["### ⚖️ 数据冲突日志（不静默覆盖）", "", "| 冲突项 | 冲突值 | 仲裁取舍 |", "|---|---|---|"]
            for cf in state["conflict_results"][:20]:
                if not isinstance(cf, dict):
                    continue
                # 键必须与 `node_check_conflict` 的产出对齐（2026-09-14 审计）。
                # 生产端给的是 `dimension / evidence_a{claim,score} / evidence_b{...} / card{decision}`，
                # 而这里原先读 `claim / values / verdict` —— 三个键一个都不存在，
                # 于是渲染出来是**一张三列全空的表**，标题还叫《数据冲突日志》。
                _a = (cf.get("evidence_a") or {}).get("claim") or ""
                _b = (cf.get("evidence_b") or {}).get("claim") or ""
                _v = (cf.get("card") or {}).get("decision") or cf.get("verdict") or ""
                # **枚举值不许原样印给读者**（2026-09-16 全量审计）。
                # 病案：《数据冲突日志》的「仲裁取舍」列印的是 `adopt_winner` / `show_both`
                # ——机器枚举直接进了交付报告。这一列正是"规则层用公开可对账的权重把冲突讲清楚"
                # 那句话的**证据位**，读者在这里看到的却是一个英文下划线标识符。
                # 映射表由 `_fulltest` 的 `test_conflict_decision_is_human_readable` 锁住
                # （扫 `core/models.py` 里所有可能的取值，漏一个就红）。
                _v = _ARBITRATION_ZH.get(str(_v), str(_v))
                cf_lines.append(
                    f"| {str(cf.get('dimension') or '').strip('·')[:30]} | "
                    f"{str(_a)[:46]} ／ {str(_b)[:46]} | {str(_v)[:40]} |"
                )
            final += "\n\n---\n\n" + "\n".join(cf_lines)
        except Exception:
            pass
    # **用户声音章节的一手/二手边界**（2026-09-15 三份外部评审同时点名）。
    # 报告里有一章叫「下沉市场用户真实反馈」，而同一份报告的缺口披露写着"无一手调研、
    # 只能给二手舆情线索"——**两个说法对不上，而章节标题是读者唯一会记住的那句**。
    # 对参赛作品，这一条伤的不是技术，是"诚实"这个人设。
    # 判据走**源路由结果**（结构），不走标题词表：词表治不完，且无条件贴"二手"标签
    # 会把真做过一手调研的报告说成没做——那是往自贬方向偏的另一种不诚实。
    # 位置放在规则层之前：让下游（规则层/RedTeam/闸门）看到的都是**读者将看到的那份文本**。
    try:
        from core.voice_boundary import stamp_voice_boundary as _vb_stamp

        final = _vb_stamp(final, (state.get("subtask_results") or {}).get("_routes") or {})
    except Exception as _vbe:
        logging.getLogger(__name__).warning("用户声音边界声明失败（不阻断交付）: %s", _vbe)
    # **来源落地净化**（2026-09-18）：核心指标表点名的机构若不在本次真的取回的来源里，
    # **就地标注 + 降级为「待核」**，并在表下把两种可能（转述 vs 记忆）都告诉读者。
    # ⚠️ **必须排在规则层自检之前**——先修正、再检查。反过来会得到
    # "附录里报了一条 R12，而正文那张表原样印着那个机构名"的旧病（判定与交付不同源）。
    try:
        final = _annotate_ungrounded_metric_sources(final, citations)
    except Exception:
        pass
    # 规则层自检（零模型成本，先于 LLM Red Team 拦机械性错误：口径冲突/未来年份/术语未定义）
    # 分工：本层纯规则毫秒级；LLM Red Team 负责语义性错误（误读/违背常识）。两层互证、不重复。
    # `_rule_v` 必须先初始化：它后面会被决策完整性闸门读取，而本 try 若在赋值前抛错，
    # 下游会吃到 NameError——那属于"检查器自己成了故障源"，本仓库刚在别处踩过同型。
    _rule_v: list = []
    try:
        from core.rule_lint import format_rule_lint_md, lint_report

        _rule_v = lint_report(final)
        # **R12：决策区点名的来源机构，本次台账里有没有**（2026-09-18 加）。
        # 需要 `citations` 才能核（纯文本规则层看不到取回了什么），所以在这里接。
        # ⚠️ 这一条是 `level="warn"`——**没有阻断权**。理由写在 `rule_lint` 的注释里：
        # 它的假阳性是结构性的（转述上游机构名 vs 来源幻觉，机器分不清），
        # 而真阳性也是结构性的（24 份历史报告只有 4 份真取回过 iresearch，
        # 却有 21 份把「艾瑞咨询」写成来源机构）。真假混杂的判据不许阻断交付。
        try:
            from core.rule_lint import check_metric_source_grounding

            _rule_v = _rule_v + check_metric_source_grounding(final, citations)
        except Exception:
            pass
        if _rule_v:
            # **就地消费**（2026-09-15 深访实跑）：R7 判"这条来源挂错了"，
            # 而正文那行 `[依据：国家统计局…]` 原样保留——判决书只写在 500 行后的附录里。
            # 先标正文（在追加附录**之前**，这样标记只落在正文、不落进附录的判决条目上），
            # 再把附录接上去。只标不删，幂等、限次、异常不阻断。
            try:
                from core.decision_guard import mark_source_qualification

                final = mark_source_qualification(final, _rule_v)
            except Exception:
                pass
            final += "\n\n---\n\n" + format_rule_lint_md(_rule_v)
    except Exception as e:
        logger.warning("规则层自检失败: %s", e)
    # 对抗性数字校验（Red Team Review）：独立怀疑论校验官重审关键数字，敢挑战结论
    adv_issues = None  # 作用域提级：except 时保持 None，供块后"高风险置顶红条"判断
    try:
        from core.adversarial import adversarial_review, format_adversarial_md

        # 反方证据接入（P0 对抗性核查轮扩展）：把反证检索的【反方】内容交给校验官，
        # 用它挑战报告的乐观结论，防止只讲利好
        _counter_ev = []
        for _ev in state.get("evidences") or []:
            if str(getattr(_ev, "claim", "")).startswith("【反方】"):
                _counter_ev.append(str(getattr(_ev, "claim", ""))[:120])
            if len(_counter_ev) >= 3:
                break
        adv_issues = adversarial_review(
            final,
            state.get("raw_question", ""),
            counter_evidence="；".join(_counter_ev),
        )
    except Exception as e:
        logger.warning("对抗性数字校验异常: %s", e)
        adv_issues = None  # 异常 = 未完成，不许当"干净"交付
    # 三态处理（v0.9.44）：None=未完成/失败 → 显式人工复核提示（不给"干净通过"假象）；
    # [] = 确已运行且未发现问题（干净账单）；list = 发现项（附录 + 高风险"置顶红条"）
    if adv_issues is None:
        final += (
            "\n\n---\n\n> ⚠️ 对抗性数字校验（Red Team）本次未能完成（模型不可用或调用/解析异常），"
            "报告关键数字请人工复核后再用于正式决策。"
        )
    else:
        final += "\n\n---\n\n" + format_adversarial_md(adv_issues)
        # 多角色辩论：紧接对抗校验之后——上一节是**单向**指控（质疑方即法官，被质疑方无申辩权），
        # 这一节补上申辩与裁定。两节连读才是一套完整的对抗流程。
        debate_rep = None
        try:
            from core.debate import format_appendix_md as _deb_md, run as _deb_run
            from core.debate import VERDICT_LABEL as VERDICT_LABEL_CN

            _dd = getattr(getattr(state.get("plan"), "depth", None), "value", "") or "standard"
            debate_rep = _deb_run(state.get("evidences") or [], depth=_dd)
            if debate_rep:
                final += _deb_md(debate_rep)
                _open_n = int(debate_rep.get("open") or 0)
                from core.decision_trace import record as _dec

                _rows = debate_rep.get("rows") or []
                _dec(
                    state,
                    "arbitrate",
                    f"独立校验官对这个报告提了 {len([i for i in adv_issues if _risk_high(i.get('risk'))])} 项高风险指控"
                    f"——被质疑方有没有申辩机会？",
                    [
                        "照单全收：把指控全部标『待核』（质疑方即法官，它永远赢）",
                        "让被质疑方申辩，再由第三方裁定（质疑 → 申辩 → 裁定）",
                    ],
                    "走完整辩论：" + "；".join(
                        f"「{r['claim'][:26]}」{VERDICT_LABEL_CN.get(r['verdict'], r['verdict'])}"
                        for r in _rows[:3]
                    ),
                    "单向审查里**质疑方永远是赢家**，而被质疑方没有申辩权——"
                    "一条数字被标『待核』，可能只是因为校验官没看到支撑它的那一页，"
                    "这个误判在单向流程里没有任何机会被纠正。"
                    "补上申辩与裁定，读者拿到的才是「这个争议解决了没有、怎么解决的」，"
                    "而不是「这里有争议」。",
                    evidence=(f"共辩 {debate_rep.get('total')} 条；"
                              f"未消解/未完成 {_open_n} 条（已计入不确定点）"),
                    trigger="hybrid",
                )
                if _open_n:
                    _trace(state, "deliver", f"辩论有 {_open_n} 条分歧未消解——已如实进附录，未按原表述静默交付")
        except Exception as _e:
            logging.getLogger(__name__).warning("多角色辩论失败（不阻断交付）: %s", _e)
        # 高风险"置顶红条"：披露不再只埋在可折叠文末附录，默认档也 30 秒可见（防带病无感交付）
        try:
            _adv_high = [i for i in adv_issues if _risk_high(i.get("risk"))]
            if _adv_high:
                _items = []
                for _i in _adv_high[:4]:
                    _nm = str(_i.get("number") or "").strip()
                    if not _nm:
                        _nm = (str(_i.get("issue") or "")[:14]) or "待核项"
                    _items.append(f"「{_clip_number_label(_nm)}」")
                _nums = "、".join(_items)
                _dots = "…" if len(_adv_high) > 4 else ""
                _lead = (
                    f"\n> ⚠️ **本次交付含 {len(_adv_high)} 项 RedTeam 判定『高风险』的关键数字**"
                    f"（{_nums}{_dots}）：正式采用前请先人工复核或改按『待核』引用，"
                    "详见文末『对抗性数字校验（Red Team Review）』。\n\n"
                )
                final = _lead + final
        except Exception:
            pass
    # P0-3 决策层就地降级（v0.9.47）：RedTeam 判『高风险』的数字若出现在
    # 执行摘要/决策清单/核心指标表/关键假设等决策头部区 → 就地标『（RedTeam待核）』，
    # 决策不再建立在自己判假/违背常识的锚上（病案：报告(6) 186杯/1.2杯/>63% 在决策内容未降级）。
    # 仅动头部区；正文证据区保留原值（文末 RedTeam 附录已如实披露）；幂等/容错/异常不阻断。
    try:
        from core.decision_guard import demote_high_risk_in_head

        if isinstance(adv_issues, list):
            final = demote_high_risk_in_head(final, adv_issues)
            # —— 决策：Red Team 判高风险的数字怎么处置 ——
            _hi_nums = [
                str(i.get("number") or "").strip()
                for i in adv_issues
                if _risk_high(i.get("risk")) and str(i.get("number") or "").strip()
            ]
            if _hi_nums:
                from core.decision_trace import record as _dec

                _dec(
                    state,
                    "degrade",
                    f"独立校验官判定 {len(_hi_nums)} 个关键数字『高风险』"
                    f"（{('、'.join(_hi_nums[:4]))}）——改数字、删数字，还是标出来？",
                    [
                        "按校验官建议改写数值（但我们无法确认它的建议值更对）",
                        "删除这些数字（读者失去了可能有用的线索）",
                        "保留原值 + 就地标『（RedTeam待核）』+ 决策头部区降级",
                    ],
                    "保留原值并在决策头部区就地标注，正文证据区保留原值",
                    "改数或删数都是**替读者做了本该他自己做的判断**，而且我们并不比校验官更确信哪个值对。"
                    "保留原值 + 显式标注，把判断权交回读者——"
                    "同时**把决策链路上对这些数字的依赖降级**，避免结论建立在自己判假的锚上。",
                    evidence="；".join(
                        f"{i.get('number')}：{str(i.get('issue') or '')[:40]}"
                        for i in adv_issues if _risk_high(i.get("risk"))
                    )[:200],
                    trigger="hybrid",
                )
    except Exception:
        pass
    # 行业基线沉淀（数据护城河）：本次调研关键指标存成行业卡片，下次同行业直接复用。
    # v0.9.47 护栏：把 RedTeam 高/中风险数字原文一并传入 → industry_kb 落库前确定性剔除，
    # 防"被自己判假/存疑的数字写成行业基线、下次跑再自复制"（报告(6)验收 13:24:48 病案）。
    try:
        from core.industry_kb import save_industry_card

        _red_tk_hi: list[str] = []
        _red_tk_med: list[str] = []
        if isinstance(adv_issues, list):
            for _i in adv_issues:
                _n = str(_i.get("number") or "").strip()
                if not _n:
                    continue
                if _risk_high(_i.get("risk")):
                    _red_tk_hi.append(_n)
                elif _risk_med(_i.get("risk")):
                    _red_tk_med.append(_n)
        _ind = _extract_industry(state.get("raw_question", ""))
        if _ind:
            save_industry_card(_ind, final, redteam_high=_red_tk_hi, redteam_med=_red_tk_med)
    except Exception:
        pass
    # ===== 全量坏URL过滤：正文所有 markdown 链接（含摘要/决策清单/方法论/附录）统一核验 =====
    # 证书无效/过期/失效/幻觉 URL → 数据源清空 + 正文转纯文字，
    # 防用户点开来源看到"连接非私密"或"页面无法访问"（本次修复核心）。
    _bad_urls: set[str] = set()
    _resolves: dict[str, str] = {}
    # **按合规姿态没去敲门的地址**——与"死链"是两件事，必须分开（2026-09-15 外部审计）。
    # 清洗层此前只按 `live` 判断，而合规跳过也是 `live=False` → 被当死链删掉。
    # 后果：同一份报告里，md 核验表标着「🚪 未探测（按合规姿态主动不访问）」，
    # 而清洗层已经把它的链接降级成纯文字、把 citations 的 url 清空了——**两层说法相反**。
    # 「没敲门」不等于「门是坏的」：保留链接、保留出处、交给读者自己判断，
    # 并在附录里如实报出这类地址有多少条。
    _unprobed_urls: set[str] = set()
    try:
        import re as _re2

        from core.verification import verify_urls

        pending = []
        for _m in _re2.finditer(r"\]\((https?://[^)\s]+)", final or ""):
            _u = _m.group(1).rstrip("。.,，,;；)")
            if _u.startswith("http") and _u not in _all_verdicts and _u not in pending:
                pending.append(_u)
        # 证据卡多来源页（v0.9.34 Evidence.sources 的第2/3个URL）也要过同一核验——
        # 否则死链/证书坏链漏到前端可点（ccfa ERR_CERT_DATE_INVALID、"点开原文404"主因）。
        for _ev in state.get("evidences") or []:
            for _s in getattr(_ev, "sources", None) or []:
                _eu = (getattr(_s, "url", "") or "").strip()
                if _eu.startswith("http") and _eu not in _all_verdicts and _eu not in pending:
                    pending.append(_eu)
        if pending:
            _all_verdicts.update(verify_urls(pending))
        # 四态：bad（域名也不存在，清空）/ unprobed（合规没敲门，保留）/ resolve / pass
        from core.verification import is_compliance_skipped as _is_skip

        _unprobed_urls = {
            u for u, (_res, _live, _d) in _all_verdicts.items() if not _live and _is_skip(_d)
        }
        _bad_urls = {
            u for u, (_res, _live, _d) in _all_verdicts.items()
            if not _live and not _is_skip(_d)
        }
        _resolves = {u: r for u, (r, _live, _d) in _all_verdicts.items() if _live and r != u}
        if _bad_urls or _resolves or _unprobed_urls:
            _url_notes = {
                u: (_d if not _live else "原文章页已失效，已转为文字摘要")
                for u, (_res, _live, _d) in _all_verdicts.items()
                if not _live or _res != u
            }
            _orig = citations
            citations = []
            for c in _orig:
                u = c.get("url", "")
                if not u or u in _bad_urls:
                    citations.append({**c, "url": "", "url_note": _url_notes.get(u, "")})  # 彻底失效 → 纯文字（宁空勿错）
                elif u in _unprobed_urls:
                    # **保留 url**，但把"我们没敲过这扇门"写在明面上。
                    # 删掉它等于用一个"死链"的判定去描述一件"我们没去核实"的事。
                    citations.append({**c, "url_note": _url_notes.get(u, "未探测（按合规姿态主动不访问）")})
                elif u in _resolves:
                    citations.append({**c, "url": _resolves[u], "url_note": _url_notes.get(u, "")})  # 页面失效 → 官网域名（能点开）
                else:
                    citations.append({**c, "url_note": ""})
    except Exception as e:
        logger.warning("正文链接核验失败: %s", e)
    # ===== 最终防线：报告正文坏链接清理 + citations URL 双保险（宁空勿错）=====
    # LLM 正文/搜索引擎可能把游戏站链接写进报告，这里在交付前统一清洗，
    # 防止用户点开"我的世界官网"等无关/误导链接。
    try:
        from agents.researchers import _is_clean_url, _is_junk_source

        def _strip_bad_links(md: str) -> str:
            """清理报告正文链接（防跳转无关站）：
            1. markdown [label](url)：url 过健康闸门，坏链接变纯文字
            2. 裸 URL：全部转纯文字（数据源面板已有真实链接，正文不残留可跳转裸链）
            """
            # `re` 用**模块级**那个（2026-09-17）：原先这里另有一行局部 `import re`，
            # 而同日我给 graph.py 补了模块级 import——作用域探针立刻报出遮蔽风险
            # （模块级名字被局部赋值挡住，是 node_deliver 那个 logger 病案的同族）。
            # 探针开的方子就是这一条：**删掉局部赋值，用模块级的那个**。

            def _rep(m):
                label, url = m.group(1), m.group(2)
                url = url.rstrip("。.,，,;；)")
                if url in _bad_urls:
                    return label  # 域名也不存在 → 纯文字（防幻觉链接）
                if url in _resolves:
                    return f"[{label}]({_resolves[url]})"  # 原页失效 → 回退机构官网（可点开）
                if _is_clean_url(url) and not _is_junk_source(label):
                    return m.group(0)
                return label

            # 兼容 [文字](url) 与 [文字](url "标题") 两种 markdown 链接形式
            md = re.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+[^)]*)?\)", _rep, md or "")
            # 裸 URL（非 markdown 链接内）→ 转纯文字，防止点击跳转无关站
            md = re.sub(r"(?<!\]\()(https?://[^\s\)\]<>\"'，。；、（）【】《》一-鿿　]+)", lambda m: "", md)
            # 裸 www 域名：前端 marked(GFM) 会把 `www.xx.com` 自链接成可点链接 →
            # 去掉 www. 前缀防自链（正文由数据源面板承载真实可访问链接）
            md = re.sub(r"(?<![\w:/])www\.[^\s\)\]<>\"'，。；、（）【】《》一-鿿　]+", lambda m: m.group(0).replace("www.", "", 1), md)
            return md

        final = _strip_bad_links(final)
        _cits2 = []
        for c in citations:
            u = c.get("url", "")
            if u and not (_is_clean_url(u) and u not in _bad_urls and not _is_junk_source(c.get("source", ""))):
                u = ""
                c = dict(c)
                c.setdefault("url_note", "该来源为词典/聚合/非调研信源，已转为文字摘要")
                c["url"] = ""
            _cits2.append(c)
        citations = _cits2
    except Exception:
        pass
    # ===== 专业研报 = 干净版；参赛痕迹(目标大赛五维达标自检·无得分 / AI透明度)只进"参赛对照版" =====
    # 客户拿到的是企业级研报，报告里出现"大赛评分对比/自评/AI使用披露"是产品错误。
    #
    # ⚠️ **注释已改正（2026-09-16）**：这里原写"需要给复核者看时，前端用『参赛自评对照版』
    # 按钮切换到 competition_report"——**那个按钮不存在**。前端有一处明确的相反决策
    # （`App.jsx` ReportCard：「只交付企业级干净研报——参赛对照版板块整体移除，
    # 大赛自检只在材料侧，不进产品/报告」），CSS 里只剩一个没人用的 `.comp-active`。
    # 所以这一份的**实际出口是 API**（`result["competition_report"]`，见 `_result_json` /
    # `_serialize_result`），供说明现场与 `_verify_v0970.py` 这类核验脚本取用；
    # **它不在产品界面上**。想让它进界面，是一个产品决策，不是接一根线的事。
    _comp_extra: list[str] = []

    def _comp_log(tag: str) -> None:
        logger.warning("参赛附录[%s]生成失败（已跳过该项）", tag, exc_info=True)

    # 红线补丁：参赛对照版不再外发"正大杯 Rubric 七维带分自评"（他赛维度 + 自评硬分外泄）；
    # score_report 仍作质量门内部自查用（见 quality_check），只是不再印进任何交付件。
    try:
        from core.rubric import competition_selfcheck, format_competition_md

        _comp_extra.append(format_competition_md(competition_selfcheck(state, final)))
    except Exception:
        _comp_log("目标大赛五维")
    # 假设驱动·验证章节（仅 causal 模式）：研究方法论的一部分，专业研报保留
    try:
        _plan0 = state.get("plan") or {}
        if getattr(_plan0, "question_type", "") == "causal" and getattr(_plan0, "hypotheses", []):
            _hyps = getattr(_plan0, "hypotheses", [])
            _evs0 = state.get("evidences") or []
            _sup = [ev for ev in _evs0 if not str(getattr(ev, "claim", "")).startswith("【反方】")]
            _ctr = [ev for ev in _evs0 if str(getattr(ev, "claim", "")).startswith("【反方】")]
            _hl = ["\n\n---\n\n# 🧪 研究假设与验证（假设驱动 · 因果模式）"]
            # **口径必须写在读数旁边**（2026-09-15 深访实跑）。
            # 病案：下面的 `_sup`/`_ctr` 是**全报告**证据，与具体是哪条假设无关，
            # 于是实跑报告里 H1/H2/H3 三条的"支持证据 50 条（高置信 10）· 反证 7 条"
            # **一字不差地相同**。读者看到的是"每条假设都经过 50 条证据检验"，
            # 实际是"报告一共有 50 条证据"——**读数没错，标签错了**。
            #
            # 为什么不改成按假设逐条归属：归属只能靠词面重合猜，而"猜出来的归属"
            # 会产出看起来精确、实际错配的分布——那比"如实说这是全报告口径"更糟。
            # 本仓库在遗漏层已经吃过一次同样的教训（漏报/误报的代价不对称）。
            _hl.append(
                f"> **口径**：本节的支持/反证条数是**全报告**口径（全部 {len(_sup)} 条正面证据 / "
                f"{len(_ctr)} 条反方证据），**未按假设逐条归属**——三条假设下出现相同数字是口径如此，"
                "不是每条假设各自经受了那么多证据的检验。"
            )
            for _h in _hyps:
                _sup_high = sum(1 for e in _sup if getattr(e, "confidence_level", "") == "high")
                _hl.append(f"\n**{_h}**")
                _hl.append(f"- 支持证据（全报告口径）：{len(_sup)} 条（高置信 {_sup_high}）")
                _hl.append(f"- 反证证据（全报告口径）：{len(_ctr)} 条")
                # **因果模式下的措辞不能让人以为"有因果证据"**（GPT 评审 2026-09-15）。
                # 原话是「反证偏弱，假设暂不证伪」——它读起来像"检验过了、没被推翻"，
                # 而这一节**根本没有做因果识别**（识别要 DML/uplift 一类方法，
                # 见本节末尾那条自陈）。相关性证据多 ≠ 因果关系成立。
                _hl.append(
                    "- 初步判断："
                    + ("存在显著反证，假设需谨慎"
                       if len(_ctr) >= len(_sup) and _ctr
                       else "**该机制假设尚未获得因果识别证据**，当前仅有描述性/相关性支持")
                )
            _hl.append("\n> 假设检验是因果类研究的标准方法：支持与反证证据表 + 强度判断；因果量级建议用 DML/uplift 等因果方法验证，禁止用相关断言因果。")
            final += "\n".join(_hl)
    except Exception:
        pass
    # AI 透明度（参赛/合规用，默认只进参赛对照版；企业研报不"自爆"）
    try:
        from core.transparency import build_transparency_md

        _comp_extra.append(build_transparency_md("competition"))
    except Exception:
        _comp_log("AI透明度")
    try:
        from urllib.parse import urlparse as _up_d

        _hosts_d, _gov_d = set(), 0
        for _c in citations:
            _u = (_c.get("url") or "").strip()
            if not _u.startswith("http"):
                continue
            _h = (_up_d(_u).netloc or "").lower().split(":")[0]
            _hosts_d.add(_h)
            if _h.endswith("gov.cn") or ".gov" in _h:
                _gov_d += 1
        _nongov_d = max(len(_hosts_d) - _gov_d, 0)
        __import__("logging").getLogger(__name__).info(
            "deliver完成: citations=%d条, 有URL=%d条, 域名=%d个(非gov %d个), 清掉=%d条, 未探测保留=%d条 | 来源类型应多元(政府/研报/媒体/财报)",
            len(citations),
            sum(1 for c in citations if c.get("url")),
            len(_hosts_d),
            _nongov_d,
            len(_bad_urls),
            len(_unprobed_urls),
        )
    except Exception:
        pass
    # 最终兜底：五维自检/假设/AI透明等附录都追加完后，正文可能夹带的坏链接/裸链再清一遍（幂等）
    try:
        import re as _re3

        from agents.researchers import _is_clean_url, _is_junk_source

        def _rep3(m):
            label, url = m.group(1), m.group(2)
            url = url.rstrip("。.,，,;；)")
            if url in _bad_urls:
                return label
            if url in _resolves:
                return f"[{label}]({_resolves[url]})"
            if _is_clean_url(url) and not _is_junk_source(label):
                return m.group(0)
            return label

        final = _re3.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+[^)]*)?\)", _rep3, final or "")
        final = _re3.sub(r"(?<!\]\()(https?://[^\s\)\]<>\"'，。；、（）【】《》一-鿿　]+)", lambda m: "", final)
        final = _re3.sub(r"(?<![\w:/])www\.[^\s\)\]<>\"'，。；、（）【】《》一-鿿　]+", lambda m: m.group(0).replace("www.", "", 1), final)
    except Exception:
        pass
    # ── 决策链占位符回填（2026-09-17）────────────────────────────────────────
    # **所有 `record()` 到这里都已经跑完**（最后一条记在 `degrade`，远在上面），
    # 所以现在渲染出来的才是"每一次取舍"的完整一份。
    # 回填前用全量 `state["decisions"]` 渲染，回填后**断言计数一致**——
    # 这个断言正是用来防止本缺陷复发的：附录条数 == payload.decision_chain 长度。
    if _DEC_SLOT in final:
        try:
            from core.decision_trace import render_appendix_md as _dec_md

            _final_decs = list(state.get("decisions") or [])
            _dec_md_text = _dec_md(_final_decs)
            final = final.replace(_DEC_SLOT, _dec_md_text)
            # 一致性自检：**渲染出来的条数必须等于最终 decisions 的长度**。
            # 不等就说明还有 record 发生在这一行之后（本缺陷的复发形态），如实告警。
            _n_dec = len(_final_decs)
            if _dec_md_text.count("**处境**") != _n_dec:
                logging.getLogger(__name__).warning(
                    "决策链计数不一致：附录渲染 %d 条 / decisions 共 %d 条——"
                    "有 record 发生在回填之后，报告会与徽章对不上",
                    _dec_md_text.count("**处境**"), _n_dec,
                )
        except Exception as _de:
            # **不许把占位符漏进交付文本**：渲染失败就清掉它，并留痕。
            final = final.replace(_DEC_SLOT, "")
            logging.getLogger(__name__).warning("决策链附录回填失败（不阻断交付）: %s", _de)
    # ── 主体级反证提醒：同一句话也钉到**结论处**（见 _inject_subject_tension）
    final = _inject_subject_tension(final)
    # ── 遗漏层（v0.9.75；2026-09-15 移到**最后**）───────────────────────────────
    # 为什么不在这里图省事：这个扫描数的是"正文里有多少个没来源的数字断言"，
    # 而上面每一道链接清理都会**改写正文**（坏链接降级为纯文字、裸 URL 整段删除）。
    # 扫在清理前 → 数出来的是"一个读者根本看不到的版本"；且重跑时序不同就得不同的数。
    # 现在扫的是**交付给读者的那一份文本**：稳定、可复现、与读者所见一致。
    # 位置在全部附录追加与全部清理之后，所以 `final` 此刻已经定型。
    try:
        from core.omission import format_appendix_md as _omi_md, scan as _omi_scan

        omission_report = _omi_scan(final)
        final += _omi_md(omission_report)
    except Exception as _oe:
        logging.getLogger(__name__).warning("遗漏层扫描失败（不阻断交付）: %s", _oe)
    # ===== 决策完整性闸门（Decision Integrity Gate）=====
    # 位置是刻意的：**全部核验层跑完之后**（规则/RedTeam/质检/遗漏都在上面），
    # `final` 此刻已经定型，闸门看到的是**读者将要拿到的那份文本**。
    #
    # 它不新增任何检测能力——它给已有检测接一个动作。在此之前，各层都能"发现问题"，
    # 而问题原样留在执行摘要、核心指标表和决策依据里参与叙事（2026-09-15 深访实跑：
    # 68.4% 与 14.2万亿 全被自家机制点名过，又全部留在决策区）。
    # 闸门只做两件事：判定 Go→Validate（**只降不升**），以及把"为什么"置顶。
    integrity_gate: dict = {}
    _gate_verdict = str(state.get("decision_verdict") or "")
    try:
        from core.integrity_gate import apply_to_report as _ig_apply, evaluate as _ig_eval

        integrity_gate = _ig_eval(
            report=final,
            rule_findings=_rule_v,
            adv_issues=adv_issues if isinstance(adv_issues, list) else [],
            omission=omission_report or {},
            quality_report=state.get("quality_report"),
            # **引擎自报越界**（确定性事实，优先于文本复核）。
            # 病案（2026-09-16）：这里原先读 `state["sizing_violation"]`——一个**全仓没有写入方**
            # 的键，所以永远是空串，`integrity_gate` 声称的"引擎侧 + 文本侧两层互补"
            # 实际只有文本正则这一道。真正的标记在 `subtask_results["_sizing_violation"]`。
            sizing_violation="；".join(
                str(v) for v in ((state.get("subtask_results") or {}).get("_sizing_violation") or {}).values()
            ),
            verdict=_gate_verdict,
        )
        if integrity_gate.get("level") != "PASS":
            final, _gate_verdict = _ig_apply(final, integrity_gate, _gate_verdict)
            _trace(
                state, "deliver",
                f"决策完整性闸门 {integrity_gate['level']}"
                + (f"：判定 {integrity_gate.get('downgraded_from')} → {integrity_gate.get('downgraded_to')}"
                   if integrity_gate.get("downgraded_to") else
                   f"（{len(integrity_gate.get('warnings') or [])} 项非决策区问题）"),
            )
    except Exception as _ige:
        logging.getLogger(__name__).warning("决策完整性闸门执行失败（报告原样交付）: %s", _ige)
    # 研究经验落盘：把本次「源 × 调用 × 命中」计数写进跨任务经验库（供下次规划参考）。
    # 放在交付末端是因为 `source_trace` 到这里才完整。失败不阻断交付。
    try:
        from core.exp_memory import record_run as _exp_record

        _exp_record(state)
    except Exception as _e:
        logging.getLogger(__name__).warning("研究经验落盘失败（不影响交付）: %s", _e)
    # 参赛对照版 = 干净报告 + 参赛痕迹附录（目标大赛五维达标自检·无得分 + AI透明度）；默认不进正式报告
    _competition_md = ""
    if _comp_extra:
        _competition_md = final + "\n\n".join(_comp_extra)
    return {
        "report_final": final,
        # **回吐全量证据**（2026-09-16 修）。理由见下方流式组装处那条病案：
        # `check_conflict` 是**就地改对象、不回吐 `evidences`** 的，
        # 于是"每个节点返回时抓快照"的做法永远抓不到那次改动。
        # deliver 是最后一个节点，它回吐的就是**最终状态里的对象**——
        # 这才是与正文（正文同样读 state 对象）**同源**的那一份。
        # 注意：这只是为了让 result 拿到正确对象，**不是"新证据"**——
        # 流式循环里对 deliver 节点要跳过"证据事件"推送，否则前端证据卡会整份翻倍。
        "evidences": list(state.get("evidences") or []),
        "competition_report": _competition_md,
        "citations": citations,
        "citation_report": cite_report,
        "bad_urls": sorted(_bad_urls),
        # 按合规姿态**没去敲门**的地址（≠ 死链）：保留在正文与 citations 里、附「未探测」标注，
        # 但不计入通过率分母。单列出来，好让附录与看板说得清"哪些是我们没核实的"。
        "unprobed_urls": sorted(_unprobed_urls),
        "url_fixes": dict(_resolves),
        "status": "done",
        "metrics": metrics,
        # 决策完整性闸门结论 + **闸门降级后的判定**（可能是 Go→Validate）。
        # 必须随结果出去：前端角标、决策回访入库、复购看板都读 `decision_verdict`——
        # 只在正文里改而这里不改，等于**正文说 Validate、数据层还记着 Go**，
        # 两处不一致比不降级更难查。
        "integrity_gate": integrity_gate,
        "decision_verdict": _gate_verdict,
        # 自主决策链：交付用快照（累积日志是 state["decisions"]）。
        # **必须是 TaskState 里的真键**——先前只写在返回 dict 里，被 LangGraph 静默丢弃，
        # 结果同步路径（/api/execute、run_task）拿到的是空列表，而 SSE 路径侥幸正常。
        "decision_chain": list(state.get("decisions") or []),
        # 引用可信度两节：链接层（citation_report.pass_rate）+ 内容层（entailment.pass_rate）。
        # 前端交付物头部用它显示"引用支撑率"——**这是本产品唯一一个"能被当场证伪"的指标**，
        # 也是与外部研究同层可比的那一个（见 core/entailment.INDUSTRY_BASELINE）。
        "entailment": {
            "pass_rate": (entail_report or {}).get("pass_rate", 0.0),
            "support_rate": (entail_report or {}).get("support_rate", 0.0),
            "judged": (entail_report or {}).get("judged", 0),
            "pairs": (entail_report or {}).get("pairs", 0),
            "unjudged": (entail_report or {}).get("unjudged", 0),
            "index_only": (entail_report or {}).get("index_only", 0),
            "counts": (entail_report or {}).get("counts", {}),
            "executed": bool(entail_report),
            # 遗漏层（v0.9.75）：与支撑率**必须并排出现**。只报"支撑率 100%"会被读成
            # "核验器太松"（公开基线 DeepTRACE 40–80%）；并排报出"另有 N 处裸断言"才叫自审。
            #
            # **`executed` 必须有**（2026-09-15 外部审计）：没有它，"扫描崩了"与
            # "claims=0（正文确实没有数字断言）"在数据上**完全不可区分**，
            # 而前端角标在 claims 为真值时才渲染 —— 于是崩溃的表现为**角标静默消失**，
            # 读起来是"这次没什么问题"。本仓库刚在链接核验上修过同型缺陷（0% vs 未测）。
            "omission": {
                "claims": (omission_report or {}).get("claims", 0),
                "cited": (omission_report or {}).get("cited", 0),
                "labeled": (omission_report or {}).get("labeled", 0),
                "derived": (omission_report or {}).get("derived", 0),
                "omitted": (omission_report or {}).get("omitted", 0),
                "cite_rate": (omission_report or {}).get("cite_rate"),
                # 裸断言率单列：**不该由消费端拿 1-cite_rate 去算**——
                # 分母含"自陈不确定"与"本报告自算"两档，它们本就不该有外部来源。
                "bare_rate": (omission_report or {}).get("bare_rate"),
                "samples": (omission_report or {}).get("samples") or [],
                # **`excluded_plan_lines` 必须进投影**（2026-09-17 实跑修复）。
                # 病案：`scan()` 算出了它，`method` 文本里还写着"条数见 `excluded_plan_lines`"，
                # 而这里的手写白名单**没抄它** → 那句承诺印进了每一份报告，
                # 字段却在报告/存档/result 里都不存在。
                # 手写白名单的病就在这儿：**加一个键要记得改两个地方，漏一个不会报错，只会静默少一个数。**
                "excluded_plan_lines": (omission_report or {}).get("excluded_plan_lines", 0),
                "method": (omission_report or {}).get("method") or "",
                "executed": bool(omission_report),
            },
            # 核验器自校准（v0.9.77）：支撑率的分母是我们自己判的，
            # 所以必须并排给出"这个核验官会不会把错的判成对的"。
            "calibration": {
                "fpr": (calibration or {}).get("fpr"),
                "fpr_loose": (calibration or {}).get("fpr_loose"),
                "tpr": (calibration or {}).get("tpr"),
                "n_pos": (calibration or {}).get("n_pos", 0),
                "n_neg": (calibration or {}).get("n_neg", 0),
                "ran_at": (calibration or {}).get("ran_at", ""),
                "executed": bool(calibration),
            },
        },
        "citation_pass_rate": (cite_report or {}).get("pass_rate", 0.0),
        "deliverables": [
            {"type": "report_markdown", "name": "研究报告.md"},
            {"type": "decision_sop", "name": "决策清单.md"},
            {"type": "evidence_chain", "count": len(state["evidences"])},
            {"type": "arbitration_cards", "count": len(state["conflict_results"])},
        ],
    }


# ============================================================
# 状态机构建
# ============================================================
def build_graph():
    """构建 LangGraph 状态机"""
    g = StateGraph(TaskState)

    # 节点
    g.add_node("router", node_router)
    g.add_node("human_confirm", node_human_confirm)
    g.add_node("route_skill", node_route_skill)
    g.add_node("industry", node_industry)
    g.add_node("competition", node_competition)
    g.add_node("user", node_user)
    g.add_node("statistics", node_statistics)
    g.add_node("check_conflict", node_check_conflict)
    g.add_node("replan", node_replan)
    g.add_node("summarize", node_summarize)
    g.add_node("budget_check", node_budget_check)
    g.add_node("quality_check", node_quality_check)
    g.add_node("rework_fix", node_rework_fix)
    g.add_node("decision_sop", node_decision_sop)
    g.add_node("asset_sink", node_asset_sink)
    g.add_node("deliver", node_deliver)

    # 边（主线）
    g.add_edge("router", "human_confirm")
    g.add_edge("human_confirm", "route_skill")
    g.add_edge("route_skill", "industry")
    g.add_edge("route_skill", "competition")
    g.add_edge("route_skill", "user")
    g.add_edge("route_skill", "statistics")
    g.add_edge("industry", "check_conflict")
    g.add_edge("competition", "check_conflict")
    g.add_edge("user", "check_conflict")
    g.add_edge("statistics", "check_conflict")
    g.add_edge("check_conflict", "replan")
    g.add_edge("replan", "summarize")
    g.add_edge("summarize", "budget_check")
    g.add_edge("budget_check", "quality_check")
    g.add_edge("asset_sink", "deliver")
    g.add_edge("deliver", END)

    # 质检返工：不通过 → 先返工修复（注入致命项重跑相关子Agent）→ 再汇总复检
    # 通过 → 决策SOP → 资产沉淀 → 交付
    g.add_conditional_edges(
        "quality_check",
        route_after_quality,
        {
            "finalize": "decision_sop",
            "rework": "rework_fix",
        },
    )
    g.add_edge("rework_fix", "summarize")
    g.add_edge("decision_sop", "asset_sink")

    g.set_entry_point("router")
    if _CHECKPOINTER is not None:
        return g.compile(checkpointer=_CHECKPOINTER)
    return g.compile()


def run_task(
    raw_question: str,
    clarified_context: dict[str, str] | None = None,
    plan_override: ResearchPlan | None = None,
) -> dict[str, Any]:
    """运行一次完整调研任务（顶层入口，同步版）"""
    import time as _t

    # token 计量归零（v0.9.75）：不归零的话"单份成本"会变成"进程历史总成本"，
    # 而算账页正是拿它当分子。
    _seq0 = None
    try:
        from core.llm import reset_usage as _ru

        _seq0 = _ru()          # 返回本次运行序号，落账时用来判断有没有被并发重置
    except Exception:
        pass
    graph = build_graph()
    state = init_state(raw_question, clarified_context, plan_override)
    # 编译了 checkpointer 的图必须带 thread_id 才能 invoke（修复同步路径崩溃）
    config = {"configurable": {"thread_id": f"t{abs(id(state))}"}}
    _t0 = _t.monotonic()
    result = graph.invoke(state, config=config)
    # 运行统计：**与流式路径共用同一个构造函数**。
    # 此处原有一份内联副本（字段集与 _build_run_stats 不同）——正是这种"两份实现"
    # 让上一轮的 run_stats 落账在两条路径上表现不一致。统一后不会再漂移。
    try:
        result["run_stats"] = _build_run_stats(result, _t.monotonic() - _t0, _seq0)
    except Exception:
        pass
    # 可复算包（同步路径同样产出，与流式路径对齐）
    _attach_audit_bundle(result, state, raw_question, graph, config)
    return result


def _seq_now() -> int:
    """当前计量器序号（拿不到时返回 -1：与任何真实起始序号都不同 → 会标成共享，宁严勿松）"""
    try:
        from core.llm import usage_seq

        return usage_seq()
    except Exception:
        return -1


def _build_run_stats(
    result: dict, elapsed_s: float, usage_seq_at_start: int | None = None
) -> dict[str, Any]:
    """运行统计（MR-Bench 台账原语）：耗时/成本/返工/检索轮次/独立域名数/判定。

    历史 bug：这段只在**同步路径 run_task** 里构造；**流式路径（API 实际走的）从未设置
    `result["run_stats"]`** → `log_run_stats` 拿到 None 直接静默返回 → 账本永远写不进去
    （实测两笔跑完、industry_kb 已沉淀，账本仍 total=0）。抽成公共函数供两条路径共用。
    """
    _doms: set[str] = set()
    for _c in result.get("citations") or []:
        _u = str((_c or {}).get("url") or "")
        if _u.startswith("http"):
            try:
                from urllib.parse import urlparse as _up

                _h = (_up(_u).hostname or "").lower().removeprefix("www.")
                if _h:
                    _doms.add(_h)
            except Exception:
                pass
    _dom = len(_doms)
    # 成本核算（v0.9.75）：此前 `budget_spent` 恒为 0 —— 不是没花钱，是**从来没记过**。
    # 现在记**真实 token 用量**（事实），钱按可配置单价算；没配单价如实给 None。
    _usage: dict[str, Any] = {}
    _cost: float | None = None
    try:
        from core.llm import usage_snapshot, estimate_cost_cny

        _usage = usage_snapshot()
        _cost = estimate_cost_cny(_usage)
    except Exception:
        _usage = {}
    # 「成功结果成本」的分母：任务成功率口径 = 是否产出可交付报告（非中途失败）
    _ok = 1 if (result.get("report_final") or result.get("report")) else 0
    return {
        "elapsed_s": round(elapsed_s, 1),
        "budget_spent": round(result.get("budget_spent", 0) or 0, 2),
        "rework_count": result.get("rework_count", 0) or 0,
        "search_rounds": len(result.get("search_debug") or []),
        "citation_count": len(result.get("citations") or []),
        "domain_count": _dom,
        "decision_verdict": result.get("decision_verdict", "") or "",
        # ---- 成本与质量（复购看板四个指标里我们此前一个都算不出的那两个）----
        "llm_tokens": _usage,
        # 并发抹账标记：本次期间计量器被别的任务重置过 → 这个 token 数**不是本次的实测值**。
        # 落账带上它，看板就能把这一笔排除在成本均值之外——
        # **报一个混过的数比报不出来更糟**（它会被当成实测值）。
        "llm_shared": bool(usage_seq_at_start is not None and _seq_now() != usage_seq_at_start),
        "cost_cny": _cost,          # None = 未配置单价，**不猜**
        "delivered": _ok,           # 任务成功率分子
        # 降级也要落账：一份"成功交付"但决策清单是模板的报告，不该和正常报告同样计分
        "degraded": bool(result.get("sop_fallback") or {}),
        # 复购看板另两位（培训点名建议带上台的四个指标）
        "took_over": bool(result.get("stop_card")),      # 人工接管率分子
        "reused_asset": bool(result.get("asset_reused")),  # 有效复用率分子
        # 遗漏层读数（v0.9.79）：只取四个计数，样例不进账本（体积）。
        #
        # ⚠️ **落账的原始动机是"不落账 = 跨报告看板永远看不到这一层"——
        # 而它此前落进去的恒为空**（2026-09-16）。因为 `omission` **不是 `TaskState` 的键**：
        # 遗漏层的完整读数只作为 `entailment` 块的一个子键回吐（见 node_deliver 的
        # `"entailment": {..., "omission": {...}}`）。所以 `result.get("omission")`
        # 在**两条路径上**都取不到东西——这是同一家族里第四个独立断链：
        # 值恒为空 + 落进去后也没有任何读取方。
        # 这里按真实位置取；两条路径都能命中（state 与 deliver 增量都带 entailment）。
        "omission": dict(
            result.get("omission")
            or (result.get("entailment") or {}).get("omission")
            or {}
        ),
    }


def _log_run_stats_safe(result, raw_question, resume, plan_override, state) -> None:
    """run_stats 落账：在 **graph 层**完成，不依赖 SSE 客户端是否存活。

    历史 bug：落账原先写在 app.py 的 SSE 生成器 for 循环**之后**——客户端断开/切标签页
    导致生成器被关闭时，循环后那段永不执行 → 任务跑完但账本仍为空（实测两笔未落账）。
    resume 不重复计一笔。
    """
    # ⚠️ **`resume` 不再直接 return**（2026-09-15 外部审计）。
    # 原先第一行是 `if resume: return`，注释理由是"resume 不重复计一笔"——
    # 它假定**首跑已经落过账**。而暂停路径上，首跑恰恰在落账点**之前**被 abort 掐断
    # （SSE 生成器在 for 循环里被关闭，那段没有 finally）。于是前提不成立：
    # **一条真跑完的报告，账本里 0 行**——看板分母少一笔、单份成本少一笔，而且无人察觉。
    #
    # 现在改为：**交付就落账**，并用 `resumed` 标记它是续跑的那一次。
    # 这不会重复计：前端只在「暂停」后提供「继续」，而暂停那次的账从未落过；
    # 万一将来出现"成功后仍续跑"的路径，`resumed` 字段让重账**可被识别**（而不是不可能）。
    try:
        from core.runstats import log_run_stats

        # 档位取值必须走**已经跑完的那份计划**，不能用 `state`。
        #
        # 病案（2026-09-15 深访实跑）：账本 7 笔**全部**写着 `depth=标准`——
        # 包括一笔明确跑的是「深入」（报告里有 `st_*_deep` 子任务、路由审计写着 16 个维度，
        # 深入档才会生成这些）。也就是说，**账本的档位字段从来没记过真实档位**。
        #
        # 根因：`run_task_stream` 里的 `state` 是 `init_state(...)` 的返回值，
        # 而 `graph.stream(...)` 的产出**从来没有回写进它**。于是这里读到的
        # `state["plan"]` 是 init_state 造的那个**空计划**（`plan_override or ResearchPlan(...)`），
        # 而 `ResearchPlan.depth` 的默认值正是 `DepthLevel.STANDARD` ——
        # 一个恒定的兜底值，被当成"实测档位"记了 7 笔。
        #
        # 后果：复购看板按档位筛选（上一轮刚把 `standard`/`标准` 两种写法归一）
        # **分出来的永远是同一堆**；「成功结果成本」的档位维度不成立。
        # 计费不受影响（app.py 用请求里的 depth 扣分），所以这是**只错账、不错钱**的一处——
        # 也正因为不痛，它活了很久。
        #
        # 取值顺序：用户计划 override → **已跑完的结果里的计划** → 兜底。
        _p = plan_override if plan_override is not None else None
        if _p is None:
            _rp = (result or {}).get("plan")
            if isinstance(_rp, dict) and _rp.get("depth"):
                _d = _rp.get("depth")
            elif _rp is not None and getattr(_rp, "depth", None) is not None:
                _d = _rp.depth
            else:
                # 最后才回退到 state；走到这里说明结果里连计划都没有（异常路径）
                _d = getattr((state or {}).get("plan"), "depth", None)
        else:
            _d = getattr(_p, "depth", None)
        if hasattr(_d, "value"):
            _d = _d.value
        _rs = dict(result.get("run_stats") or {})
        _rs["resumed"] = bool(resume)
        # 提示词指纹进账本（2026-09-17）：**同一份报告在不同提示词下跑出来的是两份东西**。
        # 没有这个字段，看板上的"成功率/成本"跨提示词版本求平均——**分母混了两种产品**。
        # 与 `depth` 那次是同一个道理（分母口径不一致 → 指标不成立），
        # 只是 `depth` 混的是档位、这里混的是**指令正文**。
        # 取不到指纹不阻断落账（`prompt_set()` 自己 fail-soft，取不到会得到一个
        # 含 `missing` 的指纹值——**退化会改变读数，而不是悄悄通过**）。
        try:
            from core.prompt_registry import prompt_set as _pset

            _rs["prompt_set"] = _pset()
        except Exception:
            pass
        # 假设集指纹进账本（2026-09-18 第十跑）：`prompt_set` 管判据、它管假设——
        # **两个都进账本，跨跑可比性才完整**。只印在报告里而账本不收，
        # 就等于"这一笔和上一笔的假设是不是同一套"在账本层面**永远查不到**，
        # 看板会照旧把两套假设的两笔当成同一个产品的两笔（与 `depth` 混档同型）。
        try:
            from core.assumption_set import fingerprint as _afp

            _a = _afp(str(result.get("report") or ""))
            if _a:
                _rs["assumption_set"] = _a
        except Exception:
            pass
        log_run_stats(str(_d or "standard"), raw_question, _rs)
    except Exception as e:
        try:
            __import__("logging").getLogger(__name__).warning("run_stats 落账失败(不阻断交付): %s", e)
        except Exception:
            pass


# `node_deliver` **不回吐、但下游要用的** TaskState 字段：流式路径从图内最终状态补齐。
#
# 病案（2026-09-16 / 2026-09-19）：`deliver` 的返回 dict 只列了 16 个键，而
# `run_task_stream` 是拿 `final_payload = deliver 那一个节点的 delta` 去喂 `_result_json` 的
# ——凡是 deliver 没回吐、`_result_json` 又照读的字段，在**流式路径（也就是演示走的那条）
# 上恒为初值**。历史上逐个补，于是补漏了两次：
#   · `sop_fallback` 恒 {} → 前端「⚠️ 决策清单为兜底模板」红角标**永不出现**；
#   · `conflict_scan` 恒缺失 → 「（比对 N 对）」分母永不显示，tooltip 反过来说
#     「本次未产出冲突检测读数（旧报告）」；
#   · `trace_events` 恒 []（2026-09-19 补）→ done 事件里轨迹是空的，而流上推了 30+ 条。
#     ⚠️ **它在这一组里，但真正填它的是流本身**（见下方组装处那段注释）：
#     `_trace()` 是就地 append、不返回 delta 的，图状态里这个键**永远是空的**——
#     从 `_final_state` 补不出来。留在这里只作兜底（图状态万一有值就用）。
#     这是本名单最值得记住的一课：**同一个名单里的键，来源可以完全不同**。
# 教训不是"又漏了一个"，而是**这份名单本身没有牙**。所以它现在是一个有名字的常量，
# 并且由 `_fulltest.py` 机械核对：「凡是 TaskState 字段、被 `_result_json` 消费、
# deliver 又没回吐的，必须在这份名单里」——加字段忘了改这里，测试会红。
_DELIVER_BACKFILL_KEYS: tuple[str, ...] = (
    "conflict_scan",
    "sop_fallback",
    "stop_card",
    "rework_count",
    "asset_reused",
    "budget_spent",
    "trace_events",
)


def run_task_stream(
    raw_question: str,
    clarified_context: dict[str, str] | None = None,
    plan_override: ResearchPlan | None = None,
    thread_id: str = "",
    resume: bool = False,
):
    """运行一次完整调研任务（流式版，供 SSE 实时推送）

    用 LangGraph 的 stream() 逐节点产出事件：
    - node_enter: 节点开始
    - node_exit: 节点结束（带该节点写入的状态增量）
    - 前端据此实时渲染 Trace / 证据，像 DeepSeek 思考过程一样逐条出现。

    thread_id + resume：用户暂停后点"继续"→ 用同一 thread_id 从 checkpoint 恢复，
    从上次中断节点续跑（不重新生成计划/不重复已完成的检索）。

    yield 事件格式：
    {"type": "trace", "node": ..., "detail": ..., "status": "running|done"}
    {"type": "evidence", "evidence": {...}}
    {"type": "done", "result": {...}}
    """
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id or f"t{id(graph)}"}}
    import time as _t

    # token 计量归零（v0.9.75）：resume 不归零——续跑是同一笔任务的延续，
    # 归零会把"这一份报告花了多少"截成两段，两段都对不上账。
    _seq0 = None
    if not resume:
        try:
            from core.llm import reset_usage as _ru

            _seq0 = _ru()
        except Exception:
            pass
    else:
        # 续跑不归零（它是同一笔任务的延续），**但起始序号仍要记**——
        # 计量器里还留着暂停前那段，若期间有别的任务重置过，读到的就是混过的数。
        try:
            from core.llm import usage_seq as _us

            _seq0 = _us()
        except Exception:
            pass
    _t0 = _t.monotonic()  # 运行计时（run_stats.elapsed_s 用）
    if resume and _CHECKPOINTER is not None:
        # 恢复：从 checkpoint 续跑（事件从断点开始，不重复已完成步骤）
        try:
            snap = graph.get_state(config)
            if snap is None or not getattr(snap, "values", None):
                raise ValueError("checkpoint 不存在")
            events_iter = graph.stream(None, config=config, stream_mode="updates")
            state = snap.values or {}
        except Exception:
            # checkpoint 丢失（如后端重启）→ 降级从头执行，不崩
            state = init_state(raw_question, clarified_context, plan_override)
            events_iter = graph.stream(state, config=config, stream_mode="updates")
    else:
        state = init_state(raw_question, clarified_context, plan_override)
        events_iter = graph.stream(state, config=config, stream_mode="updates")

    # 流式过程中累积"最终交付所需字段"，避免流结束后对整个状态机二次 invoke。
    # 二次 invoke 会重复执行全部节点（成本翻倍、更慢），且偶发异常会让前端显示"执行失败"。
    last_quality: dict[str, Any] = {}
    final_payload: dict[str, Any] | None = None
    decision_payload: dict[str, Any] = {}
    streamed_evidences: list[Any] = []
    streamed_conflicts: list[Any] = []
    # 流式轨迹：**交付结果里的 trace_events 只能从这里来**（2026-09-19 修，见下）。
    streamed_traces: list[dict[str, Any]] = []

    for event in events_iter:
        for node_name, node_updates in event.items():
            # 防御：图结束时 LangGraph 会发 __end__ 事件（node_updates=None），直接跳过
            if node_updates is None:
                continue
            # 累积交付数据（先于 yield，避免下游读取缺失）
            if node_name == "quality_check":
                last_quality = dict(node_updates)
            if node_name == "decision_sop":
                decision_payload = dict(node_updates)
            if node_name == "deliver":
                final_payload = dict(node_updates)
            # ⚠️ **deliver 回吐的全量证据不是"新证据"**（2026-09-16）：
            # 它回吐是为了让 `result` 拿到含冲突标记的**最终对象**（见组装处病案）。
            # 若在这里照单累积/推送，前端证据卡会在收尾时整份翻倍。
            _new_evs = [] if node_name == "deliver" else (node_updates.get("evidences") or [])
            streamed_evidences.extend(
                _evidence_json(ev) for ev in _new_evs
            )
            streamed_conflicts.extend(node_updates.get("conflict_results") or [])

            # 节点开始
            streamed_traces.append(
                {"node": node_name, "detail": _node_hint(node_name, node_updates),
                 "ts": datetime.now().isoformat(), "status": "running"}
            )
            yield {
                "type": "trace",
                "node": node_name,
                "detail": _node_hint(node_name, node_updates),
                "status": "running",
            }

            # 渐进式执行：router 生成计划 → 内联推送（前端在思考流中折叠展示，
            # 替代"确认计划"阻塞卡；用户可随时暂停编辑计划）
            if node_name == "router" and node_updates.get("plan"):
                try:
                    _p = node_updates["plan"]
                    _plan_json = _p.model_dump(mode="json") if hasattr(_p, "model_dump") else _p
                    yield {"type": "plan", "plan": _plan_json}
                except Exception:
                    pass

            # 节点产出证据 → 立即推送（deliver 的回吐不算，理由见上）
            evs = [] if node_name == "deliver" else (node_updates.get("evidences") or [])
            for ev in evs:
                yield {"type": "evidence", "evidence": _evidence_json(ev)}

            # 节点产出冲突仲裁卡片
            for cf in node_updates.get("conflict_results") or []:
                yield {"type": "conflict", "conflict": cf}

            # 预算预警：已用超80% → 推送征询事件（前端弹"继续/中止"）
            if node_name == "budget_check" and node_updates.get("budget_flag"):
                yield {
                    "type": "budget",
                    "spent": state.get("budget_spent", 0),
                    "estimate": state.get("budget_estimate", 0),
                    # 口径（2026-09-15 统一）：`spent` 与 `estimate` **同为档位积分**
                    # （`estimate` 由 `agents.router._DEPTH_PRICE` 从 `core.billing.MODE_POINTS` 派生）。
                    # 此前这里一个被当"元"一个被当"积分"，前端因此无法把两者相除——
                    # 那不是"不同量纲"，是**同一个量的两种错标签**。现在可以合法相除了。
                    "message": (
                        f"预算已用{state.get('budget_spent', 0):.1f}积分"
                        f"（本档位{state.get('budget_estimate', 0):.0f}积分的"
                        f"{state.get('budget_spent', 0) / max(state.get('budget_estimate', 1), 1) * 100:.0f}%），"
                        f"是否继续？"
                    ),
                }

            # 人工接管：质检 BLOCK → 推送停止卡片（培训信号：主动停止=产品能力）
            if node_name == "quality_check" and node_updates.get("stop_card"):
                yield {"type": "stop", "stop_card": node_updates["stop_card"]}

            # 节点结束
            for _tv in reversed(streamed_traces):
                if _tv["node"] == node_name and _tv["status"] == "running":
                    _tv["status"] = "done"
                    break
            yield {
                "type": "trace",
                "node": node_name,
                "detail": _node_done_detail(node_name, node_updates),
                "status": "done",
            }

    # 最终结果：用流式累积的交付数据组装（不再对整个状态机二次 invoke）
    #
    # **先把"图里的最终状态"解析出来一次，全段共用**（2026-09-16 修 · CRITICAL）。
    # `run_task_stream` 里的局部 `state` 是 `init_state(...)` 的返回值，
    # `graph.stream(...)` 的产出**从来没有回写进它**——本文件里已经为这件事写过两条病案
    # （`_log_run_stats_safe` 的档位、`_attach_audit_bundle` 的 subtask_results），
    # 但每一处都是**各自打补丁**，于是第三个用到它的人（`result["plan"]`、
    # `result["budget_estimate"]`、`_build_run_stats`）照样踩进去。
    # 现在统一在这里取一次，下面一律用 `_final_state`。
    _final_state = _resolve_final_state(graph, config, state)
    if _final_state is state:
        logging.getLogger(__name__).warning(
            "取不到图内最终状态（回退到 run_task_stream 的局部 state，它从未被图的产出回写过）——"
            "plan/budget_estimate/run_stats 的多个字段会是初值"
        )
    if final_payload is None:
        # 极端兜底：deliver 节点未产出（异常路径），才回退二次 invoke
        final = graph.invoke(state, config=config)
        result = _result_json(final)
        # 同样清洗证据URL（坏URL清空 / 失效页回退官网域名）
        _bad_urls = set(result.get("bad_urls") or [])
        _url_fixes = dict(result.get("url_fixes") or {})
        if _bad_urls or _url_fixes:
            for ev in result["evidences"]:
                if not isinstance(ev, dict):
                    continue
                for s in ev.get("sources") or []:
                    if not isinstance(s, dict):
                        continue
                    u = s.get("url") or ""
                    if u in _bad_urls:
                        s["url"] = ""
                    elif u in _url_fixes:
                        s["url"] = _url_fixes[u]
        # **必须喂原始 state，不能喂 `_result_json` 的窄化产物**（2026-09-14 二次审计）。
        # `_result_json` 是白名单：`asset_reused` / `budget_spent` / `search_debug` /
        # `rework_count` **都不在里面** → 拿它算 run_stats，这四个字段在**流式路径上恒为 0/false**。
        # 而流式正是前端（也就是演示）走的那条路：同步命令行跑出来的账是对的，
        # 演示时看到的「有效复用率」却永远是 0。**同一个函数，两条路径两种真相。**
        result["run_stats"] = _build_run_stats(final, _t.monotonic() - _t0, _seq0)
        _log_run_stats_safe(result, raw_question, resume, plan_override, state)
        _attach_audit_bundle(result, state, raw_question, graph, config)
        if streamed_traces:
            result["trace_events"] = list(streamed_traces)
        yield {"type": "done", "result": result}
        return

    result = _result_json(final_payload)
    # ## 为什么 evidences 必须取自 deliver 的回吐，而不是流式累积（2026-09-16 病案）
    #
    # 原先这一行是 `result["evidences"] = streamed_evidences` —— 而 `streamed_evidences`
    # 是**每个节点返回时抓的快照**。`node_check_conflict` 查出冲突后是**就地改对象**
    # （`ev.is_conflicting = True`）、**不回吐 `evidences`**（它只回吐 conflict_results /
    # rework_count / conflict_scan）。于是那些快照**永远停在改动之前**。
    #
    # 后果不是"少一个字段"，是**同一份交付物自己打自己**：
    #   · 正文《数据冲突日志》读 state 对象 → 写「⚔️ 存争议 N」并列出该条；
    #   · 而可复算包 `claims.csv` 的 conflicting 列读 result["evidences"] → 全「否」，
    #     status 列没有一条 CONTESTED。
    #   而 **README 恰恰让复核者打开 claims.csv 挑一条核对**——看到的正好是与正文相反的那一列。
    #   且演示走的就是流式路径（同步路径 model_dump 在最后做，反而是对的）——
    #   又一处「同一个函数，两条路径两种真相」。
    #
    # deliver 是最后一个节点，它回吐的就是**最终状态里的对象**，与正文同源。
    if not result.get("evidences"):
        # 兜底：deliver 万一没回吐（异常路径）。**必须出声**——静默回退正好会把上面
        # 那个 bug 原样装回来，而且没人会知道。
        logging.getLogger(__name__).warning(
            "deliver 未回吐 evidences，回退到流式累积快照——"
            "注意该快照**不含 check_conflict 的就地改动**（is_conflicting 可能全为 False）"
        )
        result["evidences"] = streamed_evidences
    result["conflict_results"] = streamed_conflicts
    result["quality_report"] = last_quality.get("quality_report")
    result["stop_card"] = last_quality.get("stop_card")
    result["validation_tasks"] = decision_payload.get("validation_tasks", [])
    result["monitor_conditions"] = decision_payload.get("monitor_conditions", [])
    # **把 deliver 不回吐、但下游要用的字段从图内最终状态补齐**（2026-09-16）。
    #
    # 上面的 `last_quality` / `decision_payload` 是"逐个节点手工补回来"的写法——
    # 作者当时已经意识到 deliver 的 delta 不全，但每漏一个字段就补一行，
    # 于是漏掉了两个（`sop_fallback`、`conflict_scan`），两个都直接对应**已经写好的 UI**：
    #   · `sop_fallback` 恒 {} → 前端「⚠️ 决策清单为兜底模板」红角标**在演示路径上永不出现**
    #     （v0.9.75 那项"降级可见"的能力等于没接线）；
    #   · `conflict_scan` 恒缺失 → 「（比对 N 对）」分母永不显示，tooltip 反过来说
    #     「本次未产出冲突检测读数（旧报告）」。
    # 改用 `_final_state` 兜底：这些键都在 `TaskState` 里，图内状态一定有，
    # 不必再逐个节点去猜它从哪儿出来。**只补空的，不覆盖已有的**。
    for _k in _DELIVER_BACKFILL_KEYS:
        if not result.get(_k):
            result[_k] = _final_state.get(_k)
    # **判定以 deliver 节点的为准，不以 decision_sop 节点的为准**（2026-09-15）。
    # 病案：这一行原先无条件取 `decision_payload`（decision_sop 节点的产出），
    # 而决策完整性闸门跑在 **deliver** 节点、可能在正文里把 Go 降成了 Validate。
    # 于是出现最坏的一种不一致：**正文第一屏写着「本判定已降级为 Validate」，
    # 而前端角标、决策回访入库、复购看板读到的仍是 Go**——
    # 数据层与交付物对不上，且恰好是往"看起来更好"的方向偏。
    # deliver 在图里排在 decision_sop 之后，它的结论就是最终结论。
    _gate_payload = final_payload if isinstance(final_payload, dict) else {}
    result["decision_verdict"] = (
        _gate_payload.get("decision_verdict")
        or decision_payload.get("decision_verdict", "")
    )
    result["integrity_gate"] = _gate_payload.get("integrity_gate", {}) or {}
    result["budget_estimate"] = _final_state.get("budget_estimate")
    result["plan"] = (
        _final_state.get("plan").model_dump(mode="json") if _final_state.get("plan") else None
    )
    # 交付：把坏URL集 + 域名回退映射带给前端，并清洗证据URL
    # （证据"查看原文"链接：彻底失效→清空；页面失效但官网可访问→回退官网域名）
    _bad_urls = set(result.get("bad_urls") or [])
    _url_fixes = dict(result.get("url_fixes") or {})
    if _bad_urls or _url_fixes:
        for ev in result["evidences"]:
            if not isinstance(ev, dict):
                continue
            for s in ev.get("sources") or []:
                if not isinstance(s, dict):
                    continue
                u = s.get("url") or ""
                if u in _bad_urls:
                    s["url"] = ""
                elif u in _url_fixes:
                    s["url"] = _url_fixes[u]
    # ⚠️ **必须喂图里的最终状态，不能喂 `final_payload`**（2026-09-16 修 · CRITICAL）。
    #
    # 这一行原先写作 `_build_run_stats(final_payload, ...)`，注释还写着"喂原始 state"——
    # 而 `final_payload` **不是** state，它是 `stream_mode="updates"` 里 **deliver 一个节点
    # 返回的那个 dict**。deliver 只回吐 16 个键，**不含** budget_spent / rework_count /
    # search_debug / stop_card / sop_fallback / asset_reused ——
    # 于是这 6 个字段在**演示走的那条路径**上恒为 0 / False / {}。
    #
    # 比"字段为 0"更糟的是它通向哪里：复购看板的「任务成功率 / 人工接管率 / 有效复用率」
    # 正是拿 degraded / took_over / reused_asset 算的，而这三项在流式路径上
    # **结构上不可能为 True**——错的方向恰好是"看起来更好"：
    # 人工接管率恒 0%（读起来是"我们的智能体从不需要人接管"），
    # 有效复用率被系统性低估（而绿档那一笔对外写着「命中已沉淀行业卡」）。
    # 而且 `sop_fallback` 恒为 {} 还让前端那条「⚠️ 决策清单为兜底模板」的红角标
    # **在演示路径上永远不出现**——v0.9.75 那项"降级可见"的能力等于没接线。
    #
    # 同步路径（`run_task`）传的是 `graph.invoke()` 的完整状态，所以它是对的——
    # 又一个「同一个函数，两条路径两种真相」。
    result["run_stats"] = _build_run_stats(_final_state, _t.monotonic() - _t0, _seq0)
    _log_run_stats_safe(result, raw_question, resume, plan_override, state)
    _attach_audit_bundle(result, state, raw_question, graph, config)
    # ## `trace_events` 只能来自**流本身**（2026-09-19 · 一次真跑打回来的修复）
    #
    # 上一条修复把 `trace_events` 加进了 `_DELIVER_BACKFILL_KEYS`（从图内最终状态补齐），
    # 然后真跑了一次——**done 事件里它仍然是 `[]`**。补齐名单没错，是**来源**错了：
    # `_trace()` 是**就地 `state["trace_events"].append(...)` 且不返回任何值**，
    # 于是它从来不是节点 delta，LangGraph 的 channel 里那个键**永远是 `init_state` 的 `[]`**。
    # （同一次运行里 `asset_reused` / `budget_spent` 补上了——因为它们是节点**返回**的 delta。
    #  恰好这三个键并排放在同一份名单里，看起来一模一样，来源却完全不同。）
    #
    # 所以这里改成从**流自己产出的 trace** 累积：`run_task_stream` 每推一条 trace 事件就
    # 记一条、节点结束时把对应那条标 done（与 `_finalize_event` 同形）。
    # 这才是与前端 Trace 面板**同源**的那一份——**所见即所存**。
    if streamed_traces:
        result["trace_events"] = list(streamed_traces)
    yield {"type": "done", "result": result}


def _resolve_final_state(graph, config, fallback: Any) -> Any:
    """取**图里的最终状态**（不是 `run_task_stream` 的局部 `state`，也不是某个节点的 delta）。

    ## 为什么必须有这一个函数（2026-09-16 · CRITICAL 病案）

    `run_task_stream` 里的局部 `state` 是 `init_state(...)` 的返回值，
    `graph.stream(...)` 的产出**从来没有回写进它**。本文件为此已经写过两条病案：

      · `_log_run_stats_safe`：账本档位恒为默认 `standard`（读到了空计划）；
      · `_attach_audit_bundle`：`subtask_results` 恒为空（routes:0）。

    **但两处都是各自打补丁**，没有把"取最终状态"这件事收成一处。于是第三个用到它的人
    照样踩进去：`result["plan"]` / `result["budget_estimate"]` / `_build_run_stats`。

    最后这一处后果最重——`_build_run_stats` 被喂的是 `final_payload`
    （`stream_mode="updates"` 里 **deliver 一个节点的返回增量**），而 deliver 只回吐
    16 个键，**不含** `budget_spent` / `rework_count` / `search_debug` / `stop_card` /
    `sop_fallback` / `asset_reused`。于是这 6 个字段在**演示走的那条路径**上恒为 0/False：

      · 复购看板的「任务成功率 / 人工接管率 / 有效复用率」正是拿
        `degraded` / `took_over` / `reused_asset` 算的，三项**结构上不可能为 True**——
        错的方向恰好是"看起来更好"（人工接管率恒 0%，读起来是"从不需要人接管"）；
      · 前端「⚠️ 决策清单为兜底模板」的红角标在演示路径上**永远不出现**。

    实证（`data/run_stats.jsonl` 第 7 行，2026-09-15 深入档，服务端真实运行）：
    `budget_spent=0 / search_rounds=0 / rework_count=0 / took_over=False /
    reused_asset=False / degraded=False`；而走同步路径的第 5、6 行是
    `budget_spent=1.4 / search_rounds=7`。**同一个函数，两条路径两种真相。**

    取不到就回退（并让调用方出声），绝不抛异常——它服务于交付链路的收尾。
    """
    if graph is not None and config is not None:
        try:
            _snap = graph.get_state(config)
            _vals = getattr(_snap, "values", None)
            if _vals:
                return _vals
        except Exception:
            pass
    return fallback


def _attach_audit_bundle(result: dict, state: dict, question: str, graph=None, config=None) -> None:
    """生成可复算包 + 服务端报告存档，挂到交付结果上（各自失败都不影响交付）

    为什么放在交付末端而不是单独接口：**原始取证记录只存在于本次运行的 state 内存里**，
    流结束后就没了。要做出"第三方可复算"的实物，必须在收尾这一刻把原始记录落盘。

    ⚠️ `state` 必须传**图里的当前状态**，不能传 `run_task_stream` 的局部变量：
    `node_route_skill` 返回的 `subtask_results` 是**新字典**，LangGraph 用它**替换**了通道值，
    于是流函数手里那个初始对象上的 `subtask_results` 永远是空的 →
    包里的 `raw/` 少了各维度原始记录、`_routes` 为空（实测：routes:0、只有 authorities.json 一个 raw 文件）。
    """
    try:
        from core.audit_bundle import build_bundle

        if graph is not None and config is not None:
            state = _resolve_final_state(graph, config, state)
        b = build_bundle(state, result, question)
        if b:
            result["audit_bundle"] = b
    except Exception as e:
        logging.getLogger(__name__).warning("可复算包生成失败（不影响交付）: %s", e)
    # 服务端报告存档（病案：刷新页面后报告整段消失——报告不该只有浏览器里那一份）。
    # 与可复算包同一时刻落盘：都在"这次运行的 state 还在内存里"的收尾这一刻。
    try:
        from core.report_store import save_report

        _r = save_report(question, result)
        if _r:
            result["report_id"] = _r["id"]
        # 指标台账落库（跨报告血缘 / 锁值锚）：从**最终报告**重建，不依赖节点间的 state 传递
        # （那个坑在可复算包上踩过——node 返回新字典会替换通道值，流函数手里那份永远是旧的）。
        try:
            from core.metric_ledger import build_ledger, ingest

            _n = ingest(
                build_ledger(result.get("report_final") or ""),
                report_id=result.get("report_id") or "",
                question=question,
                industry=_extract_industry(question or ""),
            )
            if _n:
                logging.getLogger(__name__).info("指标台账落库 %d 条", _n)
        except Exception as _e2:
            logging.getLogger(__name__).warning("指标台账落库失败（不影响交付）: %s", _e2)
    except Exception as e:
        logging.getLogger(__name__).warning("报告存档失败（不影响交付）: %s", e)


def _node_hint(node: str, updates: dict) -> str:
    """节点开始的提示文案"""
    hints = {
        # 按"专业调研流程"分阶段讲，而不是流水线日志：
        # ① 界定研究问题 → ② 方法选择与取证 → ③ 分析与交叉验证 → ④ 质检与返工 → ⑤ 决策 → ⑥ 交付与沉淀
        "router": "① 界定研究问题：把管理问题转成可验证的调研问题（MECE 拆解 + 决策视角锁定）",
        "human_confirm": "研究计划已生成（可暂停就地编辑后继续）",
        "route_skill": "② 方法选择与任务分派：按问题类型匹配研究框架，分配并行研究角色",
        "industry": "② 宏观与市场取证：检索行业规模/增长/政策（多源并行）",
        "competition": "② 竞争格局取证：检索主要玩家/份额/渠道/定价",
        "user": "② 需求侧取证：检索用户画像/需求/行为与口碑",
        "statistics": "③ 定量测算：确定性引擎测算 TAM/SAM/SOM 与单位经济（LLM 不参与计算）",
        "check_conflict": "③ 交叉验证：多源口径比对与冲突仲裁（证据级并列披露，口径级交规则层）",
        "replan": "③ 动态重规划：审视已获证据与客观缺口，自主决定是否追加定向调查",
        "summarize": "③ 分析与成稿：汇总各模块，按证据链生成报告初稿",
        "budget_check": "预算与返工成本检查",
        "quality_check": "④ 质检（确定性规则闸门 + 低温复核）：口径/算术/背书/伪量化/时序",
        "rework_fix": "④ 定向返工：按质检致命项重跑相关研究角色（≤2 轮）",
        "decision_sop": "⑤ 决策输出：Go/No-Go/Validate 判定 + 14 天验证任务（谁/钱/KPI/停止条件）",
        "asset_sink": "⑥ 知识沉淀：证据入行业档案库（护栏过滤后）",
        "deliver": "⑥ 交付：生成干净研报（决策置顶 + 审计附录）",
    }
    return hints.get(node, f"执行节点 {node}")


def _node_done_detail(node: str, updates: dict) -> str:
    """节点完成的详细文案"""
    if node in ("industry", "competition", "user", "statistics"):
        dbg = updates.get("search_debug") or []
        # 无子任务路由到该节点（本深度档未安排该维度）→ 如实说明，避免"0条证据"误导
        if not dbg:
            return f"本深度档未安排该维度子任务（{node}）"
        evs = updates.get("evidences") or []
        # 有真实URL的证据 = 可点开溯源（公信力可视标记）
        linked = sum(
            1
            for e in evs
            if getattr(e, "sources", None) and e.sources[0].url
        )
        base = f"检索完成，产出 {len(evs)} 条证据"
        # 每个子任务的检索词+结果：直接显示在浏览器思考过程（诊断 + 演示透明性）
        if dbg:
            parts = "；".join(f"{sid}『{q[:26]}』→{n}" for sid, q, n in dbg)
            base = f"{base}｜{parts}"
        return f"{base}（{linked} 条可点开溯源）" if linked else base
    if node == "replan":
        # updates 里只有真实写过的通道键；本节点无缺口时返回 {} → 如实说"未发现缺口"
        if not updates:
            return "缺口检测：未发现客观缺口，按首轮结果成稿"
        evs2 = updates.get("evidences") or []
        return f"补充调查完成：取回 {len(evs2)} 条证据" if evs2 else "补充调查已尝试，未取回新证据（已如实记入决策链）"
    if node == "check_conflict":
        cfs = updates.get("conflict_results") or []
        # 措辞与规则层分工写清（报告2.0曾出现"未发现冲突"却同文5组口径冲突的表象矛盾）：
        # 本节点=证据级冲突；口径/时序/术语等机械问题由交付前规则层(零成本)复核，属另一层
        return f"证据级冲突：发现 {len(cfs)} 处并仲裁" if cfs else "证据级冲突：未发现（口径级问题由规则层复核）"
    if node == "quality_check":
        return "质检通过" if updates.get("quality_passed") else "质检发现问题，返工"
    if node == "deliver":
        return "报告已生成"
    return "完成"


def _evidence_json(ev) -> dict:
    """证据 → JSON"""
    ev_json = ev.model_dump(mode="json")
    return ev_json


def _result_json(result: dict) -> dict:
    """最终结果 → JSON（与 app.py _serialize_result 对齐）"""
    return {
        "status": result.get("status"),
        "plan": result.get("plan").model_dump(mode="json") if result.get("plan") else None,
        "evidences": [ev.model_dump(mode="json") for ev in result.get("evidences", [])],
        "trace_events": result.get("trace_events", []),
        "conflict_results": result.get("conflict_results", []),
        # 仲裁的**分母**（trunk_matched / pairs_compared）。
        # 病案（2026-09-16）：白名单里从来没有它——于是前端专门为复核者渲染的
        # 「（比对 N 对）」永不出现，而 tooltip 还会把刚跑完的新报告说成
        # 「本次未产出冲突检测读数（旧报告）」。**一个为了显示分母而写的 UI，
        # 因为分母没被传出去，反过来指责自己没产出数据。**
        "conflict_scan": result.get("conflict_scan") or {},
        "report_final": result.get("report_final", ""),
        "competition_report": result.get("competition_report", ""),
        "citations": result.get("citations", []),
        "citation_report": result.get("citation_report"),
        "bad_urls": result.get("bad_urls", []),
        "unprobed_urls": result.get("unprobed_urls", []),
        "url_fixes": result.get("url_fixes", {}),
        "quality_report": result.get("quality_report"),
        "stop_card": result.get("stop_card"),
        "validation_tasks": result.get("validation_tasks", []),
        "monitor_conditions": result.get("monitor_conditions", []),
        "decision_verdict": result.get("decision_verdict", ""),
        "metrics": result.get("metrics", []),
        "decision_chain": result.get("decision_chain", []),
        "entailment": result.get("entailment", {}),
        "citation_pass_rate": result.get("citation_pass_rate", 0.0),
        # 决策清单是否走了兜底模板（v0.9.75）。**必须传到调用方**：
        # 前端据此打角标、验收脚本据此判定"这次能不能拿去演示"。
        # 一份报告最像"决策级"的那一节可以静默变成空壳——这件事本身就是必须被看见的。
        "sop_fallback": result.get("sop_fallback", {}) or {},
        "deliverables": result.get("deliverables", []),
        "budget_estimate": result.get("budget_estimate"),
        "run_stats": result.get("run_stats"),
    }


if __name__ == "__main__":
    # 离线自测：完整跑一遍状态机
    import sys

    sys.path.insert(0, ".")
    result = run_task(
        "我想在杭州开一家咖啡店，能不能做，帮我看看市场机会，预算标准档",
        {"objective": "进入市场", "budget": "标准"},
    )
    print("=== 状态机运行完成 ===")
    print("status:", result["status"])
    print("plan.dimensions:", result["plan"].dimensions)
    print("trace_events:", len(result["trace_events"]))
    print("report_final 前200字:", result["report_final"][:200])
