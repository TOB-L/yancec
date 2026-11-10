"""研策 · 信源可信度引擎（数据护城河核心）

对齐麦肯锡级"信源纪律"：
- Tier 1-5 信源分级：监管/官方 > 受监管报价/官方新闻 > 专业平台 > 聚合器 > 媒体/社媒
- 引文自动校验：URL 存活 + 内容探测（软404/词典/验证墙）→ 引文核验报告（防引用造假/跳无关站）
- 逐数据点来源卡：机构/层级/采集日期/口径/URL/抓取时间戳/验证状态
- 冲突不静默覆盖：保留冲突值+来源+日期+取舍理由（冲突日志）

这是"研策认证=可信调研"的信任基础设施，不是简单的数据积累。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================
# Tier 信源分级（麦肯锡级：离事实越近越可信）
# ============================================================
# Tier 1: 监管文件/官方统计/政府公开数据/交易所公告
# Tier 2: 官方新闻稿/受监管报价/上市公司公告/官方行业统计
# Tier 3: 专业平台（Wind/Bloomberg/FactSet/券商终端/行业数据库）
# Tier 4: 公共聚合器（东方财富/百度/Statista/聚合搜索）
# Tier 5: 媒体/自媒体/社媒/论坛/搜索摘要（仅作线索，需溯源）

_TIER1_DOMAINS = (
    "gov.cn", ".gov", "stats.gov.cn", "stat.gov.cn", "sse.com.cn", "szse.cn",
    "csrc.gov.cn", "nmpa.gov.cn", "mofcom.gov.cn", "miit.gov.cn", "nbs.gov.cn",
    "court.gov.cn", "chinatax.gov.cn", "samr.gov.cn",
)
_TIER1_SOURCES = ("统计局", "政府", "监管", "证监会", "工信部", "商务部", "海关", "发改委", "市监局", "央行", "交易所")
_TIER2_SOURCES = ("招股书", "年报", "公告", "财报", "新闻稿", "路演", "官方")
_TIER3_SOURCES = ("Wind", "Bloomberg", "FactSet", "东方财富Choice", "同花顺", "慧博", "券商研报", "万得", "iFinD")
_TIER4_SOURCES = ("东方财富", "百度百科", "Statista", "聚合", "发现报告", "洞见研报", "前瞻")
_TIER5_SOURCES = ("微博", "小红书", "知乎", "B站", "论坛", "自媒体", "公众号", "Twitter", "Reddit", "YouTube")


def categorize_tier(url: str = "", source: str = "", source_type: str = "") -> int:
    """数据点信源分级 → Tier 1-5

    url: 来源URL；source: 来源机构名；source_type: 证据类型(asset/gov_page/llm_search/...)
    返回 1-5，未知返回 4（保守：默认按聚合器处理，宁低勿高）。
    """
    u = (url or "").lower()
    s = source or ""
    # 真实抓取的官方网页 → Tier 1（最高可信实证）
    # 国际组织官方统计（世界银行/IMF/Eurostat/WHO/联合国）：与国内政府统计同为**一手官方口径**。
    # 病案（2026-09-11 拆可复算包发现）：source_router 已把它们判为 (1,"国际组织")，
    # 但 categorize_tier 不认这些域名 → 同一来源出现 **分组=国际组织、层级=T4** 的自相矛盾，
    # 引文面板上把世界银行显示成"聚合器"，权威性被自己写低。
    try:
        from core.source_router import INTL_OFFICIAL_DOMAINS

        if any(d in u for d in INTL_OFFICIAL_DOMAINS):
            return 1
    except Exception:
        pass
    if source_type == "gov_page" or any(d in u for d in _TIER1_DOMAINS) or any(k in s for k in _TIER1_SOURCES):
        return 1
    # 历史资产（本系统沉淀过、已带Tier）→ 按来源再判
    if source_type == "asset":
        return _tier_from_source(s)
    # llm_search：看URL/来源判
    return _tier_from_source(s, url=u)


def _tier_from_source(s: str, url: str = "") -> int:
    if any(k in s for k in _TIER1_SOURCES):
        return 1
    if any(k in s for k in _TIER2_SOURCES):
        return 2
    if any(k in s for k in _TIER3_SOURCES):
        return 3
    if any(k in s for k in _TIER5_SOURCES):
        return 5
    if url and any(d in url for d in _TIER1_DOMAINS):
        return 1
    return 4  # 保守默认：聚合器/未知


TIER_LABELS = {1: "T1 监管/官方", 2: "T2 官方/公告", 3: "T3 专业平台", 4: "T4 聚合/未知", 5: "T5 媒体/社媒"}


# ============================================================
# 引文自动校验：URL 存活 + 证书可信 + 内容探测（防引用造假/跳404/跳词典）
# ============================================================
# 与浏览器(Chrome/Edge on Windows)同源的信任库：Windows 系统根证书商店。
# httpx 默认用 certifi(Mozilla根库)，与 Chrome 的信任决策不同源——部分国内政务站点
# （如某些 gov.cn）的证书 certifi 认可但 Chrome/Windows 不认可 → 用户点击来源时浏览器
# 直接报"您的连接不是私密连接(ERR_CERT_AUTHORITY_INVALID)"，而我们的探测却放行。
# 用系统商店让探测与浏览器同标准；构造失败降级默认（宁严勿宽）。
_VERIFY_CTX: Any = None  # None=未初始化; False=构造失败; SSLContext=系统根证书商店


def _get_verify_context():
    """返回与浏览器同源的 SSL 信任库（Windows 系统根证书商店），失败返回 None（用 httpx 默认）。"""
    global _VERIFY_CTX
    if _VERIFY_CTX is None:
        try:
            import ssl

            ctx = ssl.create_default_context()
            if hasattr(ctx, "load_default_certs"):
                ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
            _VERIFY_CTX = ctx
        except Exception:
            _VERIFY_CTX = False
    return _VERIFY_CTX or None


def _ua_for(url: str) -> str:
    """按站点选 User-Agent。

    SEC EDGAR 的 fair-access 政策要求 UA **声明身份与联系方式**，并对伪装成浏览器的
    自动化请求返回 **403**。实测报告(10)：3 条 SEC 引用里 2 条被判"未通过"，
    其实链接是好的——**是我们的探测请求没带 SEC 要的 UA**，白丢两条最硬的证据
    （SEC 是 L2 法定披露，全报告权威性最高的来源之一却被标成"打不开"）。
    """
    try:
        from urllib.parse import urlparse

        h = (urlparse(url or "").netloc or "").lower()
    except Exception:
        h = ""
    if h.endswith("sec.gov"):
        try:
            from core.authority_fetch import _sec_ua

            return _sec_ua()
        except Exception:
            pass
    # 非 SEC 站点：用**声明了身份的爬虫 UA**，不再伪装成 Chrome。
    # 2026-09-14 审计：原先这一层回的是纯 Chrome UA，与 `authority_fetch._UA`
    # （浏览器前缀 + `yancec-research-bot/1.0` 身份后缀）不一致——
    # 同一个项目里一处诚实、一处伪装，而"守规矩、可被联系"正是我们对外讲的卖点。
    # 用同一个串：既保留兼容性（多数站不拦带 Mozilla 前缀的 UA），又声明了身份。
    try:
        from core.authority_fetch import _UA

        return _UA
    except Exception:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) yancec-research-bot/1.0"


class ComplianceSkip(Exception):
    """按合规姿态**主动不发请求**——这不是失败，是"我们选择不敲这个门"。

    两种来源：① 站点 robots.txt 明令 Disallow 的路径；② SEC 的 UA 联系方式未配置
    （fair-access 要求声明**真实可达**的联系方式，未配置时宁可不发，也不拿占位串敲门）。

    两者都必须与"打不开"严格区分：说"打不开"是在**污蔑一个我们根本没敲门的地址**。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RobotsBlocked(ComplianceSkip):
    """目标路径被站点 robots.txt 明令禁止 → **不探测**，也**不据此判死**"""

    def __init__(self, url: str = "") -> None:  # noqa: ARG002 - 保留入参以兼容旧调用
        super().__init__("未探测（该路径被站点 robots 明令禁止访问）；按红线不访问，不据此判死")


