"""来源血缘：把"转述"从"独立佐证"里剥出来

## 为什么这是数据层最关键的一块

现在的独立佐证数是按**域名**数的：21世纪经济报道、每日经济新闻、36氪各发一篇
"咖啡市场规模 6,562 亿元（据艾瑞咨询）"，我们算 **3 个独立来源** —— 置信度被顶到"高"。
可真相是：**一个源头，被转述了三次**。

这正是研究者每天踩的坑，也是"信息不透明"最具体的样子：
数字到处都是、看着互相印证，其实全都指向同一个原始出处；只要那个出处错了，
所有"佐证"一起错，而报告上写着"多源交叉验证通过"。

**我们要做的是相反的事：把 5 条转述收敛成 1 条原始 + 4 条回声，并如实标出来。**
这比多接十个源更能提升权威性——因为它是**减法**，减掉的是虚假的确定性。

## 判断规则
- 从文本里抽"这个数字的原始出处"（据X / X数据显示 / X报告显示 / 根据X / 援引X…）。
- **只有转述方与被转述方不是同一家时**才算"转述"（艾瑞官网发自己的数据 = 一手，不是转述）。
- 抽不到明确出处的，退回按域名算独立——**不臆测**（宁可高估独立，也不要把一手源误判成回声）。
"""
from __future__ import annotations

import re

# 机构别名 → 规范名（value 是它自己的域名根，用于判"是不是同一家"）
_ORIGINS: dict[str, tuple[str, str]] = {
    # 别名: (规范名, 自己的域名根)
    "艾瑞": ("艾瑞咨询", "iresearch.com.cn"),
    "iresearch": ("艾瑞咨询", "iresearch.com.cn"),
    "欧睿": ("欧睿国际", "euromonitor.com"),
    "euromonitor": ("欧睿国际", "euromonitor.com"),
    "尼尔森": ("尼尔森", "nielsen.com"),
    "nielsen": ("尼尔森", "nielsen.com"),
    "凯度": ("凯度", "kantar.com"),
    "kantar": ("凯度", "kantar.com"),
    "国家统计局": ("国家统计局", "stats.gov.cn"),
    "统计局": ("国家统计局", "stats.gov.cn"),
    "前瞻产业研究院": ("前瞻产业研究院", "qianzhan.com"),
    "前瞻": ("前瞻产业研究院", "qianzhan.com"),
    "中商产业研究院": ("中商产业研究院", "askci.com"),
    "智研咨询": ("智研咨询", "chyxx.com"),
    "观研": ("观研天下", "chinabaogao.com"),
    "头豹": ("头豹研究院", "leadleo.com"),
    "leadleo": ("头豹研究院", "leadleo.com"),
    "麦肯锡": ("麦肯锡", "mckinsey.com"),
    "贝恩": ("贝恩", "bain.com"),
    "德勤": ("德勤", "deloitte.com"),
    "普华永道": ("普华永道", "pwchk.com"),
    "毕马威": ("毕马威", "kpmg.com"),
    "波士顿咨询": ("BCG", "bcg.com"),
    "美团研究院": ("美团研究院", "meituan.com"),
    "中国连锁经营协会": ("中国连锁经营协会", "ccfa.org.cn"),
    "中国烹饪协会": ("中国烹饪协会", "ccas.com.cn"),
    "中国汽车工业协会": ("中国汽车工业协会", "caam.org.cn"),
    "questmobile": ("QuestMobile", "questmobile.com.cn"),
    "quest mobile": ("QuestMobile", "questmobile.com.cn"),
    "gfk": ("GfK", "gfk.com"),
    "idc": ("IDC", "idc.com"),
    "cntrend": ("CTR", "ctrmarketresearch.com"),
    "秒针": ("秒针系统", "miaozhen.com"),
    "易观": ("易观分析", "analysys.cn"),
    "talkingdata": ("TalkingData", "talkingdata.com"),
    "极光": ("极光", "jiguang.cn"),
    "world bank": ("世界银行", "worldbank.org"),
    "世界银行": ("世界银行", "worldbank.org"),
    "imf": ("IMF", "imf.org"),
    "欧睿信息咨询": ("欧睿国际", "euromonitor.com"),
}

