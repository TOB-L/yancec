"""研策 · 数据源注册表 + 问答→源硬路由（Data Source Registry & Source-aware Routing）

## 为什么要有这一层

到此为止，数据源是**隐式**的：散在 `mcp/search.py`、`core/authority_fetch.py`、
`core/source_router.py` 里，靠代码路径决定用谁。评审看到的是"有些来源挺权威"，
看不到**"研策知道什么问题该信什么源"**。

这一层把数据源从"代码里的 if-else"提升成**可声明、可审计、可展示的注册表**，
并在其上做**问题类型 → 必选源层 的硬路由**：

    用户问题 → 判定 claim 类型 → 必选源层 → 命中/未命中（可见）→ 降级链

这就是"可审计"从口号变成机制的那一步：报告不只知道"我引了谁"，
还知道"这个问题我**本该**引谁、实际引到了没有、没引到走了哪条降级"。

## 三个概念

- **layer（源层）**：L1 官方统计/监管 → L2 法定披露/交易所 → L3 行业机构 → L4 市场发现 → L5 用户声音
- **claim_type（结论类型）**：公司经营数字 / 宏观指标 / 行业规模 / 政策监管 / 竞争格局 / 消费者声音
- **required（必选源层）**：某类 claim 的**一手源**在哪一层；取不到就必须走降级链并**如实标注**

## 诚实边界（勿越）
- `status="connected"` 只标**代码里真的接了**的源；路线图里的源一律 `status="planned"`。
  材料/前端展示按此两栏呈现——**不把没接的东西写成已接入**。
- `authority` 是**信源权威度**（1-5），不是置信度权重；置信度权重仍由
  `core/models.CREDIBILITY_WEIGHTS` 四维公开权重决定，本表不改动任何权重。
- 本表**不改写**结论；路由未命中只产生**可见的降级记录**，不阻断主流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ============================================================
# 源层定义（从"一手"到"二手"的权威梯度）
# ============================================================
LAYERS: dict[str, str] = {
    "L1": "官方统计/监管",
    "L2": "法定披露/交易所",
    "L3": "行业机构/协会",
    "L4": "市场发现/媒体",
    "L5": "用户声音/社媒",
}


@dataclass(frozen=True)
class SourceSpec:
    """一个数据源的声明式描述（机器可读 + 可直接渲染成"数据源体系"页）"""

    id: str
    name: str
    layer: str                     # L1..L5，见 LAYERS
    kind: str                      # official_stat / legal_disclosure / industry_body / web_search / social / internal
    authority: int                 # 1-5，信源权威度（非置信度权重）
    structure: str                 # structured(可计算) / semi(半结构) / text(纯文本)
    access: str                    # api / static_list / page_fetch / internal
    status: str                    # connected / planned
    domains: tuple[str, ...] = ()  # 归属域名（用于落地校验：声称接了就得能对上域名）
    feeds: tuple[str, ...] = ()    # 喂哪些研究维度（市场规模/竞争格局/…）
    note: str = ""                 # 一句话说明（含边界/限制，如"A 股only"）


# ============================================================
# 数据源注册表
# ------------------------------------------------------------
# connected = 代码里真的接了（`mcp/search.py` / `core/authority_fetch.py` / `core/rag.py`）
# planned   = 路线图（9/20 后按场景包逐个上）；**未接就是未接，不许写进"已接入"**
# ============================================================
def _region_domains() -> tuple[str, ...]:
    """地域统计源的归属域名——从 `core.region_stat` **现取**，不手抄一份。
    两份清单一旦各写一遍就必然漂移，而漂移的那天没人会发现（源"还在列表里"，只是域名对不上）。"""
    try:
        from core.domain_match import host_of
        from core.region_stat import REGION_SITES

        return tuple(sorted({host_of(s.pages[0]) for s in REGION_SITES if s.pages}))
    except Exception:
        return ()


def _region_blocked() -> tuple[dict[str, str], ...]:
    """实测受阻的地域源——同样现取，避免"材料里说没接、注册表里说接了" """
    try:
        from core.region_stat import REGION_BLOCKED

        return tuple(dict(x) for x in REGION_BLOCKED)
    except Exception:
        return ()


SOURCES: tuple[SourceSpec, ...] = (
    # ---------- L1 官方统计 / 监管（一手：宏观与政策的地基） ----------
    SourceSpec(
        id="nbs",
        name="国家统计局",
        layer="L1",
        kind="official_stat",
        authority=5,
        structure="semi",
        access="static_list",
        status="connected",
        domains=("stats.gov.cn",),
        feeds=("市场规模", "行业趋势", "区域市场", "消费人群"),
        note="走 /sj/zxfb/ 与 /sj/sjjd/ 静态列表页（数据发布/解读）；按指标族做最长公共子串匹配。**命中后进一步抽取文章正文数值**（主指标同比/环比、累计、分项，以及数据表），把「给链接」变成「给数字」。",
    ),
    SourceSpec(
        id="gov_portal",
        name="政府门户/部委官网",
        layer="L1",
        kind="official_stat",
        authority=5,
        structure="text",
        access="page_fetch",
        status="connected",
        domains=("gov.cn", ".gov"),
        feeds=("政策监管", "行业趋势", "市场规模"),
        note="经通用检索与机构定向补源命中；L1 判定只认域名白名单，不因'来自权威站'就无条件升级。",
    ),
    # ---------- L2 法定披露（一手：企业经营数字的唯一权威口） ----------
    SourceSpec(
        id="cninfo",
        name="巨潮资讯（上市公司法定披露）",
        layer="L2",
        kind="legal_disclosure",
        authority=5,
        structure="semi",
        access="api",
        status="connected",
        domains=("cninfo.com.cn",),
        feeds=("竞争格局", "单位经济", "企业经营", "市场规模"),
        note="证监会指定披露平台。**优先取定期报告（年报/半年报）**；问经营数字时进一步**抽取年报 PDF 正文**——"
             "一次下载同时产出两块：①「主要会计数据」（营收/归母净利/现金流/总资产 + 本地算同比）"
             "②**「行业情况/经营讨论」章节原文摘录**（市占率、行业规模、门店数、竞争格局）。"
             "第二块此前被整块跳过（只读前 28 页抽财务数字）——那是**上市公司自己披露、经审计的行业口径**，"
             "比任何第三方研报都硬。两块合并成一条来源（同一份 PDF 按 URL 去重时后者会被静默丢掉，实测踩过）。"
             "**边界：只覆盖 A 股**——港美股主体走 SEC EDGAR；港股仍缺。",
    ),
    # ---------- L4 市场发现（二手：广度与时效，权威度低但覆盖广） ----------
    SourceSpec(
        id="qwen_search",
        name="通义千问 enable_search",
        layer="L4",
        kind="web_search",
        authority=2,
        structure="text",
        access="api",
        status="connected",
        domains=(),
        feeds=("市场规模", "竞争格局", "消费人群", "行业趋势", "政策监管"),
        note="主文本通道（端到端联网检索 + 来源引用），覆盖最广；**其来源需再经白名单分级**，不直接当权威。",
    ),
    SourceSpec(
        id="bocha",
        name="博查搜索 API",
        layer="L4",
        kind="web_search",
        authority=2,
        structure="semi",
        access="api",
        status="connected",
        domains=(),
        feeds=("竞争格局", "消费人群", "行业趋势"),
        note="**结构化 URL 主源**（带标题/站名/发布日期）。限制：不支持 `site:` 定向——故「机构定向补源」靠域名校验而非查询层限定。",
    ),
    SourceSpec(
        id="bing_scrape",
        name="Bing 结果页（无 key 兜底）",
        layer="L4",
        kind="web_search",
        authority=2,
        structure="text",
        access="page_fetch",
        status="connected",
        domains=("bing.com",),
        feeds=("市场规模", "竞争格局", "消费人群", "行业趋势", "政策监管"),
        note="仅在主通道拿不到真实可点 URL 时补位；已有同题缓存 + 滑窗配额 + 礼貌间隔三道闸（合规收敛）。",
    ),
    SourceSpec(
        id="report_page",
        name="券商/研究机构研报页",
        layer="L3",
        kind="industry_body",
        authority=3,
        structure="text",
        access="page_fetch",
        status="connected",
        domains=(),
        feeds=("市场规模", "竞争格局", "行业趋势"),
        note="命中研报类域名时抓正文作为独立「研报来源」证据，避免只靠媒体转述。",
    ),
    SourceSpec(
        id="kb_assets",
        name="行业知识库（历史调研沉淀）",
        layer="L3",
        kind="internal",
        authority=3,
        structure="semi",
        access="internal",
        status="connected",
        domains=(),
        feeds=("市场规模", "竞争格局", "消费人群", "政策监管"),
        note="本地向量库（chroma + bge-m3）。作为**历史视角**参考，不替代实时取证；已带防自腐护栏（不整卡覆写）。",
    ),
    SourceSpec(
        id="sec_edgar",
        name="SEC EDGAR（美股法定申报）",
        layer="L2",
        kind="legal_disclosure",
        authority=5,
        structure="structured",
        access="api",
        status="connected",
        domains=("sec.gov",),
        feeds=("竞争格局", "单位经济", "企业经营"),
        note="美股一手 10-K/10-Q/20-F/6-K（星巴克 SBUX、瑞幸 LKNCY 等）。**仅走 efts.sec.gov 全文检索与 data.sec.gov 结构化申报**；`/cgi-bin` 被 robots 禁，不碰。须声明含联系方式的 UA、≤10 req/s。",
    ),
    SourceSpec(
        id="sse_edc",
        name="上证e互动（投资者关系问答）",
        layer="L2",
        kind="legal_disclosure",
        authority=4,
        structure="text",
        access="page_fetch",
        status="connected",
        domains=("sseinfo.com",),
        feeds=("竞争格局", "企业经营"),
        note="上市公司**经营表态/投资者关切**一手问答（巨潮只覆盖法定披露，覆盖不到这层）。**仅沪市**；深市互动易是 JS 壳+私有接口，不做。按主体过滤，匹配不到返回空。",
    ),
    # ---------- L3 行业机构/协会（官方数据与企业数据之间的"行业解释层"） ----------
    SourceSpec(
        id="ccgp",
        name="中国政府采购网",
        layer="L1",
        kind="official_stat",
        authority=4,
        structure="semi",
        access="static_list",
        status="connected",
        domains=("ccgp.gov.cn",),
        feeds=("竞争格局", "市场规模"),
        note="财政部指定发布媒体，**采购中标与预算口径**（大客户/渠道侧需求硬证据）。公告路径含日期可增量；按采购/招标/中标等词触发。",
    ),
    SourceSpec(
        id="caam",
        name="中国汽车工业协会 CAAM",
        layer="L3",
        kind="industry_body",
        authority=4,
        structure="semi",
        access="static_list",
        status="connected",
        domains=("caam.org.cn",),
        feeds=("市场规模", "行业趋势"),
        note="汽车行业月度产销/运行口径（行业机构层）。按汽车/整车/新能源车/电车等词触发。",
    ),
    SourceSpec(
        id="ccfa",
        name="中国连锁经营协会 CCFA",
        layer="L3",
        kind="industry_body",
        authority=4,
        structure="semi",
        access="static_list",
        status="connected",
        domains=("ccfa.org.cn",),
        feeds=("竞争格局", "市场规模"),
        note="连锁零售/餐饮百强榜、门店与加盟口径。按连锁/加盟/门店/特许等词触发；robots 未覆盖目标路径。",
    ),
    SourceSpec(
        id="worldbank",
        name="世界银行开放数据",
        layer="L1",
        kind="official_stat",
        authority=4,
        structure="structured",
        access="api",
        status="connected",
        domains=("worldbank.org",),
        feeds=("市场规模", "行业趋势", "区域市场"),
        note="200+ 经济体宏观年序列（GDP/人口/人均/城镇化/通胀…），**免 key 公开 JSON API**，库更新时间可查。给出海/国际对比类问题提供别国官方口径。",
    ),
    SourceSpec(
        id="imf",
        name="IMF World Economic Outlook",
        layer="L1",
        kind="official_stat",
        authority=4,
        structure="structured",
        access="api",
        status="connected",
        domains=("imf.org",),
        feeds=("市场规模", "行业趋势"),
        note="各国 GDP/增速/人口，**含官方未来预测值**（实测到 2031 年）——研究里的**预测锚**，此前只能引二手研报。免 key 公开 API。",
    ),
    SourceSpec(
        id="region_stats",
        name="省市统计局（地域统计源）",
        layer="L1",
        kind="official_stat",
        authority=5,
        structure="semi",
        access="static_list",
        status="connected",
        domains=_region_domains(),
        feeds=("区域市场", "市场规模", "消费人群", "行业趋势"),
        note="**地域问题（「在杭州开咖啡店」）的官方口径不在国家统计局，在地方统计局的"
             "《经济运行情况》《统计月报》《统计公报》里。**已逐站实测接入 16 个（北京/上海/重庆/"
             "江苏/浙江/福建/南京/苏州/杭州/宁波/河南/湖北/湖南/四川/陕西/广州）；"
             "省市同时命中只取市（省级数不能冒充市级）；含**正向标题门**（条目须像统计发布），"
             "否则首页的《走访慰问老党员》《政绩观教育》这类局内新闻会混进引用。"
             "**并做正文数值抽取**：网页散文式（`1—7月，社零287744亿元，同比增长4.2%`）与"
             "附件 PDF 表格式（实测杭州统计局的月报正文是一份 106KB 附件，网页只有一句"
             "「发布日期/来源」）两种形态都抽——**只给标题等于没给数据**。实测 12/16 个站可产出结构化数值。"
             "实测受阻的（成都 412、深圳 JS 壳、广东链路）如实记录、不接。",
    ),
    SourceSpec(
        id="cnipa",
        name="国家知识产权局（官网公告）",
        layer="L1",
        kind="official_stat",
        authority=4,
        structure="text",
        access="static_list",
        status="planned",
        domains=("cnipa.gov.cn",),
        feeds=("行业趋势", "政策监管"),
        note="政策公告/法规/统计类公开列表（实测可用）。**专利逐件检索子系统被反爬拦，不做**；未接入。",
    ),
)


# ------------------------------------------------------------
# 「未探测」的理由分类（**一处定义，两处使用**）
# ------------------------------------------------------------
# 消费方：`core/source_health.py`（巡检台账与面板摘要）、
#         `core/verification.py`（引文核验报告里那句"N 条未探测"）。
#
# 为什么必须只有一处：这两处此前各写各的判断，其中健康面板那处是
# **三元子串匹配 + 认不出返回空串**，于是 SEC 那条（detail 写"本机未启用：
# SEC_UA_CONTACT 未配置"）既不含"预算用尽"也不含"合规姿态" → `skip_kind=""`
# → 摘要里 `skipped_compliance + skipped_budget (0+0) != skipped (1)`，
# 两个数并排印在面板上，读者一减就发现对不上。
#
# 更要紧的是这四类理由对读者是**完全不同的事实**，不许共用一个说法：
#   compliance —— 站点 robots 明令 Disallow（**站点定的规矩**，我们照办）
#   config     —— 我们没配好联系方式（SEC fair-access 要求 UA 带真实联系人）
#                 （**这是我们自己的配置缺口，不是任何合规姿态**）
#   switch     —— 总开关 AUTHORITY_FETCH_ENABLED 被关掉（**我们主动关的**）
#   budget     —— 本轮巡检预算用尽（**我们没跑完**）
# `verification.py` 曾把 robots Disallow 与 SEC 联系方式未配置并列成一句
# 「按合规姿态未探测」——**等于把配置缺口讲成合规美德**。
SKIP_REASONS: tuple[tuple[str, str], ...] = (
    # 有序：先匹配到的赢。`budget`/`switch` 的文案里可能含"合规"字样，必须排在 compliance 前。
    ("预算用尽", "budget"),
    ("AUTHORITY_FETCH_ENABLED", "switch"),
    ("总开关", "switch"),
    ("SEC_UA_CONTACT", "config"),
    ("未配置", "config"),
    ("本机未启用", "config"),
    ("robots", "compliance"),
    ("合规姿态", "compliance"),
)

SKIP_REASON_LABELS: dict[str, str] = {
    "compliance": "站点 robots 明令禁止（按红线不访问）",
    "config": "本机配置缺口（未配联系方式 → 该通道 fail-closed）",
    "switch": "总开关已关闭",
    "budget": "本轮巡检预算用尽（**我们没跑完**，不是合规停用）",
    "unclassified": "**未分类**（出现即说明有新的未探测理由没进 SKIP_REASONS）",
}


def classify_skip_reason(detail: Any) -> str:
    """把一条「未探测」的 detail 归到一个**具名理由**；不是未探测则返回空串。

    兜底是**显式**的 `unclassified`，不是空串：宁可多一个刺眼的桶，
    也不许新出现的理由静默落进"失败"——那正是上一版漏掉 SEC 的方式。
    """
    t = str(detail or "")
    if "未探测" not in t:
        return ""
    for marker, code in SKIP_REASONS:
        if marker in t:
            return code
    return "unclassified"


def skip_reason_label(code: str) -> str:
    return SKIP_REASON_LABELS.get(code or "", "")


# ============================================================
# 受阻源台账（实测不可用 —— 记下来，避免重复踩坑 + 材料里不吹）
# ------------------------------------------------------------
# 2026-09-11 本机实测（中国大陆网络，经代理链路，可能放大风控）：
# 逐个按红线探测（不伪造 Referer、不回放 cookie、不绕验证码、不逆私有接口、守 robots），
# 遇 403/412 立即停止。结论：**大部分部委级源在当前链路下无法自动化取数。**
# ============================================================
BLOCKED_SOURCES: tuple[dict[str, str], ...] = (
    {"name": "国家数据 data.stats.gov.cn", "reason": "WAF 403（reason:UrlACL）", "alt": "已改用 www.stats.gov.cn 静态列表页（nbs，已接入）"},
    {"name": "海关总署统计查询平台", "reason": "412 动态反爬挑战（瑞数类）", "alt": "无误"},
    {"name": "信用中国", "reason": "412 动态反爬挑战（robots 本身不禁）", "alt": "无误"},
    {"name": "商务部商业特许经营备案查询", "reason": "服务端对非浏览器客户端空回复（连接重置）", "alt": "加盟备案核验暂无机制化来源"},
    {"name": "工信部/信通院 CAICT", "reason": "412 反爬挑战", "alt": "无误"},
    {"name": "互动易 irm.cninfo.com.cn", "reason": "纯 JS 壳，取数需逆向私有 XHR", "alt": "上证e互动（深市暂无对应）"},
    {"name": "国家知识产权局专利检索子系统", "reason": "412 反爬挑战", "alt": "官网 HTML 列表可用（未接入）"},
    {"name": "中国物流与采购联合会", "reason": "源站 502 / 证书故障", "alt": "无误"},
    {
        "name": "HKEXnews 标题搜寻（结构化公告清单）",
        "reason": "纯 GET 恒返 0 条——该页是 JSF 回传（javax.faces.ViewState）+ Akamai/F5 会话",
        "alt": "**确认受阻，不再投入**：冷客户端拿不到服务端会话里的检索条件，绕不过也不该绕",
    },
    {
        "name": "HKEXnews 内容搜寻（AdvSearch，未文档化端点）",
        "reason": "实测**可用**（GET/JSON/中文/翻页/排序全通，直吐 PDF 直链），但港交所"
                  "**从未发布过该接口文档**（官方搜寻指南只讲 UI；WCF `/help` 返 404），"
                  "且使用条款写明未经同意不得转载/使用/链接 —— 属**未授权未公开内部接口**",
        "alt": "**主动不接**（自律，非能力问题）：宁可缺港股一手源，也不拿"
               "未文档化端点去填；材料里按'我们放弃了什么、为什么'如实讲，比多一个源更有分量",
    },
    {
        "name": "消费者声音类（黑猫投诉/知乎/微博/大众点评/小红书/豆瓣/B站）",
        "reason": "小红书 robots `Disallow: /`（未访问）；知乎 `Disallow: /`；贴吧超时；"
                  "微博访客系统拦截；大众点评触发 spiderindefence 安全验证（已停手）；"
                  "黑猫/豆瓣/B站页面可读，但**无法区分真实反馈与水军**，且投诉平台"
                  "只收负面、有系统性选择偏差，且都缺可复现的检索口径",
        "alt": "**全线不接**：这些只能产出'未经核实的网络线索'，不能充当一手消费者证据，"
               "更不能支撑'满意度 X%'这类结论。要一手声音，正解仍是自采问卷/访谈",
    },
    {
        "name": "CNNIC",
        "reason": "robots 无 `*` 组但全部具名爬虫 Disallow:/ + Crawl-delay 120（运营方意图明确拒绝）",
        "alt": "改走官方报告发布渠道",
    },
    *_region_blocked(),
)

# ============================================================
# 结论类型 → 必选源层（问答→源硬路由的核心规则表）
# ------------------------------------------------------------
# 规则来源：研究员做 Source Selection 的常识——
#   公司经营数字 → 先查法定披露，不看门户转述；
#   宏观指标     → 先查官方统计，不用"某报告说"；
#   政策监管     → 先查部委原文，不用二手解读；
#   消费者声音   → 官方数据回答不了，反而以社媒/评价为主。
# `required` 是**一手源所在层**；`accept` 是可接受的替代层（降级链）。
# ============================================================
@dataclass(frozen=True)
class ClaimRoute:
    key: str                    # claim 类型机器码
    label: str                  # 中文名（前端/报告展示）
    triggers: tuple[str, ...]   # 触发词（命中任一即判为该类型）
    required: str               # 必选源层（一手）
    accept: tuple[str, ...]     # 可接受的降级层（按序）
    preferred: tuple[str, ...]  # 优先源 id（按序）
    degrade: str                # 降级说明（未命中一手源时报告里怎么讲）
    pair: str                   # 双源配对要求（验证对）


CLAIM_ROUTES: tuple[ClaimRoute, ...] = (
    ClaimRoute(
        key="company_financial",
        label="公司经营数字",
        triggers=("营收", "净利润", "毛利", "门店数", "单店", "财报", "年报", "公告", "上市公司", "财报数据", "收入结构"),
        required="L2",
        accept=("L2", "L3", "L4"),
        preferred=("cninfo", "sec_edgar", "report_page"),
        degrade="未取到法定披露原文 → 该数字降级为**第三方转述**，须在报告中标注（不得与财报原文同等采信）",
        pair="法定披露（一手）+ 第三方（媒体/研报）——用于交叉验证口径一致性",
    ),
    ClaimRoute(
        key="macro_indicator",
        label="宏观指标",
        triggers=("GDP", "人口", "居民收入", "人均", "社零", "社会消费品零售", "CPI", "PPI", "城镇化", "工业增加值", "就业"),
        required="L1",
        accept=("L1", "L3", "L4"),
        preferred=("nbs", "gov_portal", "report_page"),
        degrade="未取到官方统计口径 → 只能用第三方测算，须标注口径差异（官方口径 vs 机构测算常差 10-30%）",
        pair="官方统计（一手）+ 行业机构（口径解释）",
    ),
    ClaimRoute(
        key="industry_size",
        label="行业规模与增速",
        triggers=("市场规模", "行业规模", "增速", "增长率", "CAGR", "渗透率", "天花板", "容量", "TAM", "SAM", "SOM"),
        required="L1",
        accept=("L1", "L3", "L4"),
        preferred=("nbs", "gov_portal", "report_page", "kb_assets"),
        degrade="官方无该细分口径 → 采用机构研报测算，须标注**测算方法差异**并给出区间而非单点",
        pair="官方统计/总量（一手）+ 行业研报（结构拆分）——Two-source triangulation",
    ),
    ClaimRoute(
        key="policy_regulation",
        label="政策与监管",
        triggers=("政策", "监管", "备案", "许可", "资质", "法规", "标准", "合规", "条例", "补贴", "扶持"),
        required="L1",
        accept=("L1", "L3"),
        preferred=("gov_portal", "nbs", "report_page"),
        degrade="未取到政策原文 → **不得**引用二手解读作为合规依据（法规误读是最高危错误）",
        pair="部委原文（一手）+ 官方解读/权威媒体（释义）",
    ),
    ClaimRoute(
        key="competitive_landscape",
        label="竞争格局与份额",
        triggers=("竞争", "市场份额", "格局", "品牌", "对手", "头部", "集中度", "排名"),
        required="L2",
        accept=("L2", "L3", "L4"),
        preferred=("cninfo", "report_page", "bocha"),
        degrade="无上市公司披露覆盖 → 用研报/媒体口径，须标注**份额口径**（出货量口径 vs 零售额口径不同）",
        pair="企业侧（披露/官网）+ 第三方（研报/媒体）",
    ),
    ClaimRoute(
        key="consumer_voice",
        label="消费者声音",
        triggers=("用户", "口碑", "评价", "痛点", "复购", "消费者", "需求", "种草", "体验", "满意度"),
        required="L5",
        accept=("L5", "L4", "L3"),
        preferred=("qwen_search", "bocha", "report_page"),
        degrade="无一手调研（问卷/访谈）→ 只能给**二手舆情线索**，报告中必须显式声明未经一手验证。"
                "（2026-09-11 专项侦察了黑猫投诉/知乎/微博/大众点评/小红书/豆瓣/B站：小红书与知乎 "
                "robots 全站 Disallow，微博/大众点评触发风控，黑猫与豆瓣虽可读但**无法区分真实反馈与"
                "水军**、且投诉平台只收负面有系统性偏差、缺可复现取样口径 → **全线不接**。"
                "要一手声音，正解只有自采问卷/访谈。）",
        pair="社媒/评价（现象）+ 行业报告（解释）",
    ),
)

# 兜底：无法归类时按"广域发现"处理（不因判不出类型就阻断取证）
_DEFAULT_KEY = "industry_size"


def route_claim(text: str) -> ClaimRoute:
    """按触发词判定 claim 类型（命中数最多者胜；都不中 → 兜底 industry_size）

    为什么用"命中数最多"而不是"先命中先返回"：一段子任务描述常同时含
    "市场规模"和"政策"（如"政策扶持下的市场规模测算"）。取命中数最多者，
    更贴近该子任务的**主结论类型**；并列时按 CLAIM_ROUTES 顺序（公司数字优先，
    因为一手源最硬、取不到时标注代价最大）。
    """
    t = text or ""
    best: ClaimRoute | None = None
    best_n = 0
    for r in CLAIM_ROUTES:
        n = sum(1 for k in r.triggers if k in t)
        if n > best_n:
            best, best_n = r, n
    if best:
        return best
    return next(r for r in CLAIM_ROUTES if r.key == _DEFAULT_KEY)


@dataclass
class RouteDecision:
    """一次取证的路由判定（可审计对象，进 trace / 报告披露）"""

    claim_key: str = ""
    claim_label: str = ""
    required_layer: str = ""
    required_label: str = ""
    preferred: list[str] = field(default_factory=list)
    # 可接受的**降级层**（来自 `ClaimRoute.accept`）。2026-09-17 补：
    # 这个字段在 `ClaimRoute` 里一直存在，却**从来没被带进 RouteDecision**，
    # 于是 `mark_hit` 根本看不到它——见 `mark_hit` 里那段病案。
    accept: list[str] = field(default_factory=list)
    degrade: str = ""
    pair: str = ""
    # 命中结果（取证后回填）
    hit_sources: list[str] = field(default_factory=list)
    hit_layers: list[str] = field(default_factory=list)
    satisfied: bool = False

    def audit_line(self) -> str:
        """一行可读的审计记录（进思考过程 / 日志）"""
        if not self.claim_key:
            return ""
        mark = "✓ 命中" if self.satisfied else "✗ 未命中"
        got = "/".join(self.hit_layers) if self.hit_layers else "无"
        return (
            f"源路由[{self.claim_label}] 要求{self.required_layer}({self.required_label}) → {mark}；实得 {got}"
        )

    def to_dict(self) -> dict:
        """纯 dict 形态（进 checkpoint / 前端展示；dataclass 直接塞 state 对序列化不友好）"""
        return {
            "claim_key": self.claim_key,
            "claim_label": self.claim_label,
            "required_layer": self.required_layer,
            "required_label": self.required_label,
            "preferred": list(self.preferred),
            "degrade": self.degrade,
            "pair": self.pair,
            "hit_layers": list(self.hit_layers),
            "satisfied": self.satisfied,
            "audit": self.audit_line(),
        }


def layer_of_source(spec_id: str) -> str:
    """源 id → 所在层（未知返回 ''）"""
    for s in SOURCES:
        if s.id == spec_id:
            return s.layer
    return ""


def runtime_block_reason(s: SourceSpec) -> str:
    """该源**登记为已接入、但当前运行时不可用**的原因；可用则返回空串。

    病案（2026-09-15 外部审计）：`sec_edgar` 的 `status="connected"` 是**静态登记**，
    而 `core/authority_fetch.py` 在 `SEC_UA_CONTACT` 未配置时 **fail-closed、不发任何请求**
    （这是正确的合规行为——fair-access 要求 UA 带真实联系方式）。
    于是报告附录一边印「已接入 N 个源」并逐条列出 SEC EDGAR，一边整条 SEC 通道根本没在跑。
    **披露层把已停用的源写成"已接入"，是反向声明**——正是本产品在批评的那种"说得比实际多"。

    修法：把"运行时可用性"并入 `connected_sources()` 的判据，而不是只写在启动日志里。
    这条同时也让 `core/tool_select.py` 不再把额度浪费在一个发不出请求的源上。
    """
    if s.id == "sec_edgar":
        try:
            from core.authority_fetch import sec_contact_configured

            if not sec_contact_configured():
                return (
                    "本机未启用：`SEC_UA_CONTACT` 未配置（SEC fair-access 要求 UA 含真实联系方式）"
                    "→ 该通道 fail-closed，本次**未发出任何请求**"
                )
        except Exception:
            return ""
    return ""


def blocked_runtime_sources() -> list[tuple[SourceSpec, str]]:
    """已登记接入、但当前运行时不可用的源 → [(spec, 原因)]（诚实第三栏）"""
    out: list[tuple[SourceSpec, str]] = []
    for s in SOURCES:
        if s.status != "connected":
            continue
        _why = runtime_block_reason(s)
        if _why:
            out.append((s, _why))
    return out


def connected_sources() -> list[SourceSpec]:
    """**当前真的能用的**已接入源（已排除运行时被停用的，如未配 UA 时的 SEC）"""
    return [
        s for s in SOURCES
        if s.status == "connected" and not runtime_block_reason(s)
    ]


def planned_sources() -> list[SourceSpec]:
    return [s for s in SOURCES if s.status == "planned"]


def route_query(raw_question: str = "", subtask_desc: str = "", queries: list[str] | None = None) -> RouteDecision:
    """问答→源硬路由：给定一个问题/子任务 → 该用什么源层、优先哪个源、取不到怎么降级"""
    text = f"{raw_question or ''} {subtask_desc or ''} " + " ".join(queries or [])
    r = route_claim(text)
    return RouteDecision(
        claim_key=r.key,
        claim_label=r.label,
        required_layer=r.required,
        required_label=LAYERS.get(r.required, ""),
        preferred=list(r.preferred),
        accept=list(r.accept),
        degrade=r.degrade,
        pair=r.pair,
    )


def mark_hit(decision: RouteDecision, citations_or_raw: list[dict]) -> RouteDecision:
    """取证后回填：实际拿到了哪些层 → 判定 required 是否满足

    判定口径 = **实际拿到的 URL 按白名单路由后的层**（不是检索器自己声明的来源类型），
    与 `_page_level` / `route_source` 同一口径——防"自称权威"绕过。
    """
    try:
        from core.source_router import route_source
    except Exception:
        return decision
    layers: list[str] = []
    srcs: list[str] = []
    for it in citations_or_raw or []:
        if not isinstance(it, dict):
            continue
        u = str(it.get("url") or "")
        if not u.startswith("http"):
            continue
        try:
            tier, _grp = route_source(u, str(it.get("publisher") or it.get("source") or ""))
        except Exception:
            continue
        # tier 1 → L1，tier 2 → L2/L3（研报=机构层），其余 → L4
        if tier <= 1:
            lay = "L1"
        elif tier == 2:
            lay = "L3" if _grp == "研究报告" else "L2"
        else:
            lay = "L4"
        if lay not in layers:
            layers.append(lay)
        srcs.append(u)
    decision.hit_layers = layers
    decision.hit_sources = srcs[:8]
    # ## 「更权威」必须满足「要求权威」（2026-09-16 修）
    #
    # 原先写的是 `decision.required_layer in layers` —— **逐字相等**才算满足。
    # 于是出现一个方向完全反了的判定：
    #   · 手里握着**巨潮 / SEC 的法定披露原文**（`route_source` 实测判 tier 1 → L1）
    #     → `"L2" in ["L1"]` 为 False → **判"未命中"**；
    #   · 拿到一条**新浪财经转载**（tier 2 → L2）→ `"L2" in ["L2"]` → **判"命中"**。
    # 报告附录据此印出「未取到法定披露原文」——而法定披露原文就在我们手里。
    # `core/replan.py` 那条提示写得很清楚，这个字段的语义是**下限**
    #（"换检索角度去找 X 层的一手来源"），不是"必须恰好等于"。
    # 一个越权与一个不达标的判定长成同一个样子，是这套层级设计最不该有的错误。
    _RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
    _need = _RANK.get(str(decision.required_layer or "").strip())
    if _need is not None:
        # ── 权威度要求（L1–L4）：`required` 是**下限**，越权威越满足 ──
        decision.satisfied = any(_RANK.get(_l, 99) <= _need for _l in layers)
    else:
        # ── 类型要求（目前只有 L5「用户声音/社媒」）：**只能按 accept 匹配，不许按权威度兜底** ──
        #
        # ⚠️ **这是本跑报告里印着的一个假 ✓**（2026-09-17 实跑抓到）：
        # 报告的路由审计表逐行印着
        #     | st_3 | 消费者声音 | L5 用户声音/社媒 | ✅ | **L1/L2** |
        # 而总表写着「命中必选源层 **14 个 / 未命中 0 个**」——**100% 通过率本身就是一个坏谓词的形状**。
        #
        # 根因有两层：
        #   ① `_RANK` 里**没有 L5**，`.get(..., 99)` 兜底成 99 → 任何 L1–L4 都 ≤ 99 → **恒判命中**；
        #   ② 更根本的是**建模错了**：L1–L4 是"一手→二手"的**权威梯度**，而 L5 是**另一类证据**。
        #      把 L5 摆在权威轴末端，就必然推出"要求用户声音、拿到统计局宏观数也算满足"——
        #      而这两者根本不是一回事，跟 R7 抓的「拿政府来源背书企业微观数据」是同一个错。
        #      所以这里按 `accept`（`ClaimRoute.accept`，本跑之前它压根没传进来）做**类型匹配**。
        #
        # 修好之后：消费者声音这一维若只拿到 L1/L2，会如实判**未命中**，
        # 进而触发 `core/voice_boundary` 打上"无一手用户声音源"的边界声明——
        # **那条声明此前结构上不可能被打印出来**（这一处修的是两个缺陷）。
        _ok_set = {str(x) for x in (decision.accept or [])}
        decision.satisfied = any(_l in _ok_set for _l in layers) if _ok_set else bool(layers)
    return decision


# 不要求有归属域名的源：它们是**检索通道/本地库**，本来就没有"一个域名"。
# 这张名单是**显式**的——新增无域名源必须在这里登记，否则自检会报出来。
_NO_DOMAIN_OK = frozenset({"qwen_search", "bocha", "bing_scrape", "report_page", "kb_assets", "gov_portal"})


def validate_registry() -> list[str]:
    """注册表自检 → 问题清单（空 = 通过）

    为什么要有它（2026-09-14 审计）：`SourceSpec.domains` 的注释写着
    "用于落地校验：声称接了就得能对上域名"，但**全仓无人消费**，`/api/source-registry`
    也不返回这个字段——也就是说"数据源体系"页上那个用来建立信任的字段是**空的**，
    而注释里的那句承诺从未被实现。**材料里不能出现一个不存在的机制。**
    这里把它变成真的：每个 `status="connected"` 的源，要么给出归属域名，
    要么在 `_NO_DOMAIN_OK` 里显式声明"它是检索通道、没有单一域名"。
    """
    problems: list[str] = []
    ids = [x.id for x in SOURCES]
    if len(ids) != len(set(ids)):
        problems.append("存在重复的源 id")
    for x in SOURCES:
        if x.status != "connected":
            continue
        if not x.domains and x.id not in _NO_DOMAIN_OK:
            problems.append(f"{x.id} 声称已接入但没有归属域名，也不在 _NO_DOMAIN_OK 名单里")
        for d in x.domains:
            if not d or " " in d or "://" in d:
                problems.append(f"{x.id} 的域名 `{d}` 不是裸域名（不要带协议/空格）")
    # 地域统计局：域名从 region_stat 现取，这里校验"现取"确实拿到了东西
    _rs = next((x for x in SOURCES if x.id == "region_stats"), None)
    if _rs is not None and not _rs.domains:
        problems.append("region_stats 的域名列表为空（region_stat 可能导入失败）")
    return problems


def describe() -> str:
    """注册表说明（供方案书/说明/前端"数据源体系"页）"""
    c, p = len(connected_sources()), len(planned_sources())
    _probs = validate_registry()
    return (
        f"数据源注册表：已接入 {c} 个源 / 规划中 {p} 个，按权威梯度分 5 层"
        f"（{' → '.join(f'{k} {v}' for k, v in LAYERS.items())}）。"
        "在此之上做**问答→源硬路由**：先判结论类型（公司经营数字/宏观指标/行业规模/政策监管/竞争格局/消费者声音），"
        "再定**必选源层**（一手源在哪一层），取证后回填**命中/未命中**，未命中走降级链并在报告中如实标注。"
        "这让'可审计'从口号变成机制：报告不只知道引了谁，还知道**本该引谁、实际引到没有**。"
        + ("注册表自检：✅ 通过（每个已接入源都能对上归属域名）" if not _probs else f"注册表自检：⚠️ {len(_probs)} 项待处理")
    )
