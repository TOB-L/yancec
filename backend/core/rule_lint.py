"""研策 · 规则层自检（确定性校验，零模型成本）——交付前机械性错误拦截层

分工（与 LLM 版 Red Team 互补，两层不重复）：
  - 本模块（纯规则，毫秒级）：拦"机械性"错误——
      R1 口径一致性：同一关键指标在报告不同位置取值跨度过大（如单店年营收 42.6万 vs 123.5万）
      R2 时序校验  ：把"未来年份"当作已成事实引用（如「据国家统计局2027年数据」）
      R3 术语定义  ：财务缩写/口径术语全报告未见定义（EBITDA、LTV 等）
      R4 数字背书  ：匿名/泛化来源（行业数据显示/业内人士称…）断言里带数值且无机构可点（41% 被当事实）
      R5 算术核验  ：正文"X元相当于其日均收入P%"与实际换算偏差>4pct（32元≈日均29%，写97%=算错）
      R6 伪量化    ：0-10 打分但无方法学/样本（"价敏度指数7.8(满分10)"式装饰性精确）
  - core/adversarial.py（LLM）负责"语义性"错误：政策误读 / 违背常识 / 荒谬推论。
  规则层跑在 Red Team 之前，先把能机械判定的问题拦住；发现项一律供人工仲裁，不阻断交付。

实测锚点（蜜雪冰城 vs 喜茶县域报告，Red Team 自曝 6 问）：
  R1 → "42.6万元 vs 123.5万元 口径严重不一致"（同报告单店年营收还有 82.6万/86万）；
       同类潜伏冲突：单店净利润 18.6/22.6/28.6万、回本周期 10.2 个月 vs 40 个月。
  R2 → "据国家统计局2025年数据"式未来引用（相对时点错位 LLM 判；本层只拦绝对未来 2027+）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# ============================================================
# R2 · 时序校验：未来年份不得当作已成事实
# ============================================================
# 未来年份若出现在"计划/目标/预测"语境（目标2028年/至2030年/预计2027年）是合法的；
# 只有"已发生事实/已发布数据"语境里出现未来年份才算错误。用窗口词消歧，宁漏勿误杀。
_FORECAST_WORDS = (
    "预计", "预测", "目标", "力争", "规划", "展望", "有望", "拟于", "计划",
    "将", "将于", "至", "到", "底", "末", "完成", "突破", "超过", "达到",
    "提升至", "增至", "翻番", "愿景", "累计", "建成", "扩容", "争取", "拟",
    "前", "后", "年",  # "2027年前/2027年后"即计划性区间 → 不算已成事实
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _check_future_years(text: str, current_year: int | None = None) -> list[dict[str, Any]]:
    cur = current_year or date.today().year
    out: list[dict[str, Any]] = []
    for m in _YEAR_RE.finditer(text or ""):
        y = int(m.group(1))
        if y <= cur or y > cur + 20:
            continue
        i = m.start()
        win = text[max(0, i - 18):i + 16]
        if any(fw in win for fw in _FORECAST_WORDS):
            continue
        snip = text[max(0, i - 22):i + 22].replace("\n", " ")
        if len(out) < 8:
            out.append(
                {
                    "kind": "future_year",
                    "year": y,
                    "snippet": snip,
                }
            )
    return out


# ============================================================
# R1 · 口径一致性：同一关键指标取值跨度过大
# ============================================================
# 只盯"单份报告内应只有一个值"的单位经济类指标（口径敏感词，报告内部打架=缺陷）；
# 通用总量词（市场规模/营收/渗透率）作用域多变，不做规则拦截（交给 LLM 语义判断）。
# 阈值 RATIO≥1.8：真实缺陷（42.6万 vs 123.5万=2.9×）能抓到；同比增速类正常波动通常<1.8×。
_R1_RATIO = 1.8

# 每组：名称 + 匹配短语 + 数值捕获窗
# 2026-09-06 扩展：加入"单店日均杯量/盈亏平衡杯量/客单价/现磨渗透率/人均咖啡消费量"等
# 跨章节极易打架的同概念指标；关店率同义词扩到 关闭率/闭店率（报告实测同份报告给出 62/67/70.6/18.6/11.3 六值）。
_R1_METRICS: list[dict[str, Any]] = [
    {
        "name": "单店年营收",
        # 词形补全（2026-09-18 外部审计 A3）：实测同一份报告里
        # 「**平均**单店年营收 86.5 万」（进入壁垒章）与「单店年均营收**中位数** 182 万」
        # （核心指标表）相差 2.1×，而 R1 因"措辞不同"分组分裂、**两层审计都没把它俩放一起**。
        "patterns": (
            "单店年营收", "单店营收", "门店年营收", "单店年均营收",
            "门店平均年营收", "单店营收中位数", "门店营收中位数",
            "县域门店年营收", "单店年度营收",
            "平均单店年营收", "单店年均营收中位数", "单店平均年营收",
            "门店年均营收", "单店年均收入", "单店年收入",
        ),
    },
    {
        "name": "单店净利润",
        "patterns": (
            "单店净利润", "单店净利", "单店年化净利润", "单店年度净利润",
            "单店年均净利润", "单店净利润中位数", "门店年均净利润",
        ),
    },
    {
        "name": "投资回收期/回本周期",
        # ⚠️ **同一实体在正文里会被写成四种名字**（2026-09-18 外部审计 B2 点名）：
        #   投资回收期 / 回本周期 / 投资回收**周期** / **盈亏平衡周期**
        # 实测一份报告同时给出 14.2（瑞幸年报）· 22（历史基线）· 22–26（欧睿）· 25（UE 自算）
        # · 28–36（趋势风险），跨度 2.5×——而 R1 **一个都没聚起来**，因为词表里少了后两种写法。
        # 这是"同实体 + 不同指标名"这个盲区**第五次**被现实点名。
        # 加词形是**保守修法**：只补同义的写法，不做语义聚类（那要另立机制）。
        "patterns": (
            "投资回收期", "回本周期", "投资回报周期", "回报周期",
            "投资回收周期", "回收周期", "盈亏平衡周期", "投资回收年限", "回本年限",
        ),
    },
    {
        "name": "关店率(含闭店/关闭)",
        "patterns": ("关店率", "闭店率", "关闭率", "12个月关闭", "存活率"),
    },
    {
        "name": "单店日均杯量",
        "patterns": (
            "日均杯量", "单店杯量", "单店日均销量", "单店日均杯", "日均销量",
            "日销杯量", "单店日销量",
        ),
    },
    {
        # ⚠️ **不许用裸「盈亏平衡」当 pattern**（2026-09-18 实测吃亏两次）：
        # 它会把「盈亏平衡销量 / 盈亏平衡点 / 盈亏平衡营收 / 月固定成本 / 单杯边际利润」
        # 全收进一个桶——那**不是同一个指标**。实测造出过
        # 「盈亏平衡（元·日）跨度 8292.1×（9.9、51,000、82,258）」这种荒谬读数，
        # 读者只会得出"这份报告全是错的"，而报告本身没有任何矛盾。
        # 只认**明确带指标词**的写法。
        "name": "盈亏平衡",
        "patterns": ("盈亏平衡销量", "盈亏平衡杯量", "盈亏平衡点", "盈亏平衡日均", "保本杯量", "保本点"),
    },
    {
        "name": "客单价",
        "patterns": ("客单价", "平均客单价", "有效客单价", "客单均价", "成交单价"),
    },
    {
        "name": "现磨咖啡渗透率",
        "patterns": ("现磨咖啡渗透率", "现磨渗透率", "咖啡渗透率"),
    },
    {
        "name": "人均咖啡消费量",
        "patterns": ("人均咖啡消费量", "人均咖啡消费", "人均现磨", "人均年咖啡消费量", "人均年消费量"),
    },
]

_NUM_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(万亿元|亿元|万元|亿|万|元|个月|月|%|杯|单)?"
)
_MONEY_UNITS = {"万亿元": 1e8, "亿元": 1e4, "万元": 1.0, "亿": 1e4, "万": 1.0}
_RANGE_SPLIT = re.compile(r"[-–—~至到]")
# 指标名后紧跟的**变动量**措辞 → 后面那个数是增量，不是该指标的取值（见 `_check_metric_consistency`）。
# `(?!至|到)`：`提升至 20 元` / `增加到 20 元` 是**目标值**，必须留下。
_MARGINAL_RE = re.compile(
    r"^\s*(?:每\s*)?(?:上涨|上浮|提升|增加|下降|减少|降低|涨|降|多|少)\s*(?!至|到)"
)


def _parse_num_unit(text: str) -> tuple[float, str, str] | None:
    """解析片段首个 '数值+单位' → (归一化到万元的值, 单位类, 原串)；%与元/杯按各自单位类"""
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    unit = (m.group(2) or "").strip()
    try:
        v = float(raw)
    except ValueError:
        return None
    if unit in _MONEY_UNITS:
        return (v * _MONEY_UNITS[unit], "money_wan", raw)
    if unit in ("%",):
        return (v, "percent", raw)
    if unit in ("个月", "月"):
        return (v, "month", raw)
    if unit in ("元", "杯", "单") or not unit:
        return (v, unit or "none", raw)
    return (v, unit, raw)


_COUNT_CLASS = ("家", "人", "户", "名", "个", "处", "条", "座", "辆", "架", "栋", "张", "件", "份", "本", "岁", "亩")
"""万/亿 后紧跟的量词 → 这是"数量"(万家/亿人)，不是金额，不得当 money 参与口径比对"""

# ── 「R1 抓不到门店数」的真因不是这里（2026-09-18 第十跑，试过又撤回）──────────
# 外部审计给的处方是「把 `_points_near` 白名单扩到 `家/万人/占比`」。**我做了，然后撤回了。**
#
# ① 先按处方加了 `_COUNT_SCALE`（家/万家/亿家 → 同桶可比），**并显式排除了密度形式**
#    （`0.18 家/万人` 与 `12,470 家` 并桶会造一个 **6.9 万倍的假冲突**——那比漏检更糟）。
# ② 然后在全部 208 份历史上跑：**80 条 → 80 条，一条差异都没有。**
#    即：**这一步是死代码**——`_R1_METRICS` 里根本没有门店数组，
#    没有任何一个指标名的取值窗口会落进这个桶。
#
# **真因在 `_R1_METRICS` 少一个组，不在白名单。** 而那个组不能照抄别人的：
# 门店数是**口径依赖**指标（全国 / 三四线 / 县域 / 某品牌），
# 同名不同 scope 的值并桶 = **常态误报**——违背本仓库"宁可少一条检查"的纪律。
# 正确做法是先有 **Entity–Metric–Scope 绑定**（外部审计 GPT 的 P0-6），
# 让"三四线城市现磨咖啡门店总数"与"下沉市场咖啡门店总数"在绑定后**知道自己是两个指标**。
# **那是下一层的事，不是加一个词表能解决的。** 这里只留诊断，不留死代码。
_ = _COUNT_CLASS  # （门店数组未加，见上）


def _points_near(segment: str) -> list[dict[str, Any]]:
    """在指标提及句片段内收集数值点，全部带单位类 kind（money_wan/percent/month/杯/元）供分桶。

    区间（42.6万-123.5万）记 lo/hi，单值记 v —— 都归入同单位类桶，绝不跨单位比。
    """
    nums = []
    for m in _NUM_RE.finditer(segment):
        r = _parse_num_unit(m.group(0))
        # 只收可比单位类："money_wan/percent/month" 外，单店杯量/客单价用 杯/元 单独分桶比
        if not (r and r[1] in ("money_wan", "percent", "month", "杯", "元")):
            continue
        # "3.3万家/7.62亿人" 之类：裸 万/亿 后紧跟数量量词 → 是门店/人口数，不是金额
        if r[1] == "money_wan" and (m.group(2) or "") in ("万", "亿"):
            if segment[m.end():m.end() + 1] in _COUNT_CLASS:
                continue
        nums.append({"start": m.start(), "raw": r[2], "v": r[0], "kind": r[1]})
    # 过滤非正值伪数据（"-0.0万/0元/0%"等解析伪影）——防除零
    nums = [n for n in nums if n["v"] > 0]
    pts: list[dict[str, Any]] = []
    skip = -1
    for k, n in enumerate(nums):
        if k <= skip:
            continue
        if k + 1 < len(nums) and nums[k + 1]["kind"] == n["kind"]:
            gap = segment[n["start"]:nums[k + 1]["start"]]
            if _RANGE_SPLIT.search(gap):
                pts.append(
                    {"kind": n["kind"], "lo": min(n["v"], nums[k + 1]["v"]),
                     "hi": max(n["v"], nums[k + 1]["v"]), "raw": f"{n['raw']}~{nums[k+1]['raw']}"}
                )
                skip = k + 1
                continue
        pts.append({"kind": n["kind"], "v": n["v"], "raw": n["raw"]})
    return pts


# 单位档 → 该档在指标名里会用的词。用于判断"组名与单位是否自相矛盾"。
_UNIT_TOKENS: dict[str, tuple[str, ...]] = {
    "money_wan": ("万元", "元", "金额", "营收", "利润"),
    "percent": ("率", "占比", "百分比"),
    "month": ("月", "周期"),
    "杯": ("杯", "销量"),
    "元": ("客单价", "单价", "元"),
}


def _granularity(win: str) -> str:
    """数值所在短窗的**时间粒度**（日/月/年，取不到＝空）。

    病案（2026-09-18 实测）：报告原文写
        「月度盈亏平衡销量为 51,000元 ÷ 9.92元/杯 ≈ **5,141 杯**，折合**日均**需售出 **171 杯**」
    —— 那是**同一个指标的两种粒度**，报告没有任何矛盾。而规则层把两者收进同一个桶，
    印出「盈亏平衡（杯）跨度 **30.1×**」的**假冲突**。
    **同指标不同粒度不可比**，必须分桶；分完各自只有一个值 → 不报。
    （真的同粒度多值照报，判据不变。）
    """
    for keys, tag in (
        (("日均", "每日", "日销", "单日"), "日"),
        (("月度", "每月", "月均", "单月"), "月"),
        (("年度", "每年", "年均", "全年", "单年"), "年"),
    ):
        if any(k in win for k in keys):
            return tag
    return ""


def _unit_conflicts(name: str, kind: str) -> bool:
    """指标名暗示的单位 ≠ 这一桶的单位 → 这一桶不是本指标的数，**不报**。

    病案（2026-09-18）：「盈亏平衡」这个 pattern 同时捕到"盈亏平衡杯量"(杯) 与
    "盈亏平衡点 9.7 个月"(month)，而 `盈亏平衡` 又出现在讲**投资额**的句子里，
    于是一组「盈亏平衡杯量（万元口径）1.5、5.3」被报了出来——名字说杯量、值是万元。
    **那是两个不同指标被并进了一组**，报出来等于诬告报告"自相矛盾"。
    判据：名字里有**别的档**的词、却没有本档的词 → 冲突。
    """
    own = _UNIT_TOKENS.get(kind, ())
    if any(t in name for t in own):
        return False
    for k2, toks in _UNIT_TOKENS.items():
        if k2 == kind:
            continue
        if any(t in name for t in toks):
            return True
    return False


def _section_map(text: str):
    """返回 pos → 该位置**所属章节名**（最近的上级 `## ` 标题）。

    只认二级标题：三级标题在这份报告里是"维度内的分点"，拿它当"章节"会把同一节切碎。
    """
    heads: list[tuple[int, str]] = []
    for m in re.finditer(r"^##\s+(.+)$", text or "", re.M):
        heads.append((m.start(), m.group(1).strip()[:24]))

    def _of(pos: int) -> str:
        cur = ""
        for p, h in heads:
            if p <= pos:
                cur = h
            else:
                break
        return cur

    return _of


def _summary_metric_values(text: str, name: str, patterns: tuple[str, ...]) -> set[float]:
    """《📊 核心指标一览表》里**该指标那一行**出现过的数值（保留 1 位小数，与 R1 取值同精度）。

    ⚠️ **必须按行匹配，不能取整节**（2026-09-18 第一版就错在这）：
    整节里混着来源机构年份、别的指标的数值，于是「建议主口径」会挑出表里最小的那个数
    （实测把客单价的建议主口径给成了 12——那根本不是客单价那一行的数）。
    **给读者的建议必须是同源可核的**，宁可不定夺也不能指错行。
    取不到就返回空集 → 上层不给建议（**不猜**）。
    """
    try:
        m = re.search(r"^##\s*📊\s*核心指标一览表\s*$(.*?)(?=^##\s|\Z)", text or "", re.M | re.S)
        if not m:
            return set()
        _keys = (name,) + tuple(patterns or ())
        out: set[float] = set()
        for ln in m.group(1).splitlines():
            _l = ln.strip()
            if not _l.startswith("|"):
                continue
            _cells = [c.strip() for c in _l.strip("|").split("|")]
            if not _cells:
                continue
            _head = _cells[0]
            # 指标名对得上（行首单元格含指标名或其任一 pattern）才算这一行
            if not any(k and k in _head for k in _keys):
                continue
            for n in re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", " ".join(_cells[1:3])):
                try:
                    out.add(round(float(n.replace(",", "")), 1))
                except Exception:
                    continue
        return out
    except Exception:
        return set()


def _check_metric_consistency(text: str) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    # **只扫正文**（2026-09-18 改，与遗漏层/指标台账同一把刀）。
    # 病案：R1 此前扫全文，把**审计附录里引擎自己写的推理句**当成了观测值——
    # 交付件上印「单店日均杯量 跨度 2.6×」，而用交付的代码跑交付的文本得 3.4×，
    # 因为桶里多出的那一个是 RedTeam 的反推句「若 186 杯为一线的 57%，则一线为 326 杯」。
    # **引擎自己的产物被计入它审计的集合**，直接后果是印在交付件上的数第三方复算不出来——
    # 而"可复算"正是本产品的核心主张。同族病案还有「来源:调研数据」那条。
    try:
        from core.report_html import split_audit_tail

        _body, _tail = split_audit_tail(text or "")
        if _body and len(_body) > 200:
            text = _body
    except Exception:
        pass
    # **再去掉引用块**（2026-09-18 第二次修，同一天同一个根）。
    #
    # 第一次只切了文末审计附录，结果 4280 还在——因为它来自**闸门横幅**，
    # 而横幅在**正文区头部**。横幅原文（引擎自己生成的 RedTeam 假设算式）：
    #   「…瑞幸等大众品牌若按 **15 元客单价** 需 285 杯才能达到 **4,280 元日销**」
    # R1 的 40 字同分句窗口把「15 元客单价」和「4,280 元」一起收进了**客单价**桶，
    # 于是交付件印出「客单价 跨度 356.7×」——**而那 4280 是"日均销售额"，
    # 是引擎自己在解释为什么另一个数不合理时写的**。
    #
    # 这份报告里的引用块**全部是元层**：闸门横幅、时效补检、AI 生成标识、边界自陈。
    # 它们不是报告自己的断言，不该参与"报告有没有自相矛盾"的判定。
    # 判据是机械的：行首（去空白后）为 `>` 的行，一律不进 R1 的输入。
    _kept = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">")]
    if len(_kept) * 4 > len((text or "").splitlines()) * 3:  # 掉太多就不动，防整篇被吞
        text = "\n".join(_kept)
    for g in _R1_METRICS:
        pat = re.compile("|".join(re.escape(p) for p in g["patterns"]))
        # 按单位类分桶（金额/百分比/月份各自比），防 "营收42.6万" 与句内 "回收期10个月" 混比误报
        buckets: dict[str, list[dict[str, Any]]] = {}
        snippets: list[str] = []
        for m in pat.finditer(text or ""):
            # 值只在"短语后的同分句短窗"内取：遇 ，。；！？换行/制表 即止，
            # 避免把下一分句/别的指标(如"SOM 143亿元 倒推需3.3万家")吸进本指标的桶
            tail = text[m.end():min(len(text), m.end() + 40)]
            _cut = len(tail)
            for _mk in ("。", "，", "；", "！", "？", "\n", "\t"):
                _i = tail.find(_mk)
                if _i >= 0 and _i < _cut:
                    _cut = _i
            tail = tail[:_cut]
            # **只取紧跟指标名的第一个数**（区间除外）——2026-09-18 第三次修同一个读数。
            #
            # 病案：报告原文「月度盈亏平衡销量为 **51,000元** ÷ **9.92元/杯** ≈ **5,141 杯**」
            # —— 一句里并排三个**不同指标**（月固定成本 / 单杯边际利润 / 月销量）。
            # 旧实现把同分句 40 字内的数**全收**，于是印出
            # 「盈亏平衡（元·日）跨度 **5151.5×**（9.9、51,000、82,258）」这种荒谬读数。
            # 读者不会认为"规则层判据太宽"，只会认为**报告本身全是错的**。
            #
            # 判据：数值必须**紧贴**指标名出现（中间只隔连接词），第一个数之后的一律不收；
            # 唯一的例外是区间（"28–36 个月"），它由两个相邻数构成一个取值。
            # **边际量不是取值**（2026-09-18 外部审计 A4，本家族第四次）。
            #
            # 病案：正文写「在下沉市场，**客单价每上涨 1 元**，销量流失率即呈现非线性增长」
            # ——`1` 是**增量**（涨 1 元），不是"客单价 = 1 元"。旧实现把同分句第一个数收进桶，
            # 于是 R1 头条印出「客单价（元）跨度 **28.7×**」；剔除伪值后真实跨度
            # = 28.7 ÷ 12 = **2.4×**——**虚高 12 倍**。
            # 与本仓库前三次（RedTeam 反推句 326 杯、RedTeam 假设式 4,280 元日销、
            # 展示截断藏起 280 杯）同根：**审计层把不是观测值的东西当成了观测值**。
            #
            # 判据：指标名之后紧跟"（每）上涨/提升/增加/下降/减少/降低 N"→ N 是**变动量**，跳过。
            # **但"提升至/增加到 N"不算**——那是目标值，是真取值，必须留下。
            if _MARGINAL_RE.match(tail):
                continue
            _ms = list(_NUM_RE.finditer(tail))
            if _ms:
                _end = _ms[0].end()
                if len(_ms) > 1 and _RANGE_SPLIT.search(tail[_end:_ms[1].start()]):
                    _end = _ms[1].end()
                tail = tail[:_end]
            # 时间粒度：**取匹配前后各一小段**——"月度盈亏平衡销量"里"月度"在指标名之前，
            # 只看 tail 会漏掉它（这正是 5141/171 混桶的直接原因）。
            _gran = _granularity(text[max(0, m.start() - 14):m.end() + 40])
            for p in _points_near(tail):
                p["pos"] = m.end() + int(p.get("start") or 0)  # 绝对值，供"出自哪一节"定位
                buckets.setdefault((p["kind"], _gran), []).append(p)
            snip = text[max(0, m.start() - 16):m.end() + 34].replace("\n", " ")
            if snip not in snippets and len(snippets) < 6:
                snippets.append(snip)
        for _bk, points in buckets.items():
            kind, _gran = _bk
            if len(points) < 2:
                continue
            lows = [p["lo"] if "lo" in p else p["v"] for p in points]
            highs = [p["hi"] if "hi" in p else p["v"] for p in points]
            lo, hi = min(lows), max(highs)
            if lo <= 0 or (hi / lo) < _R1_RATIO:
                continue
            shown = sorted(
                {round(v, 1) for p in points
                 for v in ([p["lo"], p["hi"]] if "lo" in p else [p["v"]])}
            )
            unit_lab = {"money_wan": "（万元口径）", "percent": "", "month": "（月）",
                        "杯": "（杯）", "元": "（元）", "none": ""}.get(kind, "")
            # 粒度进标签：分桶已经按粒度切开，标签也必须说出来——否则读者不知道
            # 这个跨度是"同粒度内的真冲突"还是"跨粒度混比"（后者已被排除）。
            if _gran:
                unit_lab = (
                    unit_lab[:-1] + f"·{_gran}）" if unit_lab.endswith("）") else f"（{_gran}）"
                )
            # **单位与指标名自相矛盾 → 这一桶根本不报**（2026-09-18）。
            # 实测病案：「盈亏平衡杯量（万元口径）1.5、5.3」——名字说"杯量"、值是万元，
            # 那是**另一个指标**（投资额）被同一个 pattern 串了进来，不是报告自相矛盾。
            # 报出来比漏报更伤：读者会以为报告在同一指标上打架。
            if _unit_conflicts(g["name"], kind):
                continue
            # **列出全部取值**：印在交付件上的跨度必须能由印出来的取值复算出来
            # （此前 `shown[:6]` 截断，而 `spread` 按全部算 → 第三方复算对不上）。
            _cap = 12
            _vals = shown[:_cap]
            _trunc = len(shown) > _cap
            # **收敛**（2026-09-18，本节从"把矛盾交出去"改成"给判断 + 给依据"）。
            #
            # 此前只报「跨度 N×」+ 三句原文摘录，读者得自己判——**"检测到位却把矛盾原样
            # 交出去"等于把严谨做成了免责声明**。现在补两样机械可核的东西：
            #   ① 每个取值**出自哪一节**（同指标跨节不同口径是常态，摆出来读者 10 秒就能收敛）；
            #   ② **建议主口径**：出现在报告自己《核心指标一览表》里的那个值。
            #      理由：它是**报告自己选定的口径**，正文与摘要打架时以它为准才不会自相矛盾。
            #      取不到就不给建议（明说需人工定夺），**不猜**。
            _sec_of = _section_map(text)
            _by_sec: dict[str, list[float]] = {}
            for p in points:
                _v = p["lo"] if "lo" in p else p["v"]
                _by_sec.setdefault(_sec_of(p.get("pos", 0)), []).append(round(_v, 1))
            _summary_vals = _summary_metric_values(text, g["name"], tuple(g.get("patterns") or ()))
            _main = sorted({v for v in shown if v in _summary_vals})
            if len(_main) == 1:
                _advice = (
                    f"**建议主口径：{_main[0]:g}**（它出现在报告《📊 核心指标一览表》**该指标那一行**，"
                    "即报告自己选定的那个值）。其余取值请按上面各节标注的口径对照，"
                    "**或在正文里明确写成不同年份/不同口径**——同一个词指两个数，读者无法判断该信哪个。"
                )
            elif len(_main) > 1:
                _advice = (
                    "**未给建议主口径**：报告《📊 核心指标一览表》该指标那一行里就出现了 "
                    f"{'、'.join(f'{v:g}' for v in _main)} 多个取值——**报告自己的摘要表内部就不一致**，"
                    "规则层无法据此定夺，**请人工先定摘要表，再回头统一正文**。"
                )
            else:
                _advice = (
                    "**未给建议主口径**：没有一个取值出现在《📊 核心指标一览表》里，"
                    "规则层无法据此定夺——**请人工确认哪一个是主口径，并在正文中标注其余取值的口径差异**。"
                )
            violations.append(
                {
                    "kind": "metric_conflict",
                    "metric": g["name"] + unit_lab,
                    "values": [str(x) for x in _vals],
                    "values_total": len(shown),
                    "values_truncated": _trunc,
                    "spread": round(max(shown) / min(shown), 1) if shown else 0.0,
                    "by_section": {k: sorted(set(v)) for k, v in _by_sec.items() if k},
                    "advice": _advice,
                    "snippets": snippets[:3],
                }
            )
            break  # 该指标已命中，不再对其它单位类重复报
    return violations


# ============================================================
# R10 · 同一数字不许挂两个分母量纲
# ============================================================
# 病案（2026-09-18 实跑，用户交回的交付件）：
#   正文三处写「三四线城市现磨咖啡门店渗透率仅为 **0.8 家/10万人**」
#   （执行摘要 / 核心指标一览表 / 宏观环境），
#   同一份报告另一处写「三四线城市现磨咖啡门店密度仅为 **0.8 家/万人**，不足一线城市的1/5」。
#   ——**同一个数、同一个指标、分母差 10 倍**。读者若按后者理解，市场容量会算大 10 倍。
#
# **为什么 R1 抓不到它**：`家/10万人` 这个量纲**不在 `_points_near` 的单位白名单里**
# （白名单只有 money_wan / percent / month / 杯 / 元），于是整条对规则层**不可见**。
# 这不是阈值问题，是**覆盖**问题——R1 调参调一万次也照不出来。
#
# 判据刻意收紧到"**逐字相同的数值** × 同一量词 × 同一下游单位 → 分母不许差 ≥2 倍"：
# 数值必须一致（0.8 对 0.8），所以不会把"两个不同指标恰好用了不同分母"误报成矛盾。
_DENOM_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(家|人|元|杯|个|座|次|件|台|所|间)"
    r"\s*/\s*(\d+(?:\.\d+)?)?\s*(万亿|亿|万|千|百)?\s*(人|户|店|㎡|平方米|平方公里|平方千米)"
)
_DENOM_SCALE = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2, "": 1.0}


def _body_only(text: str) -> str:
    """报告的**正文**：切掉文末审计附录 + 去掉引用块。R1/R10/R11 共用同一把刀。

    两刀都是为了同一个根：**引擎自己的产物不许被算进它审计的集合**。
    - 第一刀切审计附录：R1 曾把 RedTeam 的反推句「若 186 杯为一线的 57%，则一线为 326 杯」
      当成观测值，交付件印出的跨度第三方复算不出来。
    - 第二刀去引用块（`>` 起首）：闸门横幅 / 时效补检 / AI 标识 / 边界自陈**全是元层**，
      它们不是报告自己的断言。R1 曾因此把横幅里的「4,280 元日销」收进客单价桶。
    - 第三刀（本函数新增）同样服务于 R11：`盈亏平衡` 的红线若把 RedTeam 的
      「220 杯（存疑·待核）」算进来，红线就被**质疑方的反推值**顶高了——
      报出来的"红线"不是报告主张的红线，读者拿它复算会对不上。
    """
    _body = text or ""
    try:
        from core.report_html import split_audit_tail

        _b, _t = split_audit_tail(_body)
        if _b and len(_b) > 200:
            _body = _b
    except Exception:
        pass
    _kept = [ln for ln in _body.splitlines() if not ln.lstrip().startswith(">")]
    if len(_kept) * 4 > len(_body.splitlines()) * 3:  # 掉太多就不动，防整篇被吞
        _body = "\n".join(_kept)
    return _body


def _check_unit_denominator(text: str) -> list[dict[str, Any]]:
    """同一个数值挂了两个不同的分母口径 → 报（含相差倍数与两个原文写法）。"""
    out: list[dict[str, Any]] = []
    try:
        _body = _body_only(text)

        groups: dict[tuple[str, str], dict[float, dict[float, str]]] = {}
        for m in _DENOM_RE.finditer(_body):
            val = round(float(m.group(1)), 6)
            if val <= 0:
                continue
            _n = float(m.group(3)) if m.group(3) else 1.0
            den = _n * _DENOM_SCALE.get(m.group(4) or "", 1.0)
            groups.setdefault((m.group(2), m.group(5)), {}).setdefault(val, {})[
                den
            ] = m.group(0).strip()
        for (_q, _du), byval in groups.items():
            for val, dens in byval.items():
                if len(dens) < 2:
                    continue
                _lo, _hi = min(dens), max(dens)
                if _hi / _lo < 2:
                    continue
                _forms = "、".join(f"{dens[d]}" for d in sorted(dens))
                _shown = "；".join(
                    f"「{dens[d]}」（分母 {d:,.0f} {_du}）" for d in sorted(dens)
                )
                out.append(
                    {
                        "kind": "unit_denominator_conflict",
                        "rule": "R10",
                        "title": "同一数字挂了两种分母量纲",
                        "level": "high",
                        "detail": (
                            f"数值 **{val:g}** 在同一份报告里出现了两套分母：{_shown}——"
                            f"**相差 {_hi / _lo:g} 倍**。同一个数不可能同时是这两件事，"
                            f"其中一处写错了量纲；而按错的那一处推下去，"
                            f"凡是由它派生出来的规模/门店数/人均值都会错 {_hi / _lo:g} 倍。"
                        ),
                        "advice": (
                            "统一分母量纲，并在同一次改写里**回头核对由它推出来的派生数字**"
                            "（市场容量、门店缺口、人均消费量）——量纲错通常不会只错一处。"
                        ),
                        "snippet": _forms,
                    }
                )
    except Exception:
        return []
    return out


# ============================================================
# R11 · 行动建议踩了报告自己算出来的盈亏平衡线
# ============================================================
# 病案（2026-09-18 实跑，同一份交付件）：行动建议是三条并列的编号项——
#   1.「调整为社区型小面积快取店，以适配 **92杯/日** 的低流量基准」
#   3.「优先选址高流量商圈并确保日均销量突破 **171杯** 的盈亏平衡点，否则面临长期亏损风险」
# 而 171 杯正是**报告自己**算出来的盈亏平衡（51,000 ÷ 9.92 ≈ 5,141 杯/月 ≈ 171 杯/日），
# 92 杯是它自己引用的下沉中位数。**建议 1 等于让门店按一个必然亏损的量去设计。**
# 这两句在同一个小节里隔了三行。规则层对"建议把自己算出来的红线踩了"此前**完全盲**。
#
# 判据（机械、两个数都印出来供第三方复算）：
#   ① **只在同一张建议清单里找**：`行动建议` / `行动清单` 块内、**且前面 12 字含指令词**的「N 杯」
#      （指令词那一层是防误报的：「当前 92 杯，建议提升至 200 杯」里 92 是指陈述，不是建议）；
#   ② 这些数里，**两侧 24 字出现 `盈亏平衡/保本`** 的记为**红线**，其余记为**被建议的基准**；
#   ③ 基准最小值 < 红线最大值 → 报。
#
# ⚠️ **第一版把红线取成"全篇 max(盈亏平衡杯值)"，被实测证伪**：那份报告另一节写
# 「日均杯量中位数仅为186杯（**低于盈亏平衡点220杯**）」——220 是**另一个主体/另一种店型**的
# 红线，合法存在。拿它当红线，报出来的两个数读者在正文里对不上（且 220 本身还是
# RedTeam 的争议值）。收紧到"两个数同出一块"后，既不用猜主体，也排除了跨主体误报：
# **矛盾确实就在这张清单内部**——第 1 条让按 92 杯设计，第 3 条要求突破 171 杯，相隔三行。
_ADV_BLOCK_RE = re.compile(r"(?:\*\*)?行动(?:建议|清单)(?:\*\*)?[^\n]{0,6}\n")
_CUP_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*杯")
# 指令词只回看 12 字：够到"以适配92杯""确保突破171杯"，够不到前面那句陈述
_ADV_DIRECTIVE = ("适配", "为基准", "基准", "目标", "确保", "维持", "达到", "按", "提升至", "控制在")


def _advice_cup_values(text: str) -> list[tuple[float, str, bool]]:
    """行动建议/行动清单块内、带指令词的「N 杯」→ [(值, 原文片段, 是否红线)]

    **红线与被建议的基准都只在同一张建议清单里找**（2026-09-18 第二版）。
    第一版在全篇找 `盈亏平衡` 取最大值，结果取到 **220 杯**——那是报告另一节讲的
    **另一个主体/另一种店型**的盈亏平衡（合法存在），拿它当红线，报出来的数读者复算不出。
    而真正的矛盾就在这张清单内部：第 1 条说"适配 92 杯"，第 3 条说"确保突破 171 杯的盈亏平衡点"，
    **两条相隔三行**。判据收紧到"两个数必须同出一块"后，既不需要猜主体，也排除了跨主体误报。
    """
    m = _ADV_BLOCK_RE.search(text or "")
    if not m:
        return []
    lines = (text or "")[m.end():m.end() + 1600].splitlines()
    keep: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i > 0 and (s.startswith("#") or (s.startswith("**") and s.endswith("**") and len(s) < 30)):
            break
        keep.append(ln)
    block = "\n".join(keep)
    out: list[tuple[float, str, bool]] = []
    for mm in _CUP_RE.finditer(block):
        try:
            v = float(mm.group(1).replace(",", ""))
        except Exception:
            continue
        if v <= 0:
            continue
        if not any(k in block[max(0, mm.start() - 12):mm.start()] for k in _ADV_DIRECTIVE):
            continue
        # 红线：数值**两侧**24 字内出现"盈亏平衡/保本"（中文里它常写在数**之后**：
        # 「突破 171 杯的盈亏平衡点」）
        ctx = block[max(0, mm.start() - 24):mm.end() + 24]
        is_red = any(k in ctx for k in ("盈亏平衡", "保本"))
        out.append((v, block[max(0, mm.start() - 26):mm.end() + 8].replace("\n", " "), is_red))
    return out


def _check_advice_vs_breakeven(text: str) -> list[dict[str, Any]]:
    """行动建议里"按它去设计"的杯量基准 低于 **同一张清单里**写明的盈亏平衡线 → 报。"""
    try:
        # **只扫正文**：建议与红线都必须取自报告自己的话，不许取审计附录里
        # 质疑方的反推值（否则报出的"红线"读者复算不出来——见 `_body_only`）。
        _items = _advice_cup_values(_body_only(text))
        if not _items:
            return []
        _red = [v for v, _s, r in _items if r]
        _base = [(v, s) for v, s, r in _items if not r]
        if not _red or not _base:
            return []
        _hi = max(_red)
        _lo, _snip = min(_base, key=lambda x: x[0])
        if _lo >= _hi:
            return []
        return [
            {
                "kind": "advice_below_breakeven",
                "rule": "R11",
                "title": "行动建议低于同一份建议里写明的盈亏平衡线",
                "level": "high",
                "detail": (
                    f"**同一张行动建议清单**里，一条按 **{_lo:g} 杯/日** 设计门店形态，"
                    f"另一条要求日均销量突破 **{_hi:g} 杯** 的盈亏平衡点——"
                    f"**建议让门店按一个低于自己红线的量去设计**，两者差 {_hi / _lo:.1f} 倍。"
                    f"原句：「…{_snip}…」。这不是跨章节的口径分歧（那种可以并列），"
                    f"而是**同一条建议清单内部自相矛盾**：读者照第 1 条开店，就必然踩第 3 条的红线。"
                ),
                "advice": (
                    "二者必须收敛到同一个口径：要么把建议基准提到盈亏平衡线以上（并说明靠什么达到），"
                    "要么在建议里明写「在这个量下单位经济为负、需要靠 X 补贴或降本来覆盖」。"
                    "**不能一边算出门店会亏、一边建议照这个量开店。**"
                ),
                "snippet": f"{_lo:g}杯、{_hi:g}杯",
            }
        ]
    except Exception:
        return []


# ============================================================
# R3 · 术语定义：财务缩写全报告须有中文/定义，否则提示补定义
# ============================================================
# 只查"缩写型"术语（未定义会直接损害可读性/专业性），中文概念词（渗透率等）作用域多义，
# 规则层不拦截（避免误杀，交给 LLM Red Team 语义判断）。
_R3_TERMS = [
    # (缩写, 报告内应出现的中文全称/定义词之一)
    ("EBITDA", ("息税折旧摊销前利润", "税息折旧", "摊销前利润")),
    ("CAGR", ("复合年增长", "复合增长", "年均复合")),
    ("LTV", ("生命周期价值",)),
    ("CAC", ("获客成本",)),
    ("ROI", ("投资回报",)),
    ("ROE", ("净资产收益率",)),
    ("GMV", ("成交总额", "交易总额")),
    ("BEP", ("盈亏平衡",)),
    ("AOV", ("客单价", "平均客单价")),
]
_R3_DEF_SIGNS = ("指", "即", "定义为", "全称", "缩写", "中文", "英文", "含义", "代表", "意为", "（英文", "(英文")
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])")


def _acro_pat(acro: str) -> str:
    # 用 ASCII 边界而非 \b：Python3 的 \w 含中文，\bEBITDA\b 遇 "EBITDA利润率" 会因 A后接"利"(中文字) 匹配失败
    return rf"(?<![A-Za-z0-9]){acro}(?![A-Za-z0-9])"


def _check_term_definition(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not text:
        return out
    for acro, cn_words in _R3_TERMS:
        if not re.search(_acro_pat(acro), text):
            continue
        # 已定义判定：报告任何一处出现 中文全称词；或首次出现处 ±40 字内有定义信号
        defined = any(cw in text for cw in cn_words)
        if defined:
            continue
        m = re.search(_acro_pat(acro), text)
        if m:
            i = m.start()
            seg = text[max(0, i - 30):i + 90].replace("\n", " ")
            if any(s in seg for s in _R3_DEF_SIGNS):
                continue
        out.append(
            {
                "kind": "term_undefined",
                "term": acro,
                "hint": f"请提供中文全称/定义，如 {acro}（{'/'.join(cn_words)}…）",
                "snippet": text[max(0, i - 24):i + 40].replace("\n", " ") if m else "",
            }
        )
    return out


# ============================================================
# R4 · 数字背书纪律：匿名/泛化来源断言里带数字（无机构背书的数字不得冒充已核事实）
# ============================================================
# 病案：指标表已把泛来源(行业数据)转"待核"，但正文断言句仍可能硬写"…占比高达41%"而没有任何
# 机构可点 → 评审随手一点就穿帮。本层把"匿名来源措辞 + 数值"挑出来披露，供作者补"机构+年份"
# 或改"(估算/待核)"。只匹配"主语空缺、怎么说都不算有背书"的措辞，避免误伤"据国家统计局数据显示…"。
_VAGUE_SOURCE = (
    "行业数据显示", "行业数据表明", "有数据显示", "公开资料显示",
    "公开报道显示", "公开信息显示",
    "业内人士称", "业内人士表示", "业内消息", "市场消息称", "消息人士称",
    "历史基线", "行业基线", "基线数据", "历史数据表明",
    "某机构", "某券商", "某咨询", "某研究院", "某平台", "某品牌",
)
# 分句分隔符（含中英文标点/括号/管道——管道用于跳过表格行的既有守卫，也做分句边界）
_CLAUSE_BREAK = "。，、；：！？（）()| \n\r\t"


def _is_clause_initial(text: str, i: int) -> bool:
    """该标记是否处于"分句开头"（或仅前置 据/根据，视为无机构）。

    关键防误报：真正匿名句通常是分句主语（"行业数据显示…"），而机构归属句会在标记前紧跟机构名
    （"国家统计局综合数据显示…"）。向前扫到最近分隔符，若中间前缀不是空/据/根据 → 有归属，不报。
    """
    j = i
    while j > 0 and text[j - 1] not in _CLAUSE_BREAK:
        j -= 1
    return text[j:i] in ("", "据", "根据")


# 数值必须带单位才参与（避免把"2024"这类年份当数字抓）
_UNIT_NUM = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(万亿元|亿元|万元|亿|万|元|个月|月|%|杯|单|家|人|万户|万家|亿吨|万吨)(?!\d)"
)


def _check_unauthored_number(text: str) -> list[dict[str, Any]]:
    """在正文挑"匿名/泛化来源 + 数值"句（无背书数字）。

    表格行（| 开头）跳过——结构化提取已把泛来源转"待核"，不必在附录重复刷屏。
    只取标记后同句（到句号/分号/换行）≤120 字的窗口，防跨段误报；结果去重、限 7 条。
    """
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for marker in _VAGUE_SOURCE:
        pos = 0
        while True:
            i = text.find(marker, pos)
            if i < 0:
                break
            pos = i + len(marker)
            line_start = text.rfind("\n", 0, i)
            if text[line_start + 1:i].lstrip().startswith("|"):
                continue  # 表格行，结构化处理已覆盖
            # 分句开头才认匿名（"据/根据"前置同样视为无机构）：防"XX统计局/XX咨询综合数据…"机构归属句误报
            if not _is_clause_initial(text, i):
                continue
            win = text[i:min(len(text), i + 120)]
            cut = len(win)
            for _stop in ("。", "；", "\n"):
                _j = win.find(_stop)
                if 0 <= _j < cut:
                    cut = _j
            win = win[:cut]
            m = _UNIT_NUM.search(win)
            if not m:
                continue
            snip = re.sub(r"\s+", " ", text[max(0, i - 14):min(len(text), i + cut + 10)]).strip()
            if snip in seen:
                continue
            seen.add(snip)
            out.append(
                {
                    "kind": "unauthored_number",
                    "marker": marker,
                    "number": m.group(0).strip(),
                    "snippet": snip,
                }
            )
            if len(out) >= 7:
                return out
    return out


# ============================================================
# R5 · 正文算术核验：把"X元相当于其日均收入P%"式百分比与实际换算对账（防 32元=日均97% 类算术错）
# ============================================================
# 病案（v1 审计 F2 / 报告2.0）："月可支配收入中位数3,280元 … 主力产品均价32元相当于其日均收入的97%"
# —— 3,280/30≈109 元/日，32/109≈29%，正文却写 97%。引擎算的测算数字没问题，LLM 在正文里自由写的
# 百分比是审计盲区。本层用同段"月收入基数"对账，偏差>4pct 即披露（纯规则、零模型成本）。
_PCT_EQ_RE = re.compile(
    r"([\d][\d,.]*)\s*元\s*相当于其(?P<kind>日均|月均|年均)收入的\s*(?P<pct>\d+(?:\.\d+)?)\s*%"
)
_MON_INCOME_RE = re.compile(r"月(?:均|度)?(?:可支配)?收入(?:中位数)?[约为]?\s*([\d][\d,.]*)\s*元")


def _check_arithmetic(text: str) -> list[dict[str, Any]]:
    """正文"X元相当于其(日/月/年)均收入 P%"对账。

    以"该断言之前最近的月收入基数"为对照（日=N/30、月=N、年=N×12），计算 X/基数×100 并与 P% 比较，
    偏差 >4pct 记一条。同段无基数时仅当全文档唯一基数才跨段回退（宁少勿滥）。
    """
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for para in re.split(r"\n\s*\n", text):
        for m in _PCT_EQ_RE.finditer(para):
            try:
                val = float(m.group(1).replace(",", ""))
                stated = float(m.group("pct"))
                kind = m.group("kind")
            except ValueError:
                continue
            mi = None
            # 取离该百分比断言最近(且在其前)的月收入基数——同一段含多群组收入(大学生1,500/白领5,000)时
            # 不得绑定最左的"错基数"(re.search 默认最左 → 假报)。多群组取最近者，命中仍错才可信。
            _cands = list(_MON_INCOME_RE.finditer(para[: m.start()]))
            if _cands:
                mi = _cands[-1]
            else:
                # 跨段回退：全文档仅存在 1 个月收入基数时才用（执行摘要写 97%、基数在正文的单基数场景）
                _glob = list(_MON_INCOME_RE.finditer(text))
                if len(_glob) == 1:
                    mi = _glob[0]
            if mi is None:
                continue
            monthly = float(mi.group(1).replace(",", ""))
            if monthly <= 0 or val <= 0:
                continue
            base = monthly / 30.0 if kind.startswith("日") else (monthly * 12.0 if kind.startswith("年") else monthly)
            actual = val / base * 100.0
            if abs(actual - stated) > 4.0:
                snip = re.sub(r"\s+", " ", para[max(0, m.start() - 30): min(len(para), m.end() + 12)]).strip()
                if snip in seen:
                    continue
                seen.add(snip)
                out.append(
                    {
                        "kind": "arithmetic_conflict",
                        "value": f"{val:g} 元",
                        "kind_word": kind,
                        "stated": stated,
                        "actual": round(actual, 1),
                        "month_base": monthly,
                        "snippet": snip,
                    }
                )
                if len(out) >= 5:
                    return out
    return out


# ============================================================
# R6 · 伪量化检测：0-10打分但无方法学/样本（"价敏度指数7.8(满分10)"式装饰性精确）
# ============================================================
# 病案（v1 附录A #9 / 报告2.0）："价敏度指数高达7.8（满分10）"——无计算方法与样本，
# 纯 LLM 装饰性输出。专业报告宁可给区间和判断逻辑，不给伪造的精确感。本层对
# "X（满分10）" / "X/10" 打分断言做轻检查：±60字内无方法词、也无 (L5估算) 标注即披露。
_PSEUDO_METHOD_WORDS = (
    "问卷", "调研", "受访", "样本", "量表", "测量", "统计", "加权",
    "专家打分", "评分模型", "打分法", "模型", "评级", "五点", "七点",
    "n=", "N=", "份问卷", "L5估算", "(L5估算)",
)
# 分支A：X（满分10）/ X分（满分10）/ X（满分10分）；分支B：X/10(分) 且位于 句/行/段尾（须后接标点或换行）。
# 组件之间只允许空格/Tab（[ \t]*），不允许换行——防"数字/满分10 跨空行拼接"；
# 分支B 的行/句尾由 lookahead 里纳入 \n \r $ 与中文标点承担（不再依赖 MULTILINE）。
_PSEUDO_SCORE_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)分?[ \t]*[（(][ \t]*(?:满分|总分)[ \t]*10[ \t]*分?[ \t]*[）)]"
    r"|(?<![\d.])(\d+(?:\.\d+)?)[ \t]*/[ \t]*10(?=[ \t]*[分制。．，、；;！？!?）)]|\n|\r|$)"
)


def _check_pseudo_metric(text: str) -> list[dict[str, Any]]:
    """挑"0-10 打分但没有方法学/样本"的断言句；表格行（列名不算打分）跳过。"""
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # CRLF/CR → LF：段落界定(rfind "\n\n")依赖 LF
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _PSEUDO_SCORE_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start())
        if text[line_start + 1:m.start()].lstrip().startswith("|"):
            continue  # 表格行：表头列名/单元格值不是正文断言
        # 方法词只在同一段(空行分隔)内找，防跨段读到别句的"调研/问卷"而漏报
        ps = text.rfind("\n\n", 0, m.start())
        ps = 0 if ps < 0 else ps + 2
        pe = text.find("\n\n", m.end())
        pe = len(text) if pe < 0 else pe
        ctx = text[max(ps, m.start() - 60): min(pe, m.end() + 60)]
        if any(w in ctx for w in _PSEUDO_METHOD_WORDS):
            continue
        snip = re.sub(r"\s+", " ", text[max(0, m.start() - 24): min(len(text), m.end() + 8)]).strip()
        if snip in seen:
            continue
        seen.add(snip)
        out.append(
            {
                "kind": "pseudo_metric",
                "score": m.group(1) or m.group(2),
                "snippet": snip,
            }
        )
        if len(out) >= 5:
            return out
    return out


# ============================================================
# 汇总 + 渲染
# ============================================================
# ============================================================
# R7 · 来源资格：政府/宏观来源**不得**背书"特定企业的微观经营数据"
# ------------------------------------------------------------
# 病案（2026-09-11 拆真实报告）：正文写「2026年预计下沉门店总数达约3,200家，占全国总店数的42%
# 「gov.cn 2026年数据」」，核心指标一览表照抄成「来源机构=gov.cn，数据级别=L1官方」。
# 但**中国政府网/统计局绝不会发布某家企业的门店数与单店日销**——这是**来源归属造假**，
# 且因挂着"L1官方"反而比无来源更危险：评审里任何懂行的人一眼就看穿。
# 归因只有两种可能：① 检索内容里的数字被错误挂到了政府域名上；② 模型凭空编了归属。
# 两种都必须拦——政府来源只能背书宏观/政策/统计口径，企业微观数据只能挂
# 企业披露（巨潮/SEC/年报）/ 券商研报 / 行业机构。
_GOV_SRC = (
    "gov.cn", "政府网", "中国政府网", "国家统计局", "统计局", "发改委", "商务部",
    "工信部", "市场监管总局", "海关总署", "央行", "证监会", "财政部",
)
# 强企业微观词（宏观来源不可能发布）
_MICRO_OPS = (
    "单店", "日销", "日均销售", "日均营收", "日均杯量", "客单价", "市占率", "同店",
    "复购率", "坪效", "单杯", "杯量", "关店率", "闭店率", "关店", "闭店",
    "门店数", "门店总数", "门店渗透率", "开店成本", "加盟费", "单店模型", "盈亏平衡",
)
_PICK = re.compile(
    r"[「『【(（]?\s*(" + "|".join(re.escape(x) for x in _GOV_SRC) + r")[^。；]{0,12}?"
    r"(?:数据|来源|统计|发布)?\s*[」』】)）]?"
)


def _check_source_qualification(text: str) -> list[dict[str, Any]]:
    """R7：政府/宏观来源被挂到"特定企业微观经营数据"上 → 来源资格错配（高风险）

    逐行扫描（表格行也是一行）：同一行内既出现政府来源标记、又出现企业微观指标词 → 命中。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # **按句切分**（不是按行）：报告一个自然段就是一行、动辄几百字，
    # 按行扫会把"A 句提了国家统计局"与"B 句提了单店营收"凑成一次误报（实测踩过）。
    # 归属关系只在**同一句/同一表格行**里才成立。
    for seg in re.split(r"[。；;!？?\n]", text or ""):
        s = seg.strip()
        if not s or len(s) > 200:
            continue
        m = _PICK.search(s)
        if not m:
            continue
        hits = [k for k in _MICRO_OPS if k in s]
        if not hits:
            continue
        key = m.group(1) + "|" + "".join(hits[:2])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "rule": "R7",
                "level": "high",
                "title": "来源资格错配",
                "detail": (
                    f"「{m.group(1)}」被用作**企业微观经营数据**（{('、'.join(hits[:3]))}）的来源。"
                    "政府/宏观来源只背书宏观、政策与统计口径，**不会发布某家企业的门店数/单店日销/客单价**。"
                ),
                "snippet": s[:110],
                "advice": (
                    "该数字须改挂企业披露（巨潮/SEC/年报）/ 券商研报 / 行业机构；"
                    "拿不到就把来源改成「待核」，**不得保留 L1官方 标注**。"
                ),
            }
        )
    return out


