"""研策 · 信源路由白名单（P0：Source Router 白名单）

把"哪些来源可信"从黑箱变成公开白名单：
- 一级权威（监管/官方/统计/交易所）：gov.cn、统计局、证监会、海关总署…
- 二级权威（头部研究/数据机构）：艾瑞、欧睿、GfK、IDC、红餐、前瞻…
- 白名单命中 → 信源升级（L1/L2）、分组归为"政府官方/研究报告"，可优先展示

这直接回应"引用幻觉/来源不可靠"行业公害：来源可信度先过白名单闸门，再谈内容。
"""

from __future__ import annotations

# 一级权威域名（监管/官方/统计/交易所/官方公告）
TIER1_DOMAINS: tuple[str, ...] = (
    "gov.cn", ".gov", "stats.gov.cn", "stat.gov.cn",
    "sse.com.cn", "szse.cn", "csrc.gov.cn", "nmpa.gov.cn",
    "mofcom.gov.cn", "miit.gov.cn", "nbs.gov.cn", "customs.gov.cn",
    "chinatax.gov.cn", "samr.gov.cn", "court.gov.cn",
    "pbc.gov.cn", "cninfo.com.cn", "hko.hk",
)
# 二级权威域名（头部研究/数据机构/垂直行业媒体/主流财经/平台数据）
TIER2_DOMAINS: tuple[str, ...] = (
    "iresearch.com.cn", "leadleo.com", "qianzhan.com", "eastmoney.com",
    "wind.com.cn", "bloomberg.com", "statista.com", "euromonitor.com",
    "chinairn.com", "askci.com", "hongcan.com", "canyin88.com", "dtcq.com",
    "stcn.com", "cnstock.com", "yicai.com", "cls.cn", "36kr.com",
    # 本土研究/数据机构与垂直媒体（麦肯锡级调研=多源三角，不能只有政府官网）
    "iimedia.cn", "analysys.cn", "questmobile.com.cn", "jiguang.cn",
    "talkingdata.com", "ebrun.com", "iyiou.com", "p5w.net", "thepaper.cn",
    "nbd.com.cn", "chanmama.com", "newrank.cn",
    # 主流财经/垂直媒体（品牌/市场/增速类问题需媒体与机构做第三方佐证；
    #  先前这些域漏白名单 → 权威降档为T4、且 deep-link 失效不回退，进不了每条证据的 top-n）
    "finance.sina.com.cn", "sina.com.cn", "sohu.com", "jiemian.com", "linkshop.com.cn",
    "21jingji.com", "xueqiu.com", "cyzone.cn", "lieyunwang.com",
    # 券商/财报/公告数据（上市公司原文）
    "sse.com.cn", "szse.cn",
)
# 二级·财经/垂直媒体（第三方佐证角色）：
# Tier 仍为 2（不动置信度权重），但**分组标签独立**——不再与真·研究机构同标"研究报告"，
# 评审/用户点开数据源面板时应能一眼分辨"机构研究"与"媒体报道"（此前新浪/搜狐被标成"研究报告"=专业性硬伤）。
TIER2_MEDIA_DOMAINS: tuple[str, ...] = (
    "36kr.com", "thepaper.cn", "nbd.com.cn", "ebrun.com", "iyiou.com", "p5w.net",
    "finance.sina.com.cn", "sina.com.cn", "sohu.com", "jiemian.com", "linkshop.com.cn",
    "21jingji.com", "xueqiu.com", "cyzone.cn", "lieyunwang.com",
    "qq.com", "ifeng.com", "163.com", "toutiao.com", "baijiahao.baidu.com",
    "huxiu.com", "tmtpost.com", "donews.com", "techweb.com.cn", "cnbeta.com.tw",
    # ⚠️ **补上漏掉的一半财经媒体**（2026-09-16 全量审计）：
    # 第一财经 / 财联社 / 证券时报 / 中国证券网 / 东方财富 此前只登在 `TIER2_DOMAINS` 里，
    # 于是它们在数据源面板被标成「**研究报告**」——评审点开一看，
    # "第一财经"被我们称作研究机构，这正是本段注释开头那句"专业性硬伤"的同型，
    # 只是上一轮只清了新浪/搜狐。
    # 注意：**只加标签、不动层级**——它们仍留在 `TIER2_DOMAINS` 里（tier 2 不受影响），
    # 这里只决定面板上的分组词是「机构研究」还是「财经媒体」。
    "yicai.com", "cls.cn", "stcn.com", "cnstock.com", "eastmoney.com",
)

