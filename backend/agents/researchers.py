"""子 Agent：行业扫描 / 竞争格局 / 用户洞察 / 数据统计

每个子 Agent 负责一个调研维度：
- 接收子任务（含检索词）
- 调用 MCP 工具检索真实数据（工具层）
- 整合成带引用的模块报告
- 产出证据（带信源 L1-L5）

当前骨架：建立数据流与证据产出，MCP 检索接入后填真实数据。
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# 来源质量门（core.source_quality）的拒绝记录：{subtask_id: [(url, 原因)]}
# 数据源变少必须能解释——否则用户只看到"来源没了"，分不清是**被拦了**还是**根本没搜到**，
# 而这正是一次真实事故的现场：检索层整体失效（解释器用错导致 mcp 被遮蔽）时，
# 报告看起来"只是这次没搜到东西"，没有一句话说明。
_REJECT_LOG: dict[str, list[tuple[str, str]]] = {}
from typing import Any

from pydantic import BaseModel, Field, model_validator

# 研报/券商/研究机构域名：用于"研报语料库"增强——抓取研报页作为独立来源
_REPORT_DOMAINS = (
    "eastmoney.com", "iresearch.com.cn", "leadleo.com", "qianzhan.com", "fxbaogao.com",
    "hibor", "stcn.com", "cnstock", "report", ".pdf", "cjrb", "sina.com",
    # 主流财经/垂直媒体与数据平台（行业文章抓取做独立研报源；星巴克式题缺第三方源的主因之一）
    "finance.sina.com.cn", "sohu.com", "jiemian.com", "linkshop.com.cn",
    "yicai.com", "36kr.com", "iyiou.com", "ebrun.com", "xueqiu.com", "cyzone.cn",
)

from core.facts_kb import fact_label, get_fact, population_keys
from core.models import Evidence, Source, SourceLevel, SubTask
from core.verification import categorize_tier as _categorize_tier
from core.llm import get_main_llm, has_main_llm
from core.source_util import canonical_key, registrable_root


# ============================================================
# 检索：MCP 工具调用（工具层）
# ============================================================
def _title_relevant(title: str, query: str, subject: str = "", url: str = "") -> bool:
    """标题↔检索词相关性门（主题词重叠≥1 **或** 实词重叠≥2 才放行）。

    实证教训：
    - Bing 对冷门检索词会整批返回字典SEO站，且不同查询返回同一批链接；
    - 2026-09-06 再实证：维度词"门槛"被 Bing 按本义(门框)理解 → 返回
      "古人为何要在门口装门槛"类跑题文——旧规则只要 1 个二字词重叠(门槛)就放行是漏洞。

    规则（防"1词重叠放行"的同义词/同形词陷阱）：
    - gov/edu 官方域：标题含任一查询实词即放行（政策库/法规原文标题常不带行业词，勿误杀）；
    - 有主题词(subject=原问题的实体/方向) 且标题含任一主题词 → 放行；
    - 否则标题与查询的**非停用实词**重叠 ≥2 才放行。
    中文用 2-gram 重叠；过滤高频通用词（市场/行业/分析/如何/什么…）。
    """
    if not title or not query:
        return False
    if not re.search(r"[一-鿿]", title or ""):
        return False
    _CJK = re.compile(r"[一-鿿]+")
    _STOP = {
        "市场", "行业", "分析", "研究", "报告", "数据", "如何", "什么", "为什么",
        "怎么", "多大", "多少", "是否", "最新", "情况", "现状", "趋势", "调研",
        "细分", "热销", "主要", "玩家", "格局", "规模", "增长", "影响", "关系",
        "该不该", "能不能", "怎么", "做不", "好不", "增长", "价格", "政策",
    }

    def grams(t: str):
        out = set()
        for m in _CJK.findall(t or ""):
            for i in range(len(m) - 1):
                g = m[i : i + 2]
                if g not in _STOP:
                    out.add(g)
        return out

    g_t = grams(title)
    g_q = grams(query)
    if not g_t:
        return False
    if url:
        try:
            from urllib.parse import urlparse as _up

            _h = (_up(url).netloc or "").lower()
        except Exception:
            _h = ""
        if _h.endswith("gov.cn") or ".gov" in _h or _h.endswith("edu.cn"):
            return bool(g_t & g_q)  # 官方域：≥1 实词重叠即放行，勿误杀法规/政策原文
    if subject and (g_t & grams(subject)):
        return True  # 主题词(品牌/行业/方向)命中 → 真正相关
    return len(g_t & g_q) >= 2  # 仅靠维度词重叠必须 ≥2，防"门槛"门框文这类同形词混入


def _needs_adversarial(subtask: SubTask) -> bool:
    """决策/风险/可行性型维度 → 需要反证检索（主动找反方证据，防乐观偏差）。
    深入轮(_deep)不再重复反证（省时省钱，反证结论已在基础轮给出）。
    """
    # **判据必须与 `graph._final_subtask_ids` 同一把尺**（2026-09-18 实跑发现）：
    # 那里认"id 带 `_deep` **或** 描述以 `深入『X』` 开头"两种写法，
    # 这里原先只认 id 后缀。规则模板兜底的计划 id 确实带 `_deep`，所以看不出来；
    # 一旦计划改由**模型自主生成**（`plan_source=llm`，2026-09-18 打通），
    # 子任务 id 是模型自己排的 `st_0..st_13`，**不带 `_deep`** →
    # 每个深入轮都会把基础轮已经做过的反证检索**再做一遍**。
    # 本改动对历史报告**零影响**（兜底路径两种判据都命中），只影响新打通的 LLM 路径。
    if getattr(subtask, "id", "").endswith("_deep"):
        return False
    if re.search(r"深入\s*[『「]", subtask.description or ""):
        return False
    d = (subtask.description or "") + " " + " ".join(subtask.search_queries or [])
    return any(k in d for k in ("决策", "风险", "壁垒", "可行性", "单位经济", "趋势", "进入", "该不该"))


def _build_adversarial_query(q: str) -> str:
    """把主检索词改造成反证检索词：原主题 + 风险/失败/反方（主动找反证，不只看利好）"""
    return f"{q}，风险 失败案例 反方观点 不利因素 可行性存疑"


# 维度级**权威配额**：每个子任务的证据里期望至少有 N 条来自 L1(政府/官方)/L2(机构/研报)。
# 达不到不阻断，但要**记警告**——来源结构是比赛评审维度，缺口必须看得见，不许静默达标。
# 可用环境变量 AUTH_QUOTA_PER_DIM 覆盖（置 0 = 只记账不告警）。
def _auth_quota() -> int:
    try:
        return max(0, int(os.environ.get("AUTH_QUOTA_PER_DIM", "2")))
    except Exception:
        return 2


def _only_tools_for(context: dict | None, subtask: Any) -> list[str] | None:
    """**本子任务自己的**工具选择名单（None = 未选择 → 全打，fail-open）。

    2026-09-18 第十跑病案（外部审计第 ⑧ 对，真因，由我方读代码定位）：
    `select_tools` 的入参是**当前子任务**（描述 + 检索词），算完却写进一个
    **全局共享槽位** `context["_tools"]`，而取证层读的就是那个槽位。
    子任务一交叉，A 读到的就可能是 B 的名单——于是同一份报告里：
      决策链写「st_5→sse_edc/**cninfo**/report_page/ccfa」（选它了），
      调用台账写「巨潮资讯 —— **未调用（计划跳过）**｜工具计划未选中」（没选它）。
    **两句都是真的**（各自记的是各自那一刻看到的东西），错的只是中间那个槽位。

    读法与写法成对（写法见 `_search_mcp` 里写 `_tools_by_subtask` 的那几行）：
      · `_tools_by_subtask` **在** → 只认本子任务那一格；**缺那一格就返回 None（全打）**，
        **绝不回退到共享槽位**——回退等于把这个 bug 原样留着。
      · `_tools_by_subtask` **不在**（旧 context / 离线单测）→ 退回旧行为，语义不变。
    """
    ctx = context or {}
    _m = ctx.get("_tools_by_subtask")
    if isinstance(_m, dict):
        _mine = _m.get(getattr(subtask, "id", "") or "")
        return (_mine or {}).get("tools")
    return (ctx.get("_tools") or {}).get("tools")


def _search_mcp(subtask: SubTask, context: dict | None = None) -> list[dict[str, Any]]:
    """数据源检索（资产飞轮 + 千问联网检索，多角度，并行提速）

    策略：
    0. 先查资产库：同行业历史调研命中 → 作为"历史视角"参考（省token）
    1. **并行联网检索**：每个检索词不同角度，ThreadPool 并发执行，
       避免串行等待（enable_search 单次 10-14s → 并发后多词一次完成）
    2. 会话内缓存：相同检索词不重复联网（演示提速关键）
    3. 全部失败 → 返回空，上层降级
    """
    import concurrent.futures

    from mcp.search import search_with_llm
    from core.rag import search_assets

    results: list[dict[str, Any]] = []
    query = subtask.search_queries[0] if subtask.search_queries else subtask.description
    # 多角度：用全部检索词各检索一次
    queries = subtask.search_queries or [subtask.description]

    # 0. 资产库：作为历史视角补充（最多1条，且只作参考，不阻塞实时检索）
    industry = _extract_industry(query)
    if industry:
        try:
            hits = search_assets(query, industry=industry, n_results=3)
            for hit in hits[:1]:
                if not isinstance(hit, dict):
                    continue  # 防御：资产库偶发 None/异常条目
                if hit.get("content"):
                    results.append(
                        {
                            "query": query,
                            "title": f"[历史资产] {hit.get('source_title') or hit.get('industry')}",
                            "url": hit.get("source_url") or "资产库",
                            "content": hit["content"],
                            "publisher": hit.get("source_title") or "历史调研",
                            "date": hit.get("created_at", ""),
                            "source_type": "asset",
                        }
                    )
        except Exception:
            pass  # 资产检索失败不阻塞

    # 1. 实时检索：每子任务联网1次（千问 enable_search 单次10-14s，并发会限流排队更慢）
    #    检索词 = 子任务描述(含行业+维度) + 核心词 + 澄清约束，避免裸关键词搜偏或"未指定行业"
    context_q = _build_context_query(subtask, context)
    rich = _cached_search_rich(context_q)
    # —— 通用检索通道**必须进调用台账**（2026-09-18 补，三份外部分析一致指向的洞）——
    # 此前台账只有权威源（`fetch_authority` 自己记账）与"主动跳过"两条来源，
    # **通用检索通道一次都不写**。后果：报告里 23 条引文（艾瑞/界面/36氪/每经…）
    # 与大部分证据**在台账里查无此行**——《本次数据源调用台账》列了 9 个源，
    # 一个检索通道都没有。而这一层是「可审计」最承重的砖：
    # 复核者问「艾瑞那 3 条是哪次调用来的」，答不上。
    #
    # 判据要分清三种，**不许混**（台账的全部意义就是分清它们）：
    #   ① 千问联网真答了 → 记通道 + 它带回的来源 URL；
    #   ② 千问失败、降级到 DeepSeek 内置知识 → **记降级**，且明写"无可点源"——
    #      那批正文里的 URL 是模型记忆里的，不是搜出来的，记成"检索命中"就是伪造背书；
    #   ③ 完全失败 → 记回空。
    try:
        from core.source_health import record as _rec_ch

        _tr = (context or {}).get("_health_trace")
        _urls = [s.get("url") for s in (rich or {}).get("sources") or [] if isinstance(s, dict) and s.get("url")]
        # ⚠️ **通道名由 `mcp/search.py` 写下来，不许在这里靠 `urls == []` 反推**
        # （2026-09-18 实跑踩到）：千问经常"答了但不给来源"（日志原话
        # 「千问0来源/0URL + Bing3条」），此时正文是**实时联网检索**结果，
        # 按 urls 为空判成"降级到 DeepSeek 内置知识"= **对一条真实检索的假指控**。
        _chan = str((rich or {}).get("channel") or "")
        # 备注长度受渲染层 40 字截断约束（台账表格列宽）——**写长了会被腰斩，
        # 留下半个 `**`**。所以这里一律短句，且把子任务缀在后面。
        _sid = getattr(subtask, "id", "")
        _suf = f"｜子任务 {_sid}" if _sid else ""
        if rich and (rich.get("content") or "").strip():
            if _chan == "deepseek":
                _rec_ch(
                    _tr, "qwen_search", "通义千问联网检索（enable_search）",
                    [],
                    note=f"降级：千问失败，正文为 DeepSeek 离线知识{_suf}",
                    layer="L4",
                )
            elif _urls:
                _rec_ch(
                    _tr, "qwen_search", "通义千问联网检索（enable_search）",
                    [{"url": u} for u in _urls],
                    note=f"通用检索通道（带 search_info 来源）{_suf}", layer="L4",
                )
            else:
                # **第三种结局**：答了，但没带可点来源。必须显式标 `text_only`——
                # 否则渲染层按"取回 0 条"打成「⚠️ 空返回」，与"千问已联网作答"自相矛盾。
                _rec_ch(
                    _tr, "qwen_search", "通义千问联网检索（enable_search）",
                    [],
                    note=f"千问联网作答，未带来源（可点 URL 由结构化通道补）{_suf}",
                    layer="L4", kind="text_only",
                )
        else:
            _rec_ch(
                _tr, "qwen_search", "通义千问联网检索（enable_search）", [],
                note=f"本次未返回可用内容（通道失败或无结果）{_suf}", layer="L4",
            )
    except Exception:
        pass
    if rich and rich.get("content") and len(rich["content"].strip()) > 50:
        content = rich["content"]
        # 关键：来源名 ↔ 官网URL 匹配。
        # 之前直接用 urls[0] → 搜索含"统计局"时 gov.cn 排前，导致"所有来源全是政府网站"（名实错配）。
        # 现在每个数据源匹配它自己对应的官网URL（红餐→红餐网、德勤→德勤官网、统计局→gov.cn）。
        src_list = rich.get("sources") or []
        # Bing URL 补全：千问 enable_search 常不返回 search_info → 数据源会"纯文字无网址"。
        # 恢复 Bing 补充，但过类型黑名单（游戏/视频/音乐/聚合站宁缺勿滥）+ 域名做站点名兜底。
        if not src_list or not any(isinstance(s, dict) and s.get("url") for s in src_list):
            try:
                from mcp.search import web_search_urls

                extra = web_search_urls(
                    context_q, n=5,
                    trace=(context or {}).get("_health_trace"),
                    subtask=getattr(subtask, "id", ""),
                )
                if extra:
                    logger.info("Bing原始返回%d条: %r", len(extra), [u.get("url", "")[:70] for u in extra[:6]])
                    # 相关性门：主题词重叠≥1 或 实词重叠≥2（杀"门槛"被按门框本义理解的跑题文）；
                    # gov/edu 官方域标题含任一查询实词即放行（法规原文标题常不带行业词，勿误杀）。
                    _subj = _subject_head((context or {}).get("raw_question", ""))
                    src_list = [
                        {"site_name": _domain_of(u.get("url", "")), "title": u.get("title", ""), "url": u.get("url", "")}
                        for u in extra
                        if _is_clean_url(u.get("url", ""))
                        and _title_relevant(u.get("title", ""), context_q, _subj, u.get("url", ""))
                    ]
                    if not src_list:
                        logger.warning("Bing结果无相关(字典/SEO/跑题): %r", [u.get("url", "")[:70] for u in extra[:6]])
                    else:
                        logger.info("Bing补真实URL: %d 条 for %r", len(src_list), context_q[:30])
            except Exception as e:
                logger.warning("Bing补URL失败: %s", e)
        # —— 机构名定向补源（框架级）：正文点到第三方机构(艾瑞/欧睿/IDC/协会…)，取它官方报告页进数据源。
        #   **硬闸：只收"机构名 → 该机构官网已知根域"命中的结果**——杜绝
        #   搜"美团研究院"→美的官网、搜"窄门餐眼"→汉字字典、搜"中心"→地方城市页 这类污染。
        try:
            from mcp.search import web_search_urls as _inst_search

            _INST_DOM = {
                "艾瑞": "iresearch.com.cn", "艾媒": "iimedia.cn", "易观": "analysys.cn",
                "欧睿": "euromonitor.com", "Euromonitor": "euromonitor.com",
                "IDC": "idc.com", "GfK": "gfk.com", "头豹": "leadleo.com", "前瞻": "qianzhan.com",
                "亿欧": "iyiou.com", "36氪": "36kr.com", "红餐": "hongcan.com",
                "中国连锁经营协会": "ccfa.org.cn", "连锁经营协会": "ccfa.org.cn",
                "商务部": "mofcom.gov.cn", "国家统计局": "stats.gov.cn", "统计局": "stats.gov.cn",
                "海关总署": "customs.gov.cn", "证监会": "csrc.gov.cn", "工信部": "miit.gov.cn",
                "新浪财经": "sina.com.cn", "东方财富": "eastmoney.com", "证券时报": "stcn.com",
            }

            def _inst_ok(name: str) -> str:
                """机构名 → 期望官网根域；无映射返回 ''（不补源，宁缺勿滥）"""
                for k, dom in _INST_DOM.items():
                    if k in name or name in k:
                        return dom
                return ""

            _used_hosts = {
                _domain_of(s.get("url", "")) for s in src_list
                if isinstance(s, dict) and s.get("url")
            }
            for _name in _content_institutions(content, limit=2):
                _want = _inst_ok(_name)
                if not _want:
                    continue
                if any(
                    _names_overlap(_name, (isinstance(s, dict) and (s.get("site_name") or "")))
                    for s in src_list
                ):
                    continue
                try:
                    _found = _inst_search(
                        f"{_name} 报告 白皮书",
                        n=3,
                        # 台账：这一次调用带回的正是"艾瑞/欧睿/IDC…"这类机构页——
                        # 报告《出处集中度》里"艾瑞咨询 3 条"就是从这里来的。
                        # 不记的话，复核者问"这 3 条哪次调用取的"在台账里查无此行。
                        trace=(context or {}).get("_health_trace"),
                        subtask=getattr(subtask, "id", ""),
                    )
                except Exception:
                    _found = []
                for _f in (_found or [])[:2]:
                    _u = (_f or {}).get("url", "")
                    if not _u or not _is_clean_url(_u):
                        continue
                    _h = (_domain_of(_u) or "").lower().removeprefix("www.")
                    _ok = _h == _want or _h.endswith("." + _want)
                    if _h in _used_hosts or not _ok:
                        continue
                    _used_hosts.add(_h)
                    # 插到最前：让"兜底取第一个干净URL"也不再落回品牌官网
                    src_list.insert(
                        0,
                        {
                            "site_name": ((_f.get("site_name") or _name))[:40],
                            "title": ((_f.get("title") or "")[:80]),
                            "url": _u,
                        },
                    )
                    logger.info("机构定向补源: %s → %s", _name, _u[:80])
                    break
        except Exception:
            pass  # 补源失败不阻断（仍走原 src_list）
        logger.info(
            "数据源URL: 千问%d来源/%dURL + Bing%d条 for %r",
            len(rich.get("sources") or []), len(rich.get("urls") or []), len(src_list), context_q[:30],
        )
        matched_url = _match_source_url(content, src_list, rich.get("urls") or [])
        # 名实配对：publisher 用 matched_url 对应项的 site_name（来源名=官网同源，杜绝错配）
        matched_site = ""
        for s in src_list:
            if isinstance(s, dict) and s.get("url") == matched_url and s.get("site_name"):
                matched_site = s["site_name"].strip()
                break
        src_urls = [
            s.get("url") for s in src_list
            if isinstance(s, dict) and s.get("url") and _is_clean_url(s.get("url"))
        ]
        results.append(
            {
                "query": context_q,
                # 标题取正文首句真结论（此前是检索词占位 → 引用面板/证据卡显示的是检索词，不是内容）
                "title": _claim_from_content(content, context_q) or context_q[:40],
                "url": matched_url,
                "urls": src_urls or ([matched_url] if matched_url else []),
                "sources": src_list,
                "content": content,
                "publisher": matched_site or "实时检索",
                "date": "实时",
                "source_type": "llm_search",
            }
        )
        # 研报语料库增强：抓取券商/研究机构的研报页作为独立"研报来源"证据。
        # 只抓研报类域名（非gov），报告能引用"XX券商研报"，且不造成"全是政府网站"。
        try:
            from mcp.search import fetch_page

            report_url = next(
                (
                    s.get("url")
                    for s in src_list
                    if isinstance(s, dict)
                    and (s.get("url") or "").startswith("http")
                    and not any(b in s["url"] for b in ("gov.cn", ".gov", "bing.com", "baidu.com"))
                    and any(d in s["url"] for d in _REPORT_DOMAINS)
                ),
                None,
            )
            if report_url and report_url not in [r.get("url") for r in results]:
                fetched = fetch_page(report_url)
                if fetched and fetched.get("content"):
                    results.append(
                        {
                            "query": context_q,
                            "title": f"[研报] {(fetched.get('title') or '')[:40] or report_url[:40]}",
                            "url": report_url,
                            "urls": [report_url],
                            "content": fetched["content"],
                            "publisher": (fetched.get("title") or "")[:30] or "券商研报",
                            "date": "抓取时间",
                            "source_type": "report_page",
                        }
                    )
                    logger.info("已抓取研报来源: %s", report_url)
        except Exception as e:
            logger.warning("研报抓取失败: %s", e)

    # 假设驱动·反证检索：决策/风险/可行性型维度，主动检索"反方/风险/失败"证据，
    # 防乐观偏差（只找利好数据导致结论偏乐观）。反证以独立"反方检索"来源进入报告与证据链。
    if _needs_adversarial(subtask):
        try:
            neg_q = _build_adversarial_query(context_q)
            neg_rich = _cached_search_rich(neg_q)
            if neg_rich and neg_rich.get("content") and len(neg_rich["content"].strip()) > 50:
                neg_src = neg_rich.get("sources") or []
                # 与主分支同款：search_info 缺真实源页时用 Bing 补真URL
                # （否则反证结果 urls 为空/自抠 → 反方检索整批不可点，用户曾见全部404）
                if not neg_src or not any(isinstance(s, dict) and s.get("url") for s in neg_src):
                    try:
                        from mcp.search import web_search_urls

                        _extra = web_search_urls(
                            neg_q, n=5,
                            trace=(context or {}).get("_health_trace"),
                            subtask=getattr(subtask, "id", ""),
                        )
                        if _extra:
                            _subj = _subject_head((context or {}).get("raw_question", ""))
                            neg_src = [
                                {"site_name": _domain_of(u.get("url", "")), "title": u.get("title", ""), "url": u.get("url", "")}
                                for u in _extra
                                if _is_clean_url(u.get("url", ""))
                                and _title_relevant(u.get("title", ""), neg_q, _subj, u.get("url", ""))
                            ]
                    except Exception:
                        neg_src = []
                neg_matched = _match_source_url(neg_rich["content"], neg_src, neg_rich.get("urls") or [])
                results.append(
                    {
                        "query": neg_q,
                        "title": f"[反证] {_claim_from_content(neg_rich['content'], context_q)}"[:42],
                        "url": neg_matched,
                        "urls": [s.get("url") for s in neg_src if isinstance(s, dict) and s.get("url")]
                        or ([neg_matched] if neg_matched else []),
                        "sources": neg_src,
                        "content": neg_rich["content"],
                        "publisher": "反方检索",
                        "date": "实时",
                        "source_type": "adversarial",
                    }
                )
                logger.info("反证检索已加入(真实来源%d条): %s", len(neg_src), neg_q[:34])
        except Exception as e:
            logger.warning("反证检索失败: %s", e)

    # 1.5 · 权威源直取（绕过通用搜索，直接向权威源公开发布页取数）：上市公司法定披露 / 统计局数据发布。
    # 只取公开内容、守 robots、不绕风控、per-host ≥1s；失败静默降级（AUTHORITY_FETCH_ENABLED=0 可一键关闭）。
    # 用**本子任务自己的检索词**去取（此前只传整题问句+子任务描述 → 统计局按同一串词反复匹配，
    # 维度不对口；现在统计局按维度关键词各自缓存命中，巨潮按公司候选跨维度复用）。
    # **问答→源硬路由**：先看该子任务的结论类型要求哪一层（公司数字→L2法定披露 / 宏观→L1官方统计）。
    #   要求 L1/L2 时权威源是**必选**（取不到要记缺口）；消费者声音类(L5)官方源本就不适用 → 跳过省请求。
    _route = (context or {}).get("_route")
    _need_auth = True
    if _route is not None and getattr(_route, "required_layer", "") in ("L5",):
        _need_auth = False
        # **主动不查也要留痕**（2026-09-15 修，来自一次外部对抗审计）。
        # L5（消费者声音类）不该用官方统计回答，所以整层跳过——这个设计是对的，
        # 但**不留痕**会让台账里"权威源一条都没有"，读者只会读成"这个源坏了/没取到"。
        # 「主动不查」与「查了没查到」是两件事，台账的全部意义就是分得清这两件事。
        try:
            from core.source_health import record as _rec

            _rec(
                (context or {}).get("_health_trace"),
                "authority_layer",
                "权威源层（法定披露/官方统计）",
                [],
                note="路由跳过：本子任务结论类型为 L5（消费者声音），官方统计与法定披露本就不适用——**主动未启用，非回空**",
                layer="L5",
            )
        except Exception:
            pass
    if _need_auth:
        try:
            from core.authority_fetch import fetch_authority, to_raw

            _au = to_raw(
                fetch_authority(
                    (context or {}).get("raw_question", "") or "",
                    subtask.description or "",
                    # 6 条：权威源是本系统**质量最高**的一类证据（法定披露 / 官方统计 / 国际组织），
                    # 给 3 条时"公司 + 市场"这类复合问题会被先到的源吃光预算，后面的源一条进不来。
                    # 展示层另有根域配额与媒体占比闸门收敛，这里放宽不会让报告变吵。
                    limit=6,
                    queries=list(queries),
                    # 本次调用台账（哪个源回了几条/哪些回空）——由 graph 建、跨子任务共享同一 list
                    trace=(context or {}).get("_health_trace"),
                    # 工具自主选择的名单（None = 未选择 → 全打，fail-open）
                    #
                    # ── 2026-09-18 第十跑病案（外部审计第 ⑧ 对）：**读的是不是本子任务的名单** ──
                    # 病案：决策链 9 次明写 `st_5→sse_edc/cninfo/...`，而调用台账把
                    # 巨潮/上证e互动/SEC/政府采购网 记成「**工具计划未选中**，本次跳过」、调用数 0。
                    # **两句话不能同时为真**，而真因是：`select_tools` 的入参是**当前子任务**
                    # （见下方 2503），算完却写进一个**全局共享槽位** `context["_tools"]`；
                    # 而取证层读的就是那个槽位。子任务之间一交叉，A 读到的可能是 B 的名单。
                    #
                    # **修法刻意做成"旁路"而不是改形状**：`context["_tools"]` 原样保留——
                    # 它还被两处消费（决策链的 `tool_plan`、以及 `_fulltest` 的一条锁），
                    # 改形状会同时动到那两处。这里只**新增一条按子任务 id 隔离的表**，
                    # 优先读本子任务自己的那一格。
                    #
                    # ⚠️ 本表存在、而本子任务**没有**自己的那一格 → 返回 None（全打，fail-open），
                    # **不回退到共享槽位**：回退等于把"这是别人的名单"这个 bug 原样留着。
                    only_tools=_only_tools_for(context, subtask),
                )
            )
            if _au:
                results = _au + results  # 前置：让"取第一个干净 URL"优先落权威源
                logger.info("权威源注入 %d 条（子任务 %s）", len(_au), getattr(subtask, "id", ""))
            # 维度级权威占比记账（诚实披露，不伪造）：本维度取回的证据里 L1/L2 占几条。
            # 不足 _auth_quota 时**只记警告不改写**——缺口要看得见，不许静默当"已达标"。
            try:
                from core.source_router import route_source as _route_src

                # 测**全量**可点来源。曾只测 results[:8]——而那 8 条恰好是刚前置进来的权威源，
                # 于是恒为 7/7、8/8，"占比 100%"是个假指标（拆包时才发现）。
                _hi = 0
                _tot = 0
                for _r in results[:30]:
                    _u = (_r or {}).get("url") or ""
                    if not _u:
                        continue
                    _tot += 1
                    _rt, _ = _route_src(_u, str((_r or {}).get("publisher") or ""))
                    if _rt <= 2:
                        _hi += 1
                if _tot:
                    logger.info("权威占比[%s]: L1/L2 %d/%d", getattr(subtask, "id", ""), _hi, _tot)
                    if _hi < _auth_quota():
                        logger.warning(
                            "权威不足[%s]: 仅 %d 条 L1/L2（配额 %d）——该维度结论请按'弱证据'口径表述",
                            getattr(subtask, "id", ""), _hi, _auth_quota(),
                        )
            except Exception:
                pass
        except Exception as e:
            logger.info("权威源注入跳过（降级不影响主流程）: %s", e)

    logger.info("检索[%s] 查询=%r → %d 条", subtask.id, context_q, len(results))
    return results


def _strip_methodology_prefix(desc: str) -> str:
    """去掉'调研『XX』（方法论：...）'前缀：维度名后第一个括号组整体删除（支持嵌套括号）

    修复：方法论标签含嵌套括号（如'市场规模测算（TAM/SAM/SOM）'）时，
    旧正则 `[^）]*` 在第一个右括号就停，导致剩余 body 全是方法论、把原始问题挤掉
    → 检索词丢失行业，千问回答'未指定行业'（P3 漂移的根因）。
    """
    m = re.search(r"[『「][^』」]+[』」]", desc)
    if m:
        rest = desc[m.end():].lstrip()
        if rest.startswith(("（", "(")):
            depth, end = 0, -1
            for i, ch in enumerate(rest):
                if ch in "（(":
                    depth += 1
                elif ch in "）)":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                rest = rest[end + 1:]
        return re.sub(r"^\s*[：:，,]?\s*", "", rest)
    return desc


_SUBJECT_DISCARD = (
    "该不该做", "应不应该", "能不能做", "能不能", "可不可以", "要不要", "是否应该",
    "是否该", "怎么样", "怎么才能", "怎么", "为什么", "多少", "多大", "如何", "能否",
    "值不值得", "值得吗", "该不该", "做不做得", "做不做", "为什么放缓", "门店增速为什么",
)


def _subject_head(rq: str) -> str:
    """原问题 → 精简主题头（≤16字）：截到第一个问号/句号前，剥掉话语性套话。

    病根实证：检索词把整句原问题（如'星巴克等咖啡品牌下沉三四线城市该不该做？
    下沉门店增速为什么放缓？'）原样塞到最前 → 5 个子任务检索词前 40 字完全相同，
    Bing/博查对每段返回**同一批网页** → 整篇报告只剩 3 个域名。
    这里只留'实体+方向'，检索重心交给 _dimension_lead（各维度自己的词）。
    """
    s = (rq or "").strip()
    for _ch in "？！?。；;":
        _i = s.find(_ch)
        if _i > 0:
            s = s[: _i]
            break
    for _w in _SUBJECT_DISCARD:
        s = s.replace(_w, "")
    s = re.sub(r"[，,、:：·…\s]+", "", s)
    if s.endswith("该不该做"):
        s = s[: -len("该不该做")]
    return s[:16] or (rq or "").strip()[:16]


def _dimension_lead(body: str, kw: str) -> str:
    """取本子任务**自己有区分度的**检索重心放最前（市场规模/主要玩家/消费者画像/
    政策 经济 社会 技术 影响…），让每个子任务的检索词真正不同——不是同一句话的
    不同尾巴，而是各维度自己的问题。kw 缺失时退化为从 body 剥掉主题后的差异词。

    同形词/泛词补锚：Bing 会把维度词"门槛"按门框本义理解、返回跑题文 → 补行业术语
    锚（竞争壁垒/加盟门槛）把检索拉回商业语义。
    """
    _AMBIG = {"门槛": "门槛 竞争壁垒 加盟门槛", "画像": "画像 消费者画像 用户画像"}
    lead = (kw or "").strip().strip("，,;；")
    if not lead:
        lead = (body or "").strip().strip("，,;；")[:24]
    else:
        lead = _AMBIG.get(kw.strip(), lead)
    return lead[:24]


def _build_context_query(subtask: SubTask, context: dict | None = None) -> str:
    """构造带行业+约束的检索词（检索重心前置 + 精简主题头 + 行业锚）

    铁律：
    1. **检索重心（维度词）放最前**——让不同维度检索不同批网页，来源才多元；
    2. 原问题只留精简主题头 ≤16 字，**绝不整句原样前置**（否则各维度检索同一批网页，
       全报告就剩那几个域名）；
    3. 行业/主体词必须出现（防'未指定行业'漂移）。
    """
    desc = subtask.description or ""
    body = _strip_methodology_prefix(desc)
    # 剥掉写作指令（【】块、"在上一轮基础分析之上"、①细分数据... 等），只留主题
    body = _strip_meta_instructions(body)
    kw = subtask.search_queries[0] if subtask.search_queries else ""
    if kw and kw not in body:
        body = f"{body}，{kw}".strip("，")
    rq = (context or {}).get("raw_question", "") or ""
    head = _subject_head(rq) if rq else (body[:16] or desc[:16])
    lead = _dimension_lead(body, kw)
    if head and lead and lead not in head:
        q = f"{lead} {head}".strip()
    else:
        q = head or lead or desc[:40]
    # 行业锚定：行业词必须在检索词里（qwen 无行业词会答'未指定行业'）
    ind = _extract_industry(body) or _extract_industry(rq)
    if ind and ind not in q:
        q = f"{q} {ind}行业".strip()
    # 澄清约束并入检索词（消灭 P3：跨境/对象/地域/口径传导到检索）
    for k in ("cross_border_scope", "scope", "objective", "geo"):
        v = (context or {}).get(k)
        if v and isinstance(v, str) and len(v) <= 40 and v not in q:
            q = f"{q}，{v}"
    # 时效锚：带当前年份，把检索重心推向近期（治"检索到的多是 2023/2024 旧文"）。
    # 只加年份不写死"必须最新"，避免把历史基准类（TAM/口径）检索搜窄。
    try:
        from datetime import date as _dt

        _yr = str(_dt.today().year)
        if _yr not in q:
            q = f"{q[:72].rstrip('，, ')} {_yr}"
    except Exception:
        pass
    return q[:80] or desc[:60]


def _strip_meta_instructions(text: str) -> str:
    """去掉检索词里的"写作指令"噪音：【...】块、'在上一轮基础分析之上'、①细分数据... 等

    深入子任务的描述同时承担"写作要求"和"检索主题"两个职责，
    检索词只要主题，写作要求由 _integrate 的 prompt 处理。
    """
    import re

    t = text or ""
    t = re.sub(r"【[^】]*】", "", t)  # 【写作要求：...】
    t = re.sub(r"[①②③④⑤⑥⑦].*?(?=[，。；]|$)", " ", t)  # ①细分市场... ②... 等指令项
    for ph in (
        "在上一轮基础分析之上", "只追加以下内容", "禁止重复基础结论", "禁止重复",
        "写作要求", "原始问题：", "若与基础结论冲突", "见上轮", "一手验证路径",
    ):
        t = t.replace(ph, "")
    t = t.replace("——", "，").replace("：", "，")
    t = re.sub(r"[，,；;\s]+", "，", t).strip("，,；;")
    return t


# 会话内检索缓存：同词不重复联网（提速 + 省token）
_search_cache: dict[str, str] = {}
_search_rich_cache: dict[str, dict[str, Any]] = {}


def _cached_search(query: str) -> str | None:
    """带缓存的检索：同一检索词本次会话只联网一次"""
    from mcp.search import search_with_llm

    if query in _search_cache:
        return _search_cache[query]
    result = search_with_llm(query)
    if result and len(result.strip()) > 50:
        _search_cache[query] = result
    return result


def _cached_search_rich(query: str) -> dict[str, Any] | None:
    """带缓存的富检索：同词只联网一次，返回 {"content", "urls"}（真实来源链接）"""
    from mcp.search import search_with_llm_rich

    if query in _search_rich_cache:
        return _search_rich_cache[query]
    result = search_with_llm_rich(query)
    if result and result.get("content") and len(result["content"].strip()) > 50:
        _search_rich_cache[query] = result
    elif result:
        logger.warning("检索结果过短被丢弃: %r", query)
    else:
        logger.warning("检索返回None（千问+DeepSeek均失败）: %r", query)
    return result


# 行业词表 —— **全项目唯一一份**（2026-09-15 外部审计收口）
# ============================================================
# 病案：这个函数此前只有 13 个行业词，而 `graph.py` 里另抄了一份**并带兜底**
# （"取提问前 4 个汉字"）——兜底把「2026年国内现制咖啡连锁…」取成了 **「市场目前」**，
# 于是 `industry_kb.json` 里长出了 `市场目前`/`万级电车`/`星巴克中` 这类垃圾键，
# 而检索侧用的正是这份 13 词白名单 → **沉下去的卡永远搜不到**。资产飞轮空转。
#
# 删掉兜底之后暴露出第二个问题：白名单太窄——四个真实演示题里三个认不出行业
# （"蜜雪冰城 vs 喜茶" 里没有"奶茶"二字，"星巴克中国" 里没有"咖啡"二字）。
# 所以这里补两层：
#   ① **品牌/别名 → 行业**：品牌名不等于行业名，但它唯一确定了行业；
#   ② 扩充行业词（低空经济/新能源车/光伏储能/医美/白酒…）。
#
# **扩充规则**：加词时只加"提问里真会出现、且指向唯一行业"的词。
# 宁可漏（返回空串，不沉淀）也不要错（沉淀到错误行业，下次检索串台）。
_INDUSTRY_WORDS: tuple[str, ...] = (
    # 餐饮饮品
    "咖啡", "奶茶", "茶饮", "新茶饮", "饮品", "餐饮", "烘焙", "预制菜", "火锅", "快餐", "零食",
    # 消费零售
    "美妆", "护肤", "服饰", "鞋服", "母婴", "宠物", "家居", "家电", "数码", "零售", "商超",
    # 服务
    "教育", "健身", "医美", "养老", "旅游", "酒店", "物流", "快递", "跨境电商", "跨境",
    # 制造与新兴
    "新能源车", "电动车", "汽车", "光伏", "储能", "锂电", "半导体", "机器人",
    "低空经济", "无人机", "卫星互联网", "生物医药", "医疗器械",
    # 其他
    "白酒", "乳制品", "饮料", "烟草", "农业", "建材", "化工", "钢铁",
)

# 品牌/别名 → 行业。**品牌名不等于行业名**，但提问里出现品牌时行业是确定的。
# （"星巴克中国的下沉还有空间吗" 里没有"咖啡"二字，但行业确定是咖啡。）
_INDUSTRY_ALIASES: tuple[tuple[str, str], ...] = (
    ("星巴克", "咖啡"), ("瑞幸", "咖啡"), ("库迪", "咖啡"), ("Tims", "咖啡"), ("Manner", "咖啡"),
    ("蜜雪冰城", "奶茶"), ("喜茶", "奶茶"), ("奈雪", "奶茶"), ("奈雪的茶", "奶茶"),
    ("霸王茶姬", "奶茶"), ("茶百道", "奶茶"), ("古茗", "奶茶"), ("沪上阿姨", "奶茶"),
    ("茶颜悦色", "奶茶"), ("书亦", "奶茶"),
    ("山姆", "零售"), ("盒马", "零售"), ("永辉", "零售"), ("胖东来", "零售"), ("Costco", "零售"),
    ("比亚迪", "新能源车"), ("特斯拉", "新能源车"), ("蔚来", "新能源车"), ("理想", "新能源车"),
    ("小鹏", "新能源车"), ("宁德时代", "锂电"),
    ("元气森林", "饮料"), ("农夫山泉", "饮料"), ("茅台", "白酒"), ("五粮液", "白酒"),
    ("顺丰", "物流"), ("京东物流", "物流"),
)


def _extract_industry(text: str) -> str:
    """从文本提取行业关键词。

    **全项目唯一实现**——`graph.py` 不再另抄一份（那正是垃圾键的来源）。
    返回空串表示"认不出"：调用方**必须**把空串当"不沉淀/不锚定"处理，
    **不许**编一个兜底键（编出来的键没人在检索侧找得到，等于把资产丢了还记账）。
    """
    t = text or ""
    if not t:
        return ""
    for word in _INDUSTRY_WORDS:
        if word in t:
            return word
    for brand, ind in _INDUSTRY_ALIASES:
        if brand in t:
            return ind
    return ""


def _domain_of(url: str) -> str:
    """提取 URL 域名（Bing 补全来源的站点名兜底，如 https://www.hongcan.com → hongcan.com）"""
    import re

    m = re.search(r"https?://([^/]+)", url or "")
    if m:
        return m.group(1).replace("www.", "").strip("/").strip()
    return ""