# "谁说的"句式：把出处从句子里抠出来（**只认显式归属**，不做指代消解）
#
# 为什么是**贪婪捕获 + 最长前缀匹配**，而不是"懒匹配 + 后缀候选词"：
# 后者在 `据国家统计局数据…` 上会把归属截成 **"国家"**——因为懒匹配走到"国家"时，
# 前瞻里的候选词 `统计` 恰好能匹配上后面的"统计局"，于是提前收口。
# 越是权威的机构名越容易含这些词（统计局/数据中心/信息中心），这个坑专坑最该认对的那批。
# 现在改成：贪婪吃到标点为止，再从长到短试着规范化，取**最长的那个能认出来的**。
_ATTR = re.compile(
    r"(?:据|根据|援引|引自|引用|来源[:：]?|出自)\s*([一-鿿A-Za-z][一-鿿A-Za-z\s\.&·]{0,24})"
)
# 归属名的右边界：标点、书名号、引号、空白
_ATTR_CUT = re.compile(r"[，,、。；;：:！!？?）)】》」」\"'“”\s（(【《「]")
_ATTR_POST = re.compile(
    r"([一-鿿A-Za-z][一-鿿A-Za-z\s\.&·]{1,18}?)"
    r"(?:数据显示|报告显示|发布的数据|统计显示|测算|调研显示|白皮书显示|发布的报告)"
)


def _canon(name: str) -> tuple[str, str] | None:
    """别名 → (规范名, 自有域名)。命中不了返回 None（**不猜**）"""
    n = (name or "").strip().strip("《》\"'“” ").lower()
    if not n:
        return None
    if n in _ORIGINS:
        return _ORIGINS[n]
    # 最长别名优先（"国家统计局" 不能被 "统计局" 抢先，但两者规范名相同，无碍；
    # 关键是 "中国连锁经营协会" 不被 "中国" 类前缀截断）
    for alias in sorted(_ORIGINS, key=len, reverse=True):
        if alias in n:
            return _ORIGINS[alias]
    return None


def extract_origin(*texts: str) -> tuple[str, str]:
    """从文本抽"这个数字的原始出处" → (规范名, 自有域名)；抽不到返回 ("", "")

    只认**显式归属**（"据艾瑞咨询""国家统计局数据显示"）。**不做指代消解、不做推断**——
    把"某报告认为"这类含糊表述当成出处，比不认更糟：那会凭空造出一个不存在的源头。
    """
    for t in texts:
        s = str(t or "")
        if not s:
            continue
        for rx in (_ATTR, _ATTR_POST):
            for m in rx.finditer(s):
                raw = _ATTR_CUT.split(m.group(1) or "")[0].strip()
                # 从**最长**开始试：`国家统计局数据` 要先试整串、再退到 `国家统计局`，
                # 不能一上来就取短前缀（那会把"国家"当成机构名）
                for cut in range(len(raw), 1, -1):
                    hit = _canon(raw[:cut])
                    if hit:
                        return hit
    return ("", "")


def _host_root(url_or_host: str) -> str:
    h = str(url_or_host or "").strip().lower()
    h = re.sub(r"^https?://", "", h).split("/")[0].removeprefix("www.")
    return h


def classify(page_domain: str, *texts: str) -> tuple[str, str]:
    """一条来源页 → (规范出处, 自有域名)。用途是判"它是一手，还是在替别人说话"

    返回的域名与 `page_domain` **相同** → 一手（自己发的自己的数据）；
    不同 → 转述（它引用的是别人的数字）。抽不到出处 → 返回 ("", "")，上层退回按域名算独立。
    """
    name, own = extract_origin(*texts)
    if not name:
        return ("", "")
    return (name, own)


def is_echo(page_domain: str, origin_name: str, origin_domain: str) -> bool:
    """这一页是不是"替别人说话"（转述）"""
    if not origin_name or not origin_domain:
        return False
    pd = _host_root(page_domain)
    if not pd:
        return False
    od = _host_root(origin_domain)
    # 同一家（iresearch.com.cn 发艾瑞的数据）→ 一手，不是回声
    return not (pd == od or pd.endswith("." + od) or od.endswith("." + pd))


def _bigrams(s: str) -> set[str]:
    """字符二元组。**中文不需要分词**——切词器本身就是一个引入错误的环节，
    而"两个标题像不像"用二元组已经够用（且对中英混排一视同仁）。"""
    t = re.sub(r"[\s\-—_·|｜:：，,。.、（）()\[\]【】《》\"'“”‘’]+", "", str(s or "")).lower()
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else set()


