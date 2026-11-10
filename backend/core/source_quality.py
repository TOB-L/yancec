"""来源质量三道门（确定性、零模型成本）——拦在"引用成形"之前

病案（2026-09-11 实拆报告(8) 的引文列表）：
  ① `gov.cn/zhengce/index.htm` —— **栏目首页**被当成内容页引用。首页没有正文，
     读者点进去看到的是一堆链接，等于"引了个目录"；
  ② `m.sohu.com` 一篇 **2019 年**文章 —— 2026 年的调研引 7 年前的媒体稿；
  ③ `bydrug.pharmcube.com` —— **医药站**出现在咖啡调研里（跨垂类噪声）。
这三条都躺在**数据源列表**上，是复核者/投资人最先看、最容易一眼看穿的位置。
（注：报告(8) 里 ①③ 同时还是"gov.cn 被挂成星巴克门店日销来源"那条造假线索的一部分，
但即使来源挂对了，这三类页本身也不该出现在交付物里。）

设计原则——**宁可少拦，不可误杀**：
- 权威域（政府/教育/协会/交易所/国际组织）**豁免**时效门与跨垂类门：它们的相关性来自
  角色而非题材（一份 2022 年的统计局公报在 2026 年的报告里依然是有效引证）。
- 跨垂类门只在**域名**上取证据，且要求"我方垂类"能确定、且与对方垂类**互斥**才拦。
  标题里出现"健康/教育"这类泛词太容易误伤（"健康饮品"不是医药），一律不看。
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

# 栏目页/首页的路径末段（命中即视为"目录页"而非内容页）
_SECTION_LEAF = frozenset(
    {
        "index.htm", "index.html", "index.shtml", "index.php", "index.jsp", "index.asp", "index.aspx",
        "default.htm", "default.html", "default.asp",
        "list.htm", "list.html", "list.shtml", "list.php",
        "channel.htm", "channel.html", "column.htm", "column.html",
    }
)

# 垂类标记——**只用高精度词**。刻意不收「健康/教育/旅游」这类泛词：
# 一个卖健康饮品/做研学旅行的站会被误判成医药/教育站，那是拿"少一条噪声"换"丢一个真来源"。
_VERTICALS: dict[str, tuple[str, ...]] = {
    "医药健康": ("pharm", "drug", "medicine", "medlive", "biotech", "hospital", "dxy",
                 "医药", "药业", "医疗", "医院", "生物医药", "丁香园"),
    "汽车": ("autohome", "d1ev", "gasgoo", "盖世汽车", "汽车之家", "易车", "懂车帝"),
    "游戏": ("17173", "gamersky", "3dmgame", "游戏", "手游", "电玩"),
    "美妆": ("聚美丽", "品观", "美妆", "化妆品"),
    "母婴": ("母婴", "孕婴", "奶粉"),
    "房产": ("楼盘", "安居客", "链家", "贝壳找房", "地产网"),
    "招聘": ("zhaopin", "51job", "招聘", "人才网", "boss直聘"),
    "婚嫁": ("婚礼", "婚嫁", "婚纱"),
}

# 我方垂类（按行业词判定）——同样只用高精度词
_INDUSTRY_VERTICALS: dict[str, tuple[str, ...]] = {
    "餐饮食品": ("咖啡", "茶饮", "奶茶", "餐饮", "食品", "零食", "烘焙", "饮料", "快餐",
                 "火锅", "外卖", "酒馆", "预制菜", "调味"),
    "医药健康": ("医药", "医疗", "生物医药", "医院", "药店", "保健品", "医疗器械", "创新药"),
    "汽车": ("汽车", "新能源车", "电动车", "整车", "零部件", "充电桩"),
    "游戏": ("游戏", "手游", "电竞"),
    "美妆": ("美妆", "化妆品", "护肤", "彩妆"),
    "母婴": ("母婴", "奶粉", "纸尿裤", "早教"),
    "房产": ("房产", "地产", "楼盘", "家装", "家居"),
    "教育": ("教育", "培训", "研学", "职教"),
    "旅游": ("旅游", "酒店", "民宿", "景区"),
    "零售电商": ("电商", "零售", "跨境电商", "便利店", "超市", "直播带货"),
}

_AUTHORITY_SUFFIX = (".gov.cn", ".gov", ".edu.cn", ".edu", ".org.cn", ".ac.cn")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().lstrip(".")
    except Exception:
        return ""


def is_authority(url: str) -> bool:
    """**严格**权威域（政府/教育/交易所/监管/国际组织）——完全豁免时效门与跨垂类门

    ⚠️ 刻意**不含** TIER2（艾瑞/欧睿/前瞻这类研究机构）与 TIER2_MEDIA（搜狐/新浪/36氪）。
    第一版把整个 TIER2 都当"权威"，结果实测 `m.sohu.com` 一篇 **2019** 年文章直接放行——
    时效门形同虚设，而"门户旧文被引"正是当初要拦的三件事之一。
    """
    h = _host(url)
    if not h:
        return False
    from core.domain_match import host_hit

    if host_hit(h, _AUTHORITY_SUFFIX):
        return True
    try:
        from core.source_router import INTL_OFFICIAL_DOMAINS, TIER1_DOMAINS

        return host_hit(h, tuple(TIER1_DOMAINS) + tuple(INTL_OFFICIAL_DOMAINS))
    except Exception:
        return False


def is_research_house(url: str) -> bool:
    """研究机构/咨询/数据平台（艾瑞/欧睿/前瞻/QuestMobile…）——时效门**放宽**但**不豁免**

    为什么给它一档：咨询报告的保鲜期比新闻稿长得多（一份 2022 年的欧睿行业规模，
    2026 年依然是常被引用的口径），但它终究会过期，所以只是放宽到 5 年而不是免检。
    """
    h = _host(url)
    if not h:
        return False
    try:
        from core.domain_match import host_hit
        from core.source_router import TIER2_DOMAINS

        return host_hit(h, TIER2_DOMAINS)
    except Exception:
        return False


def is_section_page(url: str) -> bool:
    """栏目页/首页（不是内容页）——引它等于"引了个目录"

    只认三种**明确信号**，不做"路径短就是栏目页"这类猜测：
      1. 路径为空或 `/`（站点首页）
      2. 路径末段是 index/list/channel/column/default 系列文件名
      3. 路径以 `/` 结尾且只剩 ≤1 段（如 `/zhengce/`）——目录列举页
    反例（**必须放行**）：`/a/951554571_121393844`（无扩展名的文章）、
    `/news/detail/123.html`、`/202609/t20260901_123.html`。
    """
    try:
        p = (urlparse(url or "").path or "").strip()
    except Exception:
        return False
    # 空/无效 URL 是"没有链接"，不是"首页"——`urlparse("").path` 恰好也是 ""，
    # 不先挡掉就会把"诚实标注的无链接引文"当成栏目页丢掉（实测 24 条里错杀 2 条）。
    if not _host(url):
        return False
    if p in ("", "/"):
        return True
    leaf = p.rstrip("/").rsplit("/", 1)[-1].lower()
    if leaf in _SECTION_LEAF:
        return True
    if p.endswith("/"):
        # 以 `/` 结尾 = 目录列举页。深度限制分两档：
        # · 政府/教育站：**任意深度**都算栏目页——这类站的正文页一定带文件名
        #   （`/sj/zxfb/202609/t20260901_123.html`），`/sj/zxfb/` 只可能是栏目；
        # · 其它站：只认 ≤1 段（`/zhengce/`），因为 `/news/12345/` 这种无文件名
        #   的文章 URL 形态在媒体站很常见，判成栏目页就是误杀。
        depth = len([x for x in p.split("/") if x])
        if depth <= 1 or is_authority(url):
            return True
    return False


def stale_year(date_text: str, max_age: int = 3, now_year: int | None = None) -> int | None:
    """日期文本若是**过期年份**则返回该年份，否则 None。

    只认 4 位年份（`2019年5月` / `2019-05-03` / `2019.5`），抽不到年份就**不算过期**——
    宁可放过一条没写日期的，也不误杀。
    """
    m = _YEAR_RE.search(str(date_text or ""))
    if not m:
        return None
    y = int(m.group(0))
    now = now_year or date.today().year
    return y if y <= now - max(1, max_age) else None


def verticals_of(text: str) -> set[str]:
    """一段文本（行业词/域名/标题）命中的垂类集合。命中多个就都返回——由调用方决定怎么用"""
    t = str(text or "").lower()
    if not t:
        return set()
    return {v for v, kws in _VERTICALS.items() if any(k in t for k in kws)}


def industry_verticals(industry: str, question: str = "") -> set[str]:
    """报告自己的垂类（可多个）。

    命中多个是**好事**：行业词含"健康食品"时同时属于 餐饮食品 与 医药健康，
    跨垂类门就会整体失效（宁可漏拦，不可把真来源判成噪声）。
    """
    t = f"{industry or ''} {question or ''}".lower()
    if not t:
        return set()
    return {v for v, kws in _INDUSTRY_VERTICALS.items() if any(k in t for k in kws)}


def is_low_quality(url: str) -> bool:
    """低质"报告工厂"域（`core.source_router.LOW_QUALITY_DOMAINS`）

    这些站（chinairn/askci/chinabgao/文库类…）专门批量生产"行业研究报告"页面，
    内容多为互相抄的估算，且**从不在正文里说清口径**。它们**不是权威域**，
    所以不会被豁免；但原来的三道门只看"栏目页/时效/跨垂类"，一个**当期的、咖啡垂类的**
    报告工厂页可以大摇大摆地过全部三道门——实测报告(10) 的引文里就有 `chinairn.com`。
    """
    h = _host(url)
    if not h:
        return False
    try:
        from core.domain_match import host_hit
        from core.source_router import LOW_QUALITY_DOMAINS

        return host_hit(h, LOW_QUALITY_DOMAINS)
    except Exception:
        return False


def screen_citation(
    url: str,
    title: str,
    date_text: str,
    industry: str = "",
    question: str = "",
    max_age: int = 3,
    research_max_age: int = 5,
    indicator: str = "",
) -> str | None:
    """引用准入闸门 → 拒绝原因（字符串）或 None（放行）。

    单一入口，便于单测与审计：报告里的每条引用都能回答"它为什么在这里 / 为什么被拦"。
    四道门 + 三档宽严：权威域免检 → 研究机构 5 年 → 其余（媒体/未知站）3 年。

    `indicator`（本条目服务的**指标/结论文本**）会经 `core.metric_freshness` 换成
    **按指标定的保鲜期**：门店数 1 年、人口/GDP 3 年、行业规模 4 年。
    两个方向都要紧——同一个「3 年」，对人口公报太严、对门店数太松。

    ⚠️ **边界（2026-09-18 审计改正，此前这句话是错的）**：
    本 docstring 曾经写「**判得出指标时，指标年限对谁都一样**（研究机构也不例外）」——
    **与实现不同源**。本函数第一道门就是 `is_authority(url) → return None`，
    排在 `indicator` 分支**之前**，所以**按指标定的保鲜期对权威域一次都没生效过**：
    实测 World Bank / IMF / 国家统计局的任何 URL（无论数据多老）在带 indicator 时仍然放行，
    而 2022 年的 iresearch 页在 indicator=门店数 时会被正确拦下。
    那是五跑「30 槽模板重复 / 2020·2022 年 GDP 增速填进六个章节」的代码级原因。
    指标档的真实作用域是：**权威域之外的所有来源**。
    判不出指标时才退回「研究机构 5 年 / 其余 3 年」的来源型默认。

    （顺序本身是**有意的**设计——权威域豁免时效与跨垂类，理由写在调用点注释里。
    本处只把话改回与判定同源；要不要让指标档也管到权威域，属判据变更，
    会改已落盘报告的读数，不在 9/19 前动。)
    """
    if is_section_page(url):
        return "栏目页/首页（不是内容页）"
    if is_low_quality(url):
        return "低质报告工厂域（批量生成、口径不明）"
    if is_authority(url):
        return None  # 权威域豁免时效与跨垂类（相关性来自角色，而非题材）
    _house = is_research_house(url)
    _iy, _why = 0, ""
    if indicator:
        try:
            from core.metric_freshness import max_age_for

            _iy, _why = max_age_for(indicator, default=0)
        except Exception:
            _iy, _why = 0, ""
    if _iy:
        # **指标已知 → 指标说了算，对谁一视同仁。**
        # 「研究机构放宽到 5 年」这条宽限，理由本来是"咨询报告的保鲜期长"——
        # 而那恰恰是**指标属性**，不是**来源属性**：一份 2023 年的欧睿行业规模确实还能用
        # （慢变档本身就给了 4 年），但一份 2023 年的"某品牌门店数"（快变档 1 年）不能因为
        # 发在咨询机构站上就免检。所以宽限只在**判不出指标**时兜底，不再叠加。
        _base = _iy
    else:
        _base = research_max_age if _house else max_age
    y = stale_year(date_text, max_age=_base)
    if y is not None:
        tail = f"，按「{_why[:28]}」档 {_base} 年" if _why else ""
        return f"时效过期（{y} 年{tail}）"
    src_v = verticals_of(_host(url))
    if src_v:
        ours = industry_verticals(industry, question)
        if ours and not (src_v & ours):
            return f"跨垂类噪声（{'/'.join(sorted(src_v))} 站，本次调研为 {'/'.join(sorted(ours))}）"
    return None