# ============================================================
# 信息整合：LLM 生成专业调研报告章节
# ============================================================
_INTEGRATION_PROMPT = """你是"研策"的商业调研分析师，负责撰写【{dimension_label}】维度的专业调研章节。本次调研行业是：【{industry}】。

{viewpoint}
写作要求（务必遵守）：
1. **行业锚定**：全部内容必须属于【{industry}】行业，严禁出现任何其他行业或通用模板内容；正文中每个主体、数据、案例都必须与【{industry}】直接相关
2. **专业商业报告风格**：直接写分析内容，用客观专业语言，不要出现"我检索到""以下为""根据搜索结果"等写作痕迹
3. **单角度深耕**：你只负责【{dimension_label}】这一个维度，聚焦该维度核心数据与判断，不要越界去写市场规模/竞争格局/用户画像等其他维度（由其他章节负责）
4. **内容去重**：同一信息只写一次。多来源交叉验证后的结论合并成一段，**绝对禁止同一结论在不同小标题下重复表述**
5. **权威来源（不写级别括号）**：关键数据句后标注「来源机构+年份」，如「据国家统计局2025年数据」「据艾瑞咨询测算」。**不要输出"(L1官方统计/监管)""(L3行业机构/协会)"这类信源级别括号标注**——信源级别与来源明细由右侧数据源面板统一展示，正文保持干净专业
6. **定量表达**：关键数据用 markdown 表格呈现（表头对齐，前端会渲染表格），禁止用文字罗列一堆数字；任何结论必须有数字或逻辑支撑，禁止"极具韧性""广阔空间""潜力巨大"这类无数据支撑的形容词
7. **禁止模板话术**：不要以"启示：企业应……""总之""综上所述"等固定句式结尾；结尾改为本章独有的、可证伪的判断（一句话能判断对错的结论）
8. **结构**：用###小标题分段，每段讲一个独立子主题，段与段之间内容不重叠
9. **数据时效**：数据有明显年份正常写出，不要声明"我的知识截止于XX"
10. **数值规范（最重要）**：
    - 任何数字结论必须是"具体数值 + 单位"（如"约 5,005 亿元"），**禁止用数学表达式代替最终结果**；
    - **禁止输出嵌套括号的表达式**（如 `3.85(3.85(5,005(1,927...)))` 这类逐层套娃），一旦出现即视为格式错误；
    - 计算过程最多用一行白话说明（如"按城镇人口×渗透率×频次×客单价测算"），**不要展开成公式链条**；
    - 所有金额必须带"元/万元/亿元"单位，大数用千分位，**严禁丢失"亿/万"单位**；
    - 你自己假设/推导的参数（渗透率/频次/转化率等）必须标注"(L5估算)"
11. **正文禁止链接**：正文严禁输出任何 http(s):// 链接、www. 网址、或 markdown 链接（`[文字](url)`）。来源 URL 由右侧数据源面板统一展示，正文只标注「来源机构+年份」即可（如「据国家统计局2025年数据」），不得粘贴网址。
12. **同概念口径一致性（返工级缺陷）**：单店日均杯量/盈亏平衡杯量/客单价/现磨渗透率/人均咖啡消费量/关店率(闭店率/关闭率)这类指标若在本章或跨章节出现 ≥2 次，**每次都必须标注样本母体口径**（如"头部连锁""全量门店""瑞幸加盟店""单一城市样本""县域""三四线全城"），严禁同一指标以不同口径的裸数字多处取值且自相矛盾；若本质就是不同样本/不同时间，必须写明差异（如"186杯为头部连锁均值，98杯为全量含亏损店"），避免同份报告自己打架。
13. **引用来源纪律**：关键数字句必须给出**可核验的具体机构+年份**（如"据艾瑞咨询2024年报告""据星巴克2024财年Q2财报"），禁止使用"宏观经济数据/行业数据/行业基线/行业观察/据网络/据行业"这类点不开的泛来源名；没有可靠机构背书的数字，必须当句标注"(估算/待核)"，不得伪装成已核数据。
14. **口径修正纪律（防"自我修正"外露）**：若资料数据口径存疑或机构间冲突，应**并列两值并说明差异与采信理由**，或直接删去不可靠值；**禁止出现"原口径偏差，此处自行修正/校准为…"这类自我修正叙事**——读者无法核验修正依据，等于把不可信暴露给读者（返工级缺陷）。
15. **判断时点纪律**：本报告生成时点为 {now_date}（基于此前公开数据）。对"已核事实/数据"句正常写年份即可；对任何**前瞻/预测/风险判断句**（将在/预计/或达/未来N个月…），必须同时交代 ①**判断做出的时点**（=本报告生成时点 {now_date}）②**预测指向的目标期**（如"预计2030年前后……"），并写明"基于 {now_date} 前公开数据外推，需持续跟踪验证"；禁止既无目标期也无判断时点的模糊远期句，禁止把预测写成已核事实。
16. **法规资格断言纠偏（防"冷食专间式"误读）**：凡引用《食品经营许可和备案管理办法》等法规判断"某业态门店能否获证/需要什么硬件"时，**不同经营项目（如"自制饮品制售"与"冷食类食品制售"）是并列的独立许可项目，禁止把一类的要求套到另一类头上**——例如"现制/自制饮品（咖啡、奶茶）门店"通常仅需专用操作区域/水吧条件即可获证，"独立冷食类操作专间"只对兼售凉菜/现切果品等冷食的经营提出。若检索资料声称"现制饮品强制设冷食专间/约43%商铺无法过审"且与上述分类冲突，**判定为疑似法规误读**：不得照抄进正文，也不得据此推导"X%商铺无法合规/选址需排除"这类结论；若确需提及，改为"(某来源称…，与现行办法分类不符，待核)"并列存疑。本规则用于**食品经营许可类**（专间/专区/冷食/生食/裱花），其他法规类推：资格/硬件类断言须忠于法规原文分类，禁止跨项目套用。
17. **同报告公司级口径锚（防跨章打脸，返工级缺陷）**：本报告各章（含"深入"章）并行独立成文、互不可见。撰写**同一家公司/同一指标/同一时间**的数字（门店数、市占率、营收、三四线占比、单店投入、客单价等）时，**全报告只允许一个权威值**：若本报告给了口径锚（见下方"数字口径锚"），以锚为准或采用与锚同源且更新的官方值并写明年份；与锚冲突的其它来源只能并列并标注"来源+年份+口径差异"，**严禁本章自取一个与锚/其它章节互不认账的数值当权威**。实体口径属性（直营 vs 加盟、全量 vs 头部连锁 vs 单城样本）也禁止错标。经营类无权威背书的数字一律标"(估算/待核)"。

{framework_requirement}

原始资料：
{raw_data}

请输出【{dimension_label}】维度的专业调研章节（不要输出章节大标题，直接写内容）。
"""