# ============================================================
# R8 · 引擎输出的"自我修正叙事"（2026-09-17 实跑病案）
# ============================================================
#
# 病案（逐字来自比亚迪那笔交付报告的「市场规模测算」节）：
#   「原始资料中存在的"**理论年均频次 0**"为系统输入**占位符**，实际测算中
#     **已**依据汽车行业"低频高额"的消费属性，通过"人口基数×潜在占比×客单价×
#     **隐含置换系数**"的逻辑闭环**修正**，**确保结果符合** 164,970亿元（TAM）的量级。」
#
# 这一句同时暴露三件事：
#   ① 测算引擎**吃进了一个占位输入（0）**，没有拒绝，照算；
#   ② 引擎出数之后，**LLM 在正文里用散文把输入改掉了**（"隐含置换系数"是一个
#      没有值、没有出处、无法复算的系数）；
#   ③ 修正的**目标被写成"确保结果符合某个量级"**——即先有结论、再凑参数。
#
# 为什么这是方向性错误：本产品的立身之本是「**引擎算数、LLM 不碰**」（确定性下沉）。
# 这次是反过来——**引擎算错、LLM 圆场**。而圆场的话术恰好长得像"专业严谨"，
# 复核者不逐字读很难发现，比一个明显的错数字更危险。
#
# 提示词里**早就禁止**这类叙事（见 `_SUMMARY_METRICS_PROMPT` 的"口径修正纪律"：
# 禁"原口径偏差，自行修正/校准为…"）——**但一直没有执行机制**，和 `_GENERIC_SRC`
# 那次是同一个形态：**写了规矩，没装牙齿**。
_SELF_CORRECTION = re.compile(
    r"占位符|"
    r"隐含(置换)?系数|"
    r"(已|经)[^。；;!？?\n]{0,14}(修正|校准|订正|调整)[^。；;!？?\n]{0,20}(符合|匹配|量级|口径|区间)|"
    r"确保结果(符合|落在|达到)|"
    r"(自行|手动|人工)(修正|校准|调整|替换)(为|至|成)"
)