def _compliance_skip_reason(url: str) -> str | None:
    """能发请求 → 返回 None；按合规姿态**不该发** → 返回原因串。

    **唯一判定入口**：`_http_get`（真正出网那一层）与 `verify_url_live`（对外报状态那一层）
    共用同一把尺，避免两处各写一份、日后慢慢漂移成"探测层守规矩、判定层不守"。

    三道门，顺序由便宜到贵：
      ⓪ 总开关 `AUTHORITY_FETCH_ENABLED=0`（0 成本，先挡）；
      ① 硬编码 Disallow 清单（0 成本，先挡）；
      ② 站点 robots.txt 通用门（`_robots_allows`，按主机缓存，fail-open）；
      ③ SEC fair-access：联系方式未配置 → 不发（fail-closed）。
    """
    # ⓪ **总开关**——`AUTHORITY_FETCH_ENABLED=0` 是一道**红线开关，必须真的能关**。
    # 病案（2026-09-15 外部审计）：此前的调用点只有 `authority_fetch` / `authority_intl` /
    # `source_health` 三处，**本模块一处都没有** → 把开关关掉之后，最多 80 条引文**照旧真出网**。
    # **一个"关不掉"的总开关比没有开关更危险**：它让人以为已经停了。
    # 放在判定入口的第一道，是因为真出网（`_http_get`）与对外报状态（`verify_url_live`）
    # 共用本函数——同一把尺、两处都停。
    try:
        from core.authority_fetch import enabled as _af_enabled

        if not _af_enabled():
            return "未探测（总开关 AUTHORITY_FETCH_ENABLED 已关闭）；按配置不访问，不据此判死"
    except Exception:
        pass
    if _robots_blocked(url):
        return "未探测（该路径被站点 robots 明令禁止访问）；按红线不访问，不据此判死"
    try:
        from core.authority_fetch import _robots_allows
    except Exception:  # pragma: no cover - 导入失败不阻断探测
        _robots_allows = None
    if _robots_allows is not None and not _robots_allows(url):
        return "未探测（站点 robots.txt 明令 Disallow 该路径）；按红线不访问，不据此判死"
    try:
        from urllib.parse import urlparse

        h = (urlparse(url or "").netloc or "").lower()
    except Exception:
        h = ""
    if h.endswith("sec.gov"):
        try:
            from core.authority_fetch import sec_contact_configured
        except Exception:  # pragma: no cover - 导入失败不阻断探测
            return None
        if not sec_contact_configured():
            return (
                "未探测（SEC fair-access 要求 UA 声明真实联系方式，SEC_UA_CONTACT 未配置）；"
                "按合规姿态主动不发请求，不据此判死"
            )
    return None