# 分析框架 → 本节强制交付要求（把"标题党框架"变成"实质测算"）
_FRAMEWORK_REQUIREMENT: dict[str, str] = {
    "TAM_SAM_SOM": "【本节必须用 TAM/SAM/SOM 三层测算，且遵守数值规范】TAM（潜在总市场）→ SAM（可服务市场，剔除政策/地理/能力限制）→ SOM（可获得市场，按渗透率假设）。每一层**只输出一个明确的最终数值+单位**（如'约 5,005 亿元'），禁止输出数学表达式代替结果，禁止嵌套括号表达式（如 a(b(c(...)))）；计算过程最多一行白话（如'按城镇人口×现制渗透率×年均频次×客单价测算'）；所有假设参数（渗透率/频次/客单价/转化率）必须标注'(L5估算)'；三层数值必须递减（TAM>SAM>SOM）且单位统一带'亿元'；用自下而上推算，并用自上而下（机构数据）交叉验证，给出保守/乐观区间。",
    "PEST": "【本节必须用 PEST 四维扫描】政治（Policy）/经济（Economy）/社会（Society）/技术（Technology）四方面逐一分析，每方面给出对该行业的具体影响与至少1个数据支撑，不要泛泛而谈；数据带单位，假设参数标注'(L5估算)'。",
    "PORTER_FIVE": "【本节必须用波特五力】逐一分析：现有竞争强度/潜在进入者威胁/替代品威胁/供应商议价力/买方议价力；并附一张头部玩家定量对比表（markdown表格，至少5家：品牌/营收/市占率/增速/门店数或用户数）；营收带单位（亿元），市占率用百分数。",
    "STP": "【本节必须用 STP】市场细分（Segmentation）→目标市场选择（Targeting）→定位（Positioning）；输出核心客群画像（年龄/收入/消费习惯/核心痛点），并给出可执行的差异化定位表述；人数/客单价带单位。",
    "COMPETITIVE_EDGE": "【本节必须识别进入壁垒与竞争优势】资金/技术/牌照/渠道/品牌五类壁垒逐一评估，给出新进入者的存活概率判断及依据；金额带单位，假设标注'(L5估算)'。",
    "UNIT_ECONOMICS": "【本节必须算到单位经济，遵守数值规范】给出 LTV（客户生命周期价值）、CAC（获客成本）、毛利率、单店/单品成本结构拆解、盈亏平衡点（BEP）与回本周期；所有金额带单位（元/万元/亿元），每个指标输出明确数值，禁止用表达式代替结果；假设参数标注'(L5估算)'；所有假设显式列出，并做乐观/中性/保守三档敏感性分析。",
    "TREND_RISK": "【本节必须扫描趋势与风险】增长趋势、颠覆性变化、政策/竞争/需求/成本风险各列出具体项，每个风险配一条对冲建议；增速带%，金额带单位。",
}


# ============================================================
# Python 测算引擎（TAM/SAM/SOM）
# 算数在代码里完成：LLM 只负责从资料定参数，数值由 Python 计算并注入，
# 报告正文直接引用算好的值——杜绝"5,005(5,005(3.85(...)))"式嵌套污染和量级错误。
# 事实参数（人口基数）从 facts_kb 只读，LLM 只能选键，值由代码注入（消灭 P1）。
# ============================================================
class MarketSizingParams(BaseModel):
    """市场测算参数 Schema——LLM 只能填这里，算数交给 Python（消灭 P9）。"""

    population_key: str = Field(description="人口基数键，必须来自 facts_kb")
    potential_ratio: float = Field(ge=0.01, le=1.0, description="潜在人群占比，0-1")
    tam_freq: float = Field(gt=0, description="TAM 理论年均频次，杯/人/年")
    avg_price: float = Field(gt=0, description="平均客单价，元/杯")
    sam_penetration: float = Field(ge=0.01, le=1.0, description="SAM 有效渗透率，0-1")
    sam_freq: float = Field(gt=0, description="SAM 年均频次，杯/人/年")
    sam_price: float = Field(gt=0, description="SAM 客单价，元/杯")
    som_share: float = Field(ge=0.01, le=1.0, description="SOM 可获得份额，0-1")

    @model_validator(mode="after")
    def check_self_consistent(self):
        """参数侧预检（消灭 P2）：SAM 的潜在规模不得高于 TAM 的潜在规模。"""
        tam_signal = self.potential_ratio * self.tam_freq * self.avg_price
        sam_signal = self.sam_penetration * self.sam_freq * self.sam_price
        if sam_signal > tam_signal:
            raise ValueError(
                f"参数自相矛盾: SAM 潜在量({sam_signal:.0f}) > TAM 潜在量({tam_signal:.0f})，"
                f"请调整 SAM 渗透率/频次/客单价使其低于 TAM 假设"
            )
        return self


_CALC_EXTRACT_PROMPT = """你是测算参数提取器。从下面的行业检索资料中，提取 TAM/SAM/SOM 测算所需参数。
输出 JSON（只输出 JSON，不要其他文字）：
{
  "population_key": "中国_城镇人口",   // 人口基数键。消费品/零售/服务市场默认"中国_城镇人口"；只有研究全国性宏观总量才用"中国_总人口"
  "potential_ratio": 0.72,      // 潜在消费人群占人口的比重（剔除无消费能力人群），0~1
  "tam_freq": 30,               // TAM 理论年均消费频次，单位：杯/人/年（必须符合规则4的现实锚）
  "avg_price": 20,              // 平均客单价，单位：元/杯
  "sam_penetration": 0.30,      // SAM 有效渗透率（实际会消费现制咖啡的人口占比），0~1
  "sam_freq": 15,               // SAM 年均消费频次，杯/人/年（必须符合规则4）
  "sam_price": 18,              // SAM 平均客单价，元/杯
  "som_share": 0.08             // SOM 可获得份额（新进入者可占 SAM 的比例），0~1
}
规则：
1. 人口基数只能从知识库键中选，禁止自行填写数字；值由代码注入，报告正文引用时必须使用该键对应口径词（全国总人口/全国城镇人口/全国农村人口），不得改称其他口径
2. 参数优先从资料中找依据；资料缺失的按行业常识给合理假设（视为 L5 估算）
3. 渗透率/占比/份额必须是 0~1 的小数
4. **频次现实锚（最重要，防凭空放大）**：人均年消费频次必须以"中国现状量级"为锚，禁止按"每人几乎天天喝"拍脑袋。
   中国基准（现制/现磨咖啡等饮品口径）：全国人均现制咖啡年消费约 10~15 杯、一线城市更高；含速溶/即饮的全渠道人均约 20 杯上下；成熟市场（日/韩）人均可达 200+ 杯。
   - tam_freq（TAM 理论频次）：默认不得超过 45；仅当资料给出"对标成熟市场人均消费量"的明确出处时才可上探到 ≤60；
   - sam_freq（SAM 现实频次）：按目标人群实际饮用习惯取 8~30，须小于 tam_freq；
   - avg_price（客单价）锚：现制咖啡主流区间约 10~30 元（9.9 活动价下探、星巴克等高端上探）。
   若资料直接给出"中国人均年消费 X 杯"等数据，优先采用资料数值并让 SAM 频次向其靠拢。
5. 上述现状基准/锚值只用于约束取值量级，**禁止当作报告数据直接引用**（除非资料另行背书）
6. 只输出 JSON
"""


