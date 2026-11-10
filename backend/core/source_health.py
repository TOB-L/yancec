"""数据源健康巡检：把"我们接了 N 个源"从**宣称**变成**可随时自证的台账**

## 为什么要它

来源面板上写着"已接入 15 个源"，但那句话的时态是**过去完成时**——
指的是"代码里写了"。它回答不了复核者/客户真正会问的那句：

> **此刻**，这 15 个源有几个真的活着？

现实里源会死：政府站改版、协会站 502、接口加风控、API key 过期。
而死掉的源**不会报错**——它们只是静默返回空，报告照常生成，
只是那份报告的"权威来源"里有几条其实是空气。

所以这一层做两件事：

1. **健康巡检（patrol）**：逐个探活，落盘成台账（`data/source_health.json`），
   带**最后一次成功时间**。报告/接口读的是台账，不是代码里的 `status="connected"`。
2. **本次调用台账（trace）**：一次调研里**实际发起**了哪些源、各回来几条、
   哪些回空。这是比"我们有什么源"更硬的东西——它是"这次报告真的用了什么"。

## 红线（与主流程一致，不因为"只是探活"就放宽）

- 探活 = **一次 GET 列表页/公开 API**，不构造查询、不翻页、不遍历。
- 守 robots（走 `authority_fetch._get`，已内置 robots 门）、per-host ≥1s。
- 遇 403/412 **立刻停手并如实记 `blocked`**，不重试、不换 UA、不绕。
- 总开关 `AUTHORITY_FETCH_ENABLED=0` 时**一个请求都不发**。

## 读法与写法

- 台账里 `ok=False` 的源**不是"删掉它"的信号**，是"这一条此刻不可用"的诚实标注。
  所以报告里写的是「本次实际调用 X 个源，其中 Y 个有返回」，而不是"我们支持 15 个源"。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core import safe_io

logger = logging.getLogger(__name__)

_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "source_health.json"
_LOCK = threading.Lock()
_PATROL_LOCK = threading.Lock()          # 同时只允许一次巡检
_STALE_SECONDS = 24 * 3600               # 超过一天未巡检 → 视为陈旧


# ============================================================
# 探针定义
# ============================================================
@dataclass(frozen=True)
class Probe:
    """一个源的探活方式。

    `expect` 是**内容判据**而不只是状态码：政府站改版后常返回 200 的
    "页面已迁移"提示页，只看 200 会把死源记成活的。
    """

    sid: str
    name: str
    layer: str                    # L1..L5（与 source_registry 对齐）
    kind: str                     # http / api_key / local / dynamic
    url: str = ""
    expect: str = ""              # 正文须匹配的正则（空 = 只要 200）
    budget_s: float = 15.0
    note: str = ""


PROBES: tuple[Probe, ...] = (
    Probe("nbs", "国家统计局", "L1", "http",
          "https://www.stats.gov.cn/sj/zxfb/", expect=r"t\d{8}_\d+\.html",
          note="数据发布列表页；判据=页内仍有文章链接，不只判 200"),
    Probe("cninfo", "巨潮资讯", "L2", "http", "https://www.cninfo.com.cn/new/index",
          expect=r"巨潮", note="A 股法定披露入口页"),
    Probe("sec_edgar", "SEC EDGAR", "L2", "http", "https://www.sec.gov/",
          expect=r"(?i)sec\.gov|securities and exchange",
          note="美股法定申报；须带含联系方式的 UA（SEC_UA_CONTACT）"),
    Probe("ccgp", "中国政府采购网", "L1", "http",
          "http://www.ccgp.gov.cn/cggg/zygg/", expect=r"/\d{6}/t\d{8}_\d+\.htm"),
    Probe("caam", "中国汽车工业协会", "L3", "http",
          "http://www.caam.org.cn/chn/1/cate_1/list_1.html", expect=r"con_\d+\.html"),
    Probe("ccfa", "中国连锁经营协会", "L3", "http",
          "http://www.ccfa.org.cn/portal/cn/index.jsp", expect=r"xiangxi\.jsp"),
    Probe("sse_edc", "上证e互动", "L2", "http", "https://sns.sseinfo.com/",
          expect=r"e互动"),
    Probe("worldbank", "世界银行开放数据", "L1", "http",
          "https://api.worldbank.org/v2/country/CHN?format=json", expect=r"China"),
    Probe("imf", "IMF WEO", "L1", "http",
          "https://www.imf.org/external/datamapper/api/v1/NGDP_RPCH", expect=r"NGDP_RPCH"),
    # ---- 模式类源（无单一入口，按命中域名即时抓取）：探"能力是否在位"，不探某个具体站点 ----
    # 一个"模式类"源没有唯一入口页，硬给它安一个 URL 探活是**假精确**：
    # 那个页活着不代表这类源可用，它死了也不代表这类源不可用。
    # 所以这类源如实标 `dynamic`，探的是"这条路还在不在"（正则/域名表是否仍在位）。
    Probe("gov_portal", "政府门户/部委官网（模式类）", "L1", "dynamic",
          note="经检索命中 gov.cn/.gov 域名后定向补源；L1 判定只认域名白名单"),
    Probe("report_page", "券商/研究机构研报页（模式类）", "L3", "dynamic",
          note="命中研报类域名时抓正文作为独立研报来源"),
    Probe("bing_scrape", "Bing 结果页（无 key 兜底）", "L4", "http",
          "https://cn.bing.com/", expect=r"(?i)bing",
          note="仅在主通道拿不到真实可点 URL 时补位（已有缓存+滑窗配额+礼貌间隔三道闸）；只探首页可达性"),
    Probe("qwen_search", "通义千问 enable_search", "L4", "api_key", note="主文本检索通道"),
    Probe("bocha", "博查搜索 API", "L4", "api_key", note="结构化 URL 主源"),
    Probe("kb_assets", "行业知识库（本地向量库）", "L3", "local", note="chroma + bge-m3"),
)


def _probe_api_key(sid: str) -> tuple[bool, str]:
    """API key 类源：只判"配没配"，**不发请求**（探活不该花钱、也不该消耗配额）"""
    keys = {
        "qwen_search": ("DASHSCOPE_API_KEY", "QWEN_API_KEY", "OPENAI_API_KEY"),
        "bocha": ("BOCHA_API_KEY",),
    }.get(sid, ())
    for k in keys:
        v = str(os.environ.get(k, "") or "").strip()
        if v and len(v) >= 8:
            return True, f"{k} 已配置"
    return False, f"未配置（{'/'.join(keys)} 为空）"


def _probe_dynamic(sid: str) -> tuple[bool, str]:
    """模式类源（无单一入口页）：探"这条路还在不在"，而不是探某个具体站点。

    一个模式类源没有唯一入口，硬安一个 URL 去探是**假精确**——那个页活着不代表这类源可用，
    它死了也不代表这类源不可用。所以这里探的是"模式还在不在位"：
    域名白名单/识别规则是否仍在代码里（被删掉 = 这条路真的断了）。
    """
    try:
        if sid == "gov_portal":
            from core.domain_match import _GOV_SUFFIXES  # type: ignore

            n = len(_GOV_SUFFIXES)
            return (n > 0), f"政府域名白名单在位（{n} 条后缀规则）——按命中域名即时抓取，无固定入口"
    except Exception:
        pass
    try:
        if sid == "report_page":
            from agents.researchers import _REPORT_DOMAINS

            n = len(_REPORT_DOMAINS)
            return (n > 0), f"研报域名识别表在位（{n} 条）——按命中域名即时抓取，无固定入口"
        if sid == "gov_portal":
            # domain_match 里没有导出后缀表时的回退判据：白名单函数可调用即可
            from core.domain_match import host_hit  # type: ignore

            return callable(host_hit), "政府域名判定函数在位（按命中域名即时抓取，无固定入口）"
    except Exception as e:
        return False, f"模式类源校验失败（{type(e).__name__}）——该通路可能已被移除"
    return False, "无探针"


def _probe_local(sid: str) -> tuple[bool, str]:
    if sid == "kb_assets":
        root = Path(__file__).resolve().parent.parent
        for p in ("data/chroma", "data/kb", "chroma_db"):
            cand = root / p
            if cand.exists():
                return True, f"本地库存在（{p}）"
        return False, "未找到本地向量库目录（可能是首次运行尚未建库）"
    return False, "无探针"


def _probe_http(p: Probe) -> tuple[bool, str, int, int]:
    """HTTP 探针 → (ok, detail, http_code, 耗时ms)。失败一律降级为 (False, 原因, code, ms)"""
    # **合规停用的源，健康探针也不许敲**（2026-09-15 外部审计）。
    # 病案：主链路已对 SEC 做了 fail-closed（未配 `SEC_UA_CONTACT` 就不发任何请求），
    # 但巡检测的是**探针**，而探针走通用 `authority_fetch._get`（不是 `_sec_get`）
    # → 它照样拿一个**不含联系方式**的 UA 去敲 www.sec.gov。
    # 后果是两个互相矛盾的真相同时成立：`data/source_health.json` 里
    # sec_edgar `ok=true / HTTP 200`，而**主通道根本没启用**。
    # 一块显示"✅ 活着"的健康面板，配一句"该通道已停用"——**这块面板本身就成了假信息**。
    # 判据复用 `source_registry.runtime_block_reason`：一处定义，两处使用。
    try:
        from core.source_registry import SOURCES, runtime_block_reason, skip_reason_label

        _spec = next((s for s in SOURCES if s.id == p.sid), None)
        if _spec is not None:
            _why = runtime_block_reason(_spec)
            if _why:
                # **尾句必须按理由给，不许一律拼"按合规姿态主动不访问"**（2026-09-17）。
                # 原来这一行是无条件拼上去的——于是 SEC 那条（原因是**本机没配联系方式**）
                # 在面板上被读成"我们出于合规姿态主动不访问"：
                # **拿合规立场给配置缺口背书**。理由分类见
                # `core/source_registry.SKIP_REASONS`（一处定义）。
                _marked = f"**未探测**（{_why}）"
                _code = _skip_kind(_marked)
                _tail = skip_reason_label(_code) or "原因未分类（请补 SKIP_REASONS）"
                return False, f"{_marked}——{_tail}", 0, 0
    except Exception:
        pass
    try:
        from core.authority_fetch import _get
    except Exception as e:  # pragma: no cover
        return False, f"取数层不可用（{type(e).__name__}）", 0, 0
    t0 = time.time()
    try:
        r = _get(p.url, timeout=p.budget_s)
    except Exception as e:
        return False, f"请求异常（{type(e).__name__}）", 0, int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    code = int(getattr(r, "status_code", 0) or 0)
    if code in (403, 412):
        return False, f"被站点拒绝（HTTP {code}）——按红线停手，不重试不绕", code, ms
    if code != 200:
        return False, f"HTTP {code}", code, ms
    if p.expect:
        import re

        if not re.search(p.expect, r.text or ""):
            return False, "页面 200 但**未见预期内容**（疑似改版/迁移页）", code, ms
    return True, f"HTTP 200 · {ms}ms", code, ms


# 「未探测」理由的识别表**已搬到** `core/source_registry.py`（一处定义，两处使用）。
# 本模块与 `core/verification.py` 都是消费方；两张表各写一遍必然漂移，
# 而漂移的表现就是"两个数加起来对不上"——正是这次要修的病。
def _skip_kind(detail: Any) -> str:
    """委托给注册表的分类器（保留本名以免调用点散落）"""
    from core.source_registry import classify_skip_reason

    return classify_skip_reason(detail)


def _skip_kind_label(code: str) -> str:
    from core.source_registry import skip_reason_label

    return skip_reason_label(code)


def _run_one(p: Probe) -> dict[str, Any]:
    at = int(time.time())
    if p.kind in ("api_key", "local", "dynamic"):
        if p.kind == "api_key":
            ok, detail = _probe_api_key(p.sid)
        elif p.kind == "local":
            ok, detail = _probe_local(p.sid)
        else:
            ok, detail = _probe_dynamic(p.sid)
        return {"sid": p.sid, "name": p.name, "layer": p.layer, "ok": ok,
                "code": 0, "ms": 0, "detail": detail, "at": at}
    ok, detail, code, ms = _probe_http(p)
    return {"sid": p.sid, "name": p.name, "layer": p.layer, "ok": ok,
            "code": code, "ms": ms, "detail": detail, "at": at,
            # **第三态**（2026-09-15）：`ok=False` 至少有两种截然不同的原因——
            # "敲了、坏了" 与 "按合规姿态没去敲"。混成一个 fail，
            # 面板就会把"我们主动不访问"渲染成"这个源挂了"，
            # 而同一份报告里另有一句"该通道已停用"——**两个说法并存 = 面板自相矛盾**。
            "skipped": ("未探测" in str(detail or "")),
            "skip_kind": _skip_kind(detail)}


# ============================================================
# 地域源探针（从 region_stat 现取，避免两份清单漂移）
# ============================================================
def _region_probes() -> list[Probe]:
    out: list[Probe] = []
    try:
        from core.region_stat import REGION_SITES

        for s in REGION_SITES:
            out.append(
                Probe(
                    sid=s.sid,
                    name=s.bureau,
                    layer="L1",
                    kind="http",
                    url=s.pages[0] if s.pages else "",
                    # 判据用**该站自己的链接正则**：能匹配到文章链接才算真活，
                    # 只判 200 会把"栏目已改版、页面还在"的死源记成活的。
                    expect=s.link_re,
                    note=s.note,
                )
            )
    except Exception as e:  # pragma: no cover
        logger.warning("地域源探针装载失败: %s", e)
    return out


def all_probes(include_region: bool = True) -> list[Probe]:
    ps = list(PROBES)
    if include_region:
        ps.extend(_region_probes())
    return ps


# ============================================================
# 巡检
# ============================================================
def patrol(include_region: bool = True, budget_s: float = 150.0, trigger: str = "manual") -> dict[str, Any]:
    """逐个探活 → 写台账。**同一时刻只允许一次**（并发调用直接返回现状）。

    ## 离线闸门（2026-09-16 补 · 第五次复发病案的第九处）

    此前没有闸门，`_gate_probe.py` 实测确认离线模式下巡检台账仍被改写。

    这一处与其它写点不同：它拦的不只是**写**，还有**出网**。
    巡检会对每个探针发真实请求，而 `app.py` 启动时就会 `_kick_source_patrol()`——
    于是"离线模式下启动服务"也会真出网，这与本仓库的红线（`AUTHORITY_FETCH_ENABLED`
    总开关、遇 403 立刻停手）在意图上冲突。所以离线时**整段跳过**，连探测都不做。
    """
    if safe_io.is_offline():
        logger.info("离线模式：跳过数据源巡检（既不出网也不写台账）")
        return {"at": int(time.time()), "skipped": "YANCEC_OFFLINE=1",
                "trigger": trigger, "results": [], "summary": {"ok": 0, "fail": 0, "total": 0}}
    if not _PATROL_LOCK.acquire(blocking=False):
        logger.info("巡检已在进行中，跳过本次")
        return ledger()
    try:
        try:
            from core.authority_fetch import enabled

            if not enabled():
                return {"at": int(time.time()), "skipped": "AUTHORITY_FETCH_ENABLED=0",
                        "results": [], "summary": {"ok": 0, "fail": 0, "total": 0}}
        except Exception:
            pass
        ps = all_probes(include_region)
        results: list[dict[str, Any]] = []
        t0 = time.time()
        for p in ps:
            if time.time() - t0 > budget_s:
                # ⚠️ **"没来得及探"不是"按合规姿态不访问"**（2026-09-16 全量审计）。
                # 病案：这里和"合规停用"共用同一个 `skipped: True`，而三态渲染（本文件第 226 行、
                # 以及前端 `reg-health`）把 `skipped` 一律读成「未探测（**按合规姿态主动不访问**）」。
                # 于是一次**预算用尽**（纯粹是我们没跑完）在界面与报告附录里
                # 被说成了"我们出于合规姿态主动没去敲"——**把资源不足叙述成了合规立场**。
                # 合规姿态是我们对外讲的东西，不能拿它给"没跑完"背书。
                # 现在给两种"未探测"各自的理由：`skip_kind` 区分，文案各说各的。
                results.append({"sid": p.sid, "name": p.name, "layer": p.layer, "ok": False,
                                "code": 0, "ms": 0,
                                "detail": "本轮巡检预算用尽，未探测（这是**没跑完**，不是合规停用）",
                                "skipped": True, "skip_kind": "budget", "at": int(time.time())})
                continue
            try:
                results.append(_run_one(p))
            except Exception as e:
                results.append({"sid": p.sid, "name": p.name, "layer": p.layer, "ok": False,
                                "code": 0, "ms": 0, "detail": f"探针异常（{type(e).__name__}）",
                                "at": int(time.time())})
        ok_n = sum(1 for r in results if r.get("ok"))
        skip_n = sum(1 for r in results if r.get("skipped"))
        # 「未探测」按**理由**分开计数（见 `_skip_kind` 的病案）。
        # 只报一个总数时，"我们没跑完"会被读者自动读成"我们主动不碰"。
        _kinds = ("compliance", "config", "switch", "budget", "unclassified")
        _by_kind = {k: sum(1 for r in results if r.get("skip_kind") == k) for k in _kinds}
        # **不变式**：五个子计数之和必须等于 `skipped`。少了就是有理由没被分类——
        # 上一版正是这么静默漏掉 SEC 的（盘上实测 skipped=1 而两个子计数都是 0，
        # 两个数并排印在面板上）。宁可让 unclassified 那个桶难看，也不许对不上。
        _sum_kinds = sum(_by_kind.values())
        if _sum_kinds != skip_n:
            logger.warning(
                "巡检分类对不上：skipped=%d 而五个理由之和=%d（差 %d 条）——"
                "有新的未探测理由没进 _SKIP_MARKERS，请补表",
                skip_n, _sum_kinds, skip_n - _sum_kinds,
            )
        doc = {
            "at": int(time.time()),
            "trigger": trigger,
            "elapsed_s": round(time.time() - t0, 1),
            "results": results,
            # 三桶：可用 / **未探测** / 失败。未探测再按**理由**拆开。
            # 只报"可用/失败"会把未探测算进失败，把"我们没敲门"说成"门坏了"；
            # 而把"预算用尽"混进"合规停用"，是把"没跑完"说成"主动不碰"。
            "summary": {"ok": ok_n, "skipped": skip_n,
                        "skipped_compliance": _by_kind["compliance"],
                        "skipped_budget": _by_kind["budget"],
                        # 下面三个是 2026-09-17 补的：它们原先**没有名字**，
                        # 没有名字就没人能核对，也就没人发现加起来对不上。
                        "skipped_config": _by_kind["config"],
                        "skipped_switch": _by_kind["switch"],
                        "skipped_unclassified": _by_kind["unclassified"],
                        "skipped_kinds_sum": _sum_kinds,
                        "fail": len(results) - ok_n - skip_n, "total": len(results)},
        }
        try:
            safe_io.makedirs(_LEDGER_PATH.parent)
            tmp = _LEDGER_PATH.with_suffix(".tmp")
            safe_io.write_text(tmp, json.dumps(doc, ensure_ascii=False, indent=1))
            safe_io.replace(tmp, _LEDGER_PATH)
        except Exception as e:
            logger.warning("巡检台账写入失败: %s", e)
        logger.info("数据源巡检完成：%d/%d 可用（%.1fs）", ok_n, len(results), doc["elapsed_s"])
        return doc
    finally:
        _PATROL_LOCK.release()


def patrol_async(trigger: str = "startup", min_interval_s: int = _STALE_SECONDS) -> bool:
    """后台巡检（不阻塞启动）。距上次巡检不足 `min_interval_s` 则跳过。"""
    led = ledger()
    if led.get("at") and (time.time() - int(led["at"])) < min_interval_s:
        return False
    t = threading.Thread(target=lambda: patrol(trigger=trigger), name="yancec-source-patrol", daemon=True)
    t.start()
    return True


def ledger() -> dict[str, Any]:
    """读台账（不存在返回空壳，**不触发巡检**）"""
    try:
        if _LEDGER_PATH.exists():
            return json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("巡检台账读取失败: %s", e)
    return {"at": 0, "results": [], "summary": {"ok": 0, "fail": 0, "total": 0}}


def is_stale(max_age_s: int = _STALE_SECONDS) -> bool:
    at = int(ledger().get("at") or 0)
    return (not at) or (time.time() - at) > max_age_s


def live_sources() -> dict[str, dict[str, Any]]:
    """{sid: 探测结果}——只含**台账里 ok=True** 的源（"此刻可用"的唯一依据）"""
    return {r["sid"]: r for r in (ledger().get("results") or []) if r.get("ok")}


def status_line() -> str:
    """一行健康摘要（进 /health 与报告数据说明）"""
    led = ledger()
    s = led.get("summary") or {}
    at = int(led.get("at") or 0)
    if not at:
        return "数据源巡检：**尚未巡检**（接口 /api/sources/health?probe=1 可现场巡检）"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
    age_h = (time.time() - at) / 3600.0
    stale = "（**已过期**，建议重新巡检）" if age_h > 24 else ""
    return (
        f"数据源巡检：{when} 实测 **{s.get('ok', 0)}/{s.get('total', 0)}** 个源可用{stale}"
    )


# ============================================================
# 本次调用台账（trace）——一次调研里**真的发了**哪些源
# ============================================================
def new_trace() -> list[dict[str, Any]]:
    return []


def record(
    trace: list[dict[str, Any]] | None,
    src: str,
    name: str,
    items: Any,
    note: str = "",
    layer: str = "",
    kind: str = "",
) -> None:
    """记一条"本次调用"。`items` 为取回条目（list）或 None。

    `layer` 用于那些**不在注册表里**的源（如 16 个地方统计局）——
    它们有权威层级但没有 registry id，靠调用方显式带上来。

    `kind`：**当"取回条目数"不足以描述这次调用的结局时**用它。
    唯一的取值 `text_only` = 通道**答了但没带回可点 URL**（如千问 enable_search
    经常"有正文、无 search_info"）。不标它的话，渲染层会按 n=0 打成「⚠️ 空返回」，
    而同一行的备注写着"千问已联网作答"——**同一行自己打自己脸**，
    正是本模块注释里点名的那类病案（"备注写计划跳过、标记却打空返回"）。
    **默认空串 = 沿用原判据，对既有行零影响。**

    **同时记下条目 URL**：一次调研里同一条目会被多个子任务重复取到（进程内缓存命中），
    若只累加每次的条数，台账会把"3 条不同条目被取了 10 次"报成"30 条"——
    这正是我们一直在批评的**数字虚高**，不能出在自己身上。
    有 URL 就按 URL 去重计数，没有就退回"单次最大值"。
    """
    if trace is None:
        return
    try:
        n = len(items) if isinstance(items, (list, tuple)) else (1 if items else 0)
        urls: list[str] = []
        for it in items or ():
            if isinstance(it, dict):
                u = str(it.get("url") or "").strip()
                if u and u not in urls:
                    urls.append(u)
        trace.append(
            {"src": src, "name": name, "n": n, "ok": n > 0, "note": note,
             "layer": layer, "urls": urls[:8], "kind": kind}
        )
    except Exception:
        pass


def _is_skip_note(note: Any) -> bool:
    """这条**备注**是不是"主动不查"的措辞。

    三种"主动不查"都要认：①工具自主选择没挑它；②**路由判定这一层本就不适用**
    （如 L5 消费者声音类子任务不用官方统计回答）；③**通道停用**
    （SEC 未配 `SEC_UA_CONTACT` → fail-closed）。三者与"查了没查到"含义相反，
    混进"返回空"会让读者得出"这个源坏了"的错误结论。

    病案（2026-09-16）：③此前**不在**这里的词表里。而调用方 `core/authority_fetch`
    为它写的备注是「通道停用（…）——本次**未发起任何请求**」，措辞一个词都对不上 →
    被当成真实调用计入 `calls`。一次调研有十几个子任务，于是台账把它记成
    **「SEC EDGAR：调用 13 次、有返回 0 次」**——而同一行的说明明写着"未发起任何请求"。
    **一句话里前后互相否定**，而且这条假经验还会被 `exp_memory` 写进生产经验台账
    （沉淀成"SEC 这个源不行"，喂回下一次调研的上下文）。
    """
    _note = str(note or "")
    return (
        "未选中" in _note
        or "路由跳过" in _note
        or "通道停用" in _note
        or "未发起任何请求" in _note
    )


def _skipped(row: dict[str, Any]) -> bool:
    """该源族是**被工具计划主动跳过**的，而不是"打了但返回空"。

    两者在台账里都是 0 条，但含义完全相反：一个是"我们判断这次用不上它"，
    另一个是"它没给我们东西"。混为一谈会让读者得出"这个源坏了"的错误结论。

    **判据以逐调用计数（`planned_skip`）为准，不以备注措辞为准。**
    病案（2026-09-15 深访实跑）：同一个源族在不同子任务里**既被跳过又真跑过**——
    国家统计局 9 次调用、取回 3 条，而合并行的 note 是**最后一次**子任务留下的
    "工具计划未选中"，因为 note 是"后一条覆盖前一条"。于是：
      · 摘要行印成「涉及 9 个源，其中向 **0** 个实际发起了请求（合计 **30** 次调用，
        其中 **4** 个有返回）」——**一句话里前后互相否定**，而这句是印给复核者看的；
      · 表内 4 行出现「取回 3 条 / 9 次调用 / 说明：本次跳过」这种同row自相矛盾。
    判据只有一处（逐调用计数），备注只是显示文本——「同一动作两处实现」正是本项目
    最常复发的一类缺陷，这里收口到 `planned_skip`。
    """
    r = row or {}
    if "planned_skip" in r:
        return bool(r["planned_skip"])
    # 兼容单条 trace（自测/旧数据）：没有逐调用计数时退回备注措辞
    return _is_skip_note(r.get("note"))


def summarize_trace(trace: list[dict[str, Any]] | None) -> dict[str, Any]:
    """本次调用汇总：命中源数 / 回空源数 / **去重后**总条数（同名源多次调用合并）

    `calls` 是调用次数（**含缓存命中**），`n` 是**去重后的不同条目数**。
    两者分开看才有信息量：「3 条 / 20 次调用」说明缓存生效、没在重复打源站。
    """
    agg: dict[str, dict[str, Any]] = {}
    for c in trace or []:
        k = str(c.get("src") or c.get("name") or "?")
        cur = agg.setdefault(
            k,
            {"src": k, "name": c.get("name", ""), "calls": 0, "note": "",
             "layer": c.get("layer", ""), "kind": c.get("kind", ""), "_urls": set(), "_nmax": 0,
             "_skip_hits": 0, "_skip_note": ""},
        )
        # **计划跳过不算"调用"**：跳过意味着一个请求都没发。
        # 若把它计进"合计 N 次调用"，台账就在最该精确的地方虚高——
        # 而这张表的全部意义是"这次报告真的向哪些源发了请求"。
        if _is_skip_note(c.get("note")):
            cur["_skip_hits"] += 1
            if not cur["_skip_note"]:
                cur["_skip_note"] = str(c.get("note") or "")
        else:
            cur["calls"] += 1
            # **备注只认非跳过那一次**：跳过备注是"这一次子任务没查它"，
            # 不是"这个源整个没用过"。让它覆盖掉真跑过的记录，就会印出
            # 「3 条 / 9 次调用 / 说明：本次跳过」这种同row打架的行。
            if c.get("note"):
                cur["note"] = c["note"]
        cur["_nmax"] = max(cur["_nmax"], int(c.get("n") or 0))
        try:
            cur["_urls"].update(c.get("urls") or [])
        except Exception:
            pass
        if c.get("layer") and not cur.get("layer"):
            cur["layer"] = c["layer"]
        # 与 note 同源：**只认非跳过那一次的 kind**，理由同上面的备注
        # （跳过那次的 kind 描述的是"这一次没查它"，不是这个源族的整体结局）。
        if c.get("kind") and not _is_skip_note(c.get("note")):
            cur["kind"] = c["kind"]
    rows: list[dict[str, Any]] = []
    for cur in agg.values():
        urls = cur.pop("_urls")
        nmax = cur.pop("_nmax")
        _skip_hits = int(cur.pop("_skip_hits", 0) or 0)
        _skip_note = str(cur.pop("_skip_note", "") or "")
        # **只有一次都没真跑过的源族才算"计划跳过"**（判据 = 逐调用计数，不是备注措辞）。
        # 混合源族（既跳过又跑过）保留真跑那次的备注。
        cur["planned_skip"] = bool(_skip_hits > 0 and cur["calls"] == 0)
        if not cur["calls"] and not cur["note"]:
            cur["note"] = _skip_note
        # 有 URL → 按 URL 去重（最诚实）；一个 URL 都没有 → 退回单次最大值（**不累加**）
        cur["n"] = len(urls) if urls else nmax
        cur["deduped"] = bool(urls)
        rows.append(cur)
    rows.sort(key=lambda x: -x["n"])
    # **"计划跳过"必须与"回空"分开计**（2026-09-14 实跑）：
    # 工具自主选择会主动不打某些源族，那与"打了但没返回"是两件事——
    # 混在一起，摘要行会写"其中 N 个返回空"，而其中一部分**根本没发请求**。
    _skip_n = sum(1 for r in rows if _skipped(r))
    # **"只回了文本、没回可点源"是第三种结局**（2026-09-18 补）：
    # 千问 enable_search 经常"答了但不给 search_info"（日志原话「千问0来源/0URL + Bing3条」）。
    # 不单列它，就会同时犯两个错：① 摘要行把它算进"返回空"——可它明明答了；
    # ② 交付台账打「⚠️ 空返回」而备注写着"千问已联网作答"——**同一行自己打自己脸**。
    _text_only = sum(1 for r in rows if r.get("kind") == "text_only")
    return {
        "rows": rows,
        # 实际发起过请求的源数 = 总行数 − 纯跳过的行数
        "contacted": len(rows) - _skip_n,
        # "有返回"= 通道确实回应了（含只回文本的），**不是**"有可点 URL"
        "hit": sum(1 for r in rows if r["n"] > 0 or r.get("kind") == "text_only"),
        "empty": sum(1 for r in rows if r["n"] == 0 and not _skipped(r) and r.get("kind") != "text_only"),
        "text_only": _text_only,
        "skipped": _skip_n,
        "items": sum(r["n"] for r in rows),
        "calls": sum(r["calls"] for r in rows),
    }


def render_trace_appendix(trace: list[dict[str, Any]] | None) -> str:
    """本次调用台账 → 报告附录里的一段表（**取不到就一个字不加**）"""
    s = summarize_trace(trace)
    if not s["rows"]:
        return ""
    lines = [
        "\n\n---\n\n# 🛰️ 本次数据源调用台账",
        f"本次调研涉及 **{len(s['rows'])}** 个源，其中向 **{s.get('contacted', len(s['rows']))}** 个"
        f"实际发起了请求（合计 {s.get('calls', 0)} 次调用，"
        f"其中 **{s['hit']}** 个有返回、**{s['empty']}** 个返回空、"
        f"**{int(s.get('skipped') or 0)}** 个被工具计划主动跳过（跳过的源在下方单列写明，不计入「返回空」），"
        f"取回 **{s['items']}** 条**去重后**的公开条目。"
        "列在这里是为了让「用了什么源」可核对——**回空的源也照实列**，不假装个个都有产出：",
        "",
        "| 数据源 | 层 | 取回（去重） | 调用次数 | 说明 |",
        "| :--- | :--- | ---: | ---: | :--- |",
    ]
    for r in s["rows"]:
        lay = r.get("layer") or ""
        if not lay:
            try:
                from core.source_registry import layer_of_source

                lay = layer_of_source(r["src"]) or ""
            except Exception:
                pass
        # 标记列必须与备注列一致：备注写"计划跳过"、标记却打"空返回"，是自己打自己脸——
        # 读者只会看标记，然后得出"这个源坏了"的结论，而它根本没被调用过。
        if r["n"]:
            mark = f"{r['n']} 条"
        elif r.get("kind") == "text_only":
            # 答了，但没给可点源。**不许打「空返回」**——那与同一行的备注自相矛盾。
            mark = "文本（无可点源）"
        elif _skipped(r):
            mark = "— 未调用（计划跳过）"
        else:
            mark = "⚠️ 空返回"
        lines.append(f"| {r['name'] or r['src']} | {lay or '—'} | {mark} | {r['calls']} | {(r['note'] or '')[:40]} |")
    lines.append("")
    lines.append(
        "> 条数是**按条目 URL 去重**后的数字：同一条目被多个子任务重复取到时只计一次"
        "（进程内缓存命中），所以「3 条 / 20 次调用」不是取少了，而是**没有重复打源站**。"
        "「空返回」不等于源有问题：可能是**本次问题与该源无关**（关键词未触发，根本没发请求的源不会出现在上表），"
        "也可能是**该源此刻不可用**。要区分二者，看 `GET /api/sources/health` 的健康台账——"
        "那是探活结果，与本表（本次调用）是两件事。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "数据源健康巡检：逐个探活并落盘台账（data/source_health.json），带最后一次成功时间。"
        "判据不只看 HTTP 200，还要求**页面仍有预期内容**（政府站改版后常返回 200 的迁移页，只看状态码会把死源记成活的）。"
        "另有「本次调用台账」：一次调研实际向哪些源发了请求、各回几条、哪些回空——"
        "报告里写的是「本次实际调用 X 个源、Y 个有返回」，而不是「我们支持 15 个源」。"
    )