# ── R8 第二族：**自协商取值**（2026-09-17 蜜雪冰城跑新增）────────────────────────
# 原文（印在交付报告的市场规模测算一节里）：
#   「…系统基准乐观值为 125 亿元，此处取保守上限与基准值的中间态以反映现实摩擦成本，
#     或直接引用系统乐观值 112-125 亿元区间，本报告取 **112 亿元** 作为乐观上限参考」
# 比第一族更直白：**读者能亲眼看到模型没有在引用引擎，而是在跟自己商量。**
# 而情景数恰恰是系统**已经算好并明确禁止改动**的（见 `core/scenario.py` 的指令块
# "禁止自行改动或重算"）——正文却把一个既不是系统值、也没说明来源的数印成了结论。
#
# 判据分两档，**两档都要求同句出现 ≥2 个不同数字**。这条共现条件不是装饰：
# 没有它，「本报告采用区间估计」「本次选取 2026 年为预测期」这类完全正常的表述
# 会被常态误伤——而一条常态误报的检查，会连同它旁边那些真检查一起被无视。
_SELF_NEGOTIATION_STRONG = re.compile(r"(本报告|本文|此处|这里)\s*(取|选取|选用)")
_SELF_NEGOTIATION_WEAK = re.compile(
    r"中间态|折中|折衷|或直接引用|(取|选)[^。；;!？?\n]{0,20}(区间|上限|下限)"
)
# ⚠️ 共现条件用的是**带单位的数**，不是任意数字（2026-09-17 首版实测打回）。
# 首版用 `\d[\d,\.]*`，于是**每份报告都会被误报一次**：
#     「**调研方法论**：本报告采用咨询级分析框架…数据标注来源（L1官方/L2媒体/L3行业/L4用户/L5推测）」
# —— `本报告采用` + L1..L5 里的 5 个数字 = 命中。**这条会常态误报的检查，
# 会连同它旁边那些真检查一起被无视**（与 LTV 那个被撤掉的标志同款教训）。
# 收紧成一个必须**同时**满足的条件：数值必须带单位（%／元／万元／亿元／万家…），
# 且同句 ≥2 个不同。`L1`／`2026`／`±5%` 这类因此全部出局，而真病案（15%、25亿元、112亿元、125亿元）留下。
_NEGOTIABLE_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:[%％]|万元|亿元|万亿|元|万家|万人|家)")