def _extract_calc_params(raw: list[dict[str, Any]], industry: str = "", manual: str = "") -> dict | None:
    """LLM 从检索资料提取测算参数（JSON），人口基数值由事实库注入；失败返回 None

    manual：用户人工补充的数据（如"渗透率3%、客单价25元"），优先级高于检索资料
    （Stateful HITL：StopCard 人工补参 → 真正流进测算）。
    """
    if not has_main_llm() or not raw:
        return None
    try:
        data_str = "\n".join(
            f"- {r.get('content', '')[:400]}" for r in raw[:6] if isinstance(r, dict)
        )
        llm = get_main_llm(temperature=0.2, max_tokens=800)
        manual_line = f"\n用户手动指定（优先级最高，必须采用）：{manual}" if manual else ""
        # 公司级口径锚（P0-4）：测算取值也须与同报告口径锚一致（如星巴克直营投入120-180万、联营45-65万）
        _anchor_line = ""
        try:
            from core.company_anchor import build_anchor_block

            _anchor = build_anchor_block(industry)
            if _anchor:
                _anchor_line = f"\n\n【本报告数字口径锚（取值须以其为准或并列标注冲突）】\n{_anchor}"
        except Exception:
            _anchor_line = ""
        resp = llm.invoke(
            [
                ("system", _CALC_EXTRACT_PROMPT),
                (
                    "human",
                    f"行业：{industry or '目标行业'}\n可选人口键：{population_keys()}\n\n检索资料：\n{data_str}{manual_line}{_anchor_line}",
                ),
            ]
        )
        text = (resp.content or "").strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(text)
        # 人口基数必须来自知识库（值由代码注入，LLM 无权定数字）
        pop_key = data.get("population_key") or "中国_城镇人口"
        try:
            population = get_fact(pop_key)
        except KeyError:
            pop_key, population = "中国_城镇人口", get_fact("中国_城镇人口")
        data["population_key"] = pop_key
        data["base_population"] = population["value"]  # 亿人，代码注入
        # 频次现实锚——构造前清洗（与 _CALC_EXTRACT_PROMPT 规则4 同源，代码兜底）：
        # 吃超频次不整组拒绝（那会让整节测算消失），而是先夹到现实上界，再由 pydantic 自洽校验把关。
        _tam_orig = float(data.get("tam_freq") or 0)
        _sam_orig = float(data.get("sam_freq") or 0)
        if _tam_orig > 60:
            data["tam_freq"] = 60.0
        if _sam_orig > 40:
            data["sam_freq"] = 40.0
        # pydantic 强校验：非法参数直接拒绝（渗透率越界 / 参数自相矛盾等）
        validated = MarketSizingParams(**data)
        p = validated.model_dump()
        p["base_population"] = population["value"]  # 事实值由知识库注入（不在Schema字段，需手动回填）
        p["_population"] = population
        p["_pop_label"] = fact_label(pop_key)  # 口径锁定词（全国城镇人口…），正文引用严禁换口径
        p["_freq_clipped"] = bool(_tam_orig > 60 or _sam_orig > 40)
        return p
    except Exception:
        return None


def _compute_tam_sam_som(p: dict) -> dict:
    """Python 计算 TAM/SAM/SOM。

    单位约定：人口=亿人、频次=杯/人/年、价格=元/杯 → 结果自动为"亿元"。
    例：9.3亿城镇人口 × 0.72 × 45杯 × 20元 ≈ 6,030 亿元。

    **SAM 必须从 TAM 派生，不能各自从 base_population 算**（2026-09-15 深访实跑）。
    病案：原实现是两行各自独立的乘法——

        tam = base_population * potential_ratio * tam_freq * avg_price   # 基数 6.696 亿
        sam = base_population * sam_penetration * sam_freq * sam_price   # 基数 9.3 亿 ←

    第二行**漏了 `potential_ratio`**，于是 SAM 的基数（9.3 亿，全部城镇人口）
    **比 TAM 的基数（6.696 亿，潜在人群）还大**——SAM ⊄ TAM，逻辑上不可能，
    而两行代码各自看都"没写错"。

    实跑后果（星巴克下沉，真实报告）：TAM 4,687 亿 → SAM **904 亿**（应为 651 亿，
    正好等于 9.3×30%×18×18）→ SOM 72 亿。而且**错值沿链条自洽**：SOM=904×8%=72、
    敏感性=904×35/30=1,055，全都对，所以从报告内部怎么看都查不出来——
    只有拿声称的公式重算一遍才会暴露。**报告正文写着的公式恰恰是对的**
    （"TAM基数×SAM渗透率×SAM年均频次/理论频次×SAM客单价/理论客单价"），
    是代码没照它实现。

    为什么改成"从 TAM 派生"而不是"补一个 potential_ratio"：**补因子能修好这一次，
    改派生能修好这一类**。写成 `tam × …` 之后，"SAM 是 TAM 的子集"变成**结构性事实**
    而不是一条要靠人记住的约定——以后谁再加一层（如 TAM→SAM 之间插一个"可及人群"），
    也不可能再出现子集越界。

    数学上两者等价（当 potential_ratio 存在时）：
      tam × sp × (sf/tf) × (sp2/ap)
      = bp×pr×tf×ap × sp × sf/tf × sp2/ap
      = bp × pr × sp × sf × sp2   ∎
    """
    tam = p["base_population"] * p["potential_ratio"] * p["tam_freq"] * p["avg_price"]
    # 从 TAM 逐层收窄：渗透率 → 频次比 → 客单价比。三层都 ≤1（渗透率须 <1），
    # 故 SAM ≤ TAM 天然成立（见 `_assert_tier_order` 的兜底断言）。
    _freq_ratio = (p["sam_freq"] / p["tam_freq"]) if p.get("tam_freq") else 0.0
    _price_ratio = (p["sam_price"] / p["avg_price"]) if p.get("avg_price") else 0.0
    sam = tam * p["sam_penetration"] * _freq_ratio * _price_ratio
    som = sam * p["som_share"]
    out = {
        "tam": round(tam),
        "sam": round(sam),
        "som": round(som),
        "som_opt": round(sam * min(p["som_share"] + 0.04, 0.3)),
        "som_cons": round(sam * max(p["som_share"] - 0.03, 0.01)),
    }
    _assert_tier_order(out, p)
    return out


def _assert_tier_order(out: dict, p: dict) -> None:
    """三层必须严格递减（TAM > SAM > SOM）。**不满足就不许静默输出。**

    这条断言的存在理由与上面那条病案同源：TAM/SAM/SOM 是**我们这个引擎算出来的**，
    而"算术正确"正是我们对外卖的卖点。一个子集越界的数一旦落进报告，
    被懂行的复核者拿计算器一按，伤的就不是这一处数字，是整条"确定性下沉"的叙事。

    处理方式**刻意不是"抛异常阻断交付"**：测算块只是报告的一节，
    为一个参数问题让整份报告生不出来，代价大于收益。改为**打日志 + 在结果里留下标记**，
    由交付层决定怎么披露（见 `_build_tam_calc` 的告警行）。
    """
    tam, sam, som = out.get("tam") or 0, out.get("sam") or 0, out.get("som") or 0
    if sam > tam or som > sam:
        try:
            import logging

            logging.getLogger(__name__).warning(
                "TAM/SAM/SOM 层级越界（SAM⊄TAM 或 SOM⊄SAM）：TAM=%s SAM=%s SOM=%s 参数=%s",
                tam, sam, som, {k: p.get(k) for k in ("potential_ratio", "sam_penetration", "som_share")},
            )
        except Exception:
            pass
        out["tier_violation"] = f"SAM({sam}) ⊄ TAM({tam}) 或 SOM({som}) ⊄ SAM({sam})"


# 假设登记表元数据：参数 → (标签, 类型, 可信度) —— 对齐"Assumption Register"（值/类型/可信度/范围/敏感性）
_ASSUMPTION_META = {
    "potential_ratio": ("潜在人群占比", "L5假设", "低"),
    "tam_freq": ("理论年均频次", "资料/假设", "中"),
    "avg_price": ("客单价", "资料/假设", "中"),
    "sam_penetration": ("SAM渗透率", "L5假设", "低"),
    "sam_freq": ("SAM年均频次", "资料/假设", "中"),
    "sam_price": ("SAM客单价", "资料/假设", "中"),
    "som_share": ("SOM可获得份额", "L5假设", "低"),
}


def _build_assumption_register(params: dict) -> str:
    """假设登记表（Assumption Register）：每个关键假设给出 值/类型/可信度/建议范围 + 敏感性提示。

    把"算术正确"升级为"商业模型真实"——测算不再是一个黑箱答案，而是
    "在什么假设成立时，这个决策成立"。哪个参数最不确定 → 优先核验。
    """
    conf_range = {"高": "±10%", "中": "±15%", "低": "±20%"}
    rows = []
    low_conf = []
    for key, (label, stype, conf) in _ASSUMPTION_META.items():
        if key not in params:
            continue
        val = params[key]
        display = (
            f"{val * 100:.0f}%"
            if key in ("potential_ratio", "sam_penetration", "som_share")
            else f"{val:.0f}"
        )
        rows.append(f"| {label} | {display} | {stype} | {conf} | {conf_range[conf]} |")
        if conf == "低":
            low_conf.append(label)
    sens_txt = "、".join(low_conf) if low_conf else "关键参数"
    return (
        "\n\n【系统测算 · 假设登记表 Assumption Register】（报告须引用本表，勿删改）\n"
        "| 关键假设 | 值 | 类型 | 可信度 | 建议核验范围 |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(rows)
        + f"\n- 敏感性：各参数按比例影响测算结果；最不确定（低可信度）的是「{sens_txt}」，"
        "建议优先补充实际数据核验后再用于正式决策。"
    )


def _build_tam_calc(
    raw: list[dict[str, Any]],
    industry: str = "",
    manual: str = "",
    viol_sink: list[str] | None = None,
) -> str:
    """生成测算引擎注入块：参数 + 已算好的数值 + 假设登记表 + 禁止重算指令

    manual：用户人工补充的数据（Stateful HITL），透传给参数提取，优先级最高。
    viol_sink：**把引擎侧的层级越界标记带出去**（2026-09-16）。
        病案：`_check_tier_nesting` 一直往 `out["tier_violation"]` 里写越界标记，
        但**只有这里**（`viol_note`）读它，读出来的是一句给写作者看的中文指令。
        而决策完整性闸门（`core/integrity_gate`）声称的"引擎侧 + 文本侧两层互补"，
        读的是 TaskState 里的 `sizing_violation` —— **那个键在全仓没有任何写入方**，
        于是那一层永远是空串，闸门**只剩正文正则这一道**。
        `tier_violation` 是我们自己算出来的、确定性的事实；让它走到闸门，
        才配得上"算术在代码里"这句话。
    """
    params = _extract_calc_params(raw, industry, manual=manual)
    if not params:
        # **抽不到参数必须说，不能返回空块**（2026-09-16 修）。
        #
        # 病案：这里原先直接 `return ""`。而本函数**只在
        # `framework == "TAM_SAM_SOM"` 时被调用**——也就是说，
        # "框架要求必须有 TAM/SAM/SOM" 和 "引擎什么都没抽到" 会同时成立。
        # 此时注入块是空的，框架要求还在，于是模型只能**自己拍三个数**，
        # 而全文唯一的披露就是**没有披露**：读者看到的是三个看起来精算过的规模数字，
        # 系统其实一个参数都没抽到、一次乘法都没做。
        # **"没算"与"算了"在交付物上长得一模一样**——这正是本产品一直在批评的东西。
        #
        # 与上面 `tier_violation` 的处理是同一条纪律：宁可报告里写"本层未通过校验/未测算"，
        # 也不要一个看起来精确、一按计算器就穿的数。
        return (
            "\n## ⚙️ 测算引擎：本次未能启用\n"
            "- **未能从本次检索资料中抽取到可用参数**（人口基数 / 渗透率 / 消费频次 / "
            "客单价等，至少一项缺失），因此 **TAM / SAM / SOM 不是本系统测算得出的**。\n"
            "- 若本章仍需给出规模数字：**必须写明它是引用值还是估算值，并给出来源与数据期**；"
            "**不得**以『本系统测算』的口吻呈现，也不得省略这条说明。\n"
            "- 请在《数据缺口披露》一节中一并写明缺的是哪一类参数。\n"
        )
    c = _compute_tam_sam_som(params)
    pop = params.get("_population") or {}
    pop_label = params.get("_pop_label") or fact_label(params.get("population_key", ""))
    pop_txt = (
        f"{pop_label} {pop['value']:.1f}{pop.get('unit', '亿人')}（{pop.get('source', '')}{pop.get('as_of', '')}，{pop.get('level', 'L1')}）"
        if pop
        else f"{params['base_population']:.1f} 亿人"
    )
    clip_note = (
        "\n- ⚠️ 抽取频次超出现实锚上限（tam_freq≤60、sam_freq≤40），系统已按上限取值，登记表如实呈现。"
        if params.get("_freq_clipped")
        else ""
    )
    # **层级越界必须说给写作者听**（2026-09-15）。此前的做法是"算出来什么就注入什么"，
    # 于是 904 亿（基数比 TAM 还大）被当成正常值注入、写进报告、印给复核者。
    # 现在引擎会在结果里留 `tier_violation`；这里把它翻成一句**指令**，
    # 让正文如实披露而不是照抄一个越界的数——**宁可报告里写"本层测算未通过一致性校验"，
    # 也不要一个看起来精确、一按计算器就穿的数**。
    _viol = c.get("tier_violation")
    if _viol and viol_sink is not None:
        # 带出引擎侧标记 → 节点落进 state → 决策完整性闸门按"引擎自报越界"归类并阻断。
        viol_sink.append(str(_viol))
    viol_note = (
        f"\n- 🔴 **本层测算未通过一致性校验（{_viol}）**：三层必须严格递减。"
        "正文**不得**把上述 SAM/SOM 当作结论引用；应写明『本次测算的层级关系未通过校验，"
        "相关数值仅作量级参考，不作决策锚』，并把该提示保留在本节内。"
        if _viol
        else ""
    )
    # ── 情景与敏感性（2026-09-17）─────────────────────────────────────────
    # **确定性、零模型、零联网**：参数已经结构化、计算走**上面同一个** `_compute_tam_sam_som`
    # （公式唯一出处——本层不复制任何公式，仓库里"同一公式三处实现"的病案不制造第四处）。
    #
    # 决策红线**不是我们编的**，就是本层自己的一致性判据：三层必须严格递减。
    # 于是这句话有了真内容：**基准算得对，不等于参数动一动还对**——
    # "只要 ±15% 就把 SAM 顶到 TAM 之上"是一个真实的脆弱性发现，
    # 而它正好是"结论有多稳"最直白的答案。
    # 基准已经越界（`_viol`）时不跑：**在一组已经判错的数上做敏感性，是给错误加装饰**。
    scen_md = ""
    if not _viol:
        try:
            from core.scenario import analyze as _scen_analyze
            from core.scenario import format_prompt_block as _scen_render

            scen_md = _scen_render(_scen_analyze(
                params,
                _compute_tam_sam_som,
                variables=[
                    "potential_ratio", "tam_freq", "avg_price",
                    "sam_penetration", "sam_freq", "sam_price", "som_share",
                ],
                redline=lambda r: (
                    bool((r.get("sam") or 0) > (r.get("tam") or 0)
                         or (r.get("som") or 0) > (r.get("sam") or 0)),
                    "三层不再严格递减（TAM>SAM>SOM）——参数在这个幅度内就能把结论顶翻",
                ),
            ))
        except Exception as _se:
            logger.warning("情景分析失败（不阻断交付）: %s", _se)
            scen_md = ""
    return (
        "\n\n【系统测算引擎已计算好以下数值，报告正文必须直接引用，禁止自行重算、禁止输出数学表达式】\n"
        f"- TAM（潜在总市场）≈ {c['tam']:,} 亿元\n"
        f"- SAM（可服务市场）≈ {c['sam']:,} 亿元\n"
        f"- SOM（可获得市场）≈ {c['som']:,} 亿元（乐观 {c['som_opt']:,} / 保守 {c['som_cons']:,}）\n"
        f"- 人口基数：{pop_txt}（由事实库注入；正文引用该人口基数时**必须使用『{pop_label}』口径词**，"
        f"禁止改称总人口/城镇人口等其他人口口径，也禁止再出现其他人口数字）\n"
        f"- 关键假设参数（L5估算）：潜在人群占比 {params['potential_ratio'] * 100:.0f}%、"
        f"理论年均频次 {params['tam_freq']:.0f} 杯、客单价 {params['avg_price']:.0f} 元、"
        f"SAM 渗透率 {params['sam_penetration'] * 100:.0f}%\n"
        + viol_note
        + clip_note
        + "三层数值必须递减（TAM>SAM>SOM）且统一带'亿元'；正文可补一句白话解释口径"
        "（如'按人口基数×渗透率×频次×客单价测算'），但禁止展开公式链条，"
        "禁止在本节出现与上述参数不同的渗透率/占比/份额数字。"
        # **本节只许有一组 TAM/SAM/SOM**（2026-09-18 实跑病案 + 生成侧预防）。
        #
        # 上面那句"禁止展开公式链条"已经写了，但**没有点明 失败长什么样**，模型照犯：
        # 交付件里一边用引擎给的 SAM **879 亿元**，一边自己列
        #     `5.5亿人 × 15% × 75杯/年 × 15元/杯 ≈ 928 亿元`
        # ——**同一份报告里同一个 SAM 两个数**（差 5.3%，任何跨度阈值都放行）。
        # 后果不是"多写了一句"：读者无法判断该信哪个，而下游 SOM 正是按 SAM 的百分比推的，
        # **基数不定，SOM 就定不下来**。
        #
        # 这一段是把"硬错"讲成**可识别的形态**（同层两个数），而不是再重申一遍"禁止"——
        # 本仓库的经验是：只说"禁止"的约束等于没约束，得让模型认得出自己要犯的那个错。
        "\n- 🔴 **本节（以及全篇）只许出现上面这一组 TAM/SAM/SOM 数值**：不得另设参数、"
        "另起公式把同一层再算一遍。**判据很硬：同一个层级的名字在全篇只能指一个数。**"
        "反例（实测发生过）：正文一边写「SAM 约为 879 亿元」，一边自己列"
        "「5.5亿人×15%×75杯/年×15元/杯≈928亿元」——那就等于报告对自己矛盾，"
        "读者无法判断该信哪个，下游按份额推算的 SOM 也随之失去基数。"
        "**这是硬错，不是口径分歧**（口径分歧可以并列，同一个词指两个数不行）。"
        "你要解释口径，就用白话复述上面**已有的**参数，不要动数字。"
        + _build_assumption_register(params)
        + scen_md
    )