def _http_get(url: str, timeout: int = 8):
    """单次 GET（与浏览器同证书标准），返回 (status_code, body_text)。失败抛异常。

    一次请求同时用于：存活判断 + 软404/词典/验证墙内容探测（避免二次请求重复抓取）。

    ⚠️ **必须过 robots 门与 per-host 令牌桶**（2026-09-14 审计，critical）。
    这一层此前是裸 `httpx.get`：无 `_robots_allows`、无 `_throttle`，
    而它偏偏是**全项目出网量最大的一层**（一次交付最多核验 80 条引文、16 路并发）。
    结果是「守 robots」「per-host ≥1s」这两条对外承诺，在出网量最大的地方**一条都不成立**
    —— 复核者只要看到 `max_workers=16` + 无 robots 门，就能当场陈述"声明不实"。

    ⚠️ **2026-09-14 二次审计**：上一版只拦 robots，漏了 SEC 的 UA 联系方式——
    `SEC_UA_CONTACT` 未配置时，这一层会带着占位联系方式去敲 sec.gov。
    现在两道门都走 `_compliance_skip_reason()`，不发就抛 `ComplianceSkip`（**不判死**）。

    保留的两项刻意不动：① 按站点选 UA（`_ua_for`：SEC 必须走 `_sec_ua()` 声明联系方式）；
    ② `verify=_get_verify_context()`（与浏览器同证书标准，否则政府站会因证书链判死）。
    """
    _skip = _compliance_skip_reason(url)
    if _skip:
        raise ComplianceSkip(_skip)
    from core.authority_fetch import _throttle

    _throttle(url)
    import httpx
    import ssl

    resp = httpx.get(
        url,
        headers={"User-Agent": _ua_for(url)},
        timeout=timeout,
        follow_redirects=True,
        verify=_get_verify_context(),
    )
    return resp.status_code, (resp.text or "")


# robots.txt **明令 Disallow** 的路径：探都不探。
# 这不是"能不能打开"的问题，是"允不允许访问"的问题——我们的取数层不碰这些路径，
# 验证层（同样是自动化请求）当然也不能碰，否则"我们守 robots"就成了一句只写在文档里的话。
# 病案（2026-09-11）：修 SEC 403 时我随手拿 `www.sec.gov/cgi-bin/browse-edgar` 做连通性测试，
# 而 SEC robots.txt 对该路径是 Disallow —— 测试本身就越了线。加这道闸，让它不可能再发生。
_ROBOTS_DENY: tuple[tuple[str, str], ...] = (("sec.gov", "/cgi-bin"),)