def _self_negotiation_hit(s: str):
    """自协商取值句 → 命中的正则（无命中返回 None）。两档都要求 ≥2 个不同的**带单位**数字。"""
    _nums = {re.sub(r"\s+", "", m.group(0)) for m in _NEGOTIABLE_NUM.finditer(s or "")}
    if len(_nums) < 2:
        return None
    return _SELF_NEGOTIATION_STRONG.search(s) or _SELF_NEGOTIATION_WEAK.search(s)


def _check_engine_self_correction(text: str) -> list[dict[str, Any]]:
    """R8：正文不得声称"修正/校准了引擎输出"，也不得**当众在两个数之间挑一个**。

    只报**同一句内**命中（与 R7 同款切分）：跨句凑出来的不算。
    """
    out: list[dict[str, Any]] = []
    for seg in re.split(r"[。；;!？?\n]", text or ""):
        s = seg.strip()
        if not s or len(s) > 300:
            continue
        m = _SELF_CORRECTION.search(s)
        _family = 1
        if not m:
            m = _self_negotiation_hit(s)
            _family = 2
        if not m:
            continue
        _title = (
            f"正文自称修正了引擎/测算输出（命中「{m.group(0)}」）"
            if _family == 1 else
            f"正文当众在两个数值之间自行择一（命中「{m.group(0)}」）"
        )
        _detail = (
            "测算结果只能由确定性引擎产出，正文不得改写、不得用无法复算的系数"
            "（如「隐含置换系数」）把它凑到某个量级。**发现引擎值不合理时的正确处置是："
            "标为高风险 / 进缺口披露 / 拒绝出数**——不是在正文里圆场。"
            "若输入本身是占位值（如频次 0），应当**不出这个测算**并如实标注，"
            "而不是先有结论再凑参数。"
            if _family == 1 else
            "情景与敏感性数值**同样由引擎算好**，提示词里明写「禁止自行改动或重算」。"
            "正文写出一个既非系统值、又未说明来源的数，等于把引擎的结论替换成模型的口头判断——"
            "而读者无从分辨哪个才是系统的输出。**正确处置：要么原样引用系统值，"
            "要么不写这一档情景**，不许在正文里自行择取中间态或自定一个数。"
        )
        out.append(
            {
                "rule": "R8",
                "kind": "engine_self_correction",
                "level": "high",
                "severity": "high",
                "title": _title,
                "detail": _detail,
                "quote": s[:160],
            }
        )
    return out



# ══════════════════════════════════════════════════════════════════════════
# R9 · 正文印出的算式必须自洽（确定性复算，零模型）
# ══════════════════════════════════════════════════════════════════════════
# 病案（2026-09-17 元气森林跑，交付报告原文）：
#     | **核心目标市场规模 (SAM)** | **约 758.4 亿元** | 计算过程：9.3亿 × 18.5% × 42.0% × 20.5 × 4.8 |
#   `9.3亿 × 18.5% × 42% × 20.5 × 4.8 = 71.1 亿元`——**印出来的是 758.4 亿元，差 10.7 倍**。
#   同一份报告的 TAM 行 `9.3亿 × 85% × 40 × 6 = 1,897 亿` 却是对的，所以这不是"口径分歧"，
#   而是**印出的结果和它自己印出的输入对不上**。
#
# 为什么这条值得单独立规则：产品对外的头号卖点是
#   「人口基数等事实参数由系统知识库注入，测算数值由 Python 引擎计算；LLM 不参与计算」。
# 一个 10.7 倍的乘积错误印在测算表里，攻击的正是这句话本身——
# 它比任何一条内容判断错误都更伤，因为**别的错误可以争论，算术不能**。
#
# 判据刻意收得很紧（宁可少抓，不可常态误报）：
#   ① 同一**行**内（跨行的算式不认——那多半是排版断裂，不是算式）；
#   ② `×` 链上 **≥3 个**因子（两项相乘的散文太多，如"135,000 元 × 65%"）；
#   ③ 结果必须是**带金额单位**的数（亿元/万亿元/万元）——单位缺失就不判，
#      因为"这个数是结果吗"本身就成了猜测；
#   ④ 容差 10%：输入里 `9.3亿` 这类写法本身带四舍五入，卡太紧会把正确算式判错。
_UNIT_SCALE = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "": 1.0}
_FACTOR = re.compile(r"(?<![A-Za-z\d.])(\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|万)?\s*(%|％)?")
_RESULT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|万)\s*元")