# ============================================================
# Python 测算引擎（单位经济 UNIT_ECONOMICS）
# LTV/CAC/毛利/盈亏平衡/回本周期 全部由代码计算，LLM 只引用算好的值。
# ============================================================
_UNIT_CALC_EXTRACT_PROMPT = """你是单位经济参数提取器。从下面的行业检索资料中，提取单店/单品模型的单位经济参数。
输出 JSON（只输出 JSON，不要其他文字）：
{
  "investment": 40,         // 单店初期总投资（装修+设备+首批原料+押金），单位：万元
  "daily_orders": 300,      // 日均客单量（杯/单）
  "avg_price": 15,          // 客单价，单位：元/杯
  "gross_margin": 0.65,     // 毛利率（1-原料成本率），0~1
  "monthly_rent": 2.5,      // 月租金，单位：万元
  "monthly_staff": 3.0,     // 月人力成本，单位：万元
  "monthly_other": 1.0,     // 月水电/损耗/杂费等，单位：万元
  "repeat_years": 2.5,      // 客户平均留存年限
  "annual_freq": 80,        // 年复购次数（杯/年）
  "cac": 120                // 获客成本，单位：元/客户
}
规则：
1. 参数优先从资料中找依据；资料缺失的按行业常识给合理假设（视为 L5 估算）
2. 毛利率必须是 0~1 的小数
3. 只输出 JSON
"""


def _extract_unit_params(raw: list[dict[str, Any]], industry: str = "", manual: str = "") -> dict | None:
    """LLM 从检索资料提取单位经济参数（JSON），失败返回 None

    manual：用户人工补充的数据（Stateful HITL），优先级高于检索资料。
    """
    if not has_main_llm() or not raw:
        return None
    try:
        data_str = "\n".join(
            f"- {r.get('content', '')[:400]}" for r in raw[:6] if isinstance(r, dict)
        )
        llm = get_main_llm(temperature=0.2, max_tokens=800)
        manual_line = f"\n用户手动指定（优先级最高，必须采用）：{manual}" if manual else ""
        # 公司级口径锚（P0-4）：单位经济参数取值须与同报告口径锚一致（实体口径/单店投入归属禁错标）
        _anchor_line = ""
        try:
            from core.company_anchor import build_anchor_block

            _anchor = build_anchor_block(industry)
            if _anchor:
                _anchor_line = f"\n\n【本报告数字口径锚（取值须以其为准或并列标注冲突）】\n{_anchor}"
        except Exception:
            _anchor_line = ""
        resp = llm.invoke(
            [
                ("system", _UNIT_CALC_EXTRACT_PROMPT),
                ("human", f"行业：{industry or '目标行业'}\n\n检索资料：\n{data_str}{manual_line}{_anchor_line}"),
            ]
        )
        text = (resp.content or "").strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(text)
        required = [
            "investment", "daily_orders", "avg_price", "gross_margin",
            "monthly_rent", "monthly_staff", "monthly_other",
            "repeat_years", "annual_freq", "cac",
        ]
        if not all(k in data for k in required):
            return None
        data["gross_margin"] = min(max(float(data["gross_margin"]), 0.1), 0.95)
        for k in ("investment", "daily_orders", "avg_price", "monthly_rent",
                  "monthly_staff", "monthly_other", "repeat_years", "annual_freq", "cac"):
            data[k] = max(float(data[k]), 0)
        return data
    except Exception:
        return None


def _compute_unit_economics(p: dict) -> dict:
    """Python 计算单店单位经济模型（金额单位统一为元，显示时转万元）"""
    gross = p["gross_margin"]
    daily_rev = p["daily_orders"] * p["avg_price"]                       # 元/日
    monthly_rev = daily_rev * 30                                          # 元/月
    fixed_cost = (p["monthly_rent"] + p["monthly_staff"] + p["monthly_other"]) * 10000  # 元/月
    profit = monthly_rev * gross - fixed_cost                             # 月净利（满产）
    bep_rev = fixed_cost / gross if gross > 0 else 0                      # 盈亏平衡月营收
    bep_orders = bep_rev / 30 / p["avg_price"] if p["avg_price"] > 0 else 0
    payback = p["investment"] * 10000 / profit if profit > 0 else 0       # 回本月数
    ltv = p["avg_price"] * p["annual_freq"] * p["repeat_years"] * gross   # 元
    cac = p["cac"] if p["cac"] > 0 else 1
    ltv_cac = ltv / cac
    # ── 参数合理性核验 + **就地披露**（2026-09-17 实跑病案）────────────────────
    #
    # 病案（逐字来自本跑交付报告）：
    #   「用户**年均**消费频次为 12 次(L5估算)，客单价 15 元，生命周期为 **12 个月**(L5估算)，
    #     即 12 × 15 × 12 = 2,160 元」→ LTV/CAC「比值**高达 27.0**」
    # 公式本身是对的（`年频次 × 年数`），错在**提取器把"12 个月"填进了名为 `repeat_years`
    # 的年限字段**——而这段代码里 `gross_margin` 有钳位、`repeat_years` 没有，
    # 只做了 `max(float(x), 0)`（即只挡负数，不挡量级）。**同一段代码，一个参数有合理性校验、一个没有。**
    # 后果是一个 12 倍的 LTV 高估，且它一路传到 LTV/CAC=27.0 还被正文当作"很健康"引用。
    #
    # 修法选择**披露而不是悄悄改数**（公理六：降级必须告警，不许悄悄兜底）：
    # 值照原样进模型上下文，但**把"这个参数不可信"印在数值旁边**——
    # 让正文即便想引 LTV/CAC 也必须同时带上这句。数可以错，但**不许错得看不出来**。
    param_flags: list[str] = []
    if not (0.5 <= p["repeat_years"] <= 10):
        param_flags.append(
            f"客户留存年限 repeat_years={p['repeat_years']:g} 超出合理区间[0.5,10]年"
            "——**该值很可能是月数被填进了年限字段**（咖啡/零售顾客平均留存不会到十年）"
        )
    if not (1 <= p["annual_freq"] <= 2000):
        param_flags.append(f"年复购次数 annual_freq={p['annual_freq']:g} 超出合理区间[1,2000]")
    # ⚠️ **这里刻意没有"LTV/CAC 过高"这一条**。第一版加过（阈值 15），实测发现
    # **用完全合理的参数也会触发**：15 元 × 80 次/年 × 2.5 年 × 0.6 ÷ 80 元 CAC = 22.5。
    # 高频刚需品类的 LTV/CAC 本来就可能两位数——那不是错，是生意本身的样子。
    # 会常态误报的检查比没有检查更糟（它训练读者忽略告警），所以撤掉，
    # 只留**真正的量纲守卫**（参数本身超出物理合理区间）。
    ltv_reliable = not param_flags
    # 三档敏感性（月净利，元）：乐观=量+15%/毛利+3pct；保守=量-20%/毛利-5pct
    opt_profit = (monthly_rev * 1.15) * (gross + 0.03) - fixed_cost
    cons_profit = (monthly_rev * 0.80) * (gross - 0.05) - fixed_cost
    return {
        "gross": gross,
        "daily_rev": daily_rev,
        "monthly_rev": monthly_rev,
        "fixed_cost": fixed_cost,
        "profit": profit,
        "opt_profit": opt_profit,
        "cons_profit": cons_profit,
        "bep_rev": bep_rev,
        "bep_orders": bep_orders,
        "payback": payback,
        "ltv": ltv,
        "cac": cac,
        "ltv_cac": ltv_cac,
        # 参数合理性：**空 = 全过**。非空时渲染侧必须把告警印在数值旁边（见 `_unit_economics_block`）。
        "param_flags": param_flags,
        "ltv_reliable": ltv_reliable,
        "investment": p["investment"],
    }


def _build_unit_calc(raw: list[dict[str, Any]], industry: str = "", manual: str = "") -> str:
    """生成单位经济引擎注入块：参数 + 已算好的数值 + 禁止重算指令

    manual：用户人工补充的数据（Stateful HITL），透传给参数提取，优先级最高。
    """
    params = _extract_unit_params(raw, industry, manual=manual)
    if not params:
        return ""
    c = _compute_unit_economics(params)
    payback_txt = (
        f"约 {c['payback']:.0f} 个月（初期投资 {params['investment']:.0f} 万元 ÷ 月净利 {c['profit']/10000:.1f} 万元）"
        if c["payback"] > 0
        else "（当前参数下月净利未覆盖固定成本，短期难回本，建议调优单店模型）"
    )
    # 参数不可信时，**告警必须紧贴数值**，不能挪到段落末尾——
    # 挪走了模型引用那一行时就看不到它（本跑正是这样：LTV/CAC=27.0 被正文当成"很高、很好"引用了）。
    ltv_warn = ""
    if not c.get("ltv_reliable", True):
        ltv_warn = (
            "- ⚠️ **上面这组 LTV/CAC 不可信，正文禁止把它当作优势或结论依据引用**；"
            "若必须提及，须原样带上以下告警：\n"
            + "".join(f"  · ⚠️ {_f}\n" for _f in (c.get("param_flags") or []))
        )
    return (
        "\n\n【系统测算引擎已计算好以下单位经济数值，报告正文必须直接引用，禁止自行重算、禁止输出数学表达式】\n"
        f"- 单店模型：初期投资 {params['investment']:.0f} 万元；日营收 {c['daily_rev']:,.0f} 元"
        f"（{params['daily_orders']:.0f} 单 × {params['avg_price']:.0f} 元）；月营收 {c['monthly_rev']:,.0f} 元\n"
        f"- 成本结构：月固定成本 {c['fixed_cost']:,.0f} 元（租金 {params['monthly_rent']:.1f} + 人力 {params['monthly_staff']:.1f} + 其他 {params['monthly_other']:.1f} 万元）；毛利率 {c['gross']*100:.0f}%\n"
        f"- 月净利（满产）：{c['profit']/10000:.1f} 万元\n"
        f"- 盈亏平衡点：月营收 {c['bep_rev']:,.0f} 元 ≈ 日均 {c['bep_orders']:.0f} 单\n"
        f"- 回本周期：{payback_txt}\n"
        f"- LTV ≈ {c['ltv']:,.0f} 元；CAC ≈ {c['cac']:.0f} 元；LTV/CAC ≈ {c['ltv_cac']:.1f}\n"
        f"{ltv_warn}"
        f"- 三档敏感性（月净利，万元）：乐观 {c['opt_profit']/10000:.1f} / 中性 {c['profit']/10000:.1f} / 保守 {c['cons_profit']/10000:.1f}\n"
        "正文直接引用以上数值，只可补一句白话解释口径；关键假设参数标注'(L5估算)'。"
    )


def _detect_framework(description: str, agent_type: str = "") -> str:
    """从子任务描述里识别绑定哪个分析框架（方法论标签或关键词）"""
    t = description or ""
    if any(k in t for k in ["TAM", "SAM", "SOM", "市场规模测算", "市场总量"]):
        return "TAM_SAM_SOM"
    if any(k in t for k in ["PEST", "宏观环境"]):
        return "PEST"
    if any(k in t for k in ["波特五力", "五力", "竞争格局"]):
        return "PORTER_FIVE"
    if any(k in t for k in ["STP", "目标市场", "消费人群", "消费洞察"]):
        return "STP"
    if any(k in t for k in ["壁垒", "竞争优势"]):
        return "COMPETITIVE_EDGE"
    if any(k in t for k in ["单位经济", "盈亏平衡", "成本测算", "决策建议"]):
        return "UNIT_ECONOMICS"
    if any(k in t for k in ["趋势", "风险"]):
        return "TREND_RISK"
    # 兜底：按子Agent类型
    return {"industry": "PEST", "competition": "PORTER_FIVE", "user": "STP", "statistics": "UNIT_ECONOMICS"}.get(
        agent_type, ""
    )


def _build_framework_requirement(framework: str, agent_type: str = "") -> str:
    """取该框架的强制交付要求；无框架时按子Agent类型给默认要求"""
    if framework:
        return _FRAMEWORK_REQUIREMENT.get(framework, "")
    fw = {"industry": "PEST", "competition": "PORTER_FIVE", "user": "STP", "statistics": "UNIT_ECONOMICS"}.get(agent_type, "")
    return _FRAMEWORK_REQUIREMENT.get(fw, "")


def _extract_dimension_label(description: str) -> str:
    """提取子任务描述里的干净维度名（如 市场规模测算/竞争格局分析）"""
    import re

    t = (description or "").strip()
    m = re.search(r"[『「]([^』」]+)[』」]", t)
    if m:
        return m.group(1)[:30]
    return _clean_subtask_label(t) or "本维度"