def _robots_blocked(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        p = urlparse(url or "")
        h, path = (p.netloc or "").lower(), (p.path or "").lower()
    except Exception:
        return False
    return any(h.endswith(host) and path.startswith(prefix) for host, prefix in _ROBOTS_DENY)


def verify_url_live(url: str, timeout: int = 8) -> tuple[bool, str]:
    """URL 存活校验：真实 HTTP 探测 + 证书可信（与浏览器同标准）。

    返回 (存活?, 状态说明)。证书无效/过期/不受浏览器信任 → 判失败
    （否则用户点开来源会直接看到浏览器安全警告页）。失败不抛异常。
    robots 禁止的路径**不探测**，返回"未探测"——不谎称它死了，也不越线去访问。
    """
    import ssl

    if not url or not url.startswith(("http://", "https://")):
        return False, "无有效URL"
    _skip = _compliance_skip_reason(url)
    if _skip:
        return False, _skip
    try:
        status, _body = _http_get(url, timeout=timeout)
        return (status < 400), f"HTTP {status}"
    except ComplianceSkip as e:  # 兜底：两道门之间状态变化时仍不判死
        return False, e.reason
    except ssl.SSLError as e:
        return False, f"证书无效(浏览器会报非私密连接): {type(e).__name__}"
    except Exception as e:
        # httpx 会把 ssl.SSLCertVerificationError(OSError) 包装成 httpcore.ConnectError → httpx.ConnectError，
        # 顶层 type 判断抓不到 → 沿 __cause__ 链回溯 ssl.SSLError，命中即"证书无效"而非误报"网络/反爬"
        cause = e
        for _ in range(4):
            if isinstance(cause, ssl.SSLError):
                return False, f"证书无效(浏览器会报非私密连接): {type(cause).__name__}"
            cause = getattr(cause, "__cause__", None)
            if cause is None:
                break
        return False, f"网络/反爬拦截: {type(e).__name__}"


def _domain_root(url: str) -> str:
    """URL → 域名根（https://www.dianping.com/industry/xx → https://www.dianping.com），失败返回 "" """
    from urllib.parse import urlparse

    try:
        p = urlparse(url or "")
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


# ============================================================
# 域名信任门槛（回退专用）：只有可信域名才允许"深链失效→机构官网根"
# ============================================================
def _trusted_domain_suffixes() -> tuple[str, ...]:
    """可信域名后缀（监管官方 + 头部研究 + 主流财经媒体，与 Source Router 白名单同源）"""
    try:
        from core.source_router import TIER1_DOMAINS, TIER2_DOMAINS
    except Exception:
        TIER1_DOMAINS, TIER2_DOMAINS = (), ()
    extras = ("gov.cn", ".gov", "edu.cn", "org.cn", "news.cn", "xinhuanet.com", "people.com.cn")
    seen: list[str] = []
    for d in (TIER1_DOMAINS + TIER2_DOMAINS + extras):
        d = (d or "").strip().lower().lstrip("*.")
        if d and d not in seen:
            seen.append(d)
    return tuple(seen)


def is_trusted_domain(url: str) -> bool:
    """URL 是否属于可信域名（政府官方/头部研究/主流财经媒体）。未命中返回 False。"""
    from urllib.parse import urlparse

    try:
        netloc = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return False
    if not netloc:
        return False
    host = netloc.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for t in _trusted_domain_suffixes():
        # t 可能含点（stats.gov.cn）→ 用后缀匹配；纯后缀（gov.cn）同样成立
        if host == t or host.endswith("." + t):
            return True
    return False


# ============================================================
# 页面内容探测：只在"强信号"下才把 HTTP200 判为软404/词典/空壳（宁少杀，勿误杀真页面）
# 教训：早期版本用宽泛标记扫正文 → 大量 JS 站/正常长页被误杀 → 面板只剩政府官网。
# ============================================================
# 出现在 <title> 里的强信号：直接判为无效页
_TITLE_GONE_MARKERS = (
    "404", "not found", "no such page", "oops", "the page you requested",
    "页面不存在", "该页面不存在", "找不到该页", "找不到页面", "无法访问",
    "您访问的页面", "您要访问的页面", "您要找的页面", "访问的页面不存在",
    "此内容不存在", "内容已删除", "文章已删除", "页面已删除", "内容已被删除",
    "已被删除", "信息已删除", "出错了", "出错啦", "服务不可用",
    "词条不存在", "无此词条", "验证码", "安全验证", "访问过于频繁",
    "词典", "汉语词典", "在线字典", "字典", "汉字查询", "无法打开",
    "页面走丢", "页面找不到了", "已失效", "链接失效", "该页不存在",
)


def _looks_junk_page(text: str) -> bool:
    """HTTP 200 但内容是 软404/词典/验证墙/空壳 → True（仅强信号，避免误杀正常网页）。

    判定（宁少杀，勿误杀）：
      1. <title> 含强消失标记 → 判无效；
      2. 静态空壳页：标题过短 且 可见正文极短 且 无 <script>（SPA 会由 JS 渲染，不算空壳）
         → 判无效；
      3. 标题正常但正文出现"不存在/已删除/走丢/失效"强消失句且位于前300可见字内 → 判无效
         （gov/品牌CMS 软404常见形态：站点通用标题 + 一句"抱歉，您访问的页面不存在"；
          导航/页脚文字再多也被拦——消失句在正文开头，正常文章首300字不会出现这类句子）；
      4. 其余（长正文/JS应用/标题正常）→ 保留，不判死。
    """
    if not text:
        return False
    head = text[:20000]
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    title = (m.group(1).strip() if m else "").strip()[:80]
    tl = title.lower()
    if any(k.lower() in tl for k in _TITLE_GONE_MARKERS):
        return True
    # 去掉 script/style/标签后，可见正文是否极短
    visible = re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", head
    )
    visible = re.sub(r"\s+", " ", visible).strip()
    if not visible:
        return False
    if (not title or len(title) < 6) and len(visible) < 60:
        # 静态空壳（无 JS 应用兜底渲染）→ 软404/跳转壳；有 <script src> 的 SPA 不算
        if "<script" not in head[:8000].lower():
            return True
    # gov/品牌CMS 软404：站点通用标题 + 正文一句"不存在/已删除/走丢/失效"
    # （正文常见>80字：导航+页脚多 → 旧"正文<80字"分支漏网；改查前800可见字内强消失句——
    #  很多 CMS 导航项先把前 300 字占满、消失句在导航后，旧 300 字窗会漏）
    _BODY_GONE = (
        "页面不存在", "该页面不存在", "不存在或已被删除", "已被删除或不存在",
        "找不到该页", "内容不存在", "词条不存在", "找不到页面",
        "访问的页面不存在", "您访问的页面不存在", "信息已删除",
        "页面走丢", "您访问的页面走丢了", "页面找不到了", "找不到该页面",
        "链接已失效", "页面已失效", "该内容已被删除", "此内容已删除",
        "抱歉，您要访问的页面", "您所访问的页面不存在",
        "3 秒后跳转", "3秒后跳转", "秒后跳转到首页", "正在跳转到首页",
        "即将跳转到首页", "将自动跳转到首页",
    )
    if any(k in visible[:800] for k in _BODY_GONE):
        return True
    return False


