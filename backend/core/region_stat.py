"""地域统计源下沉：省市统计局——**只有实测通了的才写"可用"**

## 为什么这是真实的覆盖缺口

原来的权威源层只连了**国家统计局**。可实务里大量问题是**地域性**的：
"在杭州开一家咖啡店""成渝火锅市场""苏州工业园区的消费力"——
这些题的官方口径不在北京，在**当地统计局**的《经济运行情况》和《统计月报》里。

一个"杭州咖啡店选址"的调研报告，通篇引国家统计局全国社零、再套一个全国均值去推杭州，
**这在方法论上就是错的**——地域差异（人口结构、消费习惯、竞争密度）恰恰是这类题的全部。

## 这一层怎么做

`REGION_SITES` = **逐站实测**过的省市统计局列表页（2026-09-11 本机，红线同主流程：
不伪造 Referer、不绕验证码、守 robots、per-host ≥1s、遇 403/412 立即停手）。

**实测不通的一律不写进来**，改记 `REGION_BLOCKED`；官网目录里有、但我们没验通的，
只进 `REGION_DOMAIN_INDEX`（仅用于**识别**来源身份，不用于主动取数）。

所以本表有三种状态，**不许混**：
- `REGION_SITES`：实测**能取到条目** → 主动取数
- `REGION_DOMAIN_INDEX`：官方目录收录（可判身份）→ 只在**来源识别**时用
- `REGION_BLOCKED`：实测受阻 → 报告里如实讲"为什么没有"

## 边界（勿越）

- 最多命中 **2 个地域**（再多就不是"地域市场题"，而是"全国题顺带提了城市名"）。
- 省市同时命中时**只留市**（市级更具体；省级数据不能冒充市级）。
- 抽不到地域词 → **一个请求都不发**（不发无谓请求是本层的既有纪律）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 通用链接形态：`…/202609/t20260910_4857753.html`（政府站最主流的一种，路径自带年月+日期）
# 标题用 `(.*?)</a>` 而非 `([^<]+)</a>`：实测杭州/宁波的条目标题里**嵌了 `<span>` 等标签**
# （`<a …><span>2026年7月月报</span>2026-08-28</a>`），只认纯文本会**静默 0 条**——
# 而 0 条与"这个源没数据"在日志里长得一模一样，属于最难发现的那类故障。
# 捕获后统一交 `_strip_html` 去标签。
_LINK_T8 = r'(?s)href="([^"]*?/t\d{8}_\d+\.(?:html?|shtml))"[^>]*>(.*?)</a>'
_DATE_T8 = r"t(\d{8})_"
_DATE_COL = r"/art/(\d{4})/art_"


def _link_col(*cols: str) -> str:
    """限定栏目的文章链接正则——`/col/<栏目id>/art/<年>/art_<hash>.html`

    **必须限定栏目 id**：浙江/杭州/宁波这类站的栏目 id 都是 `colNNNNNNN`，
    不限定就会把"统计要闻""党建工作"里的新闻也抓进来——
    实测浙江首页未限定时抓出的第一条是《向着中国式现代化的光明前景迈进》，
    这种条目进了引用列表比"没取到"更糟。
    """
    alt = "|".join(re.escape(c) for c in cols)
    return (
        r'(?s)href="([^"]*?/col/(?:' + alt + r')/art/\d{4}/art_[0-9a-f]{16,}\.html)"'
        r"[^>]*>(.*?)</a>"
    )


# 标题判据：条目**必须**像一条统计发布，才准进引用。
# 为什么需要：不少统计局首页把"数据发布"和"局内新闻"混在一条流里，
# 实测江苏省统计局未加此门时，头三条是《省统计局开展"七一"走访慰问老党员活动》
# 和《我省举办领导干部"深化政绩观教育"…》——**栏目正确、条目无关**。
# 这道门只做**正向**筛选（像数据才留），不做"不像就删"的黑名单——
# 黑名单会随站点改版失效，白名单词反而更稳。
_TITLE_DATA = (
    r"同比|环比|经济运行|经济形势|经济|统计公报|主要指标|数据解读|统计分析|统计月报|月报|"
    r"指数|价格|消费|市场|规模|投资|产值|工业|服务业|就业|收入|财政|外贸|进出口|"
    r"GDP|CPI|PPI|社会消费品|零售|人口|城镇化|民生|发展|月度数据|核心指标"
)


@dataclass(frozen=True)
class RegionSite:
    """一个已实测可用的地方统计局数据栏目（字段名与 `authority_fetch._ListSite` 对齐，可直接喂 fetch_list_site）"""

    sid: str
    region: str                    # 规范地域名（省/市）
    bureau: str                    # 统计局全称（引用时的发布方）
    level: str                     # province / city
    aliases: tuple[str, ...]       # 触发词（地域名 + 常用别称）
    pages: tuple[str, ...]         # 实测能解析出条目的列表页
    link_re: str                   # 文章链接正则（组1=href，组2=标题）
    date_in: str = ""              # 从 href 提日期（1 组=8位/6位/4位；3 组=年,月,日）
    text_date_re: str = ""         # 从**锚文本**提日期（3 组：年,月,日）——有的站日期只在标题里
    title_re: str = _TITLE_DATA    # 条目标题的**正向**判据（空串 = 不筛）
    parent: str = ""               # 上级地域（省市同时命中时只留市）
    note: str = ""

    @property
    def name(self) -> str:
        """与 `authority_fetch._ListSite.name` 对齐——`fetch_list_site` 直接读这个"""
        return self.bureau



REGION_SITES: tuple[RegionSite, ...] = (
    # ---------------- 直辖市 ----------------
    RegionSite(
        sid="tjj_beijing", region="北京", bureau="北京市统计局", level="city",
        aliases=("北京", "北京市"),
        pages=("https://tjj.beijing.gov.cn/tjsj_31433/",),
        link_re=_LINK_T8, date_in=_DATE_T8,
        note="数据发布/数据解读栏目；实测条目如「2026年8月份北京工业生产者出厂价格同比下降1.4%」",
    ),
    RegionSite(
        sid="tjj_shanghai", region="上海", bureau="上海市统计局", level="city",
        aliases=("上海", "上海市", "沪上"),
        pages=("https://tjj.sh.gov.cn/sjfb/",),
        link_re=r'href="([^"]*?/sjxx/\d{8}/[0-9a-f]{16,}\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"/sjxx/(\d{8})/",
        note="数据发布栏目；实测条目如「2026年1-7月份上海外贸进出口总额比去年同期增长17.7%」。另有 /tjgb/ 统计公报栏目可作补充",
    ),
    RegionSite(
        sid="tjj_chongqing", region="重庆", bureau="重庆市统计局", level="city",
        aliases=("重庆", "重庆市"),
        pages=("https://tjj.cq.gov.cn/zwgk_233/fdzdgknr/tjxx/sjjd_55469/",
               "https://tjj.cq.gov.cn/zwgk_233/fdzdgknr/tjxx/sjzl_55471/jdsj_55474/",
               "https://tjj.cq.gov.cn/zwgk_233/fdzdgknr/tjxx/sjzl_55471/tjgb_55472/"),
        link_re=_LINK_T8, date_in=_DATE_T8,
        note="数据解读 + 进度数据（综合月度数据）+ 统计公报三栏目；实测条目如「2026年7月综合月度数据」",
    ),
    # ---------------- 华东 ----------------
    RegionSite(
        sid="tjj_jiangsu", region="江苏", bureau="江苏省统计局", level="province",
        aliases=("江苏", "江苏省", "苏南", "苏北"),
        pages=("http://tj.jiangsu.gov.cn/",),
        link_re=r'href="([^"]*?/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"/art/(\d{4})/(\d{1,2})/(\d{1,2})/",
        note="首页即时发布经济运行简况；**路径自带完整日期**。站点无 /tjsj/ 一类固定栏目（实测 404），故直接取首页",
    ),
    RegionSite(
        sid="tjj_zhejiang", region="浙江", bureau="浙江省统计局", level="province",
        aliases=("浙江", "浙江省", "浙北", "浙南"),
        pages=("https://tjj.zj.gov.cn/",),
        link_re=_link_col("col1229129214", "col1229129213"), date_in=_DATE_COL,
        note="首页「经济运行情况」+「新闻发布稿」两个栏目（**已限定栏目 id**，否则会把统计要闻/党建新闻一并抓进来，实测踩过）；"
             "**路径只含年份**（无月日），故日期只能精确到年——引用时按此如实标注。另有数据子站 data.tjj.zj.gov.cn",
    ),
    RegionSite(
        sid="tjj_fujian", region="福建", bureau="福建省统计局", level="province",
        aliases=("福建", "福建省"),
        pages=("https://tjj.fujian.gov.cn/xxgk/tjxx/", "https://tjj.fujian.gov.cn/xxgk/tjgb/"),
        link_re=r'href="([^"]*?/t\d{8}_\d+\.htm)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=_DATE_T8,
        note="统计信息 + 统计公报双栏目；实测公报栏目条目最多（含人口抽样调查公报、国民经济和社会发展统计公报）",
    ),
    RegionSite(
        sid="tjj_nanjing", region="南京", bureau="南京市统计局", level="city", parent="江苏",
        aliases=("南京", "南京市"),
        pages=("https://tjj.nanjing.gov.cn/tjxx/",),
        link_re=_LINK_T8, date_in=_DATE_T8,
        note="市级统计局；实测条目如「2026年1—7月全市规模以上工业企业利润增长17.6%」。**省级数据不能冒充市级**，故市与省分开建源",
    ),
    RegionSite(
        sid="tjj_suzhou", region="苏州", bureau="苏州市统计局", level="city", parent="江苏",
        aliases=("苏州", "苏州市", "昆山", "常熟", "张家港"),
        pages=("http://tjj.suzhou.gov.cn/sztjj/index.shtml",),
        link_re=r'href="([^"]*?/sjfbyjd/\d{6}/[0-9a-f]{20,}\.shtml)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"/sjfbyjd/(\d{6})/",
        note="「数据发布与解读」栏目。⚠️ 该栏目的 list.shtml **是 JS 壳**（实测只回 4 个导航链），"
             "条目实际出现在首页，故取首页；**路径只到月**，日期按此标注",
    ),
    RegionSite(
        sid="tjj_hangzhou", region="杭州", bureau="杭州市统计局", level="city", parent="浙江",
        aliases=("杭州", "杭州市", "余杭", "滨江", "萧山", "临平", "桐庐"),
        pages=("https://tjj.hangzhou.gov.cn/",),
        link_re=_link_col("col1229279726"), date_in=_DATE_COL,
        text_date_re=r"(\d{4})-(\d{2})-(\d{2})",
        note="「统计月报」栏目（已限定栏目 id）。⚠️ 栏目页 index.html **是 JS 壳**（实测 7KB、无文章链接），"
             "条目取自首页；**发布日期只写在列表页标题文字里**（如「2026年7月月报2026-08-28」），"
             "故需从锚文本取日期——路径本身只有年份",
    ),
    RegionSite(
        sid="tjj_ningbo", region="宁波", bureau="宁波市统计局", level="city", parent="浙江",
        aliases=("宁波", "宁波市", "慈溪", "余姚"),
        pages=("http://tjj.ningbo.gov.cn/",),
        link_re=_link_col("col1229042910", "col1229042912", "col1229042827"), date_in=_DATE_COL,
        note="首页「统计分析 / 区县分析 / 综合数据」栏目（已限定栏目 id）。⚠️ 栏目页 index.html **是 JS 壳**，条目取自首页；路径只含年份",
    ),
    # ---------------- 华中 ----------------
    RegionSite(
        sid="tjj_henan", region="河南", bureau="河南省统计局", level="province",
        aliases=("河南", "河南省", "郑州", "郑州市"),
        pages=("https://tjj.henan.gov.cn/tjfw/zxfb/",),
        link_re=r'href="([^"]*?/\d{4}/\d{2}-\d{2}/\d+\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"/(\d{4})/(\d{2})-(\d{2})/",
        note="「最新发布」栏目；实测条目如「2026年1-7月份河南省经济运行情况」。郑州市统计局无独立可用源，落到省级",
    ),
    RegionSite(
        sid="tjj_hubei", region="湖北", bureau="湖北省统计局", level="province",
        aliases=("湖北", "湖北省", "武汉", "武汉市"),
        pages=("http://tjj.hubei.gov.cn/tjsj/tjfx/qstjfx/",),
        link_re=r'href="([^"]*?/t\d{8}_\d+\.shtml)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=_DATE_T8,
        note="「全省统计分析」栏目；实测条目如「2026年上半年全省消费市场平稳增长」",
    ),
    RegionSite(
        sid="tjj_hunan", region="湖南", bureau="湖南省统计局", level="province",
        aliases=("湖南", "湖南省", "长沙", "长沙市"),
        pages=("http://tjj.hunan.gov.cn/hntj/tjfx/index.html",),
        link_re=r'href="([^"]*?/t\d{8}_\d+\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=_DATE_T8,
        note="「统计分析」栏目；实测条目如「1-7月湖南经济运行情况」",
    ),
    # ---------------- 西南 / 西北 ----------------
    RegionSite(
        sid="tjj_sichuan", region="四川", bureau="四川省统计局", level="province",
        aliases=("四川", "四川省", "成都", "成都市", "川渝", "绵阳", "德阳"),
        pages=("https://tjj.sc.gov.cn/scstjj/c112117/list.shtml",
               "https://tjj.sc.gov.cn/scstjj/c112126/list.shtml"),
        link_re=r'href="([^"]*?/c\d+/\d{4}/\d{1,2}/\d{1,2}/[0-9a-f]{20,}\.shtml)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"/(\d{4})/(\d{1,2})/(\d{1,2})/[0-9a-f]{20,}\.",
        note="「主要指标数据」+「统计公报」双栏目。**成都市统计局实测 412 反爬挑战（不接）**，故成都类问题落到省级",
    ),
    RegionSite(
        sid="tjj_shaanxi", region="陕西", bureau="陕西省统计局", level="province",
        aliases=("陕西", "陕西省", "西安", "西安市"),
        pages=("https://tjj.shaanxi.gov.cn/tjsj/tjxx/qs/", "https://tjj.shaanxi.gov.cn/"),
        link_re=_LINK_T8, date_in=_DATE_T8,
        note="「统计信息·**全省**」栏目（qs=全省）。⚠️ 早期误用了同级的 qg（全国）栏目——"
             "那里是**陕西省统计局转载的全国数据**，挂「陕西省统计局」发布方会把全国口径说成陕西的。已改 qs",
    ),
    # ---------------- 华南 ----------------
    RegionSite(
        sid="tjj_guangzhou", region="广州", bureau="广州市统计局", level="city", parent="广东",
        aliases=("广州", "广州市", "天河", "番禺"),
        pages=("http://tjj.gz.gov.cn/stats_newtjyw/sjjd/index.html",
               "http://tjj.gz.gov.cn/stats_newtjyw/sjfb/index.html"),
        link_re=r'href="([^"]*?/post_\d+\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in="",
        note="「数据解读」+「数据发布」双栏目；实测条目如「2026年上半年广州经济运行情况」。**链接不含日期**，日期只能留空由正文补齐",
    ),
)


# ============================================================
# 全国统计局官网目录（来源：浙江省统计局首页「统计网」导航，2026-09-11 实测抓取）
# ------------------------------------------------------------
# 用途**仅限来源识别**：给一个 gov 统计站 URL，能说出它是哪个地区的统计局。
# 判身份用它、主动取数**不用它**——没实测过能不能解析，就不假装能。
# ============================================================
REGION_DOMAIN_INDEX: dict[str, tuple[str, str]] = {
    # 域名: (统计局名, 官网)
    "tjj.beijing.gov.cn": ("北京市统计局", "http://tjj.beijing.gov.cn/"),
    "stats.tj.gov.cn": ("天津市统计局", "http://stats.tj.gov.cn/"),
    "tjj.hebei.gov.cn": ("河北省统计局", "http://tjj.hebei.gov.cn/"),
    "tjj.shanxi.gov.cn": ("山西省统计局", "http://tjj.shanxi.gov.cn/"),
    "tj.nmg.gov.cn": ("内蒙古自治区统计局", "http://tj.nmg.gov.cn/"),
    "tjj.ln.gov.cn": ("辽宁省统计局", "http://tjj.ln.gov.cn/"),
    "tjj.jl.gov.cn": ("吉林省统计局", "http://tjj.jl.gov.cn/"),
    "tjj.hlj.gov.cn": ("黑龙江省统计局", "http://tjj.hlj.gov.cn/"),
    "tjj.sh.gov.cn": ("上海市统计局", "http://tjj.sh.gov.cn/"),
    "tj.jiangsu.gov.cn": ("江苏省统计局", "http://tj.jiangsu.gov.cn/"),
    "tjj.zj.gov.cn": ("浙江省统计局", "http://tjj.zj.gov.cn/"),
    "tjj.ah.gov.cn": ("安徽省统计局", "http://tjj.ah.gov.cn/"),
    "tjj.fujian.gov.cn": ("福建省统计局", "http://tjj.fujian.gov.cn/"),
    "tjj.jiangxi.gov.cn": ("江西省统计局", "http://tjj.jiangxi.gov.cn/"),
    "tjj.shandong.gov.cn": ("山东省统计局", "http://tjj.shandong.gov.cn/"),
    "tjj.henan.gov.cn": ("河南省统计局", "https://tjj.henan.gov.cn/"),
    "tjj.hubei.gov.cn": ("湖北省统计局", "http://tjj.hubei.gov.cn/"),
    "tjj.hunan.gov.cn": ("湖南省统计局", "http://tjj.hunan.gov.cn/"),
    "stats.gd.gov.cn": ("广东省统计局", "http://stats.gd.gov.cn/"),
    "tjj.gxzf.gov.cn": ("广西壮族自治区统计局", "http://tjj.gxzf.gov.cn/"),
    "stats.hainan.gov.cn": ("海南省统计局", "http://stats.hainan.gov.cn/tjj/index.html"),
    "tjj.cq.gov.cn": ("重庆市统计局", "http://tjj.cq.gov.cn/"),
    "tjj.sc.gov.cn": ("四川省统计局", "http://tjj.sc.gov.cn/"),
    "stjj.guizhou.gov.cn": ("贵州省统计局", "http://stjj.guizhou.gov.cn/"),
    "stats.yn.gov.cn": ("云南省统计局", "http://stats.yn.gov.cn/"),
    "tjj.xizang.gov.cn": ("西藏自治区统计局", "http://tjj.xizang.gov.cn/"),
    "tjj.shaanxi.gov.cn": ("陕西省统计局", "http://tjj.shaanxi.gov.cn/"),
    "tjj.gansu.gov.cn": ("甘肃省统计局", "http://tjj.gansu.gov.cn/"),
    "tjj.qinghai.gov.cn": ("青海省统计局", "http://tjj.qinghai.gov.cn/"),
    "tj.nx.gov.cn": ("宁夏回族自治区统计局", "http://tj.nx.gov.cn/"),
    "tjj.xinjiang.gov.cn": ("新疆维吾尔自治区统计局", "http://tjj.xinjiang.gov.cn/"),
    "tjj.xjbt.gov.cn": ("新疆生产建设兵团统计局", "http://tjj.xjbt.gov.cn/"),
    "tjj.hangzhou.gov.cn": ("杭州市统计局", "http://tjj.hangzhou.gov.cn"),
    "tjj.ningbo.gov.cn": ("宁波市统计局", "http://tjj.ningbo.gov.cn"),
    "wztjj.wenzhou.gov.cn": ("温州市统计局", "http://wztjj.wenzhou.gov.cn/"),
    "tjj.jiaxing.gov.cn": ("嘉兴市统计局", "http://tjj.jiaxing.gov.cn"),
    "tjj.sx.gov.cn": ("绍兴市统计局", "http://tjj.sx.gov.cn/"),
    "tjj.jinhua.gov.cn": ("金华市统计局", "http://tjj.jinhua.gov.cn"),
    "tjj.qz.gov.cn": ("衢州市统计局", "http://tjj.qz.gov.cn/"),
    "zstj.zhoushan.gov.cn": ("舟山市统计局", "http://zstj.zhoushan.gov.cn"),
    "tjj.zjtz.gov.cn": ("台州市统计局", "http://tjj.zjtz.gov.cn/"),
    "tjj.lishui.gov.cn": ("丽水市统计局", "http://tjj.lishui.gov.cn/"),
    "tjj.nanjing.gov.cn": ("南京市统计局", "http://tjj.nanjing.gov.cn/"),
    "tjj.suzhou.gov.cn": ("苏州市统计局", "http://tjj.suzhou.gov.cn/"),
    "tjj.gz.gov.cn": ("广州市统计局", "http://tjj.gz.gov.cn/"),
    "tjj.sz.gov.cn": ("深圳市统计局", "http://tjj.sz.gov.cn/"),
    "cdstats.chengdu.gov.cn": ("成都市统计局", "https://cdstats.chengdu.gov.cn/"),
    "tjj.wuhan.gov.cn": ("武汉市统计局", "http://tjj.wuhan.gov.cn/"),
    "zjzd.stats.gov.cn": ("国家统计局省级调查总队", "http://zjzd.stats.gov.cn/"),
}


# ============================================================
# 实测受阻（2026-09-11，红线同主流程）——**记下来，材料里不吹**
# ============================================================
REGION_BLOCKED: tuple[dict[str, str], ...] = (
    {"name": "成都市统计局 cdstats.chengdu.gov.cn", "reason": "412 动态反爬挑战（瑞数类）",
     "alt": "落到四川省统计局（已接入）——**省级数据不代表成都市，引用时按省级标注**"},
    {"name": "广东省统计局 stats.gd.gov.cn", "reason": "本机链路 ReadError（连接被重置）",
     "alt": "广州市统计局已接入；深圳/东莞暂无"},
    {"name": "深圳市统计局 tjj.sz.gov.cn", "reason": "数据栏目为 JS 渲染，列表页只回导航壳（实测多次均无文章条目）",
     "alt": "无误（不逆私有 XHR）"},
    {"name": "杭州市数据开放平台 data.hangzhou.gov.cn", "reason": "需注册/申请，非公开直取",
     "alt": "杭州市统计局统计月报已接入"},
    {"name": "浙江省数据子站 data.tjj.zj.gov.cn", "reason": "未验证（本轮未探）",
     "alt": "浙江省统计局首页已接入"},
    {"name": "山东省/安徽省/天津市/河北省等统计局", "reason": "首页可访问但数据栏目为 JS 渲染或路径不可枚举（本轮未解出条目）",
     "alt": "官方目录身份已收录（REGION_DOMAIN_INDEX），仅在来源识别时使用"},
)


# ============================================================
# 地域识别
# ============================================================
def _norm(text: str) -> str:
    return str(text or "")


def detect_regions(text: str, limit: int = 2) -> list[str]:
    """从问题/子任务里抽出**被明确提到**的地域名（按命中长度降序，最多 limit 个）

    只认**完整地域名**（"杭州""江苏省"），不认单字简称（"浙""渝"）——
    单字地名在中文里误命中率极高（人名、品牌名、普通词里都有）。
    """
    t = _norm(text)
    if not t:
        return []
    hits: list[tuple[int, str]] = []
    for s in REGION_SITES:
        best = 0
        for a in s.aliases:
            if a and a in t:
                best = max(best, len(a))
        if best:
            hits.append((best, s.region))
    if not hits:
        return []
    # 长名优先（"杭州市"比"杭州"更明确），同长按出现顺序
    hits.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, r in hits:
        if r not in out:
            out.append(r)
        if len(out) >= limit:
            break
    return out


def sites_for(text: str, limit: int = 2) -> list[RegionSite]:
    """问题涉及的地域 → 对应的统计局源（省与市同时命中时**只留市**）

    为什么要"只留市"：市级数据与省级数据是**两个不同口径**，
    把省级数当成市级数用是地域类报告最典型的硬伤。既然有市级源，就不要用省级顶替。
    """
    regions = detect_regions(text, limit=max(limit, 2))
    if not regions:
        return []
    picked = [s for s in REGION_SITES if s.region in regions]
    # 去掉"子地域已命中"的父地域
    child_parents = {s.parent for s in picked if s.parent}
    picked = [s for s in picked if s.region not in child_parents]
    return picked[:limit]


def stat_bureau_of(url: str) -> str:
    """URL → 它属于哪个统计局（识别用；查不到返回空串）

    **只做识别，不代表我们能从那站取数**——`REGION_DOMAIN_INDEX` 里有相当一部分
    是"官网目录收录但我们没验通"的站。
    """
    try:
        from core.domain_match import host_of

        h = host_of(url)
    except Exception:
        return ""
    if not h:
        return ""
    for d, (name, _home) in REGION_DOMAIN_INDEX.items():
        if h == d or h.endswith("." + d):
            return name
    # 兜底：主机名以 tjj./stats./tj. 开头且是政府域，按"某地统计局"识别为地方统计机构
    if h.endswith(".gov.cn") and h.split(".")[0] in ("tjj", "stats", "tj", "stjj"):
        return "地方统计局"
    return ""


def describe() -> str:
    ok = len(REGION_SITES)
    idx = len(REGION_DOMAIN_INDEX)
    blk = len(REGION_BLOCKED)
    return (
        f"地域统计源下沉：已接入 **{ok}** 个省市统计局数据栏目（北京/上海/重庆/江苏/浙江/福建/南京/苏州/"
        f"杭州/宁波/河南/湖北/湖南/四川/陕西/广州），官方目录身份收录 **{idx}** 个（仅用于来源识别），"
        f"实测受阻 **{blk}** 类如实记录。"
        "地域问题（「在杭州开咖啡店」）的官方口径不在国家统计局，在**当地统计局的《经济运行情况》与《统计月报》**——"
        "通篇引全国社零再套全国均值去推某市，在方法论上就是错的。"
    )