def _integrate(
    subtask: SubTask,
    agent_type: str,
    raw: list[dict[str, Any]],
    industry: str = "",
    framework: str = "",
    rework_hint: str = "",
    manual: str = "",
    brief: str = "",
    viewpoint: str = "",
    metric_anchor: str = "",
    exp_brief: str = "",
    viol_sink: list[str] | None = None,
) -> str:
    """LLM 整合（真实模式）。

    生成专业调研章节：行业锚定 + 框架强制交付 + 无重复 + 无数据源字样。
    失败 → 直接用检索内容（清理掉"历史资产/联网搜索"字样）。

    rework_hint：质检返工时的致命项列表，注入到整合指令中，
    让子Agent针对上次质检发现的问题修正表述（数值由测算引擎决定）。
    manual：用户人工补充的数据（Stateful HITL），优先级最高，透传给测算引擎。
    brief：上次调研沉淀的行业基线（数据护城河），供增量更新与对比。
    viewpoint：决策视角锁定指令块（v0.9.42，与执行摘要共用同一决策主体，防章节主体漂移）。
    """
    # 本章是否降级（模型整合失败 → 回落检索原文）。
    #
    # ## 这一个变量被修过两次，两次都栽在同一处（2026-09-16）
    # 第一次：裸 `except: pass` 吞掉模型失败，兜底路径与成功路径在**交付物上同形**。
    # 第二次：把 `_degraded = ""` 放到 `try` 之外，**但放进 `if has_main_llm() and raw:` 里面**。
    #   注释自己写着"必须定义在 try 之外"，却漏了它仍在 `if` 之内——
    #   而兜底段落（`if _degraded:`）恰恰只在 **`if` 不成立时**才会走到。
    #   于是：无主模型 / 无检索资料的路径上，**每一次都抛 UnboundLocalError**，
    #   被 `run_researcher` 外层 `except` 接住 → 整章正文被替换成「（整合失败: …）」。
    #   比它要修的那个病更重：原来只是"降级不说"，现在是"降级直接把这一章弄没"。
    # 结论：**披露变量的定义位置，必须早于所有可能读到它的分支**。
    _degraded = ""
    if has_main_llm() and raw:
        try:
            llm = get_main_llm(temperature=0.3, max_tokens=3000)
            # 清洗资料：去掉"历史资产/联网搜索"标签，只留真实内容+来源名
            cleaned = _clean_raw_data(raw)
            data_str = "\n".join(
                f"- 资料{i + 1}（来源:{r['source']}）: {r['content'][:400]}" for i, r in enumerate(cleaned[:8])
            )
            dimension_label = _extract_dimension_label(subtask.description) or agent_type
            fw_req = _build_framework_requirement(framework, agent_type)
            # Python 测算引擎：算数在代码里完成，LLM 只引用算好的值
            if framework == "TAM_SAM_SOM":
                calc_block = _build_tam_calc(raw, industry, manual=manual, viol_sink=viol_sink)
            elif framework == "UNIT_ECONOMICS":
                calc_block = _build_unit_calc(raw, industry, manual=manual)
            else:
                calc_block = ""
            # 返工修复指令：把上次质检的致命项交给子Agent，要求修正表述
            # （若有用户手动补参，一并作为最高优先级约束注入）
            hint_merged = rework_hint
            if manual:
                hint_merged = (hint_merged + "；" if hint_merged else "") + f"用户手动提供：{manual}"
            rework_block = ""
            if hint_merged:
                rework_block = (
                    "\n\n【质检返工指令（必须执行）】\n"
                    "上次质检发现以下致命问题，本次必须修正，不得再犯：\n"
                    f"- {hint_merged}\n"
                    "修正要求：\n"
                    "1. SAM 不得大于 TAM，若来源数据导致矛盾，明确说明口径差异并改用保守估算，标注(L5估算)；\n"
                    "2. 人口基数全文统一为同一口径（城镇/总人口），禁止混用；\n"
                    "3. 所有测算/假设参数（渗透率/频次/客单价/转化率）必须标注(L5估算)；\n"
                    "4. 冲突数字必须并列展示并说明采信依据。"
                )
            # 决策视角锁定块（v0.9.42）：无主体时留空，模板 {viewpoint} 原位替掉
            vp_block = ""
            if viewpoint:
                vp_block = (
                    "\n\n【研究视角锁定（全文统一，禁止主体漂移）】\n"
                    + viewpoint
                    + "\n你的章节必须与执行摘要、决策清单自始至终以同一决策主体视角写作与下结论，"
                    "禁止在本章把研究主体换写成另一类决策者立场。"
                )
            # 判断时点（v0.9.43 时效纪律：模板 {now_date} 注入，预测句强制带时间锚）
            _now_d = __import__("datetime").date.today().strftime("%Y-%m")
            # 公司级数字口径锚（P0-4）：主/深入并行写作互不可见 → 把同报告口径锚随每章 human 消息注入
            # （无该行业登记锚 → 空串，不影响其它行业；纯只读、失败不阻断）
            _anchor_txt = ""
            try:
                from core.company_anchor import build_anchor_block

                _anchor_txt = build_anchor_block(industry)
            except Exception:
                _anchor_txt = ""
            prompt = (
                _INTEGRATION_PROMPT.replace("{dimension_label}", dimension_label)
                .replace("{industry}", industry or "本次调研的目标行业")
                .replace("{viewpoint}", vp_block)
                .replace("{framework_requirement}", fw_req + calc_block + rework_block)
                .replace("{now_date}", _now_d)
                .replace("{raw_data}", data_str)
            )
            # ## `metric_anchor` 必须真的进上下文（2026-09-16 修 · 整条链是死的）
            #
            # 原先这一行只 join 了 `(brief, _anchor_txt, exp_brief)`，**没有 `metric_anchor`**。
            # 而 `metric_anchor` 是这么一路传进来的：
            #   `core/metric_ledger.ingest()` 落库 → `locked_anchor(industry)` 读回来渲染成
            #   提示块 → `graph.py:365 ctx["metric_anchor"]` → 本函数入参 → **到此为止**。
            # 参数在函数体里只出现一次（就是它自己的声明），**从未被使用**。
            #
            # 也就是说：对外的「**跨报告锁值锚**——同一行业的既往调研会约束新报告的指标口径，
            # 改了必须写『修正：上期 X → 本次 Y（原因）』」这条能力，
            # **代码上完全不存在**（库照写、锚照算、块照渲染，然后丢掉）。
            # 这是"记录→消费→披露"断链的最纯粹形态：每一段都在，唯独最后一段没接上。
            #
            # 注意 `_anchor_txt` 是**另一个**东西（`core/company_anchor` 的人工登记公司级口径锚），
            # 不能相互替代——两者分工不同，都是"一处沉淀、全章可见"。
            _ctx_prefix = "\n\n".join(
                x for x in (brief, _anchor_txt, metric_anchor, exp_brief) if x
            )
            resp = llm.invoke(
                [
                    ("system", prompt),
                    (
                        "human",
                        f"{_ctx_prefix}\n\n原始资料:\n{data_str}" if _ctx_prefix else f"原始资料:\n{data_str}",
                    ),
                ]
            )
            content = (resp.content or "").strip()
            if content and len(content) > 30:
                return content
        except Exception as _e:
            # ⚠️ **降级必须留痕**（2026-09-16 全量审计）。
            # 原先是裸 `except Exception: pass` —— 模型调用失败（超时/限流/解析错）后
            # 静默回落到下面的"直接拼接检索原文"，而这条兜底路径与成功路径
            # **在交付物上长得一模一样**：读者看到的是一章只有资料罗列、没有分析加工的正文，
            # 却没有任何地方说它降级了。外层那条 `（整合失败: e）` 只覆盖"抛到函数外"的情况，
            # 这一条是**在函数内被吞掉**的——所以那一处披露永远等不到它。
            _degraded = f"{type(_e).__name__}: {str(_e)[:120]}"
            logger.error("章节整合失败，回落为检索原文（本章将无模型分析）: %s", _e, exc_info=True)
    # 兜底：直接整理真实内容（去重、去掉数据源字样）
    lines = []
    kept_keys: list[str] = []
    for r in _clean_raw_data(raw):
        content = r["content"]
        key = content[:40]
        # 强去重：与已保留的内容高相似（>55% 相同字符）则跳过，避免近似结果重复拼接
        if any(_overlap_ratio(key, kept) > 0.55 for kept in kept_keys):
            continue
        kept_keys.append(key)
        lines.append(f"### {r['source']}\n{content[:400]}")
    if _degraded:
        # **降级要写在交付物里，而不是只写进日志**（本仓库纪律：降级必告警）。
        # 读者拿到的这一章没有分析、只有资料罗列；不说，它看起来就是一章"写得比较朴素"的正文。
        lines.insert(
            0,
            f"> ⚠️ **本章为降级产物**：模型整合未成功（{_degraded}），"
            "以下为检索原文的直接整理，**未经分析加工**——请按原始资料对待，"
            "不要把它当作本系统的分析结论。",
        )
    return "\n\n".join(lines) if lines else "（该维度暂未检索到有效数据）"


def _overlap_ratio(a: str, b: str) -> float:
    """简单字符重叠度：两文本相同字符占比（用于近似内容去重）"""
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    inter = len(set_a & set_b)
    return inter / max(len(set_a), len(set_b)) if set_a and set_b else 0.0


def _clean_raw_data(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗原始检索资料：去掉"历史资产/联网搜索"等字样，只保留真实内容与来源名"""
    cleaned = []
    seen = set()
    for r in raw:
        if not isinstance(r, dict):
            continue  # 防御：个别来源可能是 None/异常条目
        content = (r.get("content") or "").strip()
        if not content or len(content) < 30:
            continue
        # 去重：内容前30字相同的跳过
        key = content[:30]
        if key in seen:
            continue
        seen.add(key)
        # 内容里的数据源标签也清掉（避免拼进报告/弹层）
        content = (
            content.replace("[联网检索]", "").replace("[历史资产]", "").replace("[深挖检索]", "").strip()
        )
        # 剥掉查询词回显/来源行噪音（资产库和LLM内容都可能有）
        content = _strip_query_echo(content)
        # 来源名：去掉"[历史资产]""[联网检索]"等前缀
        title = r.get("title", "")
        for prefix in ["[历史资产]", "[联网检索]", "[深挖检索]"]:
            title = title.replace(prefix, "").strip()
        source = r.get("source_title") or r.get("publisher") or title or r.get("source_type", "资料")
        # 去掉来源里的"DeepSeek实时检索""通义千问实时检索""资产库"等字样
        source = (
            source.replace("DeepSeek实时检索", "")
            .replace("通义千问实时检索", "")
            .replace("通义千问", "")
            .replace("实时检索", "")
            .replace("DeepSeek", "")
            .replace("资产库", "")
            .strip()
            or "行业资料"
        )
        cleaned.append({"source": _strip_data_labels(source), "content": _strip_data_labels(content)})
    return cleaned


def _strip_data_labels(text: str) -> str:
    """去掉数据源标签（历史资产/联网检索/实时检索等）及其残余"""
    for pre in [
        "[历史资产]", "[联网检索]", "[深挖检索]", "历史资产", "联网检索",
        "DeepSeek联网搜索", "通义千问实时检索", "通义千问", "实时检索",
    ]:
        text = text.replace(pre, "")
    return text.strip()


def _strip_query_echo(text: str) -> str:
    """去掉内容开头的噪音（模型开场白/提问回显/来源行），直到真正正文

    千问/DeepSeek 返回偶带「为您提供深度分析」「以下是关于XX的...」这类开场白，
    或把用户提问原样回显 → 全部剥掉，数据源面板只展示真实分析内容。
    """
    lines = (text or "").split("\n")
    start = 0
    for i, ln in enumerate(lines[:8]):
        s = ln.strip()
        if not s:
            start = i + 1
            continue
        # 正文标记：markdown 标题/加粗/列表/表格，或一上来就是实义结论
        if s.startswith(("###", "##", "# ", "**", "- ", "|", "1.", "一、", "据", "结论", "核心", "市场", "规模", "增长", "从")):
            start = i
            break
        # 模型开场白/提问回显噪音 → 跳过这一行
        if any(k in s for k in ("为您提供", "以下是", "下面为", "根据您", "基于您", "针对您", "针对你", "感谢您的", "好的", "您好")):
            start = i + 1
            continue
        break
    return "\n".join(lines[start:]).strip() or text


_CLAIM_NOISE = re.compile(
    r"^(?:以下|下面|根据|据|综上|首先|其次|最后|总体来看|总的来说|为您|本文将|本报告|分析如下|"
    r"数据来源|来源|具体如下|详细如下|可以看到|可以看出)[：:，,、\s]*"
)


def _claim_from_content(content: str, *fallbacks: str) -> str:
    """从检索正文里提出**一句真结论**，用作证据的 claim / 引用标题。

    病案（2026-09-11 拆真实可复算包发现）：
    证据的 claim 一直是 `{维度}：{title}`，而**千问检索结果的 title 是检索词占位**
    （千问返回的是整合答案、没有真标题）→ 全部证据的"结论"变成检索词复述：
        `目标市场：消费者画像 星巴克中国的下沉还有空间吗 2026`
    后果是**连锁的**：建在 claim 之上的「结论状态机 / 双源配对矩阵 / 置信度台账 / claims.csv」
    **全部空转**（判的是检索词，不是结论），报告附录里的"结论"栏放进 PPT 就是硬伤。
    改为：取正文第一句**实义陈述**（≥12 字、含中文、不是提问回显或开场白）。
    """
    t = _strip_query_echo(_strip_data_labels(content or ""))
    for raw in re.split(r"[。！？；\n]", t):
        s = raw.strip().lstrip("#*-—·• \t")
        s = _CLAIM_NOISE.sub("", s).strip()
        if len(s) < 12 or not re.search(r"[一-龥]", s):
            continue
        if "？" in s or "?" in s:
            continue  # 问句不是结论
        return s[:56]
    for f in fallbacks:
        f = _strip_data_labels(str(f or "")).strip()
        if len(f) >= 4:
            return f[:56]
    return ""


def _clean_subtask_label(desc: str) -> str:
    """去掉子任务描述里的"调研『XX』"前缀，得到干净维度名（如 市场全景/竞争格局）"""
    import re

    t = desc.strip()
    m = re.search(r"[『「](.+?)[』」]", t)
    if m:
        return m.group(1)
    t = re.sub(r"^(调研|分析|评估)", "", t)
    return t[:20] or desc


def _build_citations(
    raw: list[dict[str, Any]],
    industry: str = "",
    question: str = "",
    subtask_id: str = "",
) -> list[dict[str, Any]]:
    """构建引用清单（报告角标 ↳ 数据源面板详情）

    数据源面板 = 该模块**真实用到并可点开的来源页并集**（主URL + 其余可点页），
    与证据卡 sources 同一来源——此前"思考过程下网页挺多、面板只有主URL"是口径断裂：
    _build_citations 每条证据只挑 1 个主URL，把其余真实页全丢了。现在每页一条，
    带它自己的标题/站点/URL/分组/级别；摘要在主URL给正文内容，其余页给标题占位。
    全模块内按规范键去重（m.163/www.163 同文合一）；跨模块重复交给报告级配额收敛。
    """
    citations = []
    seen_ck: set[tuple[str, str]] = set()  # 规范键去重：同文双链不重复展示
    for r in raw[:8]:
        if not isinstance(r, dict):
            continue  # 防御：个别来源可能是 None/异常条目
        rtype = r.get("source_type", "")
        content = _strip_data_labels(r.get("content") or "")
        main_url = (r.get("url") or "").strip()
        if not main_url or not main_url.startswith("http"):
            main_url = ""
        try:
            pages = _real_page_sources(r, max_pages=3)
        except Exception:
            pages = []
        if not pages:
            continue
        _content_done = False  # 正文摘要只挂到一个真实页上（主URL > 首个页）
        for p in pages:
            u = (p.get("url") or "").strip()
            if not _is_clean_url(u):
                continue
            ck = canonical_key(u)
            if not ck[0] or ck in seen_ck:
                continue
            # 该页的真实日期（数字护照与来源质量门共用，取不到就空）
            _date = ""
            for _s in r.get("sources") or []:
                if isinstance(_s, dict) and (_s.get("url") or "").strip() == u and _s.get("date"):
                    _date = str(_s["date"])
                    break
            if not _date:
                _date = str(r.get("date") or "")
            # 来源质量三道门（栏目页/时效/跨垂类）——见 core.source_quality 病案。
            # 放在这里而不是检索层：检索层是"尽量多拿"，这里是"交付什么"，
            # 两道职责不同；被拦的页不进数据源面板，也不会被正文 <ref> 指到。
            _why = None
            try:
                from core.source_quality import screen_citation

                _why = screen_citation(
                    u, p.get("title") or "", _date,
                    industry=industry, question=question,
                    # 指标文本取自本条结果自身（检索词+标题+正文前段）——
                    # 时效门要判的是"这条来源支撑的是哪个指标"，而不是"这次调研属于哪个行业"。
                    indicator=f"{r.get('query') or ''} {p.get('title') or ''} {str(r.get('content') or '')[:200]}",
                )
            except Exception:
                _why = None
            if _why:
                logger.info("来源质量门[%s] 拦下 %s：%s", subtask_id, u[:80], _why)
                _REJECT_LOG.setdefault(subtask_id, []).append((u[:120], _why))
                continue
            src_name = _strip_data_labels((p.get("publisher") or "").strip()).replace("www.", "")
            if not src_name or src_name in ("实时检索", "实时检索数据", "网络检索"):
                src_name = _domain_of(u) or ""
            if not src_name or _is_junk_source(src_name):
                continue
            # 摘要：主URL/首个真实页挂正文内容；其余页给标题占位（详情可点开原网页）
            _take_content = (u == main_url) or (not _content_done and not main_url)
            excerpt = ""
            if _take_content and content:
                excerpt = _strip_query_echo(content)[:400]
                _content_done = True
            elif p.get("title"):
                excerpt = _strip_data_labels(p.get("title"))[:60]
            # 数字护照：给这条引用配 {数据期 · 口径}（诚实优先——抽不到就标"未标注"，绝不推断补一个）
            _passport = None
            try:
                from core.data_passport import build_passport

                _passport = build_passport(p.get("title") or "", excerpt, _date, url=u)
            except Exception:
                _passport = None
            seen_ck.add(ck)
            citations.append(
                {
                    "source": src_name[:30],
                    "title": _strip_data_labels(p.get("title") or "")[:50] or u[:50],
                    "url": u,
                    "excerpt": excerpt,
                    "group": _categorize_source(u, src_name, rtype),
                    "tier": _categorize_tier(u, src_name, rtype),
                    "query": str(r.get("query") or "")[:120],
                    "data_period": _passport.period if _passport else "",
                    "caliber": _passport.caliber if _passport else "",
                }
            )
    return citations


def _extract_urls_from_content(content: str, limit: int = 3) -> list[str]:
    """从检索文本中提取真实 URL（千问 enable_search 返回里常带来源链接）"""
    import re

    urls = re.findall(r"https?://[^\s\)\]\"'<>]+", content or "")
    # 去重、去尾随标点、过滤明显垃圾
    seen, clean = set(), []
    for u in urls:
        u = u.rstrip("。.,，,;；")
        if u in seen or len(u) > 200:
            continue
        seen.add(u)
        clean.append(u)
        if len(clean) >= limit:
            break
    return clean


def _extract_source_name(content: str) -> str:
    """从检索内容提取真实来源机构名（如 国家统计局/红餐大数据/德勤中国）

    千问/DeepSeek 返回文本里常带「据XX」「来源：XX」等标记 → 提取作为信源名，
    替换"通义千问实时搜索"这类泛标签，提升证据公信力。
    过滤"分析/支撑/显示"等动词短语（如"据中国咖啡行业分析"→非机构名，弃用）。
    """
    import re

    if not content:
        return ""
    # 泛标签词：提取结果含这些词 → 不是机构名，丢弃（避免数据源显示"分析/支撑/来源/抽样"等）
    BAD_WORDS = (
        "分析", "支撑", "支持", "显示", "表示", "介绍", "指出", "来看", "统计",
        "报告", "白皮书", "测算", "预测", "预计", "报道", "数据显示", "趋势",
        "来源", "抽样", "样本", "调查", "数据", "研究显示", "相关", "平台", "行业",
    )
    patterns = [
        r"据([一-龥A-Za-z0-9·（）()]{2,18})",
        r"来源[：:]\s*([一-龥A-Za-z0-9·（）()]{2,18})",
        r"出自\s*([一-龥A-Za-z0-9·（）()]{2,18})",
        r"\[([一-龥A-Za-z0-9·（）()]{2,18})\]\(https?://",  # markdown链接 [机构名](url) → 机构名
        r"[（(]([一-龥A-Za-z0-9·]{2,18}?)《",  # （机构名《报告名》）→ 机构名（如 杭州市市场监管局《2024年...》）
    ]
    for p in patterns:
        m = re.search(p, content)
        if m:
            name = m.group(1).strip().rstrip("的数据，。、；;报告白皮书")
            # 去掉括号尾巴（如 艾瑞咨询（2024年报告）→ 艾瑞咨询）
            name = re.sub(r"[（(].*?[)）]", "", name).strip()
            if not (2 <= len(name) <= 20):
                continue
            if any(w in name for w in BAD_WORDS):
                continue  # "据XX分析/支撑/显示" → 非机构名，跳过
            return name
    return ""


def _content_institutions(content: str, limit: int = 2) -> list[str]:
    """从检索正文提取"据XX /（XX《…》/ XX咨询研究院/协会"等第三方机构名（去重、滤泛词）。

    用于"机构名定向补源"：正文点到 艾瑞咨询/Euromonitor/协会 等，但可点 URL 层
    只有品牌官网（search_info 空 + 检索词品牌锚定）→ 名实脱节。此函数给出值得定向检索的机构名。
    """
    import re as _re

    if not content:
        return []
    BAD = (
        "分析", "支撑", "支持", "显示", "表示", "介绍", "指出", "统计", "报告",
        "白皮书", "测算", "预测", "预计", "报道", "数据", "来源", "抽样", "样本",
        "调查", "相关", "平台", "行业", "公司", "资料", "趋势", "研究显示", "图表",
    )
    SUFFIX_OK = (
        "咨询", "研究院", "研究所", "研究", "数据", "证券", "国际", "协会", "中心",
        "智库", "银行", "集团", "局", "官网", "财报", "白皮书", "媒体", "财经", "商报",
    )
    out: list[str] = []
    pats = (
        _re.compile(r"据([一-龥A-Za-z·]{2,16})"),
        _re.compile(r"[（(]([一-龥A-Za-z·]{2,16}?)[《（(]"),
        _re.compile(r"来源机构[：:]?\s*([一-龥A-Za-z·]{2,16})"),
        _re.compile(r"([一-龥A-Za-z·]{2,14}?咨询)"),
        _re.compile(r"([一-龥A-Za-z·]{2,14}?研究院)"),
        _re.compile(r"([一-龥A-Za-z·]{2,14}?协会)"),
        _re.compile(r"([一-龥A-Za-z·]{2,14}?(?:研究中心|数据|智库))"),
    )
    KNOWN = ("艾瑞", "欧睿", "艾媒", "易观", "头豹", "红餐", "亿欧", "前瞻", "弗若斯特", "沙利文",
             "瑞幸", "星巴克", "蜜雪", "喜茶", "窄门", "美团", "德勤", "毕马威", "麦肯锡", "Euromonitor", "GfK", "IDC")
    for pat in pats:
        for m in pat.finditer(content or ""):
            name = m.group(1).strip()
            name = _re.sub(r"[（(].*?[)）]", "", name).strip("的数据，。、；;《》")
            if not (2 <= len(name) <= 16):
                continue
            if any(b in name for b in BAD):
                continue
            if not (any(s in name for s in SUFFIX_OK) or any(k in name for k in KNOWN)):
                continue
            if name not in out:
                out.append(name)
            if len(out) >= limit:
                return out
    return out


def _names_overlap(a: str, b: str) -> bool:
    """两个机构名是否相关：互含 或 共享2字以上连续词（中文机构名匹配）"""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    for i in range(len(a) - 1):
        if a[i : i + 2] in b:
            return True
    return False


# 垃圾/误导 URL 黑名单：游戏站、搜索聚合、广告追踪。
# "数据源全是我的世界官网"的闸门——不管 URL 从哪条路进来（Bing/千问 search_info/内容正则提取），
# 出口统一过 _is_clean_url，宁空勿错，绝不把游戏站/聚合站当信源给用户。
_JUNK_URL_MARKERS = (
    # 国际游戏站（"我的世界官网"误抓根因）
    "minecraft", "mojang", "java.net", "gamepedia", "fandom",
    "steampowered.com", "epicgames.com", "playstation.com", "nintendo.com",
    "nintendo.co.jp", "roblox.com", "fortnite.com", "epicgames",
    # 中文游戏站/网易我的世界中国版（mc.163.com 是"我的世界官网"中文入口）
    "mc.163.com", "game.163.com", "yx.qq.com", "cf.qq.com",
    "4399.com", "7k7k.com", "taptap",
    # 视频/短视频站：市场调研不要视频链接（Bing 对中文查询常返回 B站视频）
    "bilibili.com/video", "/video/BV", "bilibili.com/list",
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "youku.com", "v.youku.com", "iqiyi.com", "v.qq.com", "video.qq.com",
    "mgtv.com", "douyin.com", "kuaishou.com", "ixigua.com", "vimeo.com",
    # 音乐/直播站（网易云音乐混入数据源）
    "music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "xiami.com",
    "migu.cn", "spotify.com", "itunes.apple.com", "zingmusic", "hifini",
    # 搜索聚合/重定向/广告追踪
    "baidu.com", "bing.com", "bingj.com", "bing.net", "sogou.com", "so.com",
    "microsoft.com", "cnzz.com", "51.la", "hm.baidu.com", "goo.gl", "bit.ly",
    # 词典/词条/问答/文档聚合：不是"调研信源"，点开多为无关页（"跳词典"根治）
    # 实测 Bing 曾对冷门检索词整批返回字典SEO站（hgcha/hanyuguoxue/gushici/dict.revised）
    "zdic.net", "cidian", "hanyu", "hanzi", "zidian", "iciba", "dict.cn",
    "dict.revised.moe.edu.tw", "hgcha.com", "hanyuguoxue.com", "gushici.net",
    "/zidian/", "/cidian/",
    "baike.baidu", "zhidao.baidu", "wenku.baidu", "baike.com",
    "doc88.com", "docin.com", "book118.com", "uukanshu", "chazidian", "pinyin",
    # 2026-09-18 实跑病案：一份**咖啡连锁下沉研报**的《引文核验》清单里出现了
    # `mzh.moegirl.org.cn`（**萌娘百科**，ACG 维基）与 `caifuhao.eastmoney.com`（**财富号 UGC**，
    # 任何人可发、无编辑责任）。它们不是"链接坏了"——是**通用检索通道把离题页带进了证据池，
    # 并一路活到了引用清单**。复核者翻到那一页，看到的就是"这份报告的来源里有萌娘百科"。
    # 判据是**站点的性质**（UGC 百科 / 自媒体号），不是域名是否可访问——所以在这一层掐，
    # 不在核验层掐（核验层只管"活着吗"，那是另一件事）。
    "moegirl", "mzh.moegirl", "caifuhao.eastmoney", "caifuhao.",
    "baike.so.com", "sogoubaike", "wikiwand", "fandom.com", "wikia",
)


def _is_clean_url(url: str) -> bool:
    """URL 健康检查：掐死空值/游戏站/搜索聚合。返回 False 的 URL 一律不放进数据源面板。"""
    if not url or not url.startswith("http"):
        return False
    u = url.lower()
    return not any(m in u for m in _JUNK_URL_MARKERS)


def _is_trusted_src_domain(url: str) -> bool:
    """URL 是否属于可信来源域（政府/协会/研究机构/交易所/权威财经）。

    用作"content 正文自抠 URL"的准入白名单：只有可信域的正文来源才允许当主URL兜底，
    防止把任意正文链接（媒体/论坛/广告）当主源；命中后仍须过交付层存活核验。
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url or "").netloc or "").lower().removeprefix("www.")
    except Exception:
        return False
    if not host:
        return False
    if host.endswith(".gov.cn") or host.endswith(".gov") or host.endswith(".edu.cn"):
        return True
    try:
        from core.source_router import TIER1_DOMAINS, TIER2_DOMAINS
    except Exception:
        TIER1_DOMAINS, TIER2_DOMAINS = (), ()
    for d in (TIER1_DOMAINS + TIER2_DOMAINS):
        d = (d or "").strip().lower().lstrip("*.")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def _is_junk_source(name: str) -> bool:
    """来源名健康检查：含'我的世界/游戏站/音乐平台'名称的信源一律不展示（防名实错配）"""
    if not name:
        return False
    n = name.lower()
    return any(k in n for k in (
        "我的世界", "minecraft", "mojang", "游戏官网", "游戏盒子", "游戏下载",
        "网易云", "qq音乐", "酷狗音乐", "音乐平台",
    ))