def _factor_value(num: str, unit: str, pct: str) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    if pct:
        return v / 100.0
    return v * _UNIT_SCALE.get(unit or "", 1.0)


_RANGE_IN_CHAIN = re.compile(r"\d\s*[-–—~]\s*\d")


def _is_result(line: str, pos: int) -> bool:
    """这个因子位置是否其实是**算式的结果**（带金额单位）。

    没有这一条，`按…9.3亿人 × … 85% × … 40 杯 × 客单价 6 元 测算，TAM 约为 1,897 亿元`
    里的 `1,897 亿元` 会被当成第 4 个因子，连乘出 4.4e21，于是**把一条完全正确的算式
    判成错**（首版实测就是这样）。结果不是因子——先认出结果，再从因子里摘掉。
    """
    return any(r.start() <= pos < r.end() for r in _RESULT.finditer(line))


def _check_printed_arithmetic(text: str) -> list[dict[str, Any]]:
    """R9：同一行里 `×` 链的乘积必须与同行印出的金额相符。"""
    out: list[dict[str, Any]] = []
    for ln in (text or "").split("\n"):
        if "×" not in ln or len(ln) > 600:
            continue
        segs = ln.split("×")
        if len(segs) < 3:                      # ② ≥3 个因子才判
            continue
        # 每个 × 段的**最后一个**"数字+单位"是该段的因子（段里常有中文修饰语）
        # 区间表达式不判：「8-12万元 × 3 个 × 2 轮，合计约 60 万元」里的
        # `12万元` 只是区间上端，连乘出来必然对不上"约 60 万"——那是中点，
        # 不是错误。整行含区间就整行放过（宁可少抓）。
        if _RANGE_IN_CHAIN.search(ln):
            continue
        vals: list[float] = []
        _off = 0
        for sg in segs:
            # ⚠️ 因子位置必须**换算成行内偏移**再与结果位置比：`finditer(sg)` 给的是段内偏移。
            # 首版就是拿段内偏移去比行内偏移，于是 `1,897 亿元`（结果）没被摘掉、
            # 当成第 4 个因子连乘出 6e21，**把一条完全正确的 TAM 算式判成错**。
            ms = [m for m in _FACTOR.finditer(sg) if not _is_result(ln, m.start() + _off)]
            if not ms:
                vals = []
                break
            m = ms[-1]
            v = _factor_value(m.group(1), m.group(2) or "", m.group(3) or "")
            if v is None or v == 0:
                vals = []
                break
            vals.append(v)
            _off += len(sg) + 1
        if len(vals) < 3:
            continue
        _allpos, _off2 = [], 0
        for sg in segs:
            _allpos += [m.start() + _off2 for m in _FACTOR.finditer(sg)]
            _off2 += len(sg) + 1
        _lo, _hi = (min(_allpos), max(_allpos)) if _allpos else (0, 0)
        # **一行里出现两个以上"带金额单位的结果候选"→ 整行放过**。
        # 病案（同一跑，规则层附录的引用行）：
        #   `| 1,897亿元 | 市场规模测算：TAM约为1,897亿元 | …TAM计算假设'9.3亿 × 85% × 40次/年 × 6元'。…`
        # 这一行里"结果"有两个（表头两格），到底哪个是这条算式的结果无从判断；
        # 而它末段的 `40`（"40次/年"的第二处）会被当成第 4 个因子连乘出 1.265e12，
        # 于是**给一条引用句扣上"算错了"的指控**。指代不明就不判——猜错比不说更糟。
        if len(list(_RESULT.finditer(ln))) > 1:
            continue
        prod = 1.0
        for v in vals:
            prod *= v
        if prod <= 0:
            continue
        # ③ 结果必须是带金额单位的数，且**与算式同句**（字距 ≤40）
        #    · 不在因子位置上：`9.3亿` 是因子不是结果；
        #    · **必须紧邻算式**——病案（同一跑）：规则层附录里引了一段被截断的原文
        #      「…TAM计算假设'城镇人口9.3亿 × 85%潜在占比 × 40次/年 × 6元'。其中'40次/…」
        #      末尾那个 `40`（"40次/年"的第二处）被当成第 4 个因子，连乘出 1.265e12，
        #      于是**给一条引用句扣上"算错了"的指控**。引用句里的算式是被引用的，
        #      不是本报告在演示推导——两者必须分开。
        for rm in _RESULT.finditer(ln):
            if min(abs(rm.start() - _lo), abs(rm.start() - _hi)) > 40:
                continue
            try:
                shown = float(rm.group(1).replace(",", "")) * _UNIT_SCALE[rm.group(2)]
            except ValueError:
                continue
            if shown <= 0:
                continue
            if abs(prod - shown) / shown <= 0.10:      # ④ 容差 10%
                continue
            out.append({
                "rule": "R9",
                "kind": "printed_arithmetic",
                "level": "high",
                "severity": "high",
                "title": (
                    f"正文印出的算式算不出它自己印的结果（{len(vals)} 项连乘 = "
                    f"{prod:,.4g}，报告写 {rm.group(0)}，相差 {shown / prod:,.1f}×）"
                ),
                "detail": (
                    "测算结果由确定性引擎产出，**正文不得印一个自己算不出来的乘积**。"
                    "这条不是「假设激不激进」的口径之争——**同一行里印出的输入和印出的输出对不上，"
                    "是算术问题**，而算术语义上没有可争辩的余地。"
                    "正确处置：要么让引擎出这个数，要么把这个算式连同结果一起删掉。"
                ),
                "quote": ln.strip()[:180],
                "snippet": ln.strip()[:120],
            })
            break            # 一行只报一次
    return out



def lint_report(text: str, current_year: int | None = None) -> list[dict[str, Any]]:
    """整篇报告规则层自检 → 违规列表（空=干净）。绝不抛异常、绝不阻断交付。"""
    if not text or not text.strip():
        return []
    try:
        return (
            _check_metric_consistency(text)
            + _check_future_years(text, current_year)
            + _check_term_definition(text)
            + _check_unauthored_number(text)
            + _check_arithmetic(text)
            + _check_pseudo_metric(text)
            + _check_source_qualification(text)
            + _check_engine_self_correction(text)
            + _check_printed_arithmetic(text)
            + _check_unit_denominator(text)
            + _check_advice_vs_breakeven(text)
            + _check_decision_thresholds(text)
            + _check_metric_label_scope(text)
            + _check_stale_metric_period(text)
        )
    except Exception:
        # 规则层 bug 不静默：traceback 打到 stderr（交付路径无害，仅错误时出现）
        import sys
        import traceback

        traceback.print_exc(file=sys.stderr)
        return []


