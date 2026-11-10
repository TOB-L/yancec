"""研策 · MCP 数据源层

数据获取策略（v3 §3.5，多级降级，杜绝演示翻车）：
- P0 主力：通义千问 qwen-plus enable_search=true —— 真实联网，返回带来源的实时信息
- P1 增强：RAG 资产库（历史调研复用，跨任务）
- P2 保底：DeepSeek 内置知识（无千问 Key 时降级，诚实标注数据时间）

工具调用（区别于问答机器人的关键）：
- search_with_llm(query)       → 联网检索市场信息（真实时）
- search_multi(query, facets)  → 多角度检索（价格带/竞品/政策/用户口碑）
- understand_image(image_b64)  → 图像理解（价目表/产品图，走 Qwen-VL）
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from core.config import get_settings
from core.llm import get_vlm_llm, _use_qwen

logger = logging.getLogger(__name__)

# 千问 enable_search 并发上限：避免并行子Agent同时联网触发限流/排队超时
# 信号量跨线程共享，最多2路联网并行
_search_semaphore = __import__("threading").Semaphore(2)


def search_with_llm(query: str, max_tokens: int = 1500) -> str | None:
    """联网检索市场信息（兼容旧签名：只返回文本）

    主力：千问 enable_search 真实时；千问失败自动降级 DeepSeek 内置知识。
    失败返回 None → 上层降级。
    """
    r = search_with_llm_rich(query, max_tokens=max_tokens)
    return (r or {}).get("content")


def search_with_llm_rich(query: str, max_tokens: int = 1500) -> dict[str, Any] | None:
    """联网检索（富结果）：返回 {"content": 分析文本, "urls": [真实来源链接]}

    - 千问 enable_search 响应带 search_info.search_results → 权威来源URL（可点开溯源）
    - 千问失败（限流/超时/无结果）→ 自动降级 DeepSeek，保住证据量（对齐 Tool Contract 声明）
    - 全失败 → None（上层降级为"该维度暂无数据"，不崩溃）

    返回体里带 `"channel"`：`qwen` = **千问联网真答的**（哪怕没带回 search_info 来源，
    正文仍是**实时检索**结果）；`deepseek` = 千问失败后的**离线内置知识**降级。
    ⚠️ 这个字段是给调用台账用的，**不能靠 `urls == []` 去反推**（2026-09-18 实跑踩到）：
    千问经常"答了但不给来源"（日志原话「千问0来源/0URL + Bing3条」），
    按 urls 为空就判成 DeepSeek 降级 = **对一条真实联网检索的假指控**。
    「哪条通道答的」只有这一层知道，所以由这一层写下来。
    """
    if _use_qwen():
        result = _qwen_search(query, max_tokens=max_tokens)
        if result and result["content"] and result["content"].strip():
            result["channel"] = "qwen"
            return result
        # 千问失败 → 自动降级 DeepSeek（修复：原实现直接返回 None，导致子Agent大量0证据）
        logger.warning("千问联网检索失败，自动降级 DeepSeek：%s", query)
        content = _deepseek_search(query, max_tokens=max_tokens)
        if content and content.strip():
            # DeepSeek 为离线知识：正文里的 URL 无搜索服务背书（记忆编造/过时），不给可点源，只留内容
            return {"content": content, "urls": [], "sources": [], "channel": "deepseek"}
        return None
    content = _deepseek_search(query, max_tokens=max_tokens)
    if content and content.strip():
        return {"content": content, "urls": [], "sources": [], "channel": "deepseek"}
    return None


def search_multi(query: str, facets: list[str] | None = None) -> list[dict[str, Any]]:
    """多角度检索：把一个主题拆成多个检索词分别搜，返回结构化结果

    facets 示例：["市场规模", "竞争格局", "用户口碑", "政策"]
    → 每个 facet 返回 {query, title, url, content, publisher, source_type}
    """
    from core.rag import search_assets

    results: list[dict[str, Any]] = []

    # P1 增强：先查资产库（历史复用，省token）
    industry = _extract_industry(query)
    if industry:
        try:
            for hit in search_assets(query, industry=industry, n_results=2)[:1]:
                if hit.get("content"):
                    results.append(
                        {
                            "query": query,
                            "title": hit.get("source_title") or hit.get("industry", ""),
                            "url": hit.get("source_url") or "",
                            "content": hit["content"],
                            "publisher": "历史资产",
                            "date": hit.get("created_at", ""),
                            "source_type": "asset",
                        }
                    )
        except Exception:
            pass

    # P0 主力：真实联网检索每个角度
    queries = [f"{query} {f}" for f in (facets or [query])]
    for q in queries[:4]:
        try:
            rich = search_with_llm_rich(q, max_tokens=1500)
        except Exception as e:
            logger.warning("检索失败 %s: %s", q, e)
            rich = None
        if rich and rich.get("content") and len(rich["content"].strip()) > 50:
            results.append(
                {
                    "query": q,
                    "title": q,
                    "url": rich["urls"][0] if rich.get("urls") else "",
                    "urls": rich.get("urls") or [],
                    "content": rich["content"],
                    "publisher": "通义千问实时检索",
                    "date": "实时",
                    "source_type": "llm_search",
                }
            )
    return results


def fetch_page(url: str, max_chars: int = 3000) -> dict[str, Any] | None:
    """真实网页抓取：提取标题+正文纯文本（政府官网/统计局/研报页等权威页）

    真·外部工具调用实证：子Agent对权威数据页发起真实 HTTP 抓取，
    区分"问答机器人"（只会调LLM）与"数字员工"（能调外部工具）。
    失败返回 None（反爬/编码/网络问题都不崩，上层降级）。

    ## 合规（2026-09-15 外部审计补齐）

    这里原先是一发**裸 httpx.get + 假 Chrome UA**：没有 robots 门、没有每主机令牌桶，
    而它是**正文抓取的主路径**（`agents/researchers.py` 抓研报页走的就是它）。
    同一个项目里，核验层（`core/verification._http_get`）在 2026-09-14 已改成
    「robots + 限速 + 声明式 UA」，而这里是漏掉的那一处——
    **"只治了一处"这个病，本仓库已经不是第一次犯了。**

    现在改成与权威源层、核验层**同一把尺**：
      ① `_compliance_skip_reason(url)`：robots Disallow / SEC 未配联系方式 → **不发请求**；
      ② `_throttle(url)`：每主机 ≥1s 令牌桶；
      ③ `_ua_for(url)`：声明身份的 UA（`yancec-research-bot/1.0`），SEC 走带联系方式的 UA。
    **代价如实说**：对 UA 挑站的站点，抓取成功率可能下降——上层会降级为"无正文"。
    这是拿一点覆盖率换"我们真的按自己说的做"，这个交换是本产品的立场，不是权衡。
    """
    import re
    from html.parser import HTMLParser

    try:
        from core.authority_fetch import _throttle
        from core.verification import _compliance_skip_reason, _ua_for

        _why = _compliance_skip_reason(url)
        if _why:
            logger.info("合规跳过网页抓取 %s：%s", url, _why)
            return None
        _throttle(url)
        _ua = _ua_for(url)
    except Exception:
        # 合规层不可用（导入失败）时**不静默放行**：宁可不抓，也不做一次没守规矩的抓取。
        logger.warning("合规层不可用，本次不抓取（宁可不抓也不越线）: %s", url)
        return None

    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _ua, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=25,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text[:200000]
    except Exception as e:
        logger.warning("网页抓取失败 %s: %s", url, e)
        return None

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip_depth = 0
            self.texts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "footer", "header", "noscript"):
                self.skip_depth += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "footer", "header", "noscript"):
                self.skip_depth = max(0, self.skip_depth - 1)

        def handle_data(self, data):
            if not self.skip_depth:
                t = data.strip()
                if t:
                    self.texts.append(t)

    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    body = re.sub(r"\s+", " ", " ".join(parser.texts)).strip()
    if not body:
        return None
    title = ""
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
    if tm:
        title = tm.group(1).strip()
    return {"title": title[:120], "url": url, "content": body[:max_chars]}


# 垃圾/误导域名黑名单（模块级，供 web_search_urls 与 _qwen_search 共用）：
# 游戏站（"我的世界官网"误抓根因）+ 视频/短视频站（B站视频混入数据源）
# + 搜索聚合/广告追踪。宁空勿错。
BAD_URL_DOMAINS = (
    "baidu.com", "bing.com", "bingj.com", "bing.net", "microsoft.com",
    "cnzz.com", "51.la", "hm.baidu.com", "sogou.com", "so.com",
    "minecraft", "mojang", "java.net", "gamepedia", "fandom",
    "steampowered.com", "epicgames.com", "playstation.com", "nintendo.com",
    "roblox.com", "fortnite.com",
    # 视频/短视频站：市场调研不要视频链接（Bing 对中文查询常返回 B站视频）
    "bilibili.com/video", "/video/BV", "bilibili.com/list",
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "youku.com", "v.youku.com", "iqiyi.com", "v.qq.com", "video.qq.com",
    "mgtv.com", "douyin.com", "kuaishou.com", "ixigua.com", "vimeo.com",
    # 音乐/直播站（网易云音乐混入数据源）
    "music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "xiami.com",
    "migu.cn", "spotify.com", "itunes.apple.com",
    # 词典/词条/问答/文档聚合：不是调研信源（与 researchers._JUNK_URL_MARKERS 同源防线）
    # 实测 Bing 曾对冷门检索词整批返回字典SEO站（hgcha/hanyuguoxue/gushici/dict.revised）
    "zdic.net", "cidian", "hanyu", "hanzi", "zidian", "iciba", "dict.cn",
    "dict.revised.moe.edu.tw", "hgcha.com", "hanyuguoxue.com", "gushici.net",
    "/zidian/", "/cidian/",
    "baike.baidu", "zhidao.baidu", "wenku.baidu", "baike.com",
    "doc88.com", "docin.com", "book118.com", "chazidian", "pinyin",
    # 报告聚合/转载站（博查实测返回 waitang/wkzk/lps114/0555mas 等）→ 二手转载，非一手源，拦截
    "waitang.com", "wkzk.com", "lps114.com", "0555mas.com",
    # UGC 问答/专栏（zhihu 风控 403、核验永远失效→只会显示"已转文字"且拖低可点率；T5 源，不进研报）
    # + gov.cn big5 网关壳（编码代理页，非正文）。宁缺勿滥。
    "zhihu.com", "big5.www.gov.cn", "cmsq.org.cn",
    # CSDN 博客：T5 技术博客/UGC（实测 HTTP 521 风控 + "据blog.csdn.net调研数据" 混入正文，无专业信源价值）
    "csdn.net", "blog.csdn.net",
)


def _api_structured_results(
    query: str, n: int = 6, trace: list[dict[str, Any]] | None = None, subtask: str = ""
) -> list[dict[str, str]]:
    """结构化搜索 API 真源供应：博查 AI Web Search → 微软 Bing Web Search API。

    千问 enable_search 的 search_info 在 qwen-plus 上也长期为空（backend.err.log 实证），
    此前只能靠 Bing 无头抓取/正文自抠 → "固定低质集 / 幻觉URL" 反复翻车。
    这两个 API 返回结构化 {标题, URL, 站名}，中文质量高，是"可点真源"的正主。
    都未配置/失败 → 返回 []，调用方回退原 Bing 抓取（保留三级降级）。

    `trace`：本次调研的**调用台账**。2026-09-18 补——**这是台账此前最大的一个洞**：
    报告里 23 条引文（艾瑞/界面/36氪/每经…）**全部是从这条通道进来的**，
    而《本次数据源调用台账》只列了 9 个权威源、一个检索通道都没有。
    读者/复核者问「艾瑞那 3 条是哪次调用来的」，台账里查无此行——
    而这一层恰恰是「可审计」最承重的一块砖。
    **接线点选在这里**（而不是调用方）：只有这一层知道究竟是博查还是 Bing 答的。
    """
    from urllib.parse import urlparse

    from core.config import get_settings

    s = get_settings()

    def _rec(src: str, name: str, items: list | None, note: str = "", layer: str = "L4") -> None:
        """记一条检索通道的调用。失败不阻断检索（台账是旁证，不是主链路）。

        `subtask` 一并写进备注：**"哪次调用"要具体到子任务**——
        「艾瑞那 3 条是哪次调用来的」这个问题，答"结构化检索通道"不够，
        要能答"st_2 的机构定向补源那一次」。
        """
        try:
            from core.source_health import record as _r

            _n = f"{note}｜子任务 {subtask}" if subtask else note
            _r(trace, src, name, items, note=_n, layer=layer)
        except Exception:
            pass

    def _is_mobile(u: str) -> bool:
        # m. 开头且非 mof/mon… 官方域名 → 移动镜像（同文时 desktop 优先，占同一名额）
        try:
            h = (urlparse(u).netloc or "").lower().split(":")[0].split("@")[-1]
        except Exception:
            return False
        return h.startswith("m.") and not h.startswith("mof") and not h.startswith("mo")

    def _clean(items: list) -> list[dict[str, str]]:
        # 去重只认"规范键"：m.163/www.163 同一篇、sohu 带不同 pvid 的双链 → 合一，
        # 否则同一篇文章会反复占掉可点名额，把真·多机构挤掉（"只有三个官网"的病根）。
        from core.source_util import canonical_key

        best: dict[tuple[str, str], dict[str, str]] = {}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            u = (it.get("url") or "").strip()
            if not u.startswith("http") or any(b in u for b in BAD_URL_DOMAINS):
                continue
            ck = canonical_key(u)
            if not ck[0]:
                continue
            site = (it.get("siteName") or it.get("site_name") or it.get("displayDomain") or "").strip()
            if not site:
                try:
                    site = (urlparse(u).netloc or "").removeprefix("www.")
                except Exception:
                    site = ""
            item = {
                "site_name": site[:40],
                "title": (it.get("name") or it.get("title") or "").strip()[:80],
                "url": u,
                # 收回发布日期（博查返回 datePublished）——此前被整条丢弃，
                # 导致时效无法约束、报告里混入 2023/2024 旧文。取前 10 位 = YYYY-MM-DD。
                "date": str(
                    it.get("datePublished") or it.get("dateLastCrawled") or it.get("date") or ""
                )[:10],
            }
            prev = best.get(ck)
            if prev is None or (_is_mobile(prev["url"]) and not _is_mobile(u)):
                best[ck] = item  # 同文镜像：desktop/www 版覆盖 m. 版
        return list(best.values())[:n]

    # ① 博查（中文最优，专为 Agent 搜索设计）
    if s.bocha_api_key:
        try:
            # 时效优先：先要"近一年"；若太窄搜空 → 回退 noLimit（保证不因时效门把结果搜没）。
            # 旧实现写死 freshness=noLimit = 主动放弃时效约束，是"报告里混 2023/2024 旧文"的直接病根之一。
            for _fresh in ("oneYear", "noLimit"):
                resp = None
                # 传输层失败（本地 DNS / 连接）**重试一次**——注意这不违反"遇风控立即停手"：
                # 那条针对的是**源站拒绝**（403/412），而 `getaddrinfo failed` 是**本地解析器**
                # 的问题，请求根本没到源站，重试不构成骚扰。
                # 实测（2026-09-14 真实端到端）：一次调研里博查连续 5 次 DNS 解析失败，
                # 整条结构化检索通道静默失效（好在有 Bing 兜底），报告来源多样性下降却毫无提示。
                for _try in (0, 1):
                    try:
                        resp = httpx.post(
                            "https://api.bocha.cn/v1/web-search",
                            headers={"Authorization": f"Bearer {s.bocha_api_key}", "Content-Type": "application/json"},
                            json={"query": query, "summary": False, "freshness": _fresh, "count": max(n * 2, 10)},
                            timeout=15,
                        )
                        break
                    except httpx.TransportError as _te:
                        if _try == 0:
                            logger.info("博查连接层失败(%s)，1s 后重试一次", type(_te).__name__)
                            time.sleep(1.0)
                            continue
                        raise
                if resp is None:
                    break
                if resp.status_code != 200:
                    logger.warning("博查搜索失败 http=%s body=%r", resp.status_code, resp.text[:200])
                    break
                data = resp.json().get("data") or {}
                out = _clean((data.get("webPages") or {}).get("value"))
                if out:
                    logger.info("博查结构化命中%d条(freshness=%s): %r", len(out), _fresh, [x["url"][:56] for x in out[:3]])
                    _rec("bocha", "博查 AI Web Search", out, note=f"结构化检索通道（freshness={_fresh}）")
                    return out
                logger.warning("博查搜索空结果 freshness=%s http=%s query=%r", _fresh, resp.status_code, query[:30])
        except Exception as e:
            logger.warning("博查搜索异常: %s", e)

    # ② 微软 Bing Web Search API v7（免费层）
    if s.bing_search_api_key:
        try:
            from urllib.parse import quote as _q

            resp = httpx.get(
                f"https://api.bing.microsoft.com/v7.0/search?q={_q(query)}&count={max(n * 2, 10)}&mkt=zh-CN&responseFilter=Webpages",
                headers={"Ocp-Apim-Subscription-Key": s.bing_search_api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                web = ((resp.json().get("webPages") or {}).get("value")) or []
                out = _clean(
                    [{"url": x.get("url"), "name": x.get("name"), "displayDomain": (x.get("displayUrl") or "")} for x in web]
                )
                if out:
                    logger.info("Bing Web API 命中%d条: %r", len(out), [x["url"][:56] for x in out[:3]])
                    _rec("bing_web", "Bing Web Search API", out, note="结构化检索通道（博查未命中后的降级）")
                    return out
                logger.warning("Bing Web API 空结果 http=%s", resp.status_code)
            else:
                logger.warning("Bing Web API 失败 http=%s body=%r", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Bing Web API 异常: %s", e)
    # 三条通道都没给出可点真源 → **照实记一条回空**，不假装这条通道不存在。
    # 台账的意义就是分清"查了没查到"与"根本没查"。
    _rec(
        "structured_search",
        "结构化检索通道（博查 / Bing Web API）",
        [],
        note="两条结构化通道本次均未返回可用结果"
        + ("（未配置 key）" if not (s.bocha_api_key or s.bing_search_api_key) else ""),
    )
    return []

# ============================================================
# Bing 结果页抓取闸门（2026-09-10 收紧）
# ------------------------------------------------------------
# 背景：无 Bing key 时，抓 cn.bing.com/search 结果页是"把结构化 URL 补齐"的兜底通道，
# 但实测**单次跑题被打 70 次**（14 个子任务 × 主/反证/机构定向几个入口），量偏大、
# 且属合规灰区。收紧为三道闸，任何一道先触发就**降级返回博查结果**（不报错、不空手）：
#   1) 同题缓存：同 (query,n) 10 分钟内不再抓（子任务/机构重名重复调用免费）
#   2) 滑窗配额：每 15 分钟进程内最多 BING_SCRAPE_MAX 次真实抓取（默认 30，即约减 6 成）
#   3) 礼貌间隔：两次真实抓取之间 ≥BING_SCRAPE_GAP 秒（默认 1.2s，串行不并发打引擎）
# 环境变量可覆盖；BING_SCRAPE_MAX=0 表示完全不再抓 Bing（只用博查结构化 API）。
# ============================================================
_BING_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, str]]]] = {}
_BING_CACHE_TTL = 600.0
_BING_HITS: list[float] = []
_BING_WINDOW_S = 900.0
_BING_LOCK = threading.Lock()
_BING_LAST = 0.0


def _bing_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except Exception:
        return default


def _bing_gap() -> float:
    try:
        return max(0.0, float(os.environ.get("BING_SCRAPE_GAP", "1.2")))
    except Exception:
        return 1.2


def web_search_cache_put(key: tuple[str, int], val: list[dict[str, str]]) -> list[dict[str, str]]:
    """写同题缓存并返回（缓存的是**最终合并结果**，让重复调用完全免费）"""
    try:
        _BING_CACHE[key] = (time.monotonic(), list(val))
    except Exception:
        pass
    return val


def _bing_admit() -> bool:
    """是否允许这次真实抓取（配额+礼貌间隔）；不允许 → 上层降级用博查结果"""
    global _BING_LAST
    cap = _bing_int_env("BING_SCRAPE_MAX", 30)
    now = time.monotonic()
    with _BING_LOCK:
        _BING_HITS[:] = [t for t in _BING_HITS if now - t < _BING_WINDOW_S]
        if len(_BING_HITS) >= cap:
            return False
        gap = _bing_gap() - (now - _BING_LAST)
        if gap > 0:
            time.sleep(min(gap, 3.0))  # 礼貌间隔（上限 3s，避免拖垮主流程）
        _BING_HITS.append(time.monotonic())
        _BING_LAST = time.monotonic()
        return True


def web_search_urls(
    query: str, n: int = 3, trace: list[dict[str, Any]] | None = None, subtask: str = ""
) -> list[dict[str, str]]:
    """真实搜索引擎结果：结构化搜索API（博查/Bing Web）→ 原 Bing 抓取兜底 → 解码重定向 → 提取真实URL

    解决核心痛点：千问 enable_search 可能不返回 search_info →
    证据没有真实可点URL → 右侧数据源面板只能显示纯文字摘要。
    修复"全是百度"：换国际版 www.bing.com（结果更多样）+ 解码 ck/a 重定向
    + 过滤百度/搜索聚合域名，保留 gov.cn/新闻/行业站等权威来源。
    失败返回 []，上层保留联网检索文本不阻断。

    `trace`：本次调研的调用台账（见 `_api_structured_results` 的说明）。
    **同题缓存命中时不记**——缓存命中不是一次请求，记进去会把台账的"调用次数"灌水，
    而那一列存在的意义正是"真的打了几次源站"。
    """
    # 同题缓存（闸门1）：命中直接返回，不再打任何外部接口
    _ck_key = (query or "", int(n))
    _hit = _BING_CACHE.get(_ck_key)
    if _hit and (time.monotonic() - _hit[0]) < _BING_CACHE_TTL:
        return list(_hit[1])
    # 结构化搜索 API 主供应（博查/Bing Web）：真实可点页（带站名/标题）。
    # 注意：博查命中后不早退——政策/统计类题需 gov 官方源，Bing 抓取仍要补跑再合并。
    # 多取候选(2n)：品牌/官网即使霸榜，也会被下方"每域名≤2"的分散配额挤出位置，给媒体/机构让位
    _bocha: list[dict[str, str]] = _api_structured_results(
        query, max(n * 2, 6), trace=trace, subtask=subtask
    )
    # 政策/统计类题（含 政策/扶持/监管/补贴/规划/条例/统计 等词）补一次 gov 定向检索：
    # 把部委具体文件页（民航局 caac / 发改委 ndrc / 工信部 miit / 民政部 mca…）顶上来，
    # 避免面板被 SEO 垂直站(如 lrsd.org.cn)填满。
    _POLICY_KW = ("政策", "扶持", "监管", "补贴", "规划", "条例", "通知", "办法", "试点", "规定", "标准", "统计", "支持")
    if any(k in query for k in _POLICY_KW) and len(_bocha) < n:
        try:
            for _e in _api_structured_results(
                query + " 官方文件 site:gov.cn", max(n, 6), trace=trace, subtask=subtask
            ):
                if _e.get("url") and not any(_x.get("url") == _e.get("url") for _x in _bocha):
                    _bocha.append(_e)
        except Exception:
            pass
    import base64
    import random
    import re

    # 桌面 UA 池轮换：反爬（WinError 10054 断连）时换 UA 重试
    #
    # ⚠️ **这是全项目唯一一处"用浏览器 UA + 会换 UA"的地方，是刻意保留的例外，必须如实披露**
    # （2026-09-15 外部审计挖出，材料原先写的是"声明式 UA（不伪装 Chrome）· 不换 UA"——
    #  与这里的实际行为不符，属**对外承诺说了不做**）。
    # 为什么保留：搜索引擎对非浏览器 UA 返回的标记结构不同，声明式 UA 下**解析不出结果**，
    # 整条搜索兜底会失效。可行性上只有两个选项，我们选了有代价的那个：
    #   · 用浏览器 UA + 只在**连接层异常**时换一次（现状）
    #   · 干脆不做搜索结果页解析（检索覆盖率会明显下降）
    # 保留的边界条件（都由下面的代码保证）：
    #   ① **遇 403/412 立即停手**，不换 UA、不重试 —— 源站明确拒绝不是"反爬误伤"；
    #   ② 换 UA **只发生在连接层异常**（DNS/超时/重置），不是"被拒了换个身份再敲"；
    #   ③ 受 `_bing_admit()` 的**滑窗配额 + 礼貌间隔**约束（默认 30 次/窗口、≥1.2s 间隔）；
    #   ④ Bing 只是**兜底**：博查/Bing Web API 有结果时只试 1 个 UA，结构化通道优先。
    # 材料侧同步改为"分级披露"（创意描述/方案书），不再笼统写"所有出网请求、不换 UA"。
    _UAS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.123 Safari/537.36 Edg/119.0.2151.72",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    ]

    html = ""
    # 闸门2/3：滑窗配额 + 礼貌间隔。不允许 → 直接降级用博查结构化结果（不报错、不空手）
    if not _bing_admit():
        logger.info(
            "Bing抓取已达配额/间隔限制，降级仅用结构化API结果(%d条): %r", len(_bocha), query[:30]
        )
        return web_search_cache_put(_ck_key, (_bocha or [])[:n])
    # 博查已命中时，Bing 抓取只是 gov 补充 → 只试 1 个 UA；否则(无结构化结果)才完整重试
    _rounds = 1 if _bocha else len(_UAS)
    for attempt, ua in enumerate(random.sample(_UAS, _rounds)):
        try:
            # 直接请求 cn.bing.com（绕过 www.bing.com → cn.bing.com 的 302，重定向易丢 cookie/解码失败）
            resp = httpx.get(
                f"https://cn.bing.com/search?q={quote(query)}&count={n * 5}&setmkt=zh-CN&setlang=zh-hans",
                headers={
                    "User-Agent": ua,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=20,
                follow_redirects=True,
            )
            resp.raise_for_status()
            html = resp.text
            break
        except httpx.HTTPStatusError as e:
            # **源站明确拒绝（403/412/429）→ 立即停手**，不换 UA、不重试。
            # 本项目红线（见 core/source_health.py 顶部）：「遇 403/412 立刻停手…
            # 不重试、不换 UA」，而这段原先把 HTTPStatusError 和连接异常一起接住，
            # 403 之后照样轮换下一个浏览器 UA 再敲一次——**换来的是更响的"被拒"信号**。
            # （`_UAS` 是 4 个浏览器 UA；只有博查空手时才会走到多轮。）
            logger.warning("Bing拒绝访问(HTTP %s)，按红线停手不重试", getattr(e.response, "status_code", "?"))
            html = ""
            break
        except Exception as e:
            # 连接层（DNS/超时/重置）**不是源站在拒绝**，换个 UA 重试是合理的
            logger.warning("Bing搜索抓取失败(%d/%d) %s: %s", attempt + 1, len(_UAS), e.__class__.__name__, e)
            html = ""
    if not html:
        return web_search_cache_put(_ck_key, (_bocha or [])[:n])

    def _real_url(href: str) -> str:
        """解码 Bing 重定向链接 → 真实 URL（支持 u=a1 base64 与 url/RU 参数）"""
        if "bing.com" not in href and "bingj.com" not in href:
            return href  # 已是真实 URL
        # 格式1: u=a1<base64>
        m = re.search(r"[?&]u=a1([A-Za-z0-9+/=_\-]+)", href)
        if m:
            try:
                raw = m.group(1)
                pad = "=" * ((4 - len(raw) % 4) % 4)
                u = base64.b64decode(raw + pad).decode("utf-8")
                u = re.split(r"[&)]", u)[0]
                if u.startswith("http"):
                    return u
            except Exception:
                pass
        # 格式2: url=/rmsru=/RU=/ru= 明文字段
        for key in ("rmsru=", "ur=", "url=", "RU=", "ru="):
            m = re.search(key + r"([^&\s]+)", href)
            if m:
                try:
                    from urllib.parse import unquote

                    u = unquote(m.group(1))
                    if u.startswith("http"):
                        return u
                except Exception:
                    pass
        return href

    # 关键：只提取 Bing 真实搜索结果块 <li class="b_algo"> 里的标题+链接。
    # 之前抓所有 <h2><a> → 抓到必应页面的"热门推荐/游戏"区块（如"我的世界官网"）→ 全错。
    # 无 b_algo（反爬降级页/结构变化）→ 宁空勿错，返回空（上层保留联网检索文本）。
    from core.source_util import canonical_key as _canon_key

    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
    if not blocks:
        logger.warning("Bing未返回真实搜索结果块(b_algo)，仅保留结构化API结果: %r", query[:30])
        return web_search_cache_put(_ck_key, (_bocha or [])[:n])
    for block in blocks:
        m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        raw_href = m.group(1)
        url = _real_url(raw_href)
        if "bing.com" in url:
            logger.warning("Bing解码失败: href=%r", raw_href[:200])
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not title or not url.startswith("http"):
            continue
        if any(b in url for b in BAD_URL_DOMAINS):
            continue
        ck = _canon_key(url)
        if not ck[0] or ck in seen or len(results) >= n:
            continue  # 同文镜像（m./www./pvid 差异）只留一条
        seen.add(ck)
        results.append({"site_name": "", "title": title[:80], "url": url})
    if not results:
        # 诊断：0 条时打印原始结构，定位是解码失败还是全被黑名单过滤
        logger.warning(
            "Bing解析0条: blocks=%d | 首block前200字=%r | html前120字=%r",
            len(blocks), blocks[0][:200] if blocks else "", html[:120],
        )
    # 合并：权威排序 + 根域配额，一次到位治"单一站霸榜"。
    #  m.163.com 与 163.com 属同一可注册根域 → 合并限流 2 条，星巴克五兄弟同属 starbucks.com.cn 同理；
    #  权威(白名单研究/媒体)排在 163/sohu 之前。
    from urllib.parse import urlparse as _up

    _MULTI_TLD = ("com.cn", "org.cn", "net.cn", "gov.cn", "edu.cn", "ac.cn", "com.hk", "com.tw", "co.jp")

    def _root(u: str) -> str:
        try:
            h = (_up(u).hostname or "").lower().removeprefix("www.").split(":")[0]
        except Exception:
            return ""
        parts = h.split(".")
        if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else h

    def _score(u: str) -> int:
        """排序分：0=官方(gov/edu)，1=白名单权威(研究/财经媒体/平台)，2=其它(网易/搜狐号/聚合…)"""
        h = (_up(u).netloc or "").lower().split(":")[0]
        if h.endswith("gov.cn") or ".gov" in h or h.endswith("edu.cn"):
            return 0
        try:
            from core.source_router import TIER1_DOMAINS, TIER2_DOMAINS
        except Exception:
            TIER1_DOMAINS, TIER2_DOMAINS = (), ()
        for d in (TIER1_DOMAINS + TIER2_DOMAINS):
            d = (d or "").strip().lower().lstrip("*.")
            if d and (h == d or h.endswith("." + d)):
                return 1
        return 2

    cands = list(results) + list(_bocha or [])
    cands.sort(key=lambda r: (_score(r.get("url", "")), 0 if r in results else 1))
    merged: list[dict[str, str]] = []
    seen_ck: set[tuple[str, str]] = set()  # 规范键去重：m.163 与 www.163 同文不再各占 1 名额
    _dom: dict[str, int] = {}
    for it in cands:
        u = (it.get("url") or "").strip()
        if not u or any(b in u for b in BAD_URL_DOMAINS):
            continue
        ck = _canon_key(u)
        if not ck[0] or ck in seen_ck:
            continue
        root = _root(u)
        if not root or _dom.get(root, 0) >= 2:
            continue  # 同一可注册根域最多贡献 2 条，逼面板往多机构走
        seen_ck.add(ck)
        _dom[root] = _dom.get(root, 0) + 1
        merged.append(it)
        if len(merged) >= n:
            break
    return web_search_cache_put(_ck_key, merged[:n])


def understand_image(image_b64: str, question: str = "请描述这张图的关键内容，提取其中的数据/价格/信息") -> str:
    """图像理解：价目表/产品图/截图 → 结构化文本（Qwen-VL）

    image_b64: base64 编码的图片（前端上传后转 base64 传入）
    无千问 Key 时返回占位说明（不翻车）。
    """
    s = get_settings()
    if not (s.dashscope_api_key or s.qwen_api_key):
        return "（未配置图像理解模型，跳过图片分析）"
    try:
        llm = get_vlm_llm(temperature=0.1)
        resp = llm.invoke(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                }
            ]
        )
        return (resp.content or "").strip() or "（未能从图片提取内容）"
    except Exception as e:
        logger.warning("图像理解失败: %s", e)
        return "（图像理解失败，跳过图片分析）"


# ============================================================
# 内部实现
# ============================================================
def _qwen_search(query: str, max_tokens: int = 1500, _attempt: int = 1) -> dict[str, Any] | None:
    """千问 enable_search 联网检索（信号量限并发 + 限流/5xx 重试，最多2次）

    返回 {"content": 分析文本, "urls": [权威来源URL]}；失败返回 None。
    urls 来自 enable_search 响应里的 search_info.search_results（可点开溯源）。
    """
    s = get_settings()
    key = s.dashscope_api_key or s.qwen_api_key
    if not key:
        return None
    try:
        with _search_semaphore:
            prompt = (
                f"请作为市场调研分析师，基于【实时联网信息】回答以下调研问题：{query}\n"
                "回答要求（务必遵守）：\n"
                "1. 优先引用权威来源的数据，且**来源要多样**：政府统计/行业协会与白皮书/咨询机构报告"
                "（Euromonitor、艾瑞、GfK、IDC 等）/上市公司财报公告/头部垂直媒体——不要只给政府官网，多源交叉引用；\n"
                "2. 每条关键数据必须标注来源，格式：`[机构名](https://真实来源URL)`（markdown链接，URL必须是真实可访问的来源网页，如统计局官网/研报页/新闻页，严禁编造URL）；确无URL时写`（来源：机构名）`；\n"
                "3. 直接给出专业分析，带具体数字/事实；\n"
                "4. 数据是估算/推算的，标注'估算'；不确定的标注'暂缺可靠数据'；\n"
                "5. 不要出现'我的知识截止于XX'等元说明；\n"
                "6. 不要以'为您提供深度分析''以下是关于XX的分析'等客套话/提问回显开头，第一行直接给核心结论与数据。"
            )
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            data = None
            # ① 原生 DashScope 端点：enable_search 返回 output.search_info（唯一可靠来源）
            try:
                resp = httpx.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                    headers=headers,
                    json={
                        "model": s.qwen_search_model or s.qwen_model,
                        "input": {"messages": [{"role": "user", "content": prompt}]},
                        "parameters": {
                            "enable_search": True,
                            "result_format": "message",
                            "max_tokens": max_tokens,
                        },
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                data = None
            # ② 降级：compatible-mode 端点（实测不返回 search_info，仅 content 可提取 URL）
            if data is None:
                try:
                    resp = httpx.post(
                        f"{s.qwen_base_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": s.qwen_search_model or s.qwen_model,
                            "messages": [{"role": "user", "content": prompt}],
                            "enable_search": True,
                            "enable_thinking": False,
                            "max_tokens": max_tokens,
                        },
                        timeout=120,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    data = None
            if data is None:
                return None
            # 解析 content + search_info（原生在 output 层，compatible 在顶层）
            out = data.get("output") or {}
            choices = out.get("choices") or data.get("choices") or []
            content = ""
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
            if not content or not content.strip():
                return None
            # **检索用量必须计量**（2026-09-15 外部审计）：
            # 本函数走 httpx 直连（为了拿 enable_search 的 search_info），
            # **不过 LangChain 回调** → 检索模型的 token 从来没进过计量，
            # 于是 K17 的「单份成本」分母系统性偏小，且 `by_model` 里永远只有一个模型键。
            # 两种端点的用量字段名不同（原生 output.usage 用 input/output_tokens，
            # compatible 顶层 usage 用 prompt/completion_tokens），都要认。
            try:
                from core.llm import record_external_usage

                _u = out.get("usage") or data.get("usage") or {}
                if not isinstance(_u, dict):
                    _u = {}
                record_external_usage(
                    str(s.qwen_search_model or s.qwen_model),
                    int(_u.get("input_tokens") or _u.get("prompt_tokens") or 0),
                    int(_u.get("output_tokens") or _u.get("completion_tokens") or 0),
                )
            except Exception:
                pass  # 计量失败绝不影响检索本身
            _si_raw = (
                out.get("search_info")
                or data.get("search_info")
                or (choices[0].get("search_info") if choices else {})
                or (((choices[0].get("message") or {})).get("search_info") if choices else {})
                or (out.get("usage") or data.get("usage") or {}).get("search_info")
                or {}
            )
            _sr = _si_raw.get("search_results") or [] if isinstance(_si_raw, dict) else []
            if not _sr:
                logger.warning(
                    "千问search_info为空: 模型=%s（原生+compatible均无，改从content提取URL）| data.keys=%r out.keys=%r",
                    s.qwen_search_model or s.qwen_model,
                    list(data.keys()) if isinstance(data, dict) else "non-dict",
                    list(out.keys()) if isinstance(out, dict) else "non-dict",
                )
            import re as _re
            _content_urls = _re.findall(r"https?://[^\s\)\]\"'<>]+", content)
            if not _sr and not _content_urls:
                logger.warning("千问content也无URL（enable_search未生效）: content前200=%r", content[:200])
            urls = []
            sources = []
            try:
                for item in (_sr or []):
                    u = (item.get("url") or "").strip()
                    sn = (item.get("site_name") or "").strip()
                    t = (item.get("title") or "").strip()
                    if (
                        u.startswith("http")
                        and u not in urls
                        and not any(b in u for b in BAD_URL_DOMAINS)
                    ):
                        urls.append(u)
                        sources.append({"site_name": sn, "title": t, "url": u})
            except Exception:
                pass
            # search_info 为空：不再把 content 正文正则抠出的 URL 当"真实来源"返回（幻觉URL高发，
            # 曾致 nynct 假hex/euromonitor 文章404 直接进入可点链）。真实源页由调用侧
            # web_search_urls 补全（博查/Bing Web API 优先，抓取兜底）。
            if not urls and _content_urls:
                logger.warning(
                    "千问search_info为空: 不再自抠content URL当来源(%d个), 改由真实源通道(博查/Bing Web API/抓取)补全: %r",
                    len(_content_urls), _content_urls[:3],
                )
            return {"content": content, "urls": urls[:5], "sources": sources[:5]}
    except httpx.HTTPStatusError as e:
        # 限流/服务端抖动 → 短暂退避重试一次（多子Agent并行易触发429）
        if e.response.status_code in (429, 500, 502, 503, 504) and _attempt < 2:
            time.sleep(2)
            return _qwen_search(query, max_tokens=max_tokens, _attempt=_attempt + 1)
        logger.warning("千问联网检索失败 %s: %s", query, e)
        return None
    except Exception as e:
        logger.warning("千问联网检索失败 %s: %s", query, e)
        return None


def _extract_urls(text: str, limit: int = 3) -> list[str]:
    """从检索文本中正则提取真实URL（DeepSeek 降级时的兜底溯源）"""
    import re

    urls = re.findall(r"https?://[^\s\)\]\"'<>]+", text or "")
    seen, clean = set(), []
    for u in urls:
        u = u.rstrip("。.,，,;；)")
        if u in seen or len(u) > 200:
            continue
        seen.add(u)
        clean.append(u)
        if len(clean) >= limit:
            break
    return clean


def _deepseek_search(query: str, max_tokens: int = 800) -> str | None:
    """降级：DeepSeek 内置知识（诚实标注数据时间，不假装实时）"""
    s = get_settings()
    if not s.deepseek_api_key:
        return None
    try:
        resp = httpx.post(
            f"{s.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {s.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": s.deepseek_model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"请回答以下市场调研问题：{query}\n"
                            "回答要求：\n"
                            "1. 直接给出专业分析，给出具体数据/数字/事实，第一行直接给结论，不要客套话或复述问题；\n"
                            "2. 数据标注来源机构和年份（如'据美团2024年报告'）；\n"
                            "3. 数据是推算/估值的，标注'估算'，不确定的标注'暂缺可靠数据'。"
                        ),
                    }
                ],
                "max_tokens": max_tokens,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"].get("content")
        return None
    except Exception as e:
        logger.warning("DeepSeek检索失败 %s: %s", query, e)
        return None


def _extract_industry(text: str) -> str:
    """从文本提取行业关键词（简版规则，完整版由 LLM 提炼）"""
    industry_words = [
        "咖啡", "奶茶", "宠物", "零食", "美妆", "服饰", "母婴",
        "数码", "家电", "餐饮", "教育", "健身", "跨境", "预制菜",
        "芯片", "半导体", "AI", "人工智能", "材料", "新能源", "汽车",
    ]
    for word in industry_words:
        if word in text:
            return word
    return ""


# ============================================================
# Tool Contract（可审计业务合同）—— 对齐赛前培训"工具=可审计的业务合同"
# 四维标准：清楚 / 完整 / 可恢复 / 可审计
# 每个工具声明：目的 / 输入 / 输出 / 权限 / 失败处理
# ============================================================
TOOL_CONTRACTS: list[dict[str, Any]] = [
    {
        "name": "search_with_llm",
        "purpose": "联网检索市场信息，返回带真实来源的实时文本（主力：通义千问 enable_search）",
        "inputs": "query:str 检索词；max_tokens:int 返回上限(默认1200)",
        "outputs": "text:str|None 带来源的检索文本；失败返回 None",
        "permissions": "仅研策子Agent可调用；只读公开数据，不写入外部系统",
        "failure_handling": "千问失败→自动降级 DeepSeek 内置知识；仍失败→返回 None，上层走离线兜底，不崩溃",
        "audit": "Trace 面板记录每次调用的 query、返回长度、是否降级",
    },
    {
        "name": "search_multi",
        "purpose": "多角度检索（规模/竞争/口碑/政策），先查资产库再实时检索，返回结构化结果",
        "inputs": "query:str 主检索词；facets:list[str]|None 拆解角度",
        "outputs": "results:list[{query,title,url,content,publisher,date,source_type}]",
        "permissions": "仅子Agent调用；只读公开数据与历史资产，不写库",
        "failure_handling": "资产库检索失败→跳过继续实时检索；整体失败→返回空列表，子Agent降级'该维度暂无数据'",
        "audit": "记录每个 facet 的查询、返回条数、资产命中数",
    },
    {
        "name": "understand_image",
        "purpose": "图片理解（价目表/产品图/截图）→ 结构化提取价格/数量/百分比/日期，进入测算",
        "inputs": "image_b64:str base64图片；question:str 提取要求(默认描述)",
        "outputs": "text:str 提取出的键值JSON + 图片描述",
        "permissions": "仅在用户上传图片时触发；图片不落盘、不外传",
        "failure_handling": "VLM 调用失败→返回'图片解析失败'占位，结构化字段为空，不阻断调研",
        "audit": "记录图片类型/尺寸、解析成功与否、提取项数",
    },
    {
        "name": "rag_retrieve",
        "purpose": "RAG 检索历史行业资产，跨任务复用（资产飞轮）",
        "inputs": "query:str 检索词；industry:str 行业；n_results:int 返回条数",
        "outputs": "hits:list[{content,source_title,source_url,industry,created_at}]",
        "permissions": "仅子Agent调用；只读资产库",
        "failure_handling": "Chroma 不可用→降级内存集合/空结果，不阻断实时检索",
        "audit": "记录命中条数与来源权威度排序结果",
    },
    {
        "name": "calculation_engine",
        "purpose": "确定性测算引擎（TAM/SAM/SOM、单位经济），LLM 不碰算数，Python 计算",
        "inputs": "结构化参数(Pydantic 校验)：人口口径/渗透率/频次/客单价/成本等",
        "outputs": "TAM/SAM/SOM 或 月净利/盈亏平衡/回本/LTV/CAC，带公式与单位",
        "permissions": "仅统计子Agent触发；参数自相矛盾时拒绝计算（校验拦截）",
        "failure_handling": "参数缺失/不合法→返回'参数不满足，未出测算'并说明原因，不编造结果",
        "audit": "记录参数表、单位、公式、校验结果（可复算）",
    },
    {
        "name": "fetch_page",
        "purpose": "真实网页抓取（政府官网/统计局/研报页）→ 权威一手数据正文",
        "inputs": "url:str 目标网页；max_chars:int 正文上限(默认3000)",
        "outputs": "{title, url, content} | None；反爬/编码/网络失败返回 None 不崩溃",
        "permissions": "仅子Agent调用；只读公开网页，不写外部系统",
        "failure_handling": "抓取失败→返回None，上层保留联网检索结果，不阻断调研",
        "audit": "Trace 记录抓取URL、标题、正文长度、是否成功（工具调用实证）",
    },
]


# ============================================================
# 服务契约（研策自研能力层：非外部工具，是平台内服务，同样可审计）
# ============================================================
SERVICE_CONTRACTS: list[dict[str, Any]] = [
    {
        "name": "source_router",
        "purpose": "信源路由白名单（P0）：把来源 URL/机构名 路由到 权威Tier(1官方/2研究) 与分组(政府官方/研究报告)，可信度先过白名单闸门再谈内容",
        "inputs": "url:str 来源URL；source:str 机构名",
        "outputs": "(tier:int 1-5, group:str 分组标签)；命中白名单返回 1/2 + 分组，未命中返回 (4, '')",
        "permissions": "任何环节可调用；只读白名单",
        "failure_handling": "白名单异常→返回 (4, '')，上层按默认聚合器处理，不阻断",
        "audit": "证据/引文的 tier 与分组据此判定，报告置信度披露可见",
    },
    {
        "name": "billing",
        "purpose": "积分计量引擎（档位+积分）：模式档位(免费10/标准50/深入150/定制400积分)决定单次消耗，积分作计量货币",
        "inputs": "depth:str 模式档位；consume 任务开始扣积分；preview 预计消耗+余额",
        "outputs": "{mode, cost, balance, enough, short}；任务开始扣减，恢复不重复扣",
        "permissions": "execute_stream 调用；内存账户（演示期）",
        "failure_handling": "余额不足不阻断演示，但预览明确提示不足（展示付费墙）",
        "audit": "billing 事件随流推送（预计消耗/余额），报告卡显示账单",
    },
    {
        "name": "primary_research",
        "purpose": "一手验证方案设计（AI 深访/问卷）：生成可执行的 问卷(对象/样本/问法)+深访(访谈谁/问什么)+实地(踩点/清单)+成本周期",
        "inputs": "report:str 报告全文（内部扫描缺口）",
        "outputs": "{objective, survey, interviews, field, estimated_cost} + markdown 展示",
        "permissions": "用户点报告卡按钮触发；只读报告内容",
        "failure_handling": "LLM 失败→降级模板（仍给可执行方案），不阻断",
        "audit": "方案随报告卡内联展示，治'无一手研究'公害",
    },
    {
        "name": "export_report",
        "purpose": "多格式交付：docx(Word)/xlsx(Excel)/pptx(PPT)/html(交互ECharts)/pdf——一份报告多种交付",
        "inputs": "fmt:str 格式；content:str markdown 报告",
        "outputs": "对应格式文件（html 为自包含交互报告：marked 正文 + ECharts 指标图）",
        "permissions": "任意用户可导出；只读报告内容",
        "failure_handling": "依赖未安装→返回明确安装提示，不静默失败",
        "audit": "报告卡导出按钮直连",
    },
]


def tool_contracts() -> list[dict[str, Any]]:
    """工具合同清单（供前端/材料展示：工具=可审计业务合同，培训四维标准）"""
    return TOOL_CONTRACTS + SERVICE_CONTRACTS


def describe_tools() -> str:
    """工具合同 → markdown 表格（供方案书/说明材料直接引用）"""
    lines = ["| 工具 | 目的 | 输入 | 输出 | 权限 | 失败处理 |", "|---|---|---|---|---|---|"]
    for t in TOOL_CONTRACTS + SERVICE_CONTRACTS:
        lines.append(
            f"| {t['name']} | {t['purpose']} | {t['inputs']} | {t['outputs']} | "
            f"{t['permissions']} | {t['failure_handling']} |"
        )
    return "\n".join(lines)