def _match_source_url(content: str, sources: list[Any], fallback_urls: list[str] | None = None) -> str:
    """来源名 ↔ 官网URL 匹配：让每个数据源对应它自己真实的官网，而非搜索结果的第一个URL。

    策略（按优先级，全部过 _is_clean_url 闸门）：
    1. 从内容提取来源名 → 在 sources(site_name+url) 里找 site_name 匹配的 → 用它的 url
    2. 直接用 sources 里第一个干净 url —— 千问 search_info 的 site_name↔url 是同一搜索结果的配对，
       来源名与 URL 取自同一项，名实一致（杜绝"来源名=统计局但URL=无关站"的错配）
    3. 正文自抠 URL 兜底：仅采纳"可信来源域"(gov/协会/研究/上市公司)的正文链接当主URL（其余自抠=幻觉高发，丢弃）；
       命中仍由交付层存活核验把关
    4. 兜底 fallback_urls（仅 search_info 真源）
    """
    name = _extract_source_name(content)
    if name:
        for s in sources or []:
            if not isinstance(s, dict):
                continue
            site = (s.get("site_name") or "").strip()
            u = (s.get("url") or "").strip()
            if site and _is_clean_url(u) and _names_overlap(site, name):
                return u
    # 名实配对主路径：sources 里的 site_name↔url 是搜索服务商配好的一对，直接用第一个干净的
    for s in sources or []:
        if isinstance(s, dict) and s.get("url") and _is_clean_url(s["url"]):
            return s["url"]
    # 可信域正文自抠兜底（2026-09-04 止血）：引擎(千问search_info/Bing)空窗时，仅当正文自抠 URL 属于
    # 可信来源域（gov/协会/研究/上市公司）才当主URL候选——恢复 gov/研报分组，杜绝任意媒体链接冒名；
    # 命中后仍由交付层存活核验把关（假 hex 深链照转文字）。
    for u in _extract_urls_from_content(content):
        if _is_clean_url(u) and _is_trusted_src_domain(u):
            return u
    for u in fallback_urls or []:
        if _is_clean_url(u):
            return u
    return ""


def _categorize_source(url: str, source: str, source_type: str = "") -> str:
    """数据源分组标签：政府官方 / 研究报告 / 企业信息 / 社媒论坛 / 媒体报道

    前端数据源面板按此分组展示（政府官网单独一组，不混在同一个标签里）。
    """
    if source_type == "asset":
        return "历史资产"
    if source_type == "report_page":
        return "研究报告"
    if source_type == "adversarial":
        return "反方检索"
    # Source Router 白名单（P0）：命中权威白名单 → 直接归组，宁高勿低
    try:
        from core.source_router import route_source

        _tier, _group = route_source(url, source)
        if _group:
            return _group
    except Exception:
        pass
    u = (url or "").lower()
    if any(k in u for k in ["gov.cn", ".gov", "/stat", "/stats"]) or "统计局" in (source or ""):
        return "政府官方"
    if any(k in u for k in ["research", "/report", ".pdf", "zhihu.com", "xueqiu"]) or any(
        k in (source or "") for k in ["研究院", "咨询", "协会", "券商", "白皮书", "研报"]
    ):
        return "研究报告"
    if any(k in u for k in ["qcc.com", "qichacha", "tianyancha", "/company/"]) or any(
        k in (source or "") for k in ["企查查", "天眼查"]
    ):
        return "企业信息"
    if any(k in u for k in ["weibo", "xiaohongshu", "bilibili", "douban", "zhihu"]) or any(
        k in (source or "") for k in ["微博", "小红书", "知乎", "B站", "论坛"]
    ):
        return "社媒/论坛"
    if source_type == "llm_search" and not u:
        return "网络检索"
    return "媒体报道"


def _real_page_sources(r: dict[str, Any], max_pages: int = 3) -> list[dict[str, str]]:
    """一次检索结果 → 去重后的"真实可点来源页"清单（≤max_pages，首项=主URL）。

    每条给出该页**自己的**真实标题与站名（不再用检索词复述当标题）。
    来源页来自搜索服务返回的 site_name↔url 配对（qwen search_info / Bing），
    是"这条证据背后到底有哪些能点开核验的真实网页"的诚实清单——
    置信度分级、引文核验、数据源面板全部基于它，不再凭内容文字猜。
    """
    if not isinstance(r, dict):
        return []
    from urllib.parse import urlparse

    def _dom(u: str) -> str:
        try:
            return (urlparse(u or "").netloc or "").removeprefix("www.")
        except Exception:
            return ""

    entries: list[dict[str, str]] = []
    seen: dict[tuple[str, str], int] = {}  # 规范键(根域,路径) → 在 entries 的索引

    def _mob(u: str) -> bool:
        """m. 开头且非 mof/mon… 官方域名 → 移动镜像（同文时 desktop 优先）"""
        try:
            _h = (urlparse(u).netloc or "").lower().split(":")[0].split("@")[-1]
        except Exception:
            return False
        return _h.startswith("m.") and not _h.startswith("mof") and not _h.startswith("mo")

    def _add(u: str, title: str, site: str) -> bool:
        u = (u or "").strip()
        if not _is_clean_url(u):
            return False
        ck = canonical_key(u)
        if not ck[0]:
            return False
        _row = {"url": u, "title": (_strip_data_labels(title or "").strip())[:80],
                "publisher": (_strip_data_labels(site or "").replace("www.", "").strip())[:30],
                "domain": _dom(u)}
        _idx = seen.get(ck)
        if _idx is not None:
            # 同文镜像（m.163 与 www.163、pvid 双链）只占一个名额；desktop/www 版覆盖 m. 版
            if _mob(entries[_idx]["url"]) and not _mob(u):
                entries[_idx] = _row
            return True
        seen[ck] = len(entries)
        entries.append(_row)
        return True

    matched = (r.get("url") or "").strip()
    # 主URL配对的真实标题/站名：优先取 sources 里 url==主URL 那一条
    pinfo: dict[str, str] = {}
    for s in r.get("sources") or []:
        if isinstance(s, dict) and (s.get("url") or "").strip() == matched:
            pinfo = {"title": s.get("title") or "", "publisher": s.get("site_name") or ""}
            break
    if matched and _is_clean_url(matched):
        _add(matched, pinfo.get("title") or r.get("title") or "", pinfo.get("publisher") or r.get("publisher") or "")
    for s in r.get("sources") or []:
        if isinstance(s, dict) and len(entries) < max_pages:
            _add(s.get("url") or "", s.get("title") or "", s.get("site_name") or "")
    # 注意：不把 r['urls'] 里的裸 URL（无标题/站名配对）直接加为"可点页"——
    # search_info 缺失时 urls 可能是正文自抠的幻觉链接（空标题可点、点了404）。
    # 只有搜索服务返回的 site_name↔url 配对页（sources）或主URL才算真实来源。
    return entries[:max_pages]


def _page_level(url: str) -> SourceLevel:
    """来源页 URL → 信源级别（与主路径同一白名单口径：gov→L1，研报/机构/媒体→L2）"""
    if not url:
        return SourceLevel.L2_SEMI_OFFICIAL
    try:
        from core.source_router import LOW_QUALITY_DOMAINS, route_source

        # 低质内容农场先判：不得走下面的 .com/.cn 兜底（否则中研网/华经等被判 L2 半官方 = 权威虚高）
        if any(d in (url or "").lower() for d in LOW_QUALITY_DOMAINS):
            return SourceLevel.L4_USER
        _rt, _grp = route_source(url, "")
        if _rt <= 1:
            return SourceLevel.L1_OFFICIAL
        if _rt == 2:
            return SourceLevel.L2_SEMI_OFFICIAL
        if _rt == 3:
            return SourceLevel.L3_INDUSTRY
    except Exception:
        pass
    u = url.lower()
    if "gov" in u or "stat" in u:
        return SourceLevel.L1_OFFICIAL
    if any(d in u for d in _REPORT_DOMAINS) or any(k in u for k in ("research", "report", "/pdf")):
        return SourceLevel.L2_SEMI_OFFICIAL
    if any(d in u for d in (".com", ".cn", "news", ".org", "media")):
        return SourceLevel.L2_SEMI_OFFICIAL
    return SourceLevel.L3_INDUSTRY