def format_rule_lint_md(violations: list[dict[str, Any]]) -> str:
    """规则层自检 → markdown（追加到报告附录，紧邻 Red Team 之前）

    两类条目 schema 并存，必须都认（2026-09-14 实跑踩中，且是**结构性**的）：
    - 多数检查项产出 `{"kind": ...}`（metric_conflict / future_year / …）
    - **R7 来源资格错配产出 `{"rule": "R7", ...}`，没有 `kind`**
    原先这里直接 `v["kind"]` → 只要报告里出现一条 R7，**整节规则层自检就地 KeyError 消失**
    （异常被交付层 try/except 吞掉，日志只留一行 `规则层自检失败: 'kind'`）——
    而 R7（"拿国家统计局给某家企业的单店日销背书"）恰恰是这一节最该报出来的东西。
    这就是本仓库反复出现的"判定 → 消费 → 披露"断链：**检出了，但送不到读者眼前。**
    """
    if not violations:
        # **干净也要印自一致性层**：这一层是"常驻检查"，不是"出事才出现的东西"——
        # 只在有违规时才印它，读者会以为它跟别的规则一样是**事后**才跑的。
        _rows = [
            "### 🪞 自一致性层（报告自己这几部分彼此对不对得上）",
            "",
            "本节**无论有无发现都执行**：下表是**已登记的、必须彼此对得上的每一对**，"
            "右列是它在代码里对应的那个检查。上面查「外部证据支不支持这句话」，这一层查"
            "**报告自己内部两处对不对得上**。",
            "",
            "> ⚠️ **登记表 ≠ 本跑触发清单**，而且**它是滚动清单、不是穷举**——"
            "它保证「这条关系有检查在管」，**不保证**「本跑它拦到了东西」。",
            "",
            "| # | 必须彼此对得上的一对 | 判据 | 落地检查 |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for _name, _judge, _impl in SELF_CONSISTENCY_PAIRS:
            _rows.append(f"| {_name} | {_judge} | `{_impl}` |")
        return (
            "### ✅ 规则层自检（口径一致性 / 时序 / 术语定义 / 数字背书 / 算术核验 / 伪量化 / 量纲一致性 / 建议与红线）\n\n"
            "确定性规则引擎复核：未发现机械性口径冲突、未来年份引用、未定义术语、无背书数字、算术错误或伪量化打分。\n\n"
            + "\n".join(_rows)
            + "\n"
        )
    lines = [
        "### ⚠️ 规则层自检（口径一致性 / 时序 / 术语定义 / 数字背书 / 算术核验 / 伪量化 / 量纲一致性 / 建议与红线）",
        "",
        "确定性规则引擎（零模型成本）在 LLM 校验前先拦机械性错误，发现以下需人工仲裁项：",
        "",
    ]
    for v in violations:
        kind = v.get("kind") or ""
        if kind == "metric_conflict":
            # 2026-09-18：从"把矛盾交出去"改成"给判断 + 给依据"——
            # 补「每个取值出自哪一节」（同指标跨节不同口径是常态，摆出来读者 10 秒收敛）
            # 与「建议主口径」（取报告自己《核心指标一览表》那一行的值）。
            # 取不到就明说"无法定夺"，**不猜**。
            _sec = v.get("by_section") or {}
            _seclab = "；".join(
                f"{k}：{'、'.join(f'{x:g}' for x in vals)}" for k, vals in list(_sec.items())[:6]
            )
            lines.append(
                f"- **口径冲突**：指标「{v['metric']}」在报告内取值跨度 {v['spread']}×"
                f"（{('、'.join(v['values']))}）。"
                + (f"**各取值出自哪一节**——{_seclab}。" if _seclab else "")
                + (f" {v['advice']}" if v.get("advice") else "")
                + " 原文出处：" + "；".join(f"“{s}”" for s in v["snippets"])
            )
        elif kind == "future_year":
            lines.append(
                f"- **未来年份**：出现「{v['year']}年」当已成事实（无 预计/目标/至 等语境）：…{v['snippet']}…"
            )
        elif kind == "term_undefined":
            lines.append(
                f"- **术语未定义**：「{v['term']}」全报告未见中文/定义。{v['hint']}。出现处：…{v['snippet']}…"
            )
        elif kind == "unauthored_number":
            lines.append(
                f"- **无背书数字**：「{v.get('marker','')}」后紧跟数值 {v.get('number','')}"
                f"（无具体机构可点）：…{v.get('snippet','')}…。请补具体机构+年份，"
                f"否则应改为'(估算/待核)'标注，不得冒充已核数据。"
            )
        elif kind == "arithmetic_conflict":
            lines.append(
                f"- **算术核验**：正文写「{v.get('value','')} 相当于其{v.get('kind_word','')}收入的 "
                f"{v.get('stated'):g}%」，按对应月均收入 {(v.get('month_base') or 0):,.0f} 元换算实际约 "
                f"{v.get('actual'):g}%（偏差>4pct）——请核对口径/数值或修正表述：…{v.get('snippet','')}…"
            )
        elif kind == "pseudo_metric":
            lines.append(
                f"- **伪量化**：正文给出 0-10 打分 {v.get('score','')} 分，但 ±60 字内无方法学/样本"
                f"（问卷/调研/受访/量表等），也未见 (L5估算) 标注：…{v.get('snippet','')}…。"
                f"请改为区间+判断逻辑，或补方法与样本来源。"
            )
        elif kind in ("unit_denominator_conflict", "advice_below_breakeven",
                      "decision_threshold_below_bep"):
            # 2026-09-18 新增三条：都是**硬错**（不是口径分歧），逐条给两个原文取值 + 处理要求。
            lines.append(
                f"- **{v.get('title', '')}（{v.get('rule', '')}，高风险）**：{v.get('detail', '')}"
                f" 建议：{v.get('advice', '')}"
            )
        elif kind == "source_not_retrieved":
            # 2026-09-18 新增。**刻意不标"高风险"**——它没有阻断权，理由见规则定义处的注释。
            lines.append(
                f"- **{v.get('title', '')}（{v.get('rule', '')}，**待人工确认，不阻断交付**）**："
                f"{v.get('detail', '')} 建议：{v.get('advice', '')}"
            )
        elif v.get("rule") == "R7":
            lines.append(
                f"- **来源资格错配（{v.get('level') or 'high'} 风险）**：{v.get('detail','')}"
                f"…{v.get('snippet','')}… 建议：{v.get('advice','')}"
            )
        else:
            # 兜底：schema 未知也**照印**。新增检查项时忘了在这里加分支，
            # 后果必须是"印得朴素"，而不是"整节消失"——本函数原先正是因为 R7 没有 `kind`
            # 而 KeyError，整节规则层自检在报告里从来没出现过。
            head = v.get("title") or v.get("kind") or v.get("rule") or "未分类项"
            body = v.get("detail") or v.get("hint") or v.get("snippet") or str(v)
            lines.append(f"- **{head}**：{body}")
    lines.append("")
    lines.append("> 规则层为确定性校验，发现项供人工复核后定稿，不阻断交付。")
    # **自一致性层 · 七对**（2026-09-18 加）。
    #
    # 这一节**无论有没有发现都要印**，而且**由 `SELF_CONSISTENCY_PAIRS` 直接渲染**——
    # 不许手写副本。理由：它是本产品对"我们自己内部对不对得上"的**能力声明**，
    # 声明脱离代码的那一刻就变成了宣传（本仓库在这件事上有过前科：
    # 材料说"逐条核对验证对"，而 `pair_required` 当时只被打印）。
    # 表里每一行右列的检查项**都能在代码里指名道姓地找到**，`_fulltest` 有锁。
    lines.append("")
    lines.append("### 🪞 自一致性层（报告自己这几部分彼此对不对得上）")
    lines.append("")
    lines.append(
        "上面各条查的是「**外部证据支不支持这句话**」。这一层查的是另一件事："
        "**报告自己内部的两处是不是对得上**——八层审计里此前没有一层在做这件事。"
        "下表是**已登记的、必须彼此对得上的每一对**，右列是它在代码里对应的那个检查。"
    )
    lines.append("")
    lines.append(
        "> ⚠️ **这是一张登记表，不是本跑的触发清单。** "
        "它保证的是「这条关系有检查在管」，**不保证**「本跑它真的拦到了东西」——"
        "每一对本跑拦到了什么，看上面各节的检出项。"
        "**而且它是滚动清单，不是穷举**：2026-09-18 第九跑之后外部审计又指出若干对本表未覆盖，"
        "其中 2 对经我方复核确认为真，1 对经复核被推翻（是我方证据不足，不是对方错）——登记工作未完成。"
        "（原先这里写的是「七对是穷举出来的……每对都有确定性检查落地」。"
        "那句话是一句**静态断言**，同一份报告就能推翻它：第九跑里第④对声明的 R13 "
        "因词表/量纲/表格行三道门全未命中而**返回空**，即那一对本跑什么也没拦到。"
        "把它改成登记表 + 滚动清单，是为了让这句话**不再需要修辞来维持**。）"
    )
    lines.append("")
    lines.append("| # | 必须彼此对得上的一对 | 判据 | 落地检查 |")
    lines.append("| :--- | :--- | :--- | :--- |")
    for _name, _judge, _impl in SELF_CONSISTENCY_PAIRS:
        lines.append(f"| {_name} | {_judge} | `{_impl}` |")
    lines.append("")
    return "\n".join(lines)


# ============================================================
# R12 · 决策区点名的来源机构，必须在本系统**真的检索过**的来源里找得到
# ============================================================
# 病案（2026-09-18 实跑，用户交回的交付件，由外部审计 A2 指出、我逐条复核确认）：
#   《📊 核心指标一览表》8 行里 **6 行**的来源机构写的是
#   `Euromonitor International`（×4）与 `艾瑞咨询`（×2）——
#   而这份报告全文的检索来源只有 11 个域名：
#     m.21jingji.com / ccfa.org.cn / ndrc.gov.cn / t.cj.sina.com.cn / stats.gov.cn /
#     cd.nbd.com.cn / cnstock.com / api.worldbank.org / sohu.com / eastmoney / starbucks.com.cn
#   **`euromonitor` / `iresearch` / `analysys` 在全文出现 0 次。**
#   审计面自己还在别处写着「提供的证据中无 Euromonitor 或相关…」。
#
#   这不是"来源层级低"，是**报告把数字挂给了一个本系统从未访问过的机构**——
#   即引用幻觉。而「引用可核验」正是本产品的核心主张：
#   一份自己印着"来源：Euromonitor"、而系统台账里查无此站的报告，
#   在说明桌上被问一句「Euromonitor 那页你什么时候取的」就穿了。
#
# 判据（**两步，先宽后严**，宁可漏报不可常态误报）：
#   ① **名字直配**：机构名（或其显著片段）出现在任何一条已取回来源的 title/url/source 里 → 算落地。
#      这一条不需要别名表就能覆盖大多数情况。
#   ② **别名表回退**：中文报告写"21世纪经济报道"、而台账里存的是域名 `m.21jingji.com`——
#      这时才查 `_ORG_DOMAINS`。**别名表只用来把"查得到"的情况救回来**，
#      它缺项只会导致漏报，不会导致误报（误报来自"名字直配"失败 + 别名也查不到）。
#   取不到《核心指标一览表》或没有 citations → 整条跳过（**不猜**）。
_ORG_NON_SOURCE = (
    "待核", "系统测算", "系统", "本报告", "L5估算", "L5", "估算", "模型", "测算",
    "—", "-", "/", "无", "调研", "问卷", "用户", "内部",
    # 2026-09-18 实测补：这几种是**引擎自己的标签**，不是外部机构——
    # 不补它们，24 份报告里会凭空多出十几条"来源机构查无"（`反方检索`/`基线数据` 等）。
    "反方检索", "基线数据", "历史基线", "报告正文", "抽样追踪", "自有", "知识库", "经验召回",
)
"""这些不是"外部机构"，是**我们自己**或**明确标注的推测**——它们不需要外部来源背书，跳过。"""

# **企业法定披露的落地渠道**：报告写「瑞幸2024Q1财报」，而台账里存的是 sec.gov 的 filing 页——
# 名字对不上域名是**正常的**（公司名 ≠ 交易所域名）。只要台账里有这些渠道，就算落地。
_FILING_HOSTS = ("sec.gov", "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk", "neeq.com.cn")
_FILING_WORDS = ("财报", "年报", "半年报", "季报", "招股书", "公告", "filing", "10-K", "10-Q", "20-F", "年度报告")

_ORG_DOMAINS: dict[str, tuple[str, ...]] = {
    # 官方统计 / 监管
    "国家统计局": ("stats.gov.cn",), "中国国家统计局": ("stats.gov.cn",),
    "国家发展和改革委员会": ("ndrc.gov.cn",), "国家发改委": ("ndrc.gov.cn",), "发改委": ("ndrc.gov.cn",),
    "商务部": ("mofcom.gov.cn",), "中国人民银行": ("pbc.gov.cn",), "央行": ("pbc.gov.cn",),
    "中国证监会": ("csrc.gov.cn",), "市场监管总局": ("samr.gov.cn",),
    "上海证券交易所": ("sse.com.cn",), "深圳证券交易所": ("szse.cn",),
    "巨潮资讯": ("cninfo.com.cn",), "全国中小企业股份转让系统": ("neeq.com.cn",),
    # 国际组织
    "世界银行": ("worldbank.org",), "World Bank": ("worldbank.org",),
    "国际货币基金组织": ("imf.org",), "IMF": ("imf.org",),
    "经济合作与发展组织": ("oecd.org",), "OECD": ("oecd.org",),
    "世界卫生组织": ("who.int",), "联合国": ("un.org",),
    # 行业协会
    "中国连锁经营协会": ("ccfa.org.cn",), "CCFA": ("ccfa.org.cn",),
    "中国咖啡协会": ("ccoffee.org",), "中国食品工业协会": ("cnfia.cn",),
    # 行研机构（本产品最容易被模型凭记忆挂名的几家）
    "Euromonitor": ("euromonitor.com", "euromonitor.cn"),
    "欧睿": ("euromonitor.com", "euromonitor.cn"), "欧睿国际": ("euromonitor.com", "euromonitor.cn"),
    "艾瑞咨询": ("iresearch.com.cn", "iresearch.cn"), "艾瑞": ("iresearch.com.cn", "iresearch.cn"),
    "头豹研究院": ("leadleo.com",), "头豹": ("leadleo.com",),
    "弗若斯特沙利文": ("frostchina.com", "frost.com"), "沙利文": ("frostchina.com", "frost.com"),
    "中商产业研究院": ("askci.com",), "前瞻产业研究院": ("qianzhan.com",),
    "华经产业研究院": ("huaon.com",), "智研咨询": ("chyxx.com",),
    "观研报告网": ("chinabaogao.com",), "尼尔森": ("nielsen.com",), "凯度": ("kantar.com",),
    "Mintel": ("mintel.com",), "英敏特": ("mintel.com",),
    "Statista": ("statista.com",), "Nielsen": ("nielsen.com",),
    # 交易所 / 上市公司
    "星巴克": ("starbucks.com", "starbucks.com.cn"), "Starbucks": ("starbucks.com",),
    "瑞幸": ("luckincoffee.com", "lkcoffee.com"), "瑞幸咖啡": ("luckincoffee.com", "lkcoffee.com"),
    "蜜雪冰城": ("mxbc.com",), "库迪": ("cotti.com",), "喜茶": ("heytea.com",),
    # 媒体
    "21世纪经济报道": ("21jingji.com",), "每日经济新闻": ("nbd.com.cn",),
    "证券时报": ("stcn.com",), "上海证券报": ("cnstock.com",), "中国证券报": ("cs.com.cn",),
    "界面新闻": ("jiemian.com",), "36氪": ("36kr.com",), "第一财经": ("yicai.com",),
    "财新": ("caixin.com",), "新浪财经": ("sina.com.cn", "sina.cn"),
    "东方财富": ("eastmoney.com",), "搜狐": ("sohu.com",), "网易": ("163.com",),
    "腾讯": ("qq.com",), "虎嗅": ("huxiu.com",), "钛媒体": ("tmtpost.com",),
    "Wind": ("wind.com.cn",), "万得": ("wind.com.cn",),
}
_ORG_HINT = ("咨询", "研究院", "研究所", "国际", "机构", "协会", "商会", "公司", "集团", "Inc", "Ltd")


def _retrieved_corpus(citations: list[dict[str, Any]] | None) -> str:
    """已取回来源 → 一份可检索的文本（title + url + source + publisher），小写。"""
    parts: list[str] = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        for k in ("title", "url", "source", "publisher"):
            v = c.get(k)
            if v:
                parts.append(str(v))
    return " ".join(parts).lower()


def check_metric_source_grounding(
    md: str, citations: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """《核心指标一览表》的「来源机构」列，是否都能在本系统真的取回来的来源里找到。"""
    out: list[dict[str, Any]] = []
    try:
        _corpus = _retrieved_corpus(citations)
        if not _corpus:
            return []  # 没有 citations 就无从核对——**不猜**
        m = re.search(r"^##\s*📊\s*核心指标一览表\s*$(.*?)(?=^##\s|\Z)", md or "", re.M | re.S)
        if not m:
            return []
        for ln in m.group(1).splitlines():
            _l = ln.strip()
            if not _l.startswith("|"):
                continue
            _cells = [c.strip() for c in _l.strip("|").split("|")]
            if len(_cells) < 3:
                continue
            _metric, _org = _cells[0], _cells[2]
            if not _metric or _metric in ("指标",) or set(_metric) <= set(":- "):
                continue
            _o = _org.strip()
            if not _o or any(k in _o for k in _ORG_NON_SOURCE):
                continue
            _ol = _o.lower()
            # ① 名字直配：机构名（或 ≥3 字的显著片段）出现在已取回来源里
            if _ol in _corpus:
                continue
            # ①b **企业法定披露**：公司名 ≠ 交易所域名，名字直配必然失败。
            # 只要标签里带"财报/年报/招股书/公告"，且台账里有交易所渠道，就算落地。
            if any(w.lower() in _ol for w in _FILING_WORDS) and any(h in _corpus for h in _FILING_HOSTS):
                continue
            _groun = False
            for _k, _doms in _ORG_DOMAINS.items():
                if _k.lower() in _ol:
                    if any(d in _corpus for d in _doms):
                        _groun = True
                    break
            if _groun:
                continue
            out.append(
                {
                    "kind": "source_not_retrieved",
                    "rule": "R12",
                    "title": "决策区点名的来源机构，本次台账里找不到",
                    # ⚠️ **刻意不是 high**（2026-09-18，本规则上线当天定）。
                    # 判据的**假阳性是结构性的**，有三类说不清的情况：
                    #   · 数字其实来自某个已取回页面**转述**了该机构（如界面新闻转引艾瑞），
                    #     正文却把上游机构名填进了来源列——此时数字**是可追溯的**，只是名字填错了；
                    #   · 政府机构内容常转载于 gov.cn 等上级站；
                    #   · 别名表不可能穷尽所有机构。
                    # 而它的**真阳性也是结构性的**：24 份历史报告里只有 **4 份**真的取回过
                    # iresearch，却有 **21 份**的表把「艾瑞咨询」写成来源机构。
                    # 一个真假混杂、且假阳性说不清的判据**不许有阻断权**——
                    # 本仓库的规矩是"宁可少一条检查，也不要一条会常态误报的检查"。
                    # 所以它是告警：**问题摆到读者眼前，但不替读者下结论。**
                    "level": "warn",
                    "detail": (
                        f"《核心指标一览表》里「{_metric[:26]}」一行，"
                        f"来源机构写的是 **{_o}**——而在本次**已取回的 {len(citations or [])} 条来源**里"
                        f"（标题/URL/来源名）**找不到这个机构**。"
                        f"两种可能，报告没告诉读者是哪一种："
                        f"① 数字其实来自某个已取回页面**转述**了该机构——那是**名字填错了**，"
                        f"该填你真正打开的那一页；"
                        f"② 这个机构名只是**模型记忆**里的，本次从没访问过——那是**来源幻觉**。"
                    ),
                    "advice": (
                        "三选一，不许维持原样：① 补一次该机构的定向检索，把真实来源页并进台账；"
                        "② 若数字来自别的已取回页面的转述，**改填那一页**（转述链要一并写明）；"
                        "③ 若只是模型记忆里的机构名，**降级标注为「(L5推测·来源未取回)」**。"
                        "**报告不许印一个自己没说清来路的来源机构。**"
                    ),
                    "snippet": f"{_o}、{_metric[:16]}",
                }
            )
    except Exception:
        return []
    return out


# ============================================================
# R13 · 决策阈值必须 ≥ 本报告自己算出的红线（自一致性层 · 决策输入↔同题口径）
# ============================================================
# 病案（2026-09-18 实跑，三份外部审计同时点名，Claude 称其为"最该主动自曝的一条"）：
#   报告自己的单位经济章算出**日均盈亏平衡 205 杯**
#   （55万投资 / 280杯 / 16元 / 62%毛利 / 月固定成本 6.1万：61,000÷62%÷30÷16 ≈ 205）。
#   而正文给出的**三条决策阈值全部低于它**：
#     · 风险与对策：连续 3 个月 < **92 杯** 启动止损（= 月亏约 3.4 万）
#     · 趋势风险  ：连续 6 个月 < **80 杯** 关停迁址（亏更多）
#     · 目标市场  ：「提升至 **150 杯**以上…实现可持续盈利」（仍然亏）
#   **一家日均 100 杯的店（高于止损线、不会触发）每月亏 3 万，而系统不会叫停它。**
#   九层审计没有一层说出这件事——因为每一层都在核"外部证据支持不支持"，
#   没有一层在核"**我自己这几部分彼此对不对得上**"。
#
# ⚠️ 判据必须**把红线锁在单位经济章内**：同一份报告里还有
#   「满产假设 280 杯」「县域星巴克 360 杯的盈亏平衡点」——那是**别的主体的红线**，
#   拿它们当基准就是本仓库反复踩过的"跨主体误报"（R11 第一版取全篇 max 取到 220 杯，已证伪）。
#   所以：**红线只从《单位经济》章取**，阈值从全篇取。
# ── R13 的双方向词表（2026-09-18 第九跑重做）──────────────────────────────
# 病案：第一版只有一张 10 词的**否定式**表（"无法覆盖"…），第九跑正文里
# **合计命中 0 次** → R13 结构上不可能执行。补词之后命中 9 份历史报告，
# 逐条取证得 **12 真 7 假** —— 假阳性不是"词不够准"，是**判据分不清两件事**：
#
#   · "阈值设在红线以下"（缺陷）      ← 要报
#   · "现在低于红线"（正确陈述）      ← 报了就错，那是报告在讲实话
#
# 真正的区分不在词本身，在**句式方向**：
#   A 动作触发型：「低于 N 杯即 止损/关停/迁址/No-Go」→ 这条线设在亏损区里
#   B 充分性断言：「N 杯 即可/方能 覆盖成本」→ 把不够的量写成了够
# 而「N 杯无法覆盖成本」「尚未触及盈亏平衡点」是**对的**，两个方向都不该报。
#
# A 方向词：**动作**（把这些动作挂在一条低于 BEP 的线上，等于系统不会叫停）
# `关店` 后不许跟 `率`——`关店率` 是**指标名**，不是一条动作阈值线。
# （这是本检查自己制造过的一族误报：`核心痛点在于…日均杯量不足100杯` 与
#   `关店率高达29.7%` 同句 → 命中"关店"。第九跑回归里抓到 3 条。）
_DECISION_ACTION_RE = re.compile(
    # `关店/闭店` 后面不许跟 `率`（指标名）、也不许跟 `前`/`数`（限定语）。
    # 病案（2026-09-18 第十跑，由外部审计逐条验收 R13 时指出，我方复核确认）：
    #   `| 下沉市场单店日均杯量 | 92 杯 | 下沉市场全量门店中位数（含亏损/**闭店前**数据） | … |`
    #   —— 92 杯是**被调查对象的中位数**，是市场事实，不是本报告的决策阈值。
    #   它之所以被抓，只是因为上一版守卫写的是 `闭店(?!率)`，而"闭店**前**数据"从那个缺口漏了进来。
    #   **这不是判据宽了，是守卫少列了一个后缀**；同类后缀（前/数/者）一并补上。
    r"止损|关停|关店(?!率|前|数)|闭店(?!率|前|数)|退出线|迁址|淘汰机制|反转阈值|"
    # 2026-09-18 第十跑（17:17）新增第三种写法：**警戒线 / 保底**。
    # 病案：`| **H2: 单店模型可修正** | … | 单店日均杯量是否稳定突破 150 杯**警戒线** |`
    # 与「无法在开业前锁定日均 **80杯** 以上的**保底客流**」——
    # 两条都是设在 BEP（242）以下的执行线，而当时的词表只有动作词（止损/关停），
    # 一条都没抓到。**同一个意思在真实报告里有三种写法，只认一种等于只扫三分之一。**
    r"警戒线|保底线|保底客流|"
    r"[Nn][Oo]-[Gg][Oo]"
)
# **情景 / 前瞻框架**排除：这些行在讲"如果那样会怎样"，不是在设一条执行线。
# 实测误报源：「若…无法在2027年前提升至110杯以上，则…将在2028年前后回落」
# 与「保守情景：假设日均杯量下滑至140单，低于盈亏平衡点」——后者正是报告在**正确陈述**。
# ⚠️ 刻意**不含「假设」**：关键假设表 H1/H2 的成立条件就写在「假设」列里，那是要查的。
_THRESHOLD_FRAME_RE = re.compile(
    r"保守|中性|乐观|悲观|情景下|情景中|敏感性|压力测试|"
    r"预计|预测|有望|将在\s*20\d\d|到\s*20\d\d\s*年|20\d\d\s*年前后|外推"
)
# ⚠️ 上面用了**裸的"保守/中性/乐观/悲观"**而不是"保守情景"：敏感性表的情景名
# 写在**单元格里**（`| 保守 | 日均140单 … |`），"保守"后面跟的是 `|`，不是"情景"。
# 只写"保守情景"会漏掉表行——而 140613 的误报正是这么漏过来的。
# **超长段落不报**：止损线写在一条短指令或一个表格单元里，不会藏在 300+ 字的论述段中。
# 实测误报源：134441 那段 470 字的"假设登记表 + No-Go + 复购率 + 96杯"混合论述——
# 里面的 No-Go 讲的是**月频次**，96 杯讲的是**现状**，两者都不是阈值线。
_MAX_THRESHOLD_LINE = 300
# **量纲下限**：低于 BEP 的 1/10 已经不是在讨论同一条线了。
# 实测误报源：「人均日消费量仅 **0.02 杯**」撞上同句的"止损关店"——那是人均消费量，
# 不是门店杯量阈值。一条 0.02 杯的"阈值线"在咖啡场景里不存在。
_MIN_THRESHOLD_RATIO = 0.1
# B 方向：**充分性**断言（数字 + 单位 + 至多 24 字的"即可/方能…覆盖/保本/打平"）
# 第九跑的 H2 正是这一种：「日均杯量 ≥ 100杯 即可覆盖下沉市场固定成本」，而 BEP=222。
_DECISION_SUFFICE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:杯|单)[^。；;!？?\n]{0,24}?"
    r"(?:即可|就能|便可|方能|方可|才能|足以)\s*(?:覆盖|保本|打平|盈利|回本)"
)
# 与 BEP 相对差 ≤5% 的值视为**同一个数的重述/取整**，不是另一条阈值线。
# 取证依据：`< 200杯 → No-Go` 而 BEP=203（0.985）——那不是设低了，那是 203 的近似说法。
_BEP_SAME_TOL = 0.95
_BE_CUP_RE = re.compile(r"盈亏平衡[^0-9\n]{0,16}?(\d[\d,]*(?:\.\d+)?)\s*(?:杯|单)")
# **数在前、词在后**也是标准写法：「日均杯量跌至 **205 杯**（盈亏平衡点）」——实测报告就是这种。
# 不补这条，205 会被整个漏掉（第一版就漏了，取到了下一节的 280）。
_BE_CUP_BEFORE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:杯|单)[^0-9\n]{0,8}?盈亏平衡")
# 量纲补 `单`：第九跑正文写「门店需每日完成约 **222 单**交易方可覆盖全部运营成本」——
# 全篇一个「盈亏平衡…杯」都没有，只认「杯」时 `_report_breakeven_cups` 返回 None，
# R13 在函数第一行就 return []。**同一份报告里"杯/单"是同一件事的两种写法。**
# `\d+\s*单` 的误伤面很小：「单店」是"单"在前，不匹配；必须数字紧邻"单"。
_CUP_NEAR_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:杯|单)")