# 近重复判据阈值。**取高（0.72）是刻意的**：两家不同机构写同一年的同一份行业报告，
# 标题常常只差一两个字（"2025年中国咖啡市场研究报告" vs "2025中国咖啡行业研究报告"），
# 阈值放松到 0.5 会把**两个真独立来源误折成一个**——那正好是本模块最反对的方向。
# 我们的纪律是「宁可高估独立」：**折叠是减分项，所以宁可少折**。
JACCARD_FOLD = 0.72


def fold_near_duplicates(
    pages: list[dict], domains: list[str], titles: list[str]
) -> tuple[int, list[str]]:
    """跨域**近同标题**折叠——治"同一个原始稿被转载到多个站"这种没有显式归属的情况。

    ## 为什么血缘字段挡不住它
    `independent_count` 按 `origin` 字段聚类，而 `origin` 来自**显式归属**
    （"据艾瑞咨询"）。可是**转载稿往往把出处删掉**，只留一个几乎一样的标题：
    同一个稿子发在 5 个站上，我们按域名数出 5 个"独立来源"——置信度被顶到最高，
    而真相是**一份稿子**。这与"三个媒体转述同一份艾瑞数据"是同一个病，
    只是那一例有归属字段可抓，这一例没有。

    ## 判据与方向
    · 只看**没有显式归属**的页（有归属的已由 `origin` 聚类，不重复折）；
    · 同域内的多个页面**不折**（那是同一家发的，本来就算一个）；
    · 标题字符二元组 Jaccard ≥ `JACCARD_FOLD`（0.72）才折；
    · **每一组折叠都进 notes**——折叠是**减法**，减掉的是虚假的确定性，
      而减错了读者必须能看出来。

    返回 `(被折叠掉的页数, 说明列表)`。
    """
    free = [
        i
        for i, d in enumerate(domains or [])
        if not ((pages[i] if i < len(pages) else {}).get("origin") or "").strip()
    ]
    seen: list[tuple[int, set[str]]] = []
    folded = 0
    notes: list[str] = []
    for i in free:
        ti = titles[i] if i < len(titles) else ""
        gi = _bigrams(ti)
        if not gi:
            seen.append((i, gi))
            continue
        host = _host_root(domains[i] if i < len(domains) else "")
        dup_of = None
        for j, gj in seen:
            if not gj:
                continue
            # 同域不折：同一家发的两篇，本来就算一个出处
            if _host_root(domains[j] if j < len(domains) else "") == host:
                continue
            inter = len(gi & gj)
            union = len(gi | gj) or 1
            if inter / union >= JACCARD_FOLD:
                dup_of = j
                break
        if dup_of is None:
            seen.append((i, gi))
        else:
            folded += 1
            notes.append(
                f"{host} 与 {_host_root(domains[dup_of])} 标题近重复"
                f"（Jaccard≥{JACCARD_FOLD}）→ 折叠为一个出处："
                f"「{(titles[i] if i < len(titles) else '')[:34]}」"
            )
    return (folded, notes)


def independent_count(
    pages: list[dict], domains: list[str], titles: list[str] | None = None
) -> tuple[int, list[str]]:
    """独立佐证数 = **不同出处**的个数（不是不同域名的个数）

    pages: [{"domain":..., "origin":..., "via":...}]
    返回 (独立数, 回声说明列表)。抽不到出处的页按"它自己就是一个出处"算——
    宁可高估独立，也不要把真一手源误判成回声。

    `titles` 可选：给了就**额外**做一次跨域近同标题折叠（见 `fold_near_duplicates`）。
    不给则行为与旧版完全一致——**这是刻意的**：新能力默认不改变既有读数，
    免得"升级一次、所有历史口径都变了"。
    """
    origins: set[str] = set()
    echoes: list[str] = []
    for i, d in enumerate(domains or []):
        p = pages[i] if i < len(pages) else {}
        o = (p.get("origin") or "").strip()
        if o:
            origins.add(o)
            if p.get("via"):
                echoes.append(f"{_host_root(d)} 转述自 {o}")
        else:
            origins.add("@" + _host_root(d))  # 没有归属信息 → 它自己算一个出处
    n = len(origins)
    if titles:
        folded, notes = fold_near_duplicates(pages, domains, titles)
        echoes.extend(notes)
        n = max(1, n - folded)
    return (n, echoes)