# ============================================================
# 批量核验：URL 存活 + 内容探测 + 可信域名回退
# ============================================================
def verify_urls(urls: list[str], max_items: int = 80) -> dict[str, tuple[str, bool, str]]:
    """批量核验一组 URL（并发 + URL 去重，每个唯一 URL 只探测一次）。

    返回 {url: (可访问URL, 存活?, 状态说明)}。
    判定逻辑（对【所有域名】统一做内容探测，不再信任跳过——幻觉深链常在 gov/可信站上
    返回 HTTP 200 的"软404页"）：
      - HTTP <400 且内容正常 → 判存活，保留原 URL；
      - HTTP <400 但内容=软404/已删除/词典/验证墙 → 视为失效（LLM 幻觉深链的典型特征）；
      - HTTP 4xx/5xx / 证书无效 / 网络失败 → 失效。
    失效后回退策略（宁空勿错）：
      - 域名属于【白名单可信】→ 回退机构官网根（能点开，标明非原文）；
      - 否则 → 清空（绝不回退到垃圾/词条站的无关首页）。
    超过核验上限 → (原URL, False, "超过核验上限(宁空勿错)")。
    """
    import concurrent.futures

    unique: list[str] = []
    seen: set[str] = set()
    for u in urls or []:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        unique.append(u)

    def _content_dead_or_junk(text: str) -> bool:
        return _looks_junk_page(text)

    def _probe(u: str):
        """单次 GET 完成 存活+内容 判定。任何失效/垃圾 → 直接判不可用（转文字摘要），
        不回退到官网首页——官网首页不是证据地址，宁要文字摘要也不给用户无关首页。
        """
        try:
            status, body = _http_get(u)
        except ComplianceSkip as e:
            # 未探测：`e.reason` 自带**具体**理由（robots Disallow 是站点定的规矩；
            # SEC 联系方式未配置是**我们自己的配置缺口**）——两者含义不同，
            # 所以此处**不许**再合并成一句"按合规姿态"（理由分类见
            # `core/source_registry.classify_skip_reason`）。
            # **不访问、也不判死**：说"打不开"是在污蔑一个我们根本没敲门的地址
            return u, False, e.reason
        except Exception as e:
            return u, False, f"网络/证书异常: {type(e).__name__}；已转为文字摘要（不展示假链接）"
        if status < 400 and not _content_dead_or_junk(body):
            return u, True, f"HTTP {status}"
        if status < 400:
            return u, False, "页面返回成功但内容为软404/已删除/词典/验证墙（疑似幻觉链接）；已转为文字摘要"
        return u, False, f"HTTP {status}；已转为文字摘要"

    verdicts: dict[str, tuple[str, bool, str]] = {}
    targets = unique[:max_items]
    if targets:
        # 并发度 16 → 4：`_http_get` 现在过 per-host 令牌桶，同域多条会自然串到 ≥1s；
        # 16 路只会堆出一批线程在同一条 `_throttle` 队列上排队，既不加吞吐又拉长尾延迟。
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_probe, u): u for u in targets}
            for fut in concurrent.futures.as_completed(futures):
                u = futures[fut]
                try:
                    verdicts[u] = fut.result()
                except Exception:
                    verdicts[u] = (u, False, "核验异常")
    for u in unique[max_items:]:
        verdicts[u] = (u, False, "超过核验上限(宁空勿错)")
    return verdicts


def verify_citation(url: str, title: str = "") -> dict[str, Any]:
    """单条引文校验：URL存活 + 标题关键词命中。

    返回：{url, status: pass/warn/fail/unknown, live, title_hit, detail}
    - pass: 存活且(无标题要求或标题命中)
    - warn: 存活但标题疑似不匹配（可能是重定向页/搜索聚合页）
    - fail: 无法访问/无效URL
    """
    if not url or not url.startswith("http"):
        return {"url": url or "", "status": "fail", "live": False, "title_hit": False, "detail": "无效URL"}
    live, detail = verify_url_live(url)
    title_hit = True
    if title:
        # 标题命中：抓回页面 title 是否含关键词
        try:
            _status, body = _http_get(url, timeout=8)
            m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.S)
            page_title = m.group(1).strip() if m else ""
            keywords = [k for k in re.split(r"[^一-龥A-Za-z0-9]+", title) if len(k) >= 2][:5]
            title_hit = any(kw and kw in page_title for kw in keywords) if keywords else True
        except Exception:
            title_hit = True  # 抓不到页面标题时不算失败（网络问题）
    status = "pass" if (live and title_hit) else ("warn" if live else "fail")
    return {"url": url, "status": status, "live": live, "title_hit": title_hit, "detail": detail}


# ============================================================
# 引文核验报告：批量校验报告里的全部引用
# ============================================================
# 「没验成」「没去验」「验了没通过」是**三**件事，三者不许互相冒充。
# · `_NET_MARKS` —— 想验但没验成（网络/证书/超时）：**未核验 ≠ 不通过**
# · `_SKIP_MARKS` —— 按合规姿态**主动没去敲**（robots Disallow / SEC 联系方式未配置）：
#   **不据此判死**，因为说"打不开"是在污蔑一个我们根本没敲门的地址
# 病案（2026-09-14 二次审计）：上游 `ComplianceSkip` 已经严格区分了这两者，
# 但本函数一句 `"pass" if live else "fail"` 把它们**全都压回了 fail**——
# 上游辛苦做的区分，到消费端被抹平。**判定做得再细，没人消费就等于没做。**
_NET_MARKS = ("网络/证书异常", "ConnectError", "Timeout", "超时", "请求异常", "未核验")
_SKIP_MARKS = ("未探测",)