def _build_evidence(
    subtask: SubTask,
    raw: list[dict[str, Any]],
    industry: str = "",
    question: str = "",
) -> list[Evidence]:
    """从检索结果构建证据（带信源分级 + 真实URL溯源 + 结论置信度）

    LLM 联网搜索结果 = 高可信证据（实时、带来源引用，标 L2 半官方/行业）
    反证检索（source_type=adversarial）= 反方证据，claim 显式标【反方】，防只采利好
    关键：从内容中提取真实来源URL，保证"硬溯源质检"可过（每个数字能点开来源）。
    industry/question：来源质量三道门要用（栏目页/时效/跨垂类）——证据卡上的来源
    与数据源面板同源，一侧拦了一侧放行就会出现"正文引用了、面板里没有"的错位。
    """
    evidences = []
    for r in raw[:6]:
        if not isinstance(r, dict):
            continue  # 防御：个别来源可能是 None/异常条目
        url = r.get("url", "")
        content = r.get("content", "") or ""
        source_type = r.get("source_type", "web")
        is_adv = source_type == "adversarial"

        if source_type == "authority":
            # 权威源直取（统计局数据发布 / 巨潮法定披露）：等级由**域名白名单**判定，
            # 不因"来自权威站"就无条件标 L1 —— 防经第三方检索混入的非官方内容被假冒权威。
            level = SourceLevel.L3_INDUSTRY
            try:
                from core.source_router import route_source as _rs

                _rt, _ = _rs(url, str(r.get("publisher") or ""))
                if _rt <= 1:
                    level = SourceLevel.L1_OFFICIAL
                elif _rt == 2:
                    level = SourceLevel.L2_SEMI_OFFICIAL
            except Exception:
                pass
            timeliness = 0.95
            independence = 0.9
            corroboration = 2
            pub = r.get("publisher", "")
            date = r.get("date", "")
        elif source_type in ("llm_search", "adversarial"):
            # 千问/DeepSeek 联网搜索 + 反证检索：实时信息，标 L2；从内容提取真实URL
            level = SourceLevel.L2_SEMI_OFFICIAL
            timeliness = 0.9
            independence = 0.8
            corroboration = 2 if not is_adv else 1  # 反证检索是单方视角，佐证数保守
            # 信源名：优先用与 URL 配对的 site_name（_search_mcp 传的 publisher），其次内容提取，最后泛标签
            src_list = r.get("sources") or []
            pub = r.get("publisher") or ("反方检索" if is_adv else "实时检索数据")
            if not pub or pub in ("实时检索", "实时检索数据"):
                if src_list and isinstance(src_list[0], dict) and src_list[0].get("site_name"):
                    pub = src_list[0]["site_name"]
                else:
                    pub = _extract_source_name(content) or ("反方检索" if is_adv else "实时检索数据")
            date = "实时"
            # 检索层已收回真实发布日期（博查 datePublished）→ 优先用它，
            # 让证据卡/时效块显示真实日期而非笼统"实时"（治"报告看不出数据新旧"）。
            try:
                _d0 = next(
                    (
                        str(s.get("date") or "")
                        for s in (src_list or [])
                        if isinstance(s, dict) and s.get("date")
                    ),
                    "",
                )
                if _d0:
                    date = _d0
            except Exception:
                pass
            if not url or url in ("DeepSeek联网搜索",):
                # 来源名↔官网URL匹配：红餐→红餐网、统计局→gov.cn，各数据源对应自己官网
                url = _match_source_url(content, r.get("sources") or [], r.get("urls") or [])
                # 有真实URL → 按白名单/域名升级信源等级（P0 Source Router）
                if url:
                    try:
                        from core.source_router import route_source

                        _rt, _grp = route_source(url, pub)
                        if _rt <= 1:
                            level = SourceLevel.L1_OFFICIAL
                        elif _rt == 2:
                            level = SourceLevel.L2_SEMI_OFFICIAL
                    except Exception:
                        pass
                    if any(d in url for d in ["gov", "stat", "stats"]):
                        level = SourceLevel.L1_OFFICIAL
                    elif any(d in url for d in [".com", ".cn", "media", "news", ".org"]):
                        level = SourceLevel.L2_SEMI_OFFICIAL
        else:
            level = SourceLevel.L3_INDUSTRY
            if "gov." in url or "stat" in url:
                level = SourceLevel.L1_OFFICIAL
            elif any(d in url for d in [".com", ".cn", "media", "news"]):
                level = SourceLevel.L2_SEMI_OFFICIAL
            timeliness = 0.6
            independence = 0.5
            corroboration = 1
            pub = r.get("publisher", "")
            date = r.get("date", "")

        # 统一出口闸门：任何来源的 URL 都要过健康检查（宁空勿错，防"全是我的世界"）
        if not _is_clean_url(url):
            url = ""

        # ===== 来源页诚实装配（2026-09-03 修复：不再"凭内容猜1个URL + 硬编码佐证2"）=====
        # 之前：证据只有1个Source(匹配到的第1个URL)，corroboration 硬编码=2 →
        # 置信度显示"独立来源域名1个 + 多源佐证≥2家"自相矛盾，且品牌官网被当全市场数据的证据。
        # 现在：把搜索服务真实返回的每个可点网页各挂一条 Source（带各自真实标题/站名/URL），
        # 佐证数 = 这些网页的真实数量；置信度按真实独立域名判定。没真实页就诚实标"无URL可点"。
        try:
            pages = _real_page_sources(r, max_pages=3)
        except Exception:
            pages = []
        # 来源质量三道门（与 _build_citations 同一套判定）：证据卡与数据源面板同源，
        # 一侧拦了一侧放行就会出现"正文引用了、面板里找不到"的错位。
        try:
            from core.source_quality import screen_citation

            pages = [
                p
                for p in pages
                if not screen_citation(
                    p.get("url") or "", p.get("title") or "", str(r.get("date") or ""),
                    industry=industry, question=question,
                    indicator=f"{r.get('query') or ''} {r.get('title') or ''} {str(r.get('content') or '')[:200]}",
                )
            ]
        except Exception:
            pass
        # 主URL仍以检索层名实配对为准；为空但有真实页时用首个真实页顶上
        if (not url or not _is_clean_url(url)) and pages:
            url = pages[0].get("url", "")
            _u0 = url.lower()
            if "gov" in _u0 or "stat" in _u0:
                level = SourceLevel.L1_OFFICIAL
            elif any(_d in _u0 for _d in (".com", ".cn", "news", "media", ".org")):
                level = SourceLevel.L2_SEMI_OFFICIAL
        if not _is_clean_url(url):
            url = ""
        if url:
            head = [p for p in pages if p.get("url") == url] or [
                {"url": url, "title": "", "publisher": pub or _domain_of(url) or "", "domain": _domain_of(url) or ""}
            ]
            pages = (head + [p for p in pages if p.get("url") != url])[:3]
        # 独立佐证数 —— **按"出处"数，不是按"域名"数**。
        # 病案：三家媒体各发一篇"咖啡市场规模 6,562 亿（据艾瑞咨询）"，按域名算是 3 个独立佐证、
        # 置信度顶到"高"；真相是**一个源头被转述了三次**，那个出处错了就一起错。
        # 见 core/provenance 的完整说明——这是把"转述"从"独立验证"里剥出来的那一步。
        _real_domains: set[str] = set()
        for _p in pages:
            _du = (_p.get("domain") or "").strip().lower()
            if _du:
                _real_domains.add(_du)
        for _p in pages:
            _du = (_p.get("domain") or "").strip().lower()
            if not _du:
                continue
            try:
                from core.provenance import classify, is_echo

                _name, _own = classify(_du, _p.get("title") or "", content[:600])
                if _name and is_echo(_du, _name, _own):
                    _p["origin"] = _name
                    _p["via"] = _name
                    _p["origin_domain"] = _own
                elif _name:
                    _p["origin"] = _name  # 自己就是一手出处
            except Exception:
                pass
        try:
            from core.provenance import independent_count

            # 域名列表**必须与 pages 同序同长**（2026-09-14 审计）：
            # 原传 `sorted(_real_domains)` 是个**去重且重排过的集合**，
            # 而 `independent_count` 按下标 pages[i] ↔ domains[i] 配对 →
            # 出处被安到别的域名头上，报告里会写出「iresearch.com.cn 转述自 艾瑞咨询」
            # 这种**把一手源说成回声**的自相矛盾句（还少遍历几页，独立佐证数偏低）。
            # 传 `titles` 就**额外**做一次跨域近同标题折叠（2026-09-17）：
            # 治"同一个原始稿被转载到多个站、而转载稿把出处删了"这种情况——
            # 血缘字段抓不到它（没有"据X"可抽），只能按标题相似度折。
            # 折叠是减法（减掉虚假的确定性），且**每一组折叠都进 echo 说明**，可被读者复核。
            _n_ind, _echoes = independent_count(
                pages,
                [p.get("domain") or "" for p in pages],
                [p.get("title") or "" for p in pages],
            )
        except Exception:
            _n_ind, _echoes = (len(_real_domains), [])
        # 无归属信息时 independent_count 给每个域名算一个出处，与旧口径一致（只是更保守地对齐）
        corroboration = min(max(_n_ind, 0), 3) if _real_domains else 0
        if _echoes:
            echo_note = "；".join(_echoes[:3])

        _sources: list[Source] = []
        for _i, _p in enumerate(pages):
            _u = _p.get("url", "")
            if not _u:
                continue
            _sources.append(
                Source(
                    title=_p.get("title") or "",
                    url=_u,
                    level=_page_level(_u),
                    publisher=_p.get("publisher") or (_domain_of(_u) or ""),
                    publish_date=date if _i == 0 else "",
                    excerpt=(r.get("content", "")[:300] if _i == 0 else ""),
                    timeliness_score=timeliness if _i == 0 else 0.5,
                    independence_score=independence if _i == 0 else 0.5,
                    corroboration_count=corroboration,
                    origin=_p.get("origin") or "",
                    via=_p.get("via") or "",
                )
            )
        if not _sources:
            # 完全没有真实可点页 → 诚实保留纯文字来源（置信度将按"无真实URL"封顶中）
            _sources.append(
                Source(
                    title=_strip_data_labels(r.get("title", "")),
                    url=url,
                    level=level,
                    publisher=pub or "实时检索数据",
                    publish_date=date,
                    excerpt=r.get("content", "")[:300],
                    timeliness_score=timeliness,
                    independence_score=independence,
                    corroboration_count=0,
                )
            )
        # 结论文本：**优先取正文里的真结论**（见 _claim_from_content 病案说明）——
        # 此前用 `{维度}：{title}`，而检索结果的 title 是检索词占位，导致全部"结论"是检索词复述。
        _brief = _claim_from_content(r.get("content", ""), r.get("title", ""))
        claim_text = f"{_clean_subtask_label(subtask.description)}：{_brief}" if _brief else _clean_subtask_label(subtask.description)
        if is_adv:
            claim_text = f"【反方】{claim_text}"  # 反证证据显式标记，防报告只采利好
        ev = Evidence(claim=claim_text, sources=_sources, query=str(r.get("query") or "")[:120])
        # 结论置信度分级：高/中/低 + 可解释理由（P0 可信度护城河，非黑箱）
        try:
            from core.confidence import grade_evidence_confidence

            _c = grade_evidence_confidence(ev)
            ev.confidence = _c.label
            ev.confidence_level = _c.level
            ev.confidence_reasons = _c.reasons
        except Exception:
            pass
        # 来源血缘提示：把"多源交叉"的水分挤出来，写在置信度理由里让读者一眼看到。
        # 这一步是**减法**——它下调的是虚假的确定性，比多接十个源更能提升权威性。
        if _echoes:
            try:
                ev.confidence_reasons = list(ev.confidence_reasons or []) + [
                    f"来源血缘：{len(_echoes)} 个来源实为**同一出处的转述**"
                    f"（{'；'.join(_echoes[:2])}），**不构成独立佐证**"
                ]
            except Exception:
                pass
        evidences.append(ev)
    return evidences


# ============================================================
# 子 Agent 统一入口
# ============================================================
def run_researcher(subtask: SubTask, agent_type: str, industry: str = "", context: dict | None = None, rework_hint: str = "", write: bool = True) -> dict[str, Any] | None:
    """运行一个子 Agent：检索 → 整合 → 产出证据与模块报告

    rework_hint：质检返工时的致命项，注入整合指令要求子Agent修正。
    write=False：**只检索、不出章**（同一维度的前序轮次）——写作权归该维度的最终版独占，
    从源头消灭"同一指标两套数"（见 graph._chapter_key 病案）。此时 module_report 返回空串。
    """
    # 问答→源硬路由（数据源注册表）：先判结论类型 → 定必选源层 → 传给检索层决定权威源是否必选。
    # 放在检索之前，是因为"该用哪一层源"是**取证前**就该定的策略，不是事后解释。
    # 引擎侧层级越界标记的收集器（见 `_build_tam_calc` 的 `viol_sink` 说明）。
    # 定义在 try 之外：异常路径也要能返回这个键（缺键与"没越界"不能同形）。
    _viol_sink: list[str] = []
    _route = None
    try:
        from core.source_registry import route_query

        _route = route_query(
            (context or {}).get("raw_question", "") or "",
            subtask.description or "",
            list(subtask.search_queries or []),
        )
        if context is not None:
            context["_route"] = _route
    except Exception:
        _route = None

    # 工具自主选择（core/tool_select.py）：在"合格层级"之上，让模型**自己挑这次实际打哪几个源**。
    # 与上一步 `route_query` 是串联关系：route_query 定**资格线**（这类结论必须有 L1/L2），
    # 本步在线上挑**具体工具**。`SourceSpec.feeds` 到这一步才第一次真正参与路由。
    # 失败/无模型 → `None` → 取证层退回"全打"（fail-open，宁可多打也不能缺证据）。
    try:
        from core.tool_select import select_tools

        _tp = select_tools(
            (context or {}).get("raw_question", "") or "",
            subtask.description or "",
            list(subtask.search_queries or []),
            required_layer=getattr(_route, "required_layer", "") or "",
            claim_label=getattr(_route, "claim_label", "") or "",
        )
        if context is not None:
            context["_tools"] = _tp.to_dict() if _tp else None
            # ── 旁路：**按子任务 id 隔离**（见 `_only_tools_for` 的病案）──
            # 原槽位照旧写：决策链的 `tool_plan`（`_search_mcp` 的返回值）与
            # `_fulltest` 的一条锁都还在用它，改形状会同时动到那两处。
            # 所以这里只**新增**一张表，取证层优先读它。
            # 选择失败（`_tp` 为空）时**不写**这一格 → 该子任务读到 None → 全打（fail-open），
            # 这正是"没选出来就别限制"的语义，也是原实现的语义。
            if _tp:
                context.setdefault("_tools_by_subtask", {})[
                    getattr(subtask, "id", "") or ""
                ] = _tp.to_dict()
    except Exception as e:
        logger.info("工具自主选择跳过（fail-open，退回全打）: %s", e)
        if context is not None:
            context["_tools"] = None

    try:
        raw = _search_mcp(subtask, context)
    except Exception as e:
        logger.error("子Agent[%s] 检索抛异常: %s", subtask.id, e, exc_info=True)
        # 检索失败 → 降级：产出空模块报告（标注数据缺失，不崩）
        return {
            "evidences": [],
            "module_report": f"## {subtask.description}\n（检索失败: {e}）",
            "route": _route,
        }

    # 同维度合并（方案 A）：被取代轮次检索到的原始资料并入本章的**资料池**。
    # 写作与引文用合并池（本章是该维度的唯一一章，来源当然要覆盖两轮检索）；
    # 而**证据仍只用本章自己那一轮**——否则同一批来源会在证据矩阵/claims.csv 里出现两次。
    _merge_raw = (context or {}).get("_merge_raw") or []
    raw_for_text = (list(_merge_raw) + list(raw)) if _merge_raw else raw
    # 来源质量门要用的问题原文（跨垂类门靠"本次调研是什么行业"判断），
    # 取不到就退回子任务描述——宁可判得粗，也不要因为拿不到问题就整个门失效。
    _q = (context or {}).get("raw_question", "") or subtask.description or ""
    if _merge_raw:
        logger.info(
            "同维度合并[%s]：并入前序轮次原始资料 %d 条 → 资料池 %d 条",
            subtask.id, len(_merge_raw), len(raw_for_text),
        )

    try:
        framework = _detect_framework(subtask.description, agent_type)
        # Stateful HITL：用户在 StopCard 手动补的数据，作为最高优先级约束流入整合
        manual = (context or {}).get("rework_manual", "") or ""
        # 行业基线注入（数据护城河：复用上次调研沉淀的行业卡片）
        brief = (context or {}).get("industry_brief", "") or ""
        # 已归档指标锚（指标时序库）：同行业既往调研确立的指标口径 → 跨报告锁值
        _m_anchor = (context or {}).get("metric_anchor", "") or ""
        # 决策视角锁定（v0.9.42）：与执行摘要共用同一决策主体，防章节主体漂移
        viewpoint = ""
        try:
            from core.viewpoint import viewpoint_block

            viewpoint = viewpoint_block(
                (context or {}).get("raw_question", "") or "", context
            )
        except Exception:
            viewpoint = ""
        if not write:
            # 被取代的轮次：只检索、不出章（证据与引文照常产出，供本章最终版合并使用）
            module_report = ""
            evidences = _build_evidence(subtask, raw, industry=industry, question=_q)
            citations = _build_citations(raw, industry=industry, question=_q, subtask_id=subtask.id)
        else:
            module_report = _integrate(
                subtask, agent_type, raw_for_text, industry=industry, framework=framework,
                rework_hint=rework_hint, manual=manual, brief=brief, viewpoint=viewpoint,
                metric_anchor=_m_anchor, exp_brief=(context or {}).get("exp_brief", "") or "",
                viol_sink=_viol_sink,
            )
            evidences = _build_evidence(subtask, raw, industry=industry, question=_q)
            citations = _build_citations(raw_for_text, industry=industry, question=_q, subtask_id=subtask.id)
    except Exception as e:
        # 整合/构建环节异常：打堆栈（不静默），证据尽力产出
        logger.error("子Agent[%s] 整合/构建抛异常: %s", subtask.id, e, exc_info=True)
        evidences = _build_evidence(subtask, raw, industry=industry, question=_q) or []
        module_report = "" if not write else f"## {subtask.description}\n（整合失败: {e}）"
        citations = _build_citations(raw_for_text, industry=industry, question=_q, subtask_id=subtask.id)

    # 路由命中回填（可审计）：按**实际拿到的 URL 经白名单路由后的层**判定是否命中必选源层
    if _route is not None:
        try:
            from core.source_registry import mark_hit

            mark_hit(_route, citations)
            logger.info("源路由[%s] %s", subtask.id, _route.audit_line())
        except Exception:
            pass

    return {
        "evidences": evidences,
        "module_report": module_report,
        "citations": citations,
        "route": _route,
        # 引擎侧层级越界（TAM/SAM/SOM 三层不严格递减）——**我们自己算出来的确定性事实**，
        # 必须走到决策完整性闸门，否则那一层"引擎自报越界"永远是空的（见 `_build_tam_calc`）。
        "sizing_violation": "；".join(_viol_sink),
        # 本次的工具选择结果（可能为 None = 未执行 → 全打）。
        # 带回去是为了让 graph 能在**自主决策链**里记下"这次为什么只打这几个源"——
        # 工具选择如果只发生在内部、不出现在任何交付物里，那它和没做没有区别。
        "tool_plan": (context or {}).get("_tools"),
        # 原始取证记录：**可复算包的原料**——第三方要从"检索层真实返回了什么"一路复算到报告结论，
        # 而不是只看我们整理过的结论。仅截断长度，不改内容（由 graph 侧瘦身后入 state）。
        # 出章的那一轮返回的是**合并后的资料池**（本章正文与引文都出自它），
        # 可复算包才对得上正文；被取代轮次返回自己的原始记录，由 graph 并入最终版。
        "raw": raw_for_text,
        # 来源质量门拦下的页（url, 原因）：交付期汇总成一行显示，
        # 让"来源怎么少了"永远有答案——是**被拦了**，不是没搜到。
        "rejected": _REJECT_LOG.pop(subtask.id, []),
    }