def _report_breakeven_cups(text: str) -> float | None:
    """本报告**单位经济章**算出的日均盈亏平衡杯量（取不到 → None，**不猜**）。"""
    try:
        _sec_of = _section_map(text)
        best: float | None = None
        for m in re.finditer(r"盈亏平衡", text or ""):
            _sec = _sec_of(m.start())
            if "单位经济" not in _sec and "盈亏平衡点与回本" not in _sec:
                continue
            seg = (text or "")[m.start():m.start() + 60]
            # **窗口不许跨句、更不许跨节**（第一版就错在这）：`盈亏平衡点）。\n## 目标市场\n…280杯`
            # 会被读成"盈亏平衡 = 280"——而那是**下一节另一个主体**的数。
            # 与本仓库在闸门 `_tier_values` 上修过的是同一个坑（"窗口跨句取到下一句的人口"）。
            for _sep in ("。", "\n", "；", ";", "，", "！", "？"):
                _i = seg.find(_sep)
                if _i != -1:
                    seg = seg[:_i]
            _after = _CUP_NEAR_RE.search(seg[3:])          # 跳过"盈亏平衡"四字本身
            # ── 2026-09-18 第九跑病案：杯量写在**下一句**，同句窗口只有「元」 ──
            #   「单店月度盈亏平衡营收额为 100,000 元。折算至日均维度，门店需每日完成
            #     约 **222 单**交易方可覆盖全部运营成本。」
            # 同句里没有杯/单 → `_after` 为 None → 三段全空 → 返回 None
            # → `_check_decision_thresholds` 第一行 `if not _red: return []` → **R13 从未执行**。
            # 退一步看**同段**（到空行或下一个标题为止），但**窗口两端必须落在同一节里**——
            # 这才是原来那道"不许跨节"的真正护栏（原来靠截句号代偿，代价是把下一句一起砍掉）。
            # 取**最近的一个**匹配（不是段内最小）：离「盈亏平衡」最近的那个数才是 BEP，
            # 段内更远处可能已经在讲另一个主体（同 `_tier_values` 踩过的坑）。
            if not _after:
                _para = (text or "")[m.start():m.start() + 200]
                for _stop in ("\n#", "\n\n"):
                    _k = _para.find(_stop)
                    if _k != -1:
                        _para = _para[:_k]
                if _para and _sec_of(m.start() + max(0, len(_para) - 1)) == _sec:
                    _after = _CUP_NEAR_RE.search(_para[3:])
            # 数在词前：「205 杯（盈亏平衡点）」——回看 40 字即可，**同一句内**
            _bwin = text[max(0, m.start() - 40):m.start()]
            for _sep in ("。", "\n", "；", ";", "，", "！", "？"):
                _j = _bwin.rfind(_sep)
                if _j != -1:
                    _bwin = _bwin[_j + 1:]
            _back = _BE_CUP_BEFORE_RE.search(_bwin + "盈亏平衡")
            _before = _BE_CUP_RE.search(seg)
            _v = None
            for _c in (_back, _before, _after):
                if _c:
                    try:
                        _v = float(_c.group(1).replace(",", ""))
                    except Exception:
                        _v = None
                    if _v and _v > 0:
                        break
            if _v and _v > 0:
                # 同一章可能提到多个（满产/情景）——取**最小**的一档作红线（最保守）
                best = _v if best is None else min(best, _v)
        return best
    except Exception:
        return None


def _check_decision_thresholds(text: str) -> list[dict[str, Any]]:
    """决策阈值 / 充分性断言 与本报告自己算的盈亏平衡对不上 → 报。

    两个方向（2026-09-18 第九跑重做，病案见上方词表）：
      A 动作触发型 —— 行内出现「止损/关停/迁址/No-Go/反转阈值」且杯量 < BEP：
        这条线设在亏损区里，门店在触发它之前就已经在亏，系统不会叫停它。
      B 充分性断言 —— 行内写「N 杯 即可/方能 覆盖成本」且 N < BEP：
        把不够的量写成了够。第九跑的 H2（≥100杯 即可覆盖，而 BEP=222）正是这一种。

    **刻意不报的两类**（实测 7 条误报全落在这两类里）：
      · 报告在**正确陈述**低于红线（"92杯无法覆盖成本""尚未触及盈亏平衡点"）——
        那恰恰是我们要它说的话，报了等于惩罚正确行为；
      · 与 BEP 相对差 ≤5% 的值（`<200杯→No-Go` 而 BEP=203）——
        那是同一个数的重述/取整，不是第二条线。
    """
    try:
        _body = _body_only(text)
        _red = _report_breakeven_cups(_body)
        if not _red:
            return []
        _floor = _red * _BEP_SAME_TOL
        _lo = _red * _MIN_THRESHOLD_RATIO
        out: list[dict[str, Any]] = []
        _seen: set[str] = set()
        for _line in _body.splitlines():
            _ls = _line.strip()
            # 只跳 `>`：引用块是闸门印章与审计附录区，不是报告自己的主张。
            # **表格行必须照扫**——决策阈值最常见的载体就是表
            # （《关键假设与可推翻变量》《行动清单》《风险与对策》全是表）。
            # 第一版用 `startswith(("|", ">"))` 把表格整个跳过，
            # 于是"专防阈值低于盈亏平衡"的检查，跳过了阈值所在的地方。
            if not _ls or _ls.startswith(">"):
                continue
            if len(_ls) > _MAX_THRESHOLD_LINE or _THRESHOLD_FRAME_RE.search(_ls):
                continue
            _cups = [
                float(x.replace(",", ""))
                for x in _CUP_NEAR_RE.findall(_ls)
                if x.replace(",", "").replace(".", "").isdigit()
            ]
            _low = sorted({c for c in _cups if _lo <= c < _floor})
            if not _low:
                continue
            _why = ""
            _ms = _DECISION_SUFFICE_RE.search(_ls)
            if _ms:
                _n = float(_ms.group(1).replace(",", ""))
                if _lo <= _n < _floor:
                    _low, _why = [_n], "这一行把该杯量写成了「足以覆盖成本」"
            if not _why and _DECISION_ACTION_RE.search(_ls):
                _why = "这一行的止损/关停/反转阈值"
            if not _why:
                continue
            # **全部低于红线的值都要印**，不只印最小的那一个：
            # H2 那一行同时有「≥100杯 即可覆盖」（成立条件）与「<80杯」（反转阈值），
            # 只印 80 会把**真正写错的那一个（100）**藏起来——100 才是"被写成能覆盖成本"的值。
            _lowtxt = "／".join(f"{c:g}" for c in _low)
            _key = _lowtxt + "|" + _ls
            if _key in _seen:
                continue
            _seen.add(_key)
            out.append(
                {
                    "kind": "decision_threshold_below_bep",
                    "rule": "R13",
                    "title": "决策阈值低于本报告自己算的盈亏平衡线",
                    "level": "high",
                    "detail": (
                        f"本报告《单位经济》章算出**日均盈亏平衡 ≈ {_red:g} 杯**，"
                        f"而**{_why}**是 **{_lowtxt} 杯**——**低于自己的红线**。"
                        f"意思是：按这个数执行的门店，**在触发它之前就已经在亏**，"
                        f"系统不会叫停它。原句：「{_ls[:110]}」"
                    ),
                    "advice": (
                        "把该阈值提到本模型盈亏平衡线以上，或**在句内写明**"
                        "「此阈值为行业经验值，低于本模型 BEP，触及它时单位经济已为负」。"
                        "**报告不能一边算出门店在亏、一边把止损线设在亏损线以下。**"
                    ),
                    "snippet": f"{_lowtxt}杯、{_red:g}杯",
                }
            )
        return out
    except Exception:
        return []


# ============================================================
# R14 · 《核心指标一览表》里，指标名与它**自己备注里写的口径**不符
# ============================================================
# 病案（2026-09-18 第十跑；外部审计 GPT 的 P0-6 与我方独立复核命中同一行）：
#   | 星巴克三线及以下城市门店占比 | 11.2% | 中国连锁经营协会 | L3 | 2024 |
#     备注：星巴克+瑞幸合计占比中的星巴克部分需拆解，此处为头部品牌下沉占比参考 |
#   而正文《趋势风险》写的是「**星巴克与瑞幸**在三四线合计占比仅 11.2%」。
#   → **11.2% 可能是真的，但它不是标题写的那个 11.2%。**
#
#   这是专业研究里最危险的一类错：**数字正确、指标错误**。
#   它比"数字算错"更难发现，因为**每一层来源核验都会放它过**——
#   来源是真的、链接能打开、域名对、层级也对、年份也有。R7/R12 结构上拦不住。
#   唯一能机械抓住的，是**报告自己在备注里承认了口径不等于标题**。
#
# 判据（窄而准，宁可漏报不可常态误报）：
#   同一行同时满足
#     ① 备注列出现**自陈口径不符**的措辞（需拆解 / 口径不同 / 系…合计 / 含…在内 …）
#     ② 指标名列**没有任何**合并或限定的标记（合计 / 总计 / + / 及 / 与 / 之一 / 部分 …）
#   → 报。**只在这两句同框时报**：报告自己都写了口径对不上，
#     就不该由机器替它圆回来，也更不该让它带着一个错的指标名进决策锚。
_METRIC_SCOPE_SELF_RE = re.compile(
    r"需拆解|需分开|口径不同|口径不一致|非单一|非同一|系.{0,6}合计|"
    r"含.{0,8}在内|包含.{0,8}在内|不可直接比较|二者口径|合计占比中的"
)
_METRIC_LABEL_MERGE_WORDS = (
    "合计", "总计", "累计", "综合", "整体", "+", "之一", "部分",
)
# ⚠️ **不收 `及` / `与`**（第一版收了，当场造出一个假阴性）：
# 「三线**及**以下城市门店占比」里的"及"是范围连词，不是合并标记 ——
# 收了它，本检查要抓的那一行**恰好**被自己的豁免规则放走。
# 教训与 R13 的门 1 同型：**词表宽一点看似更安全，实际是把判据最该命中的那种写法排除掉。**


def _check_metric_label_scope(text: str) -> list[dict[str, Any]]:
    """R14：核心指标表的指标名 ↔ 备注自陈口径 不符 → 报。

    只扫《📊 核心指标一览表》，只报"报告自己承接口径对不上、而标题仍按单一口径写"的行。
    """
    try:
        out: list[dict[str, Any]] = []
        for m in re.finditer(
            r"^##\s*📊\s*核心指标一览表\s*$(.*?)(?=^##\s|\Z)", text or "", re.M | re.S
        ):
            for ln in m.group(1).splitlines():
                s = ln.strip()
                if not s.startswith("|"):
                    continue
                cells = [c.strip() for c in s.strip().strip("|").split("|")]
                if len(cells) < 3:
                    continue
                name = cells[0]
                note = cells[-1]
                if not name or set(name) <= set(":- ") or name in ("指标", "指标维度", "指标名称"):
                    continue
                if not _METRIC_SCOPE_SELF_RE.search(note or ""):
                    continue
                if any(w in name for w in _METRIC_LABEL_MERGE_WORDS):
                    continue
                out.append(
                    {
                        "kind": "metric_label_scope_mismatch",
                        "rule": "R14",
                        "level": "high",
                        "title": "指标名与它自己备注里写的口径不符",
                        "detail": (
                            f"《核心指标一览表》这一行的指标名是「{name}」，"
                            f"而**同一行的备注自己写着口径对不上**：「{(note or '')[:80]}」。"
                            "**这个数可能是真的，但它不是标题写的那个数。**"
                            "来源核验拦不住这一类——来源是真的、链接能打开、层级也对。"
                        ),
                        "advice": (
                            "两条路：① 把指标名改成实际口径（如「A+B 合计占比」）；"
                            "② 把该行拆成两行分别给值。**数字正确而指标错误，"
                            "是研究报告里最危险的一类错——它看起来完全可核验。**"
                        ),
                        "snippet": f"{name}｜{(note or '')[:60]}",
                    }
                )
        return out
    except Exception:
        return []