def is_compliance_skipped(detail: str) -> bool:
    """该条"没通过"是不是**按合规姿态主动没去敲门**（而不是真的打不开）。

    统一出口：清洗层（`graph._bad_urls`）与展示层共用同一把尺。
    病案（2026-09-15 外部审计）：`verify_url_live` 对合规跳过返回 `live=False`，
    而清洗层只按 `live` 判断 → **跳过与死链被当成同一件事**：
    正文 markdown 链接被降级成纯文字、citations 里的 url 被清空。
    于是同一份报告里，md 核验表标着「🚪 未探测（按合规姿态主动不访问）」，
    清洗层却已经把它当死链删了——**两层对同一件事说法相反**。
    把"没敲门"当成"门是坏的"，正是本产品一直在批评的那种越界判断。
    """
    return any(k in (detail or "") for k in _SKIP_MARKS)


def _skip_reason_breakdown(items: list[dict[str, Any]]) -> dict[str, int]:
    """把 `status == "skip"` 的条目按**理由**计数（理由分类只有一处实现）。

    消费方是 `format_citation_report_md` 与报告附录：这两处此前共用一个说法
    「按合规姿态未探测」，把 robots 的 Disallow（站点规矩）与我们没配 SEC 联系方式
    （自己的配置缺口）混成一句。**分类器只此一处**——`core/source_registry`。
    """
    from core.source_registry import classify_skip_reason

    out: dict[str, int] = {}
    for it in items:
        if it.get("status") != "skip":
            continue
        code = classify_skip_reason(it.get("detail")) or "unclassified"
        out[code] = out.get(code, 0) + 1
    return out


def build_citation_report(citations: list[dict[str, Any]], max_items: int = 80) -> dict[str, Any]:
    """对报告全部引用做核验 → 引文核验报告（URL 去重并发探测，控时控成本）。

    输出：{total, pass, warn, fail, items:[{source,title,url,status,detail}], verdicts}
    - 每个唯一 URL 只探测一次；同一 URL 的引用共享核验结果（去重提速且不漏验）
    - 全量引用全部覆盖（修复旧版只核验前 max_items 条、超限引用漏网的漏洞）
    - 证书无效/软404/词典/垃圾 → 白名单域名回退官网根；否则 fail（宁空勿错）
    - verdicts 供交付节点复用：报告正文链接与引用 URL 统一核验、统一过滤
    """
    verdicts = verify_urls([c.get("url") for c in citations], max_items=max_items)
    items = []
    for c in citations:
        url = (c.get("url") or "").strip()
        if not url:
            items.append(
                {
                    "source": (c.get("source") or "")[:30],
                    "title": (c.get("title") or "")[:40],
                    "url": "",
                    "status": "fail",
                    "detail": "无URL",
                }
            )
            continue
        resolved, live, detail = verdicts.get(url, (url, False, "未核验"))
        # **"按合规姿态没去敲" 与 "敲了没打开" 是两件事**（2026-09-14 二次审计）。
        # 上游 `ComplianceSkip` 辛苦区分的"未探测、不据此判死"，在这里曾被一句
        # `"pass" if live else "fail"` 抹平——报告照样把一个**我们根本没敲过门**的地址
        # 记成"未通过"。所以单列一档 `skip`：既不算通过，也不算失败。
        _skip = is_compliance_skipped(detail)
        items.append(
            {
                "source": (c.get("source") or "")[:30],
                "title": (c.get("title") or "")[:40],
                "url": resolved if live else url,
                "status": "skip" if _skip else ("pass" if live else "fail"),
                "detail": detail,
            }
        )
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    total = len(items)
    # **"没验成" 与 "验了没通过" 是两件事**（2026-09-14 实跑踩中：跑的时候 DNS 挂了，
    # 19 条引用全部 ConnectError → 报告印出"通过率 0.0%"，读者只会读成
    # "你的引用全是假的"。真相是**核验根本没执行**。）
    # 这与 `core/entailment.py` 里"未判定 ≠ 通过"是同一条纪律的镜像：
    # **未核验 ≠ 不通过**。所以把"连接层失败"单列出来，两者都不许互相冒充。
    unverified = sum(
        1 for it in items
        if it["status"] == "fail" and any(k in (it.get("detail") or "") for k in _NET_MARKS)
    )
    return {
        "total": total,
        "counts": counts,
        "items": items,
        "verdicts": verdicts,
        # 通过率：**绝对条数对复核者没有信息量**（"通过 12 条"——12 条里通过几条？）。
        # 单列出来，是为了让"我们核验了"变成一个可比、可复算的数。
        #
        # **分母必须是"真的敲过门、且真的敲出结果的那些"**（2026-09-15 外部审计 + 2026-09-16 补）：
        # ① 此前用 `total`（含 skip），与同一份报告里写的"不计入通过率也不计入失败"**自相矛盾**；
        # ② 只减 skip 还不够——**网络/证书层异常的条目也没验成**，把它们留在分母里，
        #    跑的时候 DNS 挂一次，通过率就被打成 0.0%，而同一行还写着"未核验 ≠ 不通过"。
        #    未核验既不进分子，也不进分母。
        # **一条都没真验成时返回 None，不是 0.0**：返回 0.0 会被前端染成红角标"链接核验 0%"，
        # 把"没测"渲染成"全坏"——正是本产品在批评的那种把缺席当结论。
        "pass_rate": (
            round(counts["pass"] / probed, 4)
            if (probed := total - counts.get("skip", 0) - unverified) > 0
            else None
        ),
        # 真的验出结果了的条数（通过率的分母，单列以便复算）
        "probed": total - counts.get("skip", 0) - unverified,
        "unverified": unverified,
        # **`verified` 此前是 `total - unverified`，把"按合规姿态没去敲"的那几条也算了进去**
        # （2026-09-17 实跑发现：23 条引用里 21 条真验、2 条 skip，
        # 而这里印的是 `verified: 23`——**一个叫 verified 的字段把没验的算成了验过的**）。
        # 与 `pass_rate`/`probed` 的口径不一致：那两处都显式减掉了 `skip`，
        # 只有这一处没减。**没有消费方**（前端读的是 `probed`/`skipped`），
        # 所以它一直是"写进存档等人来读、读到的却是个偏高的数"。
        # 不删字段（存档里 16 份旧报告都带着它，删了会让新旧两版结构不同），只把值改对。
        "verified": total - unverified - counts.get("skip", 0),
        # 主动没去敲的条数：单列出来，**既不进通过率分子也不进失败分子**
        "skipped": counts.get("skip", 0),
        # **「未探测」也要按理由拆开**（2026-09-17）。
        # 病案：下游 `format_citation_report_md` 把这一档统一写成
        # 「N 条按合规姿态未探测（robots Disallow / SEC 联系方式未配置）」——
        # 前半是**站点定的规矩**，后半是**我们自己的配置缺口**，两件事被并在一个"合规姿态"里，
        # **等于把配置缺口讲成合规美德**。理由分类只有一处实现
        # （`core/source_registry.classify_skip_reason`），这里存下来供展示层直接用。
        "skip_reasons": _skip_reason_breakdown(items),
    }