# 低质内容聚合站（SEO"行业报告"农场 / 文库 / 文档站）：
# 这类站以"行业报告"外形批量生成重复内容，实测常占据通用检索前列
# （且博查不支持 `site:` 限定，无法在查询层排除）。一律判为「网络资料」+ L4，
# **不得**被 `.com/.cn` 兜底规则抬成 L2 半官方——旧行为 = 信源权威性虚高（中研网/华经/报告大厅被当"半官方"）。
LOW_QUALITY_DOMAINS: tuple[str, ...] = (
    "chinairn.com", "askci.com", "huaon.com", "chinabgao.com", "chinabaogao.com",
    "cir.cn", "ipoipo.cn", "baogao.com", "zhiyanzhan.cn", "gonyn.com",
    "51report.com", "report.baidu.com",
    "renrendoc.com", "doc88.com", "docin.com", "book118.com", "docer.com",
    "360doc.com", "wenku.baidu.com", "mbalib.com",
)

# 国际组织官方统计（同为**一手官方口径**，与国内政府统计同级）：
# 世界银行 / IMF / 欧盟统计局 / WHO / OECD / 联合国。给出海与国际对比类问题提供别国权威数字。
INTL_OFFICIAL_DOMAINS: tuple[str, ...] = (
    "worldbank.org", "imf.org", "europa.eu", "eurostat",
    "who.int", "oecd.org", "un.org", "unstats.un.org", "comtradeapi.un.org",
    "fao.org", "data.gov.hk", "censtatd.gov.hk",
)

# 一级权威来源名关键词
TIER1_SOURCES: tuple[str, ...] = (
    "统计局", "政府", "监管", "证监会", "工信部", "商务部", "海关总署", "海关",
    "发改委", "市监局", "央行", "交易所", "审计署", "税务总局",
)
# 二级权威来源名关键词（咨询机构/券商/垂直媒体/平台数据）
TIER2_SOURCES: tuple[str, ...] = (
    "艾瑞", "欧睿", "GfK", "IDC", "Euromonitor", "红餐", "前瞻", "亿欧",
    "弗若斯特", "沙利文", "券商", "招股书", "年报", "白皮书", "研究院",
    "艾媒", "易观", "QuestMobile", "极光", "TalkingData", "亿邦", "头豹",
    "智研", "华经", "中商", "观研", "蝉妈妈", "魔镜", "新榜", "wind", "万得",
)


def route_source(url: str = "", source: str = "") -> tuple[int, str]:
    """信源路由：URL/来源名 → (Tier, 分组标签)。

    白名单命中即权威（宁高勿低）：一级 → (1, "政府官方")；二级 → (2, "研究报告")；
    二级中的财经/垂直媒体 → (2, "财经媒体")（Tier 同 2、**置信度权重不变**，仅分组标签区分）；
    未命中 → (4, "")，由上层按默认聚合器处理。

    ⚠️ 匹配口径（2026-09-11 修正）：**在主机名上做后缀匹配**，不再对整条 URL 做子串包含。
    旧写法 `any(d in url for d in TIER1_DOMAINS)` 会把
    `https://xxx.com/redirect?to=https://www.gov.cn/…` 这类**跳转/引用链接**
    判成「政府官方」——来源面板上多一条看着很硬的权威源，点进去是别人的跳转页。
    详见 `core.domain_match` 病案。
    """
    from core.domain_match import domain_hit

    s = source or ""
    if domain_hit(url, TIER1_DOMAINS) or any(k in s for k in TIER1_SOURCES):
        return 1, "政府官方"
    # 国际组织官方统计（世界银行/IMF/Eurostat/WHO/联合国）：与国内官方统计同级——都是一手官方口径
    if domain_hit(url, INTL_OFFICIAL_DOMAINS):
        return 1, "国际组织"
    # 低质内容农场：优先级高于二级白名单与 .com 兜底（否则中研网/华经会被当"研究报告/半官方"）
    if domain_hit(url, LOW_QUALITY_DOMAINS):
        return 4, "网络资料"
    if domain_hit(url, TIER2_DOMAINS) or any(k in s for k in TIER2_SOURCES):
        if domain_hit(url, TIER2_MEDIA_DOMAINS):
            return 2, "财经媒体"
        return 2, "研究报告"
    return 4, ""


def describe() -> str:
    """白名单说明（供方案书/说明：'Source Router 白名单'是防引用幻觉的硬闸门）"""
    return (
        "信源路由白名单：一级权威（gov.cn/统计局/证监会/海关总署等监管官方 + 交易所公告）"
        "、二级权威（艾瑞/欧睿/GfK/IDC/红餐/前瞻等头部研究机构 + 主流财经媒体）。"
        "命中白名单 → 信源升级 L1/L2 并归入'政府官方/研究报告'分组优先展示；"
        "未命中 → 按默认聚合器处理。可信度先过白名单闸门，再谈内容。"
        "匹配采用**域名后缀语义**（在主机名上匹配，非 URL 全串子串包含）——"
        "`xxx.com/redirect?to=gov.cn` 这类跳转链接不会被误升级成「政府官方」。"
    )