# ============================================================
# R15 · 核心指标表里的**数据期**明显落后于研究时点，而该指标已进决策区
# ============================================================
# 病案（2026-09-18 第十跑；三份外部审计共同点名的那个洞）：
#   《核心指标一览表》里，「星巴克三线及以下城市门店占比 11.2%」的年份列写的是 **2024**，
#   而这份报告的研究时点是 **2026-09**；同表还有 12,470 家（2024）、18,590 家（2024）。
#   这些数字**全部进了决策清单**（「县域门店占比仅 1.8%」直接当关键依据）。
#   现实里 2026-04 博裕合资已交割、2026-09 门店 8,238 家——**报告是 2024 年的世界，
#   题目问的是 2026 年的事。**
#
#   自一致性层第③对（数据期 ↔ 使用位置）声明的正是这件事，而**它此前只展示不判定**：
#   《数据护照》列了 17 行数据期，一个"通过/不通过"都没有，一条都没被阻断。
#   这一条把"展示"补成"判定"。
#
# 判据（三步，都机械）：
#   ① 从《核心指标一览表》每行取「年份」列（4 位年份）；
#   ② 与**研究时点**比：落后 ≥ `_STALE_YEARS` 年 → 该行数据陈旧；
#   ③ 该行是否**进了决策区**（执行摘要 / 决策清单 / 关键假设 三节内出现同名指标或其数值）
#      → 进了：`high`（够得到闸门）；没进：`warn`（只提示）。
#   **取不到研究时点或年份 → 整条跳过（不猜）**。
_STALE_YEARS = 2
_DECISION_ZONE_HEADS = ("执行摘要", "决策清单", "关键假设", "最小验证计划", "行动清单")
_YEAR_CELL_RE = re.compile(r"\b(20\d{2})\b")
_STUDY_TS_RE = re.compile(r"(?:研究时点|生成时点|判断时点|数据截至)[^\d]{0,6}(20\d{2})")


def _decision_zone(text: str) -> str:
    """报告**决策区**的文本（执行摘要 / 决策清单 / 关键假设 / 验证计划 / 行动清单）。

    取不到任何一节 → 返回空串，调用方据此只给 `warn`。**不拿全文冒充决策区**——
    那会把"这个数出现在文献综述里"也判成"进了决策"。
    """
    out: list[str] = []
    lines = (text or "").splitlines()
    grab = False
    for ln in lines:
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", ln)
        if m:
            grab = any(h in m.group(2) for h in _DECISION_ZONE_HEADS)
            continue
        if grab:
            out.append(ln)
    return "\n".join(out)


def _check_stale_metric_period(text: str) -> list[dict[str, Any]]:
    """R15：核心指标表的数据期落后研究时点 ≥2 年，且该指标已进决策区 → 报。"""
    try:
        md = text or ""
        _ts = _STUDY_TS_RE.search(md)
        if not _ts:
            return []                      # 取不到研究时点 → 不猜
        _now = int(_ts.group(1))
        _zone = _decision_zone(md)
        out: list[dict[str, Any]] = []
        for m in re.finditer(
            r"^##\s*📊\s*核心指标一览表\s*$(.*?)(?=^##\s|\Z)", md, re.M | re.S
        ):
            for ln in m.group(1).splitlines():
                s = ln.strip()
                if not s.startswith("|"):
                    continue
                cells = [c.strip() for c in s.strip().strip("|").split("|")]
                if len(cells) < 3:
                    continue
                name = cells[0]
                if not name or set(name) <= set(":- ") or "指标" in name:
                    continue
                _ys = _YEAR_CELL_RE.findall(" ".join(cells[1:]))
                if not _ys:
                    continue               # 没写年份 → 不猜
                _y = min(int(y) for y in _ys)
                if _now - _y < _STALE_YEARS:
                    continue
                # 该行是否进了决策区：同名指标，或它的数值出现在决策区
                _vals = [v for v in _NUM_RE.findall(" ".join(cells[1:2]))]
                _hit = (name[:8] in _zone) or any(
                    v.replace(",", "") in _zone.replace(",", "") for v in _vals if len(v) >= 3
                )
                out.append(
                    {
                        "kind": "stale_metric_in_decision",
                        "rule": "R15",
                        "level": "high" if _hit else "warn",
                        "title": "决策区用了**研究时点之前 ≥2 年**的数据",
                        "detail": (
                            f"《核心指标一览表》这一行的数据期是 **{_y} 年**，"
                            f"而本报告的研究时点是 **{_now} 年**——**落后 {_now - _y} 年**。"
                            + (
                                "**而它已经进了决策区**（执行摘要/决策清单/关键假设里出现了这个指标）。"
                                if _hit else
                                "（本次未在决策区检出该指标，故只提示。）"
                            )
                            + f"指标：「{name}」。"
                        ),
                        "advice": (
                            "要么把该指标更新到研究时点附近的口径，要么**在决策区就地写明它的数据期**"
                            "（「该数为 {y} 年口径，{now} 年可能已变」）。"
                            "**报告用旧数字下现在的结论，是这一层要拦的那件事。**"
                        ).replace("{y}", str(_y)).replace("{now}", str(_now)),
                        "snippet": f"{name[:20]}｜{_y}年（研究时点{_now}年）",
                    }
                )
        return out
    except Exception:
        return []


# ============================================================
# 自一致性层 · 七对必须彼此对得上的东西
# ============================================================
# 2026-09-18 外部审计的战略结论（Claude《建议》）：本项目的缺陷**不是无限的**，
# 它只有 **7 类**，而且四跑已经穷举完：
#
#   ① 来源位 ↔ 可核来源        五跑「来源:…调研数据」→ 七跑 Euromonitor → 八跑正文欧睿国际
#   ② 同实体多值              七跑 SAM 928/879、TAM 4655/5371；八跑回本周期 5 值
#   ③ 数据期 ↔ 使用位置        五跑竞争画面停 2024Q1；八跑 IMF 数据期 2022 锚 2030
#   ④ 决策输入 ↔ 同题口径      八跑止损线 92 < BEP 205
#   ⑤ 判据 ↔ 它自己打印的列     七跑双源矩阵打印"企业侧"、判据词表里没有这个值
#   ⑥ 值 ↔ 它的文本来源        六跑 326 杯 / 七跑 280 杯 / 八跑客单价"1.0"来自"每上涨 1 元"
#   ⑦ 定义 ↔ 定义              八跑 L1–L5 两套互不相容的定义
#
# **这张表本身就是这个模块存在的意义**：它把"还能不能更好"从一个无限问题
# 变成一个可判定问题——**新缺陷只许掉进这 7 类里**，掉不进去的才叫新问题。
# 每一条都有检查落地（见右列），所以它可回归、可验收。
#
# ⚠️ 一张表**必须与实现同源**：右列的检查项名字若对不上代码，这张表就退化成宣传。
# 有一条 `_fulltest` 锁着它（谁删了检查、这张表就红）。
SELF_CONSISTENCY_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("① 来源位 ↔ 可核来源", "报告点名的来源必须在本次真的取回的清单里", "R12 / _annotate_ungrounded_metric_sources"),
    ("② 同实体多值", "同一个层级/指标的名字在全篇只能指一个数", "R1 / R10 / check_sizing_nesting"),
    # ③ 2026-09-18 第十跑：**从「只展示」升级为「判定」**。
    #    此前只有《数据护照》列 17 行数据期，一个通过/不通过都没有、一条都没被阻断——
    #    而「星巴克门店占比 11.2%（2024）」这种数直接进了决策清单。
    #    R15 补的正是这一刀：数据期落后研究时点 ≥2 年、且该指标进了决策区 → 报。
    ("③ 数据期 ↔ 使用位置", "来源的数据期必须撑得起它被用来锚的那个时点", "metric_freshness / 数据护照 / R15（判定）"),
    ("④ 决策输入 ↔ 同题口径", "止损/关停/盈利阈值必须 ≥ 本报告自己算的红线", "R13"),
    ("⑤ 判据 ↔ 打印列", "判定必须消费它自己打印出来的那一列", "pair_matrix.pair_ok"),
    ("⑥ 值 ↔ 文本来源", "取值不许把增量/反推/截断当成观测值", "R1 边际量守卫 / _body_only"),
    ("⑦ 定义 ↔ 定义", "同一个概念在报告里只许有一套定义", "L1–L5 同源锁（_fulltest 101）"),
    # ⑧ 2026-09-18 第十跑新增：外部审计（GPT P0-6）与我方独立复核命中**同一行**——
    #    「星巴克三线及以下城市门店占比 11.2%」而备注自陈是"星巴克+瑞幸合计"。
    #    **数字正确、指标错误**：来源核验全绿，R7/R12 结构上拦不住。
    ("⑧ 指标名 ↔ 口径自陈", "指标名断言的口径，必须与同一行备注自陈的口径一致", "R14"),
)


def describe_self_consistency() -> str:
    """自一致性层的一句话说明（供材料与说明引用，**与实现同源**）。"""
    return (
        f"自一致性层：报告里有 **{len(SELF_CONSISTENCY_PAIRS)} 对必须彼此对得上**的东西"
        "（来源位↔可核来源、同实体多值、数据期↔使用位置、决策输入↔同题口径、"
        "判据↔打印列、值↔文本来源、定义↔定义、指标名↔口径自陈）。"
        "它们**全部是机械判定、零模型成本**，且每一条都登记了它在代码里对应的那个检查。"
        "（**「有检查在管」≠「本跑一定拦到了」**——这一层是登记表，不是触发清单；"
        "每对本跑拦到了什么，看该跑报告各节的检出项。早期表述写的是「每对都有落地」，"
        "2026-09-18 第九跑用同一份报告推翻了它：第④对声明的 R13 那一跑三道门全未命中、返回空。）"
        "**这一层回答的不是「外部证据支不支持我」，而是「我自己这几部分彼此对不对得上」**——"
        "八层审计里此前没有一层在做这件事。"
    )


# ============================================================
# 自测（python core/rule_lint.py 直接跑，不依赖 app）
# ============================================================
if __name__ == "__main__":
    import sys

    SAMPLE = (
        "执行摘要：蜜雪冰城县域单店年营收中位数达42.6万元，投资回收期仅10–12个月。\n"
        "深入：县域门店平均年营收达123.5万元，EBITDA利润率为18.3%。\n"
        "核心表：单店年营收中位数 42.6万元（2024）。另见县域单店营收中位数 82.6万元。\n"
        "据国家统计局2027年数据，县域可支配收入增速6.2%。预计2030年达峰值。\n"
        "（EBITDA 指息税折旧摊销前利润） 回本周期约10.2个月。\n"
        "行业数据显示，2024年中国现磨咖啡市场规模约 4,000 亿元，同比增长 25%。\n"  # 唯一应命中 R4 的匿名句
        "国家统计局数据显示，2024年中国社零总额 48.8 万亿元。（合法句，不应误报）\n"
        "国家统计局综合数据显示，2024年中国网上零售额 15.5 万亿元。（机构归属，不应误报）\n"
        "| 现磨市场规模 | 行业数据显示 4,000亿元 | 待核 |\n"  # 表格行：标记+数值同格也须跳过
        "\n价格敏感型尝鲜客月均可支配收入中位数3,280元，对30元以上的现磨咖啡存在支付阻力，主力产品均价32元相当于其日均收入的97%。\n"  # R5：32/109≈29%≠97%，应命中
        "\n白领客群月均可支配收入中位数5,000元，主流客单价25元相当于其日均收入的15%。\n"  # R5：25/166.7≈15%，合法不误报
        "\n细分洞察：价格敏感型尝鲜客的价敏度指数高达7.8（满分10），替代饮品较多。\n"  # R6：无方法学/样本，应命中
        "\n满意度调研显示，目标客群对现磨咖啡接受度评分8.2（满分10），基于1,000份有效问卷。\n"  # R6：含调研/问卷，不误报
    )
    v = lint_report(SAMPLE, current_year=2026)
    # R4 探针：只能命中那条"行业数据显示…4,000亿元"匿名句；机构归属句与表格行必须零命中
    _r4 = [x for x in v if x.get("kind") == "unauthored_number"]
    assert len(_r4) == 1, f"R4应只命中1条匿名句，实际{len(_r4)}：{_r4}"
    # R5 探针：32元/日均109≈29%≠97% 应命中；25元/日均166.7≈15% 合法句不得误报
    _ar = [x for x in v if x.get("kind") == "arithmetic_conflict"]
    assert len(_ar) == 1, f"R5应只命中1条算术错，实际{len(_ar)}：{_ar}"
    assert _ar and 25 <= _ar[0].get("actual", 0) <= 35, f"32元÷(3280/30)≈29.3%，actual={_ar}"
    # R6 探针：7.8（满分10）无方法学 应命中；8.2（满分10）含调研/问卷 不误报
    _pm = [x for x in v if x.get("kind") == "pseudo_metric"]
    assert len(_pm) == 1 and _pm[0].get("score") == "7.8", f"R6应只命中7.8那条伪量化，实际{len(_pm)}：{_pm}"
    # —— 复核回归探针（防误报/漏报）——
    # R5: 同段多群组收入 → 取最近前序基数，不误报
    _ar2 = _check_arithmetic(
        "大学生月均可支配收入中位数1,500元，白领月均可支配收入中位数5,000元，"
        "白领客群主流客单价25元相当于其日均收入的15%。"
    )
    assert not _ar2, f"R5 多群组最近基数不应误报: {_ar2}"
    # R5: 百分比在摘要、基数在正文 → 全文档唯一基数回退应命中
    _ar3 = _check_arithmetic(
        "执行摘要：主力产品均价32元相当于其日均收入的97%。\n\n正文：月均可支配收入中位数3,280元。"
    )
    assert _ar3 and 25 <= _ar3[0].get("actual", 0) <= 35, f"R5 跨段单基数回退应命中≈29%: {_ar3}"
    # R6: 表格行与 /100 /20 不误报；口语 "X/10分" 应命中
    _pm2 = _check_pseudo_metric(
        "| 满意指数 | 8.5/10 | 待核 |\n"
        "自评总分 94/100（优秀），技术维度 20/20。\n"
        "内部评分7.5/10分，未披露方法。\n"
    )
    assert len(_pm2) == 1 and _pm2[0].get("score") == "7.5", f"R6 口语/10分应命中、表格/100/20不误报: {_pm2}"
    # R6: CRLF 段落不垮（相邻段"问卷/调研"不得抑制本段伪量化）
    _pm3 = _check_pseudo_metric(
        "第一段发放问卷调研。\r\n\r\n第二段指数得分6.2/10，未给方法学。\r\n"
    )
    assert len(_pm3) == 1 and _pm3[0].get("score") == "6.2", f"R6 CRLF 不垮段应命中第二段: {_pm3}"
    print(format_rule_lint_md(v))
    sys.exit(0)
