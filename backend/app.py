"""研策 · FastAPI 后端服务

把 LangGraph 状态机暴露成 REST API：
- POST /api/ingest        提问入口（返回澄清问题 或 研究计划）
- POST /api/confirm       确认澄清 + 生成计划
- POST /api/execute       执行调研（跑完整状态机）
- GET  /api/task/{id}     查询任务状态（含 Trace）
- GET  /health            健康检查

前后端分离：React 前端通过这里调用后端。
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import logging
import os
import sys

# **必须早于任何 `os.environ.get()` 的调用**：这个 import 的副作用是把 `.env`
# 灌进 `os.environ`（详见 `core/config.py` 病案）。少了它，下面 `_selfcheck_mcp_local`
# 读到的 `SEC_UA_CONTACT` 永远是空——用户填好了也照样报警告。
import core.config  # noqa: F401

# 统一日志：所有 logger（子Agent/检索/引文核验）输出到 stderr → backend.err.log
# 排查"数据源全是我的世界"这类问题可直接看 backend.err.log 里的"引文URL被拦"记录
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)

# 模块级 logger（2026-09-17 补）：本文件此前**没有**模块级 logger，
# 唯一一处日志是 `upload_attachment` 里就地 `logger = __import__("logging").getLogger(__name__)`。
# 那处本身无害（同函数内紧接着就用），但它是 `graph.py:node_deliver` 那场事故的**同一种写法**——
# 那边把同样的行抄了 7 遍，每一遍都在把整个函数作用域里的模块级 `logger` 挡掉。
# 留着这个种子，下一个人还会照抄。所以这里立成模块级，就地赋值那行一并去掉。
logger = logging.getLogger(__name__)


def _selfcheck_mcp_local() -> None:
    """启动自检：本地 `mcp/` 必须**真的**解析到本仓库，而不是 site-packages 里的同名包。

    病案（2026-09-11 v0.9.60 实跑）：用系统 Python 启动时，site-packages 里 PyPI 的 `mcp`
    （常规包）盖过了本地 `mcp/`（当时没有 `__init__.py`，是命名空间包）。
    后果不是报错退出，而是**每一章都变成「（检索失败: No module named 'mcp.search'）」**——
    证据 0、引文 0、来源面板空，交付照常完成，看起来像"这次没搜到东西"。
    这种"静默降级成废报告"必须变成**开机就吼**。
    """
    import os as _os

    _log = logging.getLogger(__name__)
    try:
        import mcp.search as _m

        _root = _os.path.dirname(_os.path.abspath(__file__)).replace("\\", "/").lower()
        _p = (getattr(_m, "__file__", "") or "").replace("\\", "/").lower()
        if not _p.startswith(_root):
            _log.error(
                "❌ mcp 被外部包遮蔽：mcp.search 解析到 %s（应为 backend/mcp/search.py）——"
                "检索层会整体失效、报告只剩空章节。请确认 backend/mcp/__init__.py 存在，"
                "并优先用项目 venv 的解释器启动。",
                getattr(_m, "__file__", "?"),
            )
    except Exception as _e:  # 导入不了 = 更严重，必须吼出来
        _log.error("❌ 检索层 mcp.search 导入失败：%s —— 报告将没有证据与来源。", _e)
    # SEC fair-access：UA 必须声明**真实可达**的联系方式。
    # 2026-09-14 起这一条改成 fail-closed：未配置就**不发请求**，不是"发个占位串凑合"。
    # 所以告警文案必须同步——旧文案说"用的是占位串"，而占位串已经不存在了；
    # 留着旧话会让用户以为自己只是"不够规范"，实际是**整条 SEC 通道（L2 法定披露）没在跑**。
    if not (os.environ.get("SEC_UA_CONTACT") or "").strip():
        _log.warning(
            "⚠️ SEC_UA_CONTACT 未配置 → **SEC EDGAR 通道已停用**（fail-closed，按 fair-access "
            "合规姿态主动不发请求）。请在 .env 里填一个真实可达邮箱："
            "SEC_UA_CONTACT=you@example.com —— 配置后重启即可恢复。"
        )


_selfcheck_mcp_local()


def _kick_source_patrol() -> None:
    """启动后台数据源巡检（**不阻塞启动**，超 24h 未巡检才跑）

    为什么要自动跑：台账一旦长期不更新，"上次实测全部可用"就成了一句越来越可疑的话。
    为什么要后台跑：巡检要逐个探活 30 个站点，放在启动路径上会让服务晚几十秒才可用。
    """
    try:
        if str(os.environ.get("SOURCE_PATROL_ON_START", "1")).strip() in ("0", "false", "False"):
            return
        from core.source_health import patrol_async

        if patrol_async(trigger="startup"):
            logging.getLogger(__name__).info("数据源巡检已在后台启动（台账过期）")
    except Exception as _e:
        logging.getLogger(__name__).warning("数据源巡检启动失败（不影响服务）: %s", _e)


_kick_source_patrol()

from agents.router import router_ingest, confirm_plan
from graph import run_task

app = FastAPI(
    title="研策 · 市场调研数字员工",
    description="基于公开数据的系统化智能调研数字员工",
    version="0.1.0",
)

# 允许前端开发服务器跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发期放开，上线前收紧
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 请求/响应模型
# ============================================================
class IngestRequest(BaseModel):
    question: str
    context: dict = {}


class ConfirmRequest(BaseModel):
    question: str
    context: dict


class ExecuteRequest(BaseModel):
    question: str
    context: dict = {}
    plan_id: str = ""
    plan: dict | None = None  # 用户编辑后的研究计划（可选，有则跳过重新生成）
    thread_id: str = ""  # 任务线程id（前端消息id）：暂停后继续用同一id从断点恢复
    resume: bool = False  # true=从checkpoint继续（不重新生成计划/检索，从上次中断节点续跑）


def _parse_plan_override(raw: dict | None) -> Any:
    """把前端传来的用户编辑计划解析成 ResearchPlan"""
    if not raw:
        return None
    from core.models import ResearchPlan, SubTask, DepthLevel

    try:
        return ResearchPlan(
            raw_question=raw.get("raw_question") or raw.get("question", ""),
            clarified_context={str(k): str(v) for k, v in (raw.get("clarified_context") or {}).items()},
            dimensions=[str(d) for d in (raw.get("dimensions") or [])],
            depth=DepthLevel(raw.get("depth") or "standard"),
            price_estimate=float(raw.get("price_estimate") or 0),
            subtasks=[
                SubTask(
                    id=str(s.get("id") or f"st_{i}"),
                    agent_type=str(s.get("agent_type") or "industry"),
                    description=str(s.get("description") or ""),
                    search_queries=[str(q) for q in (s.get("search_queries") or [])],
                    depends_on=[str(d) for d in (s.get("depends_on") or [])],
                    budget_estimate=float(s.get("budget_estimate") or 0),
                    status=str(s.get("status") or "pending"),
                )
                for i, s in enumerate(raw.get("subtasks") or [])
            ],
            status="confirmed",
        )
    except Exception:
        return None


def _normalize_context(context: dict) -> dict[str, str]:
    """把 context 的值归一化为字符串（容错：数组/数字等 → 字符串）

    前端澄清可能传数组（多选），后端统一转成字符串，避免 422。
    """
    result = {}
    for k, v in (context or {}).items():
        if isinstance(v, list):
            result[k] = "、".join(str(x) for x in v)
        else:
            result[k] = str(v)
    return result


class DeepDiveRequest(BaseModel):
    claim: str
    user_intent: str = ""
    context: dict[str, str] = {}


class PrimaryResearchRequest(BaseModel):
    report: str = ""
    design: dict[str, Any] | None = None  # 一手验证方案设计（供模拟数据贴合问卷）


# 内存任务存储（开发期；上线切持久化）
_TASKS: dict[str, dict[str, Any]] = {}


# ============================================================
# API
# ============================================================
@app.get("/health")
def health():
    # v0.9.47: ①行业卡防自腐护栏(core/industry_kb.py)——病案:报告(6)验收 13:24:48 咖啡卡被"整卡覆写",
    #           把 RedTeam 高/中风险(>63%/67天/17.3%)与无源 31% 写成"行业基线"再自复制;
    #           修复=sink 前确定性过滤(剔 低信源通道[反方检索资料/历史基线/估算]+RedTeam高/中标注数字)
    #           + 法规canonical兜底 policy(_REG_CANONICAL/旧卡 regulatory 透传, 未核杂项只存 policy_note 不注入);
    #           graph.deliver 把 adv_issues 高/中风险数字传入 save_industry_card。
    #           ②D2时效补检可产出化(core/freshness.py)——双探针(泛"最新动态"+经济事件定向"出售/收购/股权",
    #           治 N6 博裕病案泛词不敏感)+ 放宽标题硬门槛(综合分>=2) + 决策题触发必有可见产物
    #           (命中→真实线索块 / 有返回无一过门→诚实"未命中"提示,不静默视为无重大变化 / 彻底空→不硬塞)+ 全程日志;
    #           ③P0-3决策层就地降级(core/decision_guard.py)——RedTeam高风险数字(186杯/1.2杯/>63%)若出现在
    #           执行摘要/决策清单/核心指标表/假设等决策头部区 → 就地标『（RedTeam待核）』(幂等/容错/只动头部区);
    #           ④P0-4公司级数字口径锚(core/company_anchor.py+data/company_anchors.json, 规则17)——主/深入章
    #           并行写作互不可见 → 每章写章与测算参数抽取注入同报告口径锚(瑞幸门店18,590/市占口径/星巴克直营
    #           120-180万等), 同一公司/指标/时间只允许一个权威值, 防 32.5%vs18.5%、45/98/120万三套并存这类
    #           跨章打脸(规则层R1刻意不覆盖市占/门店总量词, 故锚写在写入端);
    # v0.9.46: N1 法规误读根治(2026-09-08 深入版跑批实锤)——"冷食专间"误读已沉淀进咖啡行业知识库
    #           并自复制(industry_kb.json policy字段);修复=①清洗咖啡卡policy(法规原文核验修正写入)
    #           +②researchers._INTEGRATION_PROMPT规则16 & graph._SUMMARY_METRICS_PROMPT规则7 加法规资格
    #           断言纠偏(自制饮品≠冷食类,禁"43%商铺无法过审"类派生结论,执行摘要/指标表不传播误读);
    #           +③D2门禁改"决策题无条件补检"(防个别2026来源掩盖核心指标停旧年/博裕事件缺席,措辞=例行补检)
    # v0.9.45: 检索新鲜度门禁(D2/Q0, core/freshness.py)——决策题/核心来源偏旧(最新年份距当前≥2年)时,
    #           deliver 自动补检近一年动态, "时效补检"块置顶(紧跟生成时点,30秒可见),
    #           标题真实检索+链接走既有verify+坏链清洗, 不碰<ref>编号, 空结果不硬塞; 无检索key/离线自动跳过
    # v0.9.44: 带病交付终结 batch——RedTeam返工门放宽(不再限causal)+RedTeam高风险"置顶红条"(默认档也30秒可见)
    #           + 报告生成时点注入+章节预测句强制时间锚({now_date})+置信度公示补"不含正确性"边界语(#15)
    #           + Rubric改"系统自检清单"口径(防94/100得分证明红线)+md导出路由+run_stats台账原语;
    # v0.9.43: 规则层扩 R5正文算术核验(月收入对账,治32元=97%类)+R6伪量化检测(0-10打分无方法学,治价敏度7.8类)
    #           + 决策清单/vt清单一致性(decision: vt先建喂SOP禁另造双清单)+证据级冲突措辞与规则层分工写清
    #           + 正文/摘要禁"自行修正口径"外露叙事;
    # v0.9.42: 决策视角锁定(core/viewpoint.py: 经营者/新进入者/观察者/中立 规则推断, 注入执行摘要+各章节,
    #           治"摘要=高端品牌、章节=新进入者"主体漂移) + R4无背书数字扫描(rule_lint: 匿名来源+数值句→附录披露)
    #           + RedTeam语义返工按深度档开关(仅深入/定制触发, 免费/标准省无效全模块重跑)
    #           + 对抗校验异常不再静默(报告末显式提示, 防免费/标准档"披露保证"被LLM故障架空);
    # v0.9.41: 测算口径锁——人口基数注入带口径名(fact_label 城镇/总/农村显式化,
    #           根除"城镇9.3亿被写成全国总人口"高危项)+TAM/SAM频次现实锚(prompt规则4+代码兜底夹60/40,
    #           根除120杯/人/年式凭空放大), 命中夹频注入块透明披露;
    # v0.9.40: P0质量护栏——规则层扩展同概念口径冲突(杯量/渗透率/客单价/盈亏平衡/人均消费量/关店闭店关闭率)+首轮定向返工;
    #           摘要/正文prompt来源纪律(禁泛机构名/无源数标估算/同概念口径一致); 指标提取背书纪律(泛来源→待核,L1-L5短标签)
    from core.version import ENGINE_VERSION

    return {"status": "ok", "service": "yancec", "version": ENGINE_VERSION}


@app.get("/api/audit-bundle/{bundle_id}")
def audit_bundle_download(bundle_id: str):
    """下载某次调研的**可复算包**（ZIP）

    包内含：原始取证记录 + 公开判定规则 + 两个**纯标准库**独立脚本
    （`verify.py` 重探每个引用 URL 存活、`compute.py` 从原始序列重算 CAGR/同比）。
    第三方解压即可复算，**无需安装本项目**。
    """
    try:
        from core.audit_bundle import find_bundle

        p = find_bundle(bundle_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"可复算包模块不可用: {e}")
    if not p:
        raise HTTPException(status_code=404, detail="可复算包不存在（可能已清理）")
    fn = quote(f"研策_可复算包_{bundle_id}.zip")
    return FileResponse(p, media_type="application/zip", filename=f"yancec_audit_{bundle_id}.zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})


@app.get("/api/metrics")
def metrics_lineage(q: str = "", industry: str = "", limit: int = 60):
    """指标血缘：某个指标在**历次调研**中的取值序列

    `?q=` 给指标名（如"星巴克全球营收"）；不带则返回台账概况。
    这是"数据护城河"从散文（行业基线 KB）升级到**结构化指标**后的对外入口：
    同一指标跨报告可比、可追溯，数值变化能追到是哪次调研改的口径。
    """
    try:
        from core.metric_ledger import describe, series

        if not (q or "").strip():
            return describe()
        return {"indicator": q, "industry": industry, "series": series(q, industry=industry, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"指标时序库不可用: {e}")


@app.get("/api/reports")
def reports_list(limit: int = 20):
    """历史报告清单（**不含正文**）：刷新/清缓存/换浏览器后仍能找回报告

    病案（2026-09-11 用户实测）：跑完 12 分钟的报告刷新后整段消失——前端 localStorage
    配额写满后静默失败。报告是这次调研唯一的实物交付物，不该只有浏览器里那一份。
    """
    try:
        from core.report_store import list_reports

        return {"reports": list_reports(limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"报告存档不可用: {e}")


@app.get("/api/reports/{report_id}")
def report_get(report_id: str):
    """按 id 取回一份完整报告（前端恢复报告卡所需的全部字段）"""
    try:
        from core.report_store import load_report

        d = load_report(report_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"报告存档不可用: {e}")
    if not d:
        raise HTTPException(status_code=404, detail="报告不存在（可能已清理）")
    return d


@app.get("/api/source-registry")
def source_registry():
    """数据源体系（注册表 + 受阻台账）——供前端"数据源体系"页展示

    诚实两栏：`connected`（代码里真的接了）/ `planned`（路线图），另有 `blocked`
    （2026-09-11 实测不可用的源及原因，避免重复踩坑、材料里不吹）。
    """
    try:
        from core.source_registry import (
            BLOCKED_SOURCES, CLAIM_ROUTES, LAYERS, blocked_runtime_sources,
            connected_sources, describe, planned_sources,
        )

        def _s(x):
            return {
                "id": x.id, "name": x.name, "layer": x.layer, "kind": x.kind,
                "authority": x.authority, "structure": x.structure, "access": x.access,
                "feeds": list(x.feeds), "note": x.note,
            }

        return {
            "layers": LAYERS,
            "connected": [_s(x) for x in connected_sources()],
            "planned": [_s(x) for x in planned_sources()],
            "blocked": list(BLOCKED_SOURCES),
            # **本机未启用**（登记接入、但运行时不可用，如未配 UA 时的 SEC）。
            # 与 `blocked`（robots/法务层面永不碰的源）是两回事，所以另起一个键，
            # 免得前端把"我们主动不碰"和"我们配了但没跑起来"混成一个意思。
            "runtime_disabled": [
                {**_s(x), "why": _why} for x, _why in blocked_runtime_sources()
            ],
            "claim_routes": [
                {"key": r.key, "label": r.label, "required": r.required,
                 "required_label": LAYERS.get(r.required, ""), "pair": r.pair}
                for r in CLAIM_ROUTES
            ],
            "describe": describe(),
        }
    except Exception as e:
        return {"error": str(e), "layers": {}, "connected": [], "planned": [], "blocked": [], "claim_routes": []}


def _ts(at: int) -> str:
    """epoch 秒 → 本地可读时间（0 返回空串，不给"1970-01-01"这种看着像真数据的假日期）"""
    try:
        if not at:
            return ""
        import time as _t

        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(at)))
    except Exception:
        return ""


@app.get("/api/sources/health")
def sources_health(probe: int = 0, region: int = 1, stale_h: int = 24):
    """数据源**健康台账**——"我们接了 N 个源"这句话的可自证版本

    默认读台账（不打扰任何站点）；`?probe=1` 现场巡检一次再返回。
    `region=0` 可跳过 16 个地方统计局的探活（巡检更省时）。

    为什么单独做一个接口而不是复用 `/api/source-registry`：那个接口回答
    "我们**声明**接了哪些源"（静态事实），这个回答"这些源**此刻**活不活"（动态事实）。
    两件事混在一个接口里，界面就会把"规划中"和"刚才探测失败"渲染成一个样子。
    """
    try:
        from core.source_health import is_stale, ledger, patrol, status_line

        if probe:
            doc = patrol(include_region=bool(region), budget_s=150.0, trigger="api")
        else:
            doc = ledger()
        rows = doc.get("results") or []
        return {
            "at": doc.get("at", 0),
            "when": _ts(doc.get("at", 0)),
            "elapsed_s": doc.get("elapsed_s"),
            "trigger": doc.get("trigger", ""),
            "stale": is_stale(max_age_s=max(1, int(stale_h)) * 3600),
            "summary": doc.get("summary") or {"ok": 0, "fail": 0, "total": 0},
            "results": rows,
            "failing": [r for r in rows if not r.get("ok")],
            "status_line": status_line(),
        }
    except Exception as e:
        return {"error": str(e), "summary": {"ok": 0, "fail": 0, "total": 0}, "results": []}


@app.post("/api/ingest")
def ingest(req: IngestRequest):
    """提问入口：返回澄清问题 或 研究计划"""
    from core.memory import recall, remember

    # 会话记忆：带出历史偏好，并记录本次提问
    ctx = recall(req.question, _normalize_context(req.context))
    remember(req.question, ctx)
    result = router_ingest(req.question, ctx)
    return result


@app.post("/api/confirm")
def confirm(req: ConfirmRequest):
    """确认澄清后生成计划"""
    from core.memory import recall

    ctx = recall(req.question, _normalize_context(req.context))
    plan = confirm_plan(req.question, ctx)
    return {"plan": plan.model_dump(mode="json")}


@app.post("/api/execute-stream")
def execute_stream(req: ExecuteRequest):
    """执行调研（流式 SSE）：逐节点实时推送 Trace / 证据 / 冲突 / 结果

    前端用 EventSource 消费，像 DeepSeek 思考过程一样逐条渲染。
    首个事件为积分计量（billing）：预计消耗/余额（档位+积分模式）。
    """
    from fastapi.responses import StreamingResponse
    from graph import run_task_stream

    def _gen():
        try:
            from core import billing

            plan_override = _parse_plan_override(req.plan)
            _ctx = _normalize_context(req.context)
            _depth = _ctx.get("depth") or ""
            if plan_override:
                _d = getattr(plan_override, "depth", None)
                _depth = _d.value if hasattr(_d, "value") else (_d or _depth)
            _depth = billing.MODE_ALIAS.get(str(_depth), str(_depth)) or billing.DEFAULT_MODE
            # 积分计量：任务开始即扣减（恢复任务不重复扣）；预览随流推送
            _preview = billing.preview(_depth) if req.resume else billing.consume(_depth)
            yield f"data: {__import__('json').dumps({'type': 'billing', 'preview': _preview}, ensure_ascii=False)}\n\n"
            _last_done: dict = {}
            for event in run_task_stream(
                req.question,
                _ctx,
                plan_override,
                thread_id=req.thread_id or "",
                resume=req.resume,
            ):
                if isinstance(event, dict) and event.get("type") == "done":
                    _last_done = event.get("result") or {}
                yield f"data: {__import__('json').dumps(event, ensure_ascii=False)}\n\n"
            # 注：run_stats 落账已移到 graph 层（run_task_stream 内，_log_run_stats_safe）——
            # 原先写在这里，客户端断开/切页导致生成器关闭时该段不执行 → 任务跑完却未落账。
        except Exception as e:
            # **堆栈进日志，不进聊天界面**（2026-09-16 全量审计）。
            # 原实现把 `traceback.format_exc()` 的末 1200 字符**拼进 error 事件**，
            # 而前端把它当消息渲染——于是演示现场一旦有一处异常，复核者看到的是
            # `<项目根>\backend\graph.py` 这样的**绝对路径与源码行**。
            # 用户需要知道的是"哪一步失败了 + 凭这个编号去查日志"，不是我们的调用栈。
            # 需要现场排障时置 `YANCEC_DEBUG_ERRORS=1`（默认关闭）。
            import logging
            import traceback

            _tb = traceback.format_exc()
            _rid = uuid.uuid4().hex[:8]
            logging.getLogger(__name__).error("执行流异常 [%s]:\n%s", _rid, _tb)
            _err = f"执行中断（错误编号 {_rid}）：{type(e).__name__}: {str(e)[:200]}"
            if os.environ.get("YANCEC_DEBUG_ERRORS") == "1":
                _err += f"\n\n{_tb[-1200:]}"
            yield f"data: {__import__('json').dumps({'type': 'error', 'error': _err}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/billing")
def billing_api():
    """积分余额 + 充值档位 + 模式成本表（档位+积分计量引擎）"""
    from core.billing import billing_info

    return billing_info()


@app.post("/api/recharge")
def recharge_api(payload: dict = Body(default_factory=dict)):
    """演示账户充值（按 ¥30/¥99/¥299 三档加积分）。

    病案（2026-09-19）：`RECHARGE_TIERS` 一直随 `/api/billing` 送到前端，
    界面上却**没有任何入口能改变余额**——价目表是"摆着看的"。
    「实现了 ≠ 能被用上」的第 N 例：字段有、消费者有、承重的那个动作没有。

    ⚠️ **不接真实支付，也不假装接了**：单机演示账户，调用即到账，
    返回 `simulated: true`，前端照此措辞（"演示账户·即时到账"）。
    """
    from core.billing import recharge

    return recharge((payload or {}).get("amount"))


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    """执行调研：异步启动，立即返回 task_id，后台跑完整状态机。

    前端轮询 /api/task/{id} 拿进度和 Trace（对应 Trace 面板实时展示）。

    ⚠️ **本端点此前把 `plan` / `thread_id` / `resume` 三个字段声明了却一个都不用**
    （2026-09-16 全量审计）。而 `frontend/src/api.js` 的 `execute()` **正是带 plan 调的**
    ——于是"用户在澄清卡里编辑过的研究计划"在这条路上被**静默丢弃**，
    系统照旧自己重新生成一份计划，而调用方以为自己的编辑生效了。
    `run_task` 本来就收 `plan_override`（同文件的 `/api/confirm` 与 SSE 路径都在传），
    所以这不是能力缺失，是**接线漏了一处**。

    `thread_id` / `resume` 是**断点续跑**参数，只有流式路径（`run_task_stream`）支持；
    同步路径是一次性跑完，本来就没有断点。**它们不装作被支持**——传了就如实回一句说明。
    """
    import threading

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    _unused = []
    if req.thread_id:
        _unused.append("thread_id")
    if req.resume:
        _unused.append("resume")
    _TASKS[task_id] = {
        "id": task_id,
        "status": "running",
        "progress": 5,
        "result": None,
        # 调用方传了本端点不支持的能力 → **明确记下来**，别让它以为生效了
        "ignored_params": _unused,
    }

    def _run():
        try:
            _TASKS[task_id]["progress"] = 10
            result = run_task(
                req.question,
                _normalize_context(req.context),
                _parse_plan_override(req.plan),
            )
            if _unused:
                result["ignored_params_note"] = (
                    f"本端点（同步 /api/execute）不支持 {'、'.join(_unused)}："
                    "断点续跑只在流式端点 /api/execute-stream 上支持。本次已按**从头完整执行**处理。"
                )
            try:
                from core import billing
                from core.runstats import log_run_stats

                _d0 = (req.context or {}).get("depth") or ""
                _dm = billing.MODE_ALIAS.get(str(_d0), str(_d0)) or billing.DEFAULT_MODE
                log_run_stats(_dm, req.question, result.get("run_stats"))
            except Exception:
                pass
            _TASKS[task_id].update(
                {
                    "status": "done",
                    "progress": 100,
                    "result": _serialize_result(result),
                }
            )
        except Exception as e:
            _TASKS[task_id].update(
                {"status": "failed", "progress": 100, "error": str(e)}
            )

    threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "status": "running"}


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    """查询任务状态（含进度、Trace 事件、冲突仲裁卡片、报告）"""
    task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    # 运行中：返回进度，result 为 None
    return task


# ============================================================
# 多模态：附件上传 → 内容抽取 → 纳入调研上下文
# ============================================================
class UploadResponse(BaseModel):
    filename: str
    kind: str              # image / text / pdf / unknown
    summary: str = ""      # 抽取出的文本摘要（供 LLM 纳入调研）
    content: str = ""      # 原始抽取文本（前端可预览）


@app.post("/api/upload")
async def upload_attachment(file: "UploadFile"):
    """接收用户上传的文件（图片/PDF/文本），抽取内容返回

    图片：DeepSeek 无法看图，走文件名+大小占位提示；文本/PDF：抽取文本。
    后续接 qwen-vl 可真正识图（当前保持简单、不翻车）。
    """
    import asyncio
    import io

    raw = await file.read()
    name = file.filename or "attachment"
    ext = (name.split(".")[-1] or "").lower()
    size = len(raw)

    kind = "unknown"
    content = ""
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        kind = "image"
        # 多模态：Qwen-VL 结构化识图（价目表/产品图/截图 → 键值/数字清单）
        import base64 as _b64

        from mcp.search import understand_image

        structured = []
        try:
            b64 = _b64.b64encode(raw).decode()
            content = understand_image(
                b64,
                question=(
                    "请从这张图片中提取所有关键数值信息（价格/规模/数量/百分比/日期等），"
                    "输出JSON：{\"items\":[{\"name\":\"项目名\",\"value\":\"数值+单位\"}]}，"
                    "再在JSON后补充一段图片内容描述。只输出内容，不要解释。"
                ),
            )
            # 结构化：尝试从返回里解析 JSON 键值（多模态交互的"结构化"升级）
            import json as _json
            import re as _re

            m = _re.search(r"\{.*\}", content, _re.S)
            if m:
                parsed = _json.loads(m.group(0))
                items = parsed.get("items") or []
                structured = [i for i in items if isinstance(i, dict) and i.get("name")]
        except Exception as e:
            logger.warning("图片识别失败: %s", e)
            content = f"[图片] {name}（{size}字节，图片解析失败）"
            structured = []
    elif ext == "pdf":
        kind = "pdf"
        content = _extract_pdf_text(raw) or f"[PDF] {name}（未解析出文本，{size}字节）"
        if len(content) < 30:
            content = f"[PDF] {name}（{size}字节，{content}）"
    elif ext in ("txt", "md", "csv", "json"):
        kind = "text"
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                content = raw.decode(enc)
                break
            except Exception:
                continue
    else:
        kind = "unknown"
        content = f"[文件] {name}（{size}字节）"

    if not content.strip():
        content = f"[文件] {name}（{size}字节，暂不支持解析）"

    return {
        "filename": name,
        "kind": kind,
        # 真正进提问的那一段（前端把它拼进 question）。**上限从 500 提到 2000**：
        # 500 字对一份价目表/年报就是"开头两行"，而 `content` 早就算到了 2000。
        # 但**提上限不等于不截断**——所以同时回 `chars_total`，让前端把差额写进提问
        # （2026-09-16 全量审计：实测 37,260 字的附件只有 500 字到达模型且无人告知）。
        "summary": content[:2000],
        "content": content[:2000],
        "chars_total": len(content),
        "chars_used": min(len(content), 2000),
        "structured": locals().get("structured", [])[:20],
    }


def _extract_pdf_text(raw: bytes) -> str:
    """从 PDF 字节流抽取文本（无第三方库时的简单正则兜底）"""
    import re

    try:
        # 尝试 PyPDF2/pypdf
        try:
            import io as _io

            from pypdf import PdfReader

            reader = PdfReader(_io.BytesIO(raw))
            return "\n".join(
                (p.extract_text() or "") for p in reader.pages[:20]
            ).strip()
        except Exception:
            pass
        # 兜底：正则抽取 PDF 内嵌文本（简易）
        text = raw.decode("latin-1")
        chunks = re.findall(r"\((.*?)\)", text)
        return "\n".join(c for c in chunks if c.strip() and len(c) > 1)[:4000]
    except Exception:
        return ""


# ============================================================
# 运行台账：真实 API 执行账本（P0-B 配套，算账页/稳定性页/浏览器直读）
# ============================================================
@app.api_route("/api/run-stats", methods=["GET"])
def run_stats_ledger(limit: int = 50):
    """读 run_stats 真实运行台账（最新在前，倒序）。无账本 → {"count":0,"rows":[]}。

    数据只在 app.py /api/execute-stream、/api/execute（真实用户跑）落账；
    离线 _fulltest 不经过 API，不污染账本。
    """
    from core.runstats import read_ledger

    _rows = list(reversed(read_ledger()))
    _n = max(1, int(limit or 200))
    return {"total": len(_rows), "count": min(len(_rows), _n), "rows": _rows[:_n]}


@app.api_route("/api/repurchase-board", methods=["GET"])
def repurchase_board(depth: str = "", md: int = 0):
    """最小复购看板四指标（任务成功率 / 有效复用率 / 人工接管率 / 成功结果成本）。

    这四个指标是赛道培训里明确建议参赛者带上台的。**口径与分母在返回值里写全**
    （每项带 definition / num / den），第三方可按同一口径用 `/api/run-stats` 复算。
    缺测一律给 None 而不是 0：把"没记"当"没有"会把看板变成自欺。

    `?md=1` → 返回可粘贴的 markdown（与 `/api/autonomy-map?md=1`、
    `/api/outcome-review?md=1` 同一写法）。**这一条不是锦上添花**：
    `repurchase_board.render_md` 此前**零生产调用**（只有自测在调）——
    一个没有调用方的渲染面等于没有渲染面，而材料里写着它"进报告附录/PPT/说明稿"。
    """
    from core.repurchase_board import build_board

    _b = build_board(depth=depth)
    if md:
        from fastapi.responses import PlainTextResponse

        from core.repurchase_board import render_md

        return PlainTextResponse(render_md(_b), media_type="text/markdown; charset=utf-8")
    return _b


@app.api_route("/api/autonomy-map", methods=["GET"])
def autonomy_map(md: int = 0):
    """自主性图谱：每个编排节点「谁在决定 / 影响多大 / 有没有护栏 / 可不可逆」。

    这张表存在的意义是回答那个必问的问题——"你的智能体自主在哪？拓扑不是写死的吗？"
    与其辩护，不如把每个节点摊开让人自己看。`?md=1` 直接返回可粘贴的 markdown。
    """
    from core.autonomy_map import entries, render_md, summary

    if md:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(render_md(), media_type="text/markdown; charset=utf-8")
    return {"summary": summary(), "nodes": entries()}


@app.api_route("/api/outcome-review", methods=["GET", "POST"])
def outcome_review(
    request: Request = None, run_now: int = 0, ids: str = "", max_conds: int = 3, md: int = 0
):
    """决策回访：当初写下的监控条件，今天动了没有。

    GET  → 返回**历史回访记录** + 在途基线清单（不发起检索，零成本）
    GET/POST ?run_now=1 → 现场对在途基线做一次真实回访（会发起检索，耗时/有成本）
    POST（裸，不带参数）→ **也发起回访**（与 docstring 一致；此前代码只认 `run_now`，
      于是"POST 就会跑"这句文档是**反的**——调用方 POST 完以为跑了，实际什么也没发生）
    ?md=1 → 返回 markdown（与 `/api/autonomy?md=1` 同一写法），供说明稿/报告附录直接引用

    **为什么不把它塞进报告**：回访是**报告交付之后**才发生的事（相隔数天到数月），
    它属于「这个数字员工还在替你盯着」的持续服务面，不是报告的一节。
    但它的 **markdown 面必须有真实调用方**——否则 `outcome_track.format_md` 就是死代码，
    而"回访结果可披露"这句话就没有落点。

    三态口径：changed / unchanged / **no_result（未取得）**——
    未取得绝不写成"没变化"，否则等于自己给自己盖章。
    """
    from core.outcome_track import format_md as _fmt_md, load_baselines, load_reviews, run as _run

    # **POST 就是"跑一次"**：把"是不是 POST"也算进触发条件，文档与行为才一致。
    _is_post = bool(request is not None and getattr(request, "method", "") == "POST")
    _do = bool(run_now) or _is_post
    _cur = None
    if _do:
        _id_list = [x.strip() for x in (ids or "").split(",") if x.strip()]
        _cur = _run(ids=_id_list or None, max_conds=max(1, min(int(max_conds or 3), 6)))
    if md:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(_fmt_md(_cur), media_type="text/markdown; charset=utf-8")
    _bs = load_baselines()
    return {
        "in_flight": [
            {
                "id": b.get("id"),
                "question": str(b.get("question") or "")[:120],
                "industry": b.get("industry"),
                "verdict": b.get("verdict"),
                "decision_date": b.get("decision_date"),
                "due_date": b.get("due_date"),
                "monitor_conditions": b.get("monitor_conditions") or [],
                # **来源必须可见**（2026-09-15 外部审计）：`register` 写了 `source`
                # （"auto（交付时自动登记）" vs 人手工写的），但**全链路没有任何读取方**，
                # 于是界面上两种基线长得一模一样。而它们的分量完全不同：
                # 一条是"系统自己在交付时记下的账"，一条是"人补进去的"。
                "source": b.get("source") or "手工登记",
            }
            for b in _bs
            if str(b.get("status") or "in_flight") == "in_flight"
        ],
        "reviews": load_reviews(limit=20),
        "current": _cur,
    }


# ============================================================
# 报告导出：Word / Excel
# ============================================================
@app.api_route("/api/export/{fmt}", methods=["GET", "POST"])
def export_report(
    fmt: str,
    payload: dict | None = Body(default=None),
    content: str = "",
    title: str = "研策调研报告",
):
    """把报告 markdown 导出为指定格式（docx/html/pptx/xlsx/pdf）

    2026-09-04 修复：报告正文几十KB，旧前端用 GET query 携带整篇 content →
    URL 超 uvicorn/h11 请求行上限 → 后端返回 "Invalid HTTP request received."。
    现支持 POST body {"content","title"}（无长度限制）；GET 保留兼容旧链接。
    """
    if isinstance(payload, dict):
        if payload.get("content"):
            content = payload.get("content", "")
        if payload.get("title"):
            title = payload.get("title", title)
    from fastapi.responses import Response

    if fmt == "md":
        # Markdown 原生导出（归档/再编辑/投稿用的轻量入口；报告正文本身就是 md）
        # **内联图表**：md 原生支持 `![](data:image/png;base64,...)`，Typora/VSCode/GitHub/飞书 都能渲染。
        # 病案（2026-09-11 用户实导）：md 导出一直是裸正文 → 表格齐全、图一张没有，
        # 用户看到的交付物就是"纯文字对话式输出"，而同一份内容导 docx 却有图——**是导出管线不一致**。
        try:
            from core.report_charts import MAX_EXPORT_CHARTS, decorate_md_charts

            # skip_metrics_table=False：**核心指标表是整份报告里最该出图的一张**
            # （指标|数值|来源|年份六列，全是可比数值）。原先把这条留给"交付物头部"，
            # 但 md 交付物并没有那个头部——跳过的结果是"报告一张图都没有"。
            # 2026-09-11 事故：md 导出其实一直走的是**前端**直下（见 App.jsx exportAs），
            # 这段代码从没被执行过；修好路由后必须把这张表也放进来。
            content = decorate_md_charts(
                content or "", max_charts=MAX_EXPORT_CHARTS, skip_metrics_table=False
            )
        except Exception as _e:
            import logging as _lg

            _lg.getLogger(__name__).warning("md 内联图表失败(降级为纯文本): %s", _e)
        fname = f"{title}.md"
        # 隐式标识（法定）：md 无文件级元数据槽位，用 HTML 注释承载
        # （Typora/VSCode/GitHub 均不渲染注释 → 不影响阅读，但机器可读、随文件走）
        try:
            from core.ai_label import canonical_content_id as _md_cid, implicit_meta as _md_meta

            _m = _md_meta(content or "", title=title)
            # **编号复用正文里那一个，不再重算**（2026-09-15 深访实跑）：
            # 原先这里对 `content`（已含导出期内联的 base64 图表）重算哈希，
            # 而正文标识节里的编号是引擎在**不含图表**时算的 → 同一份文件印出两个编号。
            # 编号是我们唯一能被当场证伪的承诺（"改一字即变、重导出不变"），
            # 自己先不自洽等于把这句话作废。
            _m["content_id"] = _md_cid(content or "")
            content = (
                f"<!-- AIGC: {_m['AIGC']} | content-producer: {_m['content_producer']} "
                f"| producer-name: {_m['producer_name']} | content-id: {_m['content_id']} "
                f"| basis: {_m['label_basis']} -->\n\n" + (content or "")
            )
        except Exception:
            pass
        return Response(
            (content or "").encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    'attachment; filename="report.md"; '
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    if fmt == "docx":
        # 专业版 Word：封面 + 编号章节 + 高亮框 + 规范表格 + 页码（对照咨询级报告模板）
        try:
            from core.report_docx import build_report_docx
        except ImportError:
            raise HTTPException(status_code=400, detail="Word导出组件未安装，请运行: pip install python-docx")
        _q = ""
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
            _q = str((payload.get("meta") or {}).get("question") or "").strip()
        data = build_report_docx(content or "", title, question=_q)
        fname = f"{title}.docx"
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": (
                    "attachment; filename=\"report.docx\"; "
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    if fmt == "html":
        # 交互式研报作品页（封面+hero+指标可视化+折叠审计）；meta 缺省时后端从正文自解析
        from core.report_html import build_html_report

        _meta = {}
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
            _meta = payload["meta"]
        page = build_html_report(content or "", title, meta=_meta)
        fname = f"{title}.html"
        return Response(
            page.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": (
                    "attachment; filename=\"report.html\"; "
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    if fmt == "pptx":
        # PPT 导出：标题页 + 每个章节一页 + 正文要点（python-pptx）
        try:
            import io as _io

            from pptx import Presentation
            from pptx.util import Pt
        except ImportError:
            raise HTTPException(status_code=400, detail="PPT导出组件未安装，请运行: pip install python-pptx")

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        try:
            slide.shapes.title.text = title
        except Exception:
            pass

        from pptx.util import Inches as _In

        import re as _re

        _ST = {"slide": None, "tf": None, "n": 0}

        def _clean(t):
            t = _re.sub(r"<ref>(\d+)</ref>", r"[\1]", t)
            t = _re.sub(r"</?span[^>]*>", "", t)
            t = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
            t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
            t = t.replace("**", "").replace("`", "").strip()
            return t[:200]

        def _new_slide(h):
            s = prs.slides.add_slide(prs.slide_layouts[1])
            try:
                s.shapes.title.text = _clean(h)[:42] or "研策"
            except Exception:
                pass
            _ST["slide"] = s
            _ST["tf"] = None
            try:
                ph = s.placeholders[1]
                ph.text = ""
                _ST["tf"] = ph.text_frame
            except Exception:
                pass
            if _ST["tf"] is None:
                try:
                    box = s.shapes.add_textbox(_In(0.4), _In(0.9), _In(9.2), _In(5.9))
                    box.text_frame.word_wrap = True
                    _ST["tf"] = box.text_frame
                except Exception:
                    pass
            _ST["n"] = 0
            return s

        def _para(t):
            tf = _ST["tf"]
            if tf is None or not t or _ST["n"] > 150:
                return
            t = t.replace("> ", "").replace(">", "")
            for _mk in ("- ", "* ", "• "):
                if t.startswith(_mk):
                    t = t[len(_mk):]
                    break
            p = tf.add_paragraph()
            p.text = _clean(t)
            try:
                p.font.size = Pt(13)
            except Exception:
                pass
            _ST["n"] += 1

        def _add_table_slide(block, caption):
            rows = []
            for ln in block:
                cells = [c.strip().strip("*`").strip() for c in ln.strip().strip("|").split("|")]
                rows.append(cells)
            if len(rows) < 2:
                return
            ncols = max((len(r) for r in rows), default=0)
            ncols = max(2, min(ncols, 6))
            tgt = []
            for r in rows:
                if len(r) == ncols and all(_re.fullmatch(r":?-{2,}:?|", c.strip()) for c in r):
                    continue
                tgt.append([(r[c] if c < len(r) else "")[:24] for c in range(ncols)])
            if not tgt:
                return
            s = _new_slide((caption or "报告")[:30] + " · 数据表")
            try:
                rows_h = min(len(tgt), 14)
                gt = s.shapes.add_table(
                    len(tgt), ncols, _In(0.3), _In(1.0), _In(9.4), _In(0.32 * rows_h)
                ).table
                for ri, row in enumerate(tgt):
                    for ci, cell_txt in enumerate(row):
                        try:
                            gt.cell(ri, ci).text = cell_txt
                        except Exception:
                            pass
            except Exception:
                pass

        _cur_caption = "研策"
        # 逐行遍历：##/### 新开正文页，连续 | 行整块成一张"数据表"页（while 支持跨行收集）
        _lines_ppt = (content or "").split("\n")
        _idx = 0
        while _idx < len(_lines_ppt):
            s = _lines_ppt[_idx].strip()
            if not s:
                _idx += 1
                continue
            if s.startswith("|") and "|" in s[1:]:
                block = []
                while _idx < len(_lines_ppt) and _lines_ppt[_idx].strip().startswith("|"):
                    block.append(_lines_ppt[_idx].strip())
                    _idx += 1
                try:
                    _add_table_slide(block, _cur_caption)
                except Exception:
                    pass
                continue
            _idx += 1
            if s.startswith("### "):
                _new_slide(s[4:].strip())
                _cur_caption = s[4:].strip()
            elif s.startswith("## "):
                _new_slide(s[3:].strip())
                _cur_caption = s[3:].strip()
            elif s.startswith("# ") and s.strip() != "#":
                continue  # 封面已给，单 # 主标题不再新开
            elif s.startswith(("|", "---")):
                continue
            elif _re.fullmatch(r"-{3,}|\*{3,}", s):
                continue
            elif len(s) > 1:
                _para(s)
        buf = _io.BytesIO()
        # 隐式标识（法定）：PPT 是**最容易被单独转发的一层**（每页截图/整份外发），
        # 元数据带上 AI 属性/服务提供者/内容编号。
        try:
            from core.ai_label import apply_file_meta as _aigc_meta

            _aigc_meta(prs.core_properties, content or "", title=title)
        except Exception:
            pass
        prs.save(buf)
        buf.seek(0)
        fname = f"{title}.pptx"
        return Response(
            buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={
                "Content-Disposition": (
                    "attachment; filename=\"report.pptx\"; "
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    if fmt == "xlsx":
        try:
            import io as _io
            import re

            from openpyxl import Workbook
        except ImportError:
            raise HTTPException(status_code=400, detail="Excel导出组件未安装，请运行: pip install openpyxl")

        wb = Workbook()
        ws = wb.active
        ws.title = "报告速览"
        # ── 法定 AI 标识（2026-09-15 补；xlsx 此前是第六个格式、唯一一个裸奔的）──
        # 显式：表首两行（读者打开表格第一眼就看得见，不是埋在元数据里）；
        # 隐式：docProps/core.xml 的 title/creator/subject/description/keywords/category。
        try:
            # ⚠️ **必须用 `canonical_content_id`（复用正文那个），不能用 `content_id`（重算）**
            # （2026-09-16 修）。病案：同一份 xlsx 里，显式标识行用的是**重算出来的**哈希，
            # 而元数据（`xlsx_meta` → `implicit_meta`）用的是**复用正文的**编号 ——
            # **一个文件里两个内容编号**，而紧跟着第三行还印着
            # 「导出的元数据中含同一内容编号，可核对是否同一份」。
            # 这句话本来是对外承诺的核心（"编号改了 = 内容变了"），结果它自己就是反例。
            from core.ai_label import canonical_content_id as _cid, xlsx_meta as _xlsx_meta

            ws.append(["AI 生成合成内容标识", f"内容编号 {_cid(content or '')}"])
            ws.append(["服务提供者", "研策（Yancec）· 市场调研数字员工"])
            ws.append(["说明", "内容由 AI 参与生成；导出的元数据中含同一内容编号，可核对是否同一份"])
            ws.append(["", ""])
            for _k, _v in _xlsx_meta(content or "", title=title).items():
                try:
                    setattr(wb.properties, _k, _v)
                except Exception:
                    pass
        except Exception:
            pass  # 标识失败不影响导出本身
        ws.append(["章节", "内容"])
        current = title
        for line in (content or "").split("\n"):
            s = line.strip()
            if not s:
                continue
            if s.startswith(("## ", "# ", "### ")):
                current = s.lstrip("# ").strip()
                ws.append([current, ""])
            else:
                ws.append([current, s[:200]])
        buf = _io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = f"{title}.xlsx"
        return Response(
            buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    "attachment; filename=\"report.xlsx\"; "
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    if fmt == "pdf":
        try:
            import io as _io
            import re as _re

            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        except ImportError:
            raise HTTPException(status_code=400, detail="PDF导出组件未安装，请运行: pip install reportlab")

        # 中文字体：微软雅黑(TTC)/黑体(TTF)，都失败则退回默认
        _FONT = "YancecCJK"
        try:
            pdfmetrics.registerFont(TTFont(_FONT, r"C:\Windows\Fonts\msyh.ttc", subfontIndex=0))
        except Exception:
            try:
                pdfmetrics.registerFont(TTFont(_FONT, r"C:\Windows\Fonts\simhei.ttf"))
            except Exception:
                _FONT = "Helvetica"

        st_title = ParagraphStyle("t", fontName=_FONT, fontSize=16, leading=22, spaceAfter=10)
        st_h = ParagraphStyle("h", fontName=_FONT, fontSize=13, leading=18, spaceBefore=8, spaceAfter=3)
        st_p = ParagraphStyle("p", fontName=_FONT, fontSize=10.5, leading=16)

        buf = _io.BytesIO()
        # 隐式标识（法定）：PDF 文档元数据（标题/作者/主题/创建者）写入 AI 属性与内容编号。
        try:
            from core.ai_label import pdf_meta as _pdf_meta

            _pdf_kw = _pdf_meta(content or "", title=title)
        except Exception:
            _pdf_kw = {}
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=15 * mm, rightMargin=15 * mm,
            topMargin=15 * mm, bottomMargin=15 * mm,
            **_pdf_kw,
        )

        st_q = ParagraphStyle("q", fontName=_FONT, fontSize=10, leading=14, leftIndent=10, textColor="#555555", spaceAfter=4)
        st_li = ParagraphStyle("li", fontName=_FONT, fontSize=10.5, leading=15, leftIndent=8, spaceAfter=2)
        st_cell = ParagraphStyle("cell", fontName=_FONT, fontSize=8.5, leading=11)

        # ── 字形安全化（2026-09-15 修）────────────────────────────────────
        # 病案（外部审计实读导出的 PDF）：**显式标识那一行的开头渲染成了 `\x00`（空白/方块）**。
        # 原因：标识行以 `🤖` 开头，而这里注册的中文字体（msyh.ttc / simhei.ttf）的 cmap 里
        # **没有 U+1F916**（实测 29,898 个字形，0x1F916 与 0x2B50 均为 False）。
        # 后果特别难堪：法定标识是**唯一一个会被打印出来**的格式里，恰恰是它变成了乱码。
        #
        # 做法：按**字体实际能力**判断（`face.charToGlyph`，不是猜），渲染不出的字符
        # 换成可读的文字替身；我们没给替身的就直接删掉——**绝不印方块**。
        # 判不出能力时原样保留（不误删文字）。
        #
        # 注意：**只作用于渲染，不改 `content` 本身**——内容编号必须与 docx/md/pptx
        # 用同一份原文算出同一个号（同一交付件里出现两个编号是已经修过的一个缺陷）。
        _GLYPH_TEXT = {
            "🤖": "[AI] ", "✅": "√ ", "❌": "× ", "⚠": "! ", "🔔": "* ",
            "🎯": "", "📊": "", "📌": "", "🔗": "", "💰": "", "⚡": "", "⏱": "",
            "🟢": "", "🔴": "", "🟡": "", "🚪": "", "🟨": "", "🟩": "",
        }

        def _font_ok(ch: str) -> bool:
            if _FONT == "Helvetica":
                return ord(ch) < 256
            try:
                return ord(ch) in pdfmetrics.getFont(_FONT).face.charToGlyph
            except Exception:
                return True  # 判不了就不动文字

        def _glyph_safe(s: str) -> str:
            if not s:
                return s
            out: list[str] = []
            for ch in s:
                if ord(ch) < 128 or _font_ok(ch):
                    out.append(ch)
                elif ch in _GLYPH_TEXT:
                    out.append(_GLYPH_TEXT[ch])
                # 字体没有、我们也没给替身 → 丢弃（宁少一个字，不印一个方块）
            return "".join(out)

        from reportlab.platypus import Table as _Table, TableStyle as _TS

        def _esc(s: str) -> str:
            return _glyph_safe(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _rich(s: str) -> str:
            """行内 markdown → reportlab 迷你 HTML（<ref>[n] 角标、加粗/斜体、链接只留文字）"""
            s = _re.sub(r"<ref>(\d+)</ref>", r"[\1]", s)   # 引用角标 → [n]
            s = _re.sub(r"</?span[^>]*>", "", s)            # 来源标记 span → 删
            s = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)  # 图片 → 只留 alt
            s = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)   # 链接 → 只留文字
            s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
            s = _re.sub(r"__(.+?)__", r"<b>\1</b>", s)
            s = _re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", s)
            s = s.replace("`", "")
            return s.strip()

        def _plain(s: str) -> str:
            """纯文本（不留任何 md 记号/加粗标签）"""
            t = _rich(s)
            return t.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")

        def _is_sep(cells):
            return bool(cells) and all(_re.fullmatch(r":?-{2,}:?|", c.strip()) for c in cells)

        # PDF 图表总预算：**与 Word / md 同一个常量**（2026-09-16 收口）。
        # 原先是字面量 4，而 docx 是 12、md 是 16——同一份报告三份图数不同，
        # 且两处注释互相声称"与对方对齐"。数字只有一个地方能改（`report_charts.MAX_EXPORT_CHARTS`）。
        from core.report_charts import MAX_EXPORT_CHARTS as _MAX_CHARTS

        _chart_state = {"left": _MAX_CHARTS, "n": 0}

        def _add_md_table(block):
            """连续 '|...|' 行 → 规范 reportlab Table（不再是乱码文本行）"""
            rows_data = []
            for ln in block:
                cells = [c.strip().strip("*`").strip() for c in ln.strip().strip("|").split("|")]
                rows_data.append(cells)
            ncols = max((len(r) for r in rows_data), default=0)
            if ncols < 2:
                return
            body = []
            for r_i, r in enumerate(rows_data):
                if r_i == 1 and len(r) == ncols and _is_sep(r):
                    continue  # 跳过 '|---|---|' 分隔行
                row = []
                for c_i in range(ncols):
                    cell_txt = (r[c_i] if c_i < len(r) else "").strip()
                    row.append(Paragraph(_esc(_plain(cell_txt)) or " ", st_cell))
                body.append(row)
            if not body:
                return
            cw = max(46.0, doc.width / max(ncols, 3))
            table = _Table(body, colWidths=[cw] * ncols, repeatRows=1)
            table.setStyle(
                _TS(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, "#c9cdd4"),
                        ("BACKGROUND", (0, 0), (-1, 0), "#eef1f5"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(Spacer(1, 4))
            story.append(table)
            story.append(Spacer(1, 4))
            # 任意"可绘图的数据表"（同单位 ≥2 行 + 异义守卫）后内嵌柱状图
            # （旧版只认核心指标表 → 整份 PDF 常只有一张图；配额与 Word 一致）
            if _chart_state["left"] > 0:
                try:
                    from core.report_charts import metric_chart_pngs
                    from reportlab.platypus import Image as _Img

                    for _it in metric_chart_pngs(rows_data):
                        if _chart_state["left"] <= 0:
                            break
                        _chart_state["left"] -= 1
                        _chart_state["n"] += 1
                        _cap = Paragraph(
                            _esc(f"图{_chart_state['n']}　数据对比（{_it.get('unit') or '—'}）｜数据来源：见对应表格来源列"),
                            ParagraphStyle(
                                "cap", fontName=_FONT, fontSize=8.5, leading=11,
                                alignment=1, textColor="#6b7280",
                            ),
                        )
                        story.append(Spacer(1, 2))
                        story.append(_cap)
                        story.append(_Img(_io.BytesIO(_it["png"]), width=490, height=192))
                except Exception:
                    pass

        story = [Paragraph(_esc(title), st_title), Spacer(1, 4)]
        _lines = (content or "").split("\n")
        _i = 0
        while _i < len(_lines):
            raw = _lines[_i]
            s = raw.strip()
            if not s:
                _i += 1
                continue
            # 连续表格块：整块收集成一个规范 Table
            if s.startswith("|") and "|" in s[1:]:
                block = []
                while _i < len(_lines) and _lines[_i].strip().startswith("|"):
                    block.append(_lines[_i].strip())
                    _i += 1
                try:
                    _add_md_table(block)
                except Exception:
                    pass
                continue
            _i += 1
            if _re.fullmatch(r"-{3,}|\*{3,}", s):
                story.append(Spacer(1, 2))  # 分隔线
                continue
            if s.startswith("### "):
                story.append(Paragraph(_esc(_plain(s[4:])), st_h))
            elif s.startswith("## "):
                story.append(Paragraph(_esc(_plain(s[3:])), st_h))
            elif s.startswith("# "):
                story.append(Paragraph(_esc(_plain(s[2:])), st_h))
            elif s.startswith("> "):
                q = _plain(s[2:]).strip()
                if q:
                    story.append(Paragraph(_esc(q), st_q))
            elif s.startswith(("- ", "* ", "• ")):
                li = _plain(s[2:]).strip()
                if li:
                    story.append(Paragraph(_esc("•  " + li), st_li))
            elif _re.match(r"^\d+\.\s", s):
                story.append(Paragraph(_esc(_plain(s)), st_li))
            else:
                t = _plain(s)
                if t:
                    story.append(Paragraph(_esc(t), st_p))
        doc.build(story)
        buf.seek(0)
        fname = f"{title}.pdf"
        return Response(
            buf.getvalue(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    "attachment; filename=\"report.pdf\"; "
                    f"filename*=UTF-8''{quote(fname)}"
                )
            },
        )
    raise HTTPException(status_code=400, detail="不支持的导出格式")


@app.post("/api/deep-dive")
def deep_dive(req: DeepDiveRequest):
    """追问闭环：对报告中的某句结论发起深挖（端到端闭环演示点）"""
    from agents.deep_dive import deep_dive as run_deep_dive

    try:
        result = run_deep_dive(
            claim=req.claim,
            user_intent=req.user_intent,
            context=req.context,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/primary-research")
def primary_research(req: PrimaryResearchRequest):
    """一手验证方案设计（AI 深访/问卷）：生成可执行的问卷+深访+实地方案

    治"无一手研究"的差异化护城河：报告说'建议补一手验证'，研策直接帮你设计好怎么做。
    """
    from core.primary_research import design_primary_research, format_md, scan_gaps

    try:
        report = req.report or ""
        design = design_primary_research(report)
        return {
            "design": design,
            "md": format_md(design),
            "gaps": scan_gaps(report),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/primary-research/simulate")
def primary_research_simulate(req: PrimaryResearchRequest):
    """一手研究·模拟问卷数据（演示闭环）：问卷设计后生成模拟样本+自动分析

    对齐"一手研究限定 设计+模拟数据"：真实投放需时间/预算/伦理审批，演示阶段用
    模拟样本（分布按报告结论设定）展示『问卷 → 样本 → 分析 → 结论反哺』全闭环。
    返回 md（含汇总表+关键发现+诚实标注）、csv（可下载）。
    """
    from core.simulated_data import simulate_survey

    try:
        sim = simulate_survey(req.report or "", req.design, n=120)
        return {"md": sim["md"], "csv": sim["csv"], "method": sim["method"], "n": sim["n"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 会话记忆 + 资产库（前端可查看/清除）
# ============================================================
@app.get("/api/memory")
def get_memory():
    """查看用户记忆（历史行业 + 偏好）"""
    from core.memory import _load

    return _load()


@app.post("/api/memory/clear")
def clear_memory():
    """清除用户记忆"""
    from core.memory import clear as clear_memory_profile

    clear_memory_profile()
    return {"ok": True}


@app.get("/api/monitors")
def list_monitors():
    """持续监控面板：历史决策 + 触发条件（Decision Memory / Market Monitor）"""
    from core.memory import list_decisions

    return {"decisions": list_decisions()}


@app.get("/api/industry-cards")
def industry_cards():
    """行业基线卡（沉淀下来的"越用越懂"）——**给人看的读取路径**。

    病案（2026-09-15 外部审计）：`core/industry_kb.get_industry_card` 与
    `list_industries()` **全仓无调用者**（只有自测在调）。也就是说：
    行业卡确实在沉淀、确实被注入到新报告里，但**没有任何界面能让人看见它**——
    而 Demo 分镜里有一镜就要拍它。**数据有、披露无**，等于这条护城河只有我们自己知道。

    返回每张卡的**摘要**（不返回整卡：卡里字段多，面板只需要看得懂的那几项），
    外加 `brief` 字段（就是注入报告的那段文本，逐字一致——**所见即所注**）。
    """
    from core.industry_kb import get_industry_card, list_industries

    out = []
    for ind in list_industries():
        card = get_industry_card(ind) or {}
        try:
            from core.industry_kb import format_industry_brief

            brief = format_industry_brief(ind)
        except Exception:
            brief = ""
        _ms = card.get("market_size") or {}
        # 值里常常已经带了单位（"189.7亿元"），再拼一次会变成"189.7亿元 亿元"。
        # 判据用"值是否已含单位"，而不是无脑拼——**所见即所注**，拼错了就是新的不一致。
        _v = str(_ms.get("value", "") or "").strip()
        _u = str(_ms.get("unit", "") or "").strip()
        _size = _v if (not _u or _u in _v) else f"{_v} {_u}"
        out.append(
            {
                "industry": ind,
                "updated": (card.get("_updated") or ""),
                "market_size": _size.strip(),
                "key_players": list(card.get("key_players") or [])[:5],
                "key_numbers": [
                    f"{k.get('name', '')}={k.get('value', '')}"
                    for k in (card.get("key_numbers") or [])[:6]
                ],
                "n_numbers": len(card.get("key_numbers") or []),
                "brief": brief,
            }
        )
    return {"count": len(out), "cards": out}


@app.get("/api/assets")
def list_assets():
    """查看资产库（行业档案沉淀，可复用证据）"""
    from core.rag import _get_collection

    try:
        col = _get_collection()
        if hasattr(col, "count"):
            count = col.count()
        else:
            count = 0
        # 按行业分组（从元数据提取）
        items = []
        try:
            data = col.get(limit=100)
            for i in range(min(len(data.get("ids", [])), 100)):
                m = data["metadatas"][i] if i < len(data.get("metadatas", [])) else {}
                doc = data["documents"][i] if i < len(data.get("documents", [])) else ""
                items.append(
                    {
                        "id": data["ids"][i],
                        "industry": m.get("industry", "未知"),
                        "source_title": m.get("source_title", ""),
                        "content": (doc or "")[:120],
                        "created_at": m.get("created_at", ""),
                    }
                )
        except Exception:
            pass
        # 按行业聚合
        groups: dict[str, int] = {}
        for it in items:
            groups[it["industry"]] = groups.get(it["industry"], 0) + 1
        return {"count": count, "items": items[:50], "groups": groups}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"资产库读取失败: {e}")


# ============================================================
# 工具合同（Tool Contract）：培训"工具=可审计业务合同"四维标准
# ============================================================
@app.get("/api/tools")
def list_tools():
    """工具合同清单：目的/输入/输出/权限/失败处理（清楚/完整/可恢复/可审计）"""
    from mcp.search import tool_contracts

    return {"tools": tool_contracts()}


# ============================================================
# 序列化辅助
# ============================================================
def _serialize_result(result: dict[str, Any]) -> dict[str, Any]:
    """把状态机结果转成 JSON 可序列化（Pydantic 模型 → dict）"""
    return {
        "status": result.get("status"),
        "plan": result.get("plan").model_dump(mode="json") if result.get("plan") else None,
        "evidences": [ev.model_dump(mode="json") for ev in result.get("evidences", [])],
        "trace_events": result.get("trace_events", []),
        "conflict_results": result.get("conflict_results", []),
        # 与 graph._result_json 同口径：仲裁分母两条路径都要带出去（2026-09-16）
        "conflict_scan": result.get("conflict_scan") or {},
        "report_final": result.get("report_final", ""),
        "competition_report": result.get("competition_report", ""),
        "citations": result.get("citations", []),
        "bad_urls": result.get("bad_urls", []),
        # 合规没敲门的地址（≠死链）：保留在正文里、附「未探测」标注，不计入通过率
        "unprobed_urls": result.get("unprobed_urls", []),
        "url_fixes": result.get("url_fixes", {}),
        "citation_report": result.get("citation_report"),
        "quality_report": result.get("quality_report"),
        "stop_card": result.get("stop_card"),
        "validation_tasks": result.get("validation_tasks", []),
        "monitor_conditions": result.get("monitor_conditions", []),
        "decision_verdict": result.get("decision_verdict", ""),
        "metrics": result.get("metrics", []),
        "decision_chain": result.get("decision_chain", []),
        "entailment": result.get("entailment", {}),
        "citation_pass_rate": result.get("citation_pass_rate", 0.0),
        # 决策清单是否降级（v0.9.75）——与 graph._result_json 同口径，两条路径都带出去
        "sop_fallback": result.get("sop_fallback", {}) or {},
        "deliverables": result.get("deliverables", []),
        "budget_estimate": result.get("budget_estimate"),
        "run_stats": result.get("run_stats"),
    }


# ============================================================
# 前端静态托管（生产构建：npm run build 后，后端直接服务前端）
# 开发期用 `npm run dev`（5173）；交付/演示用这里（8000 一体运行，无 dev 缓存问题）
# index.html 强制 no-cache：避免浏览器加载旧构建包（旧包有 msgId 等历史bug）
# ============================================================
try:
    from pathlib import Path

    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    _DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if _DIST.exists():
        _INDEX = _DIST / "index.html"

        @app.get("/", include_in_schema=False)
        def _index():
            content = (
                _INDEX.read_text(encoding="utf-8") if _INDEX.exists() else "<h1>前端未构建</h1>"
            )
            return HTMLResponse(
                content, headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
            )

        _ASSETS = _DIST / "assets"
        if _ASSETS.exists():
            app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")
except Exception:
    pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