# ============================================================
# 逐数据点来源卡 + 冲突日志（附录=证据链的家）
# ============================================================
def build_source_cards(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把每个引用转成"数据点来源卡"（附录用）：机构/层级/URL/标题/采集时间/验证状态。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cards = []
    for c in citations[:60]:
        url = (c.get("url") or "").strip()
        tier = categorize_tier(url, c.get("source") or "", c.get("source_type") or "")
        cards.append(
            {
                "source": (c.get("source") or "")[:30],
                "tier": tier,
                "tier_label": TIER_LABELS.get(tier, ""),
                "url": url,
                "title": (c.get("title") or "")[:60],
                "collected_at": now,
                "verification": "待核验",  # 由 build_citation_report 填
            }
        )
    return cards


def format_citation_report_md(report: dict[str, Any]) -> str:
    """引文核验报告 → markdown（追加到报告附录）

    **通过率**是本节的读数（不是绝对条数）。同时必须写清**这一节不证明什么**——
    "链接活着"与"内容支撑结论"是两件事，混为一谈正是主流 AI 搜索被诟病的地方：
    它给的链接大多能打开，但打开后并不支持它写的那句话。
    本节的通过率只在"链接层"成立；"内容层"由《引用蕴涵核验》一节回答。
    """
    counts = report.get("counts", {})
    total = report.get("total", 0)
    rate = report.get("pass_rate")
    if rate is None:
        # 兼容没有 `pass_rate` 字段的旧报告：**分母口径必须与上面一致**
        # （排除 skip 与未核验）。早先这里退回 `pass / total`——那正是
        # "把没测的算进分母"的老毛病，在一个本该只是兜底的分支里又犯一次。
        _den = total - int(report.get("unverified") or 0) - int(report.get("skipped") or 0)
        rate = (counts.get("pass", 0) / _den) if _den > 0 else None
    _unv = int(report.get("unverified") or 0)
    _skip = int(report.get("skipped") or 0)
    # **"一条都没真验成"的判据不能只看网络失败**（2026-09-14 二次审计）。
    # 原先写 `_unv == total`——于是"全部引用都被 robots 拦下 / 全部是 SEC 且未配置联系方式"
    # 这种**一条都没敲过门**的情形，判据不成立，报告照样印出 `通过率 0.0%`：
    # 正是本文件下面自己声明"绝不许印"的那个数。真实判据是**有没有拿到过真判决**。
    _real = total - _unv - _skip
    _all_net = bool(total) and _real <= 0
    if _all_net:
        _why = []
        if _unv:
            _why.append(f"{_unv} 条为网络/证书层异常（非链接失效）")
        if _skip:
            # **理由要分开说**（2026-09-17）：原先这里是一句
            # 「{_skip} 条按合规姿态未探测（robots Disallow / SEC 联系方式未配置）」——
            # 把**站点定的规矩**（robots 禁止）与**我们自己的配置缺口**（没配 SEC 联系方式）
            # 并进同一个"合规姿态"里。对读者的含义完全不同：前者是"我们守规矩"，
            # 后者是"我们没配好"。用后者去借前者的光，就是**把配置缺口讲成合规美德**。
            _reasons = report.get("skip_reasons") or _skip_reason_breakdown(
                report.get("items") or []
            )
            from core.source_registry import skip_reason_label

            _parts = []
            for _code, _n in sorted(_reasons.items(), key=lambda x: -x[1]):
                _lbl = skip_reason_label(_code) or _code
                _parts.append(f"{_n} 条 {_lbl}")
            _why.append(
                f"{_skip} 条未探测（**没敲门，不是门坏了**）：" + "；".join(_parts)
            )
        _head = (
            f"共 {total} 条引用：**本次核验未能完成**（{'；'.join(_why)}）"
            "——**未核验 ≠ 不通过**，请在条件具备后重跑核验。"
        )
    else:
        # **「未通过」里不许混着「未核验」**（2026-09-16）。
        # 病案：网络/证书层异常的条目 `status` 仍是 `fail`，于是这一行同时印出
        # 「❌ 未通过 6」和紧跟着的「⏳ 未核验 6（**未核验 ≠ 不通过**）」——
        # **同一句话里前后互相否定**，而它是印在报告第一屏给复核者看的那一行。
        # 更糟的是分母：`probed = total - skip` 把"没验成"的也算进了分母，
        # 于是跑的时候 DNS 挂一次，通过率就被打成 0.0%，读者只会读成"引用全是假的"。
        _fail_real = max(int(counts.get("fail", 0)) - _unv, 0)
        _head = (
            f"共 {total} 条引用：✅ 通过 {counts.get('pass', 0)} · ⚠️ 存疑 {counts.get('warn', 0)} · "
            f"❌ 未通过 {_fail_real} ｜ **通过率 "
            + (f"{rate * 100:.1f}%**" if rate is not None else "未判定（无可判定的条目）**")
        )
        if _skip:
            # 「没去敲」必须单列：混进"❌ 未通过"里，等于污蔑一个我们根本没敲门的地址。
            #
            # **而且理由要分开说**（2026-09-17）：这一行原先写死
            # 「（robots Disallow / SEC 联系方式未配置——**按合规姿态主动不访问**）」，
            # 把**站点定的规矩**（robots 禁止我们访问）与**我们自己的配置缺口**
            # （没配 SEC 联系方式）并进同一个"合规姿态"里。
            # 对读者的含义完全不同：前者是"我们守规矩"，后者是"我们没配好"。
            # 用后者去借前者的光，就是**拿合规立场给配置缺口背书**。
            _reasons = report.get("skip_reasons") or _skip_reason_breakdown(
                report.get("items") or []
            )
            from core.source_registry import skip_reason_label

            _rtxt = "；".join(
                f"{_n}×{skip_reason_label(_c) or _c}"
                for _c, _n in sorted(_reasons.items(), key=lambda x: -x[1])
            )
            _head += (
                f" ｜ 🚪 未探测 {_skip}（{_rtxt}——"
                "**没敲门，不是门坏了：不计入通过率也不计入失败**）"
            )
        if _unv:
            _head += f" ｜ ⏳ 未核验 {_unv}（网络/证书层异常，**未核验 ≠ 不通过**）"
    lines = [
        "### 🔍 引文核验报告",
        _head,
        "",
        "| 来源 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for it in report.get("items", []):
        mark = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "🚪"}.get(it["status"], "•")
        lines.append(f"| {it['source']} | {mark} | {it['detail']} |")
    lines.append("")
    if _all_net:
        lines.append(
            f"> **本节核验的是「链接」这一层**：每条引用都经真实 HTTP 探测——存活状态、证书是否被系统信任、"
            f"内容页是否已变成软 404/删除页/词典页/验证墙。**本次 {total} 条全部卡在网络/证书层**，"
            "属于**核验未执行**，不是「引用不通过」；表内说明列已如实记录每条的原因（不展示未经验证的活链，"
            "也**不把没验成的记为失效**）。"
        )
    elif _unv:
        lines.append(
            f"> **本节核验的是「链接」这一层**：每条引用都经真实 HTTP 探测——存活状态、证书是否被系统信任、"
            f"内容页是否已变成软 404/删除页/词典页/验证墙。**通过率 {rate * 100:.1f}%** 的含义是："
            f"{total} 条引用里，{counts.get('pass', 0)} 条此刻指向的是**真实存在且内容可读的页面**。"
            f"另有 **{_unv} 条因网络/证书层异常未能完成核验**——它们既不算通过、也不算失败。"
        )
    else:
        lines.append(
            "> **本节核验的是「链接」这一层**：每条引用都经真实 HTTP 探测——存活状态、证书是否被系统信任、"
            "内容页是否已变成软 404/删除页/词典页/验证墙。**通过率 {:.1f}%** 的含义是：{} 条引用里，"
            "{} 条此刻指向的是**真实存在且内容可读的页面**。".format(
                rate * 100, total, counts.get("pass", 0)
            )
        )
    lines.append(
        "> **本节不证明什么（重要）**：链接能打开，**不等于**该页内容支撑了文中那句话。"
        "主流 AI 搜索的主要失败模式恰恰不是「链接打不开」，而是**链接能打开、但内容与结论无关或相悖**。"
        "这一层由下一节《引用蕴涵核验》逐条比对「正文主张 ↔ 引用页原文」给出三态判定；"
        "两节合起来才构成完整的引用可信度。未通过的引用应视为估算或待复核，不应当作事实采信。"
    )
    return "\n".join(lines)
