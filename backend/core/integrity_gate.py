"""研策 · 决策完整性闸门（Decision Integrity Gate）

## 这一层补的是什么

在本模块之前，整套核验体系已经能做到「**发现问题**」：
规则层 R1–R7、RedTeam、多角色辩论、引用核验、蕴涵核验、遗漏层、来源资格、算术核验——
它们各自把问题找出来、写进审计附录、就地标红，然后……**报告照原样交付**。

2026-09-15 的深入档实跑把这件事摆到了台面上：同一份报告里，
`68.4%`（与自身 UE 模型算出的 44% 直接矛盾）、`14.2 万亿`（量级明显错误）、
`92/126/78 杯`（同一指标三个值）**全部被自家机制点名过**，
却又**全部原样留在执行摘要、核心指标表和决策依据里**参与叙事。

GPT 那份评审给这个状态起了个准确的名字：

    现在是 Audit-aware Research Agent（我会把问题告诉你）
    还不是 Decision-safe Research Agent（问题足以影响决策时，我不让它进结论）

差的正是这一层。**它不新增检测能力，它给已有检测接上一个动作。**

## 三级判定

    PASS   决策区没有被高风险项污染
    WARN   有问题，但都不落在决策区（写在附录里就够）
    BLOCK  有高风险项落进了决策区 → 必须改变交付行为

## BLOCK 到底改变什么（这是本模块存在的全部意义）

只做两件事，但都是**真动作**：

1. **降级判定**：`Go` → `Validate`。**只降不升**——`Validate`/`No-Go` 不动。
   理由：判定是整份报告唯一被拿去行动的那句话。
   一个建在自相矛盾数字上的 `Go`，是这份产品能造成的最大伤害。
   （反过来永不成立：闸门没有资格把 `Validate` 抬成 `Go`——
   那是"证据够了"的判断，属分析师的职责，不属闸门。）
2. **置顶横幅 + 就地说明**：读者第一屏就要看到"这次交付被闸门拦过、拦的是什么"。
   不写在附录深处——**附录是给想看的人看的，横幅是给必须知道的人看的。**

## 纪律

- **纯确定性、零模型成本**：输入是各层已经产出的结构化发现，本模块只做归属判定与合并。
- **异常绝不阻断交付**：任何失败退回 `PASS` 并留日志。闸门是加在交付前的动作，
  它自己不能成为新的单点故障。
- **判定为 `BLOCK` 不等于"报告不能交"**：它等于"这份报告不许以 `Go` 的口吻交"。
  报告照常交付，只是**从"建议进入"变成"建议先验证"**，并把原因写在第一屏。
- **唯一的例外，且必须写在这里**（2026-09-17 加）：第 ⑥ 层消费的是 **LLM 语义复核**
  产出的发现，是全模块唯一一个**模型来源**的输入。它被允许进来，是因为不接它就没
  任何一层能管住"这条断言根本没有来源"。但**模型不因此获得阻断权**——每一条都要
  先过一个机械条件（该发现里带单位的数逐字出现在决策区），闸门只核对那个条件，
  不核对模型的语义判断。**换模型或改复核提示词后，这一层的表现必须重新测。**
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 决策区：这几节里的数字会被拿去行动。判定归属用**文本包含**而不是精确匹配——
# 发现项里的 `number` 常常只是原文片段（`68.4%`），而正文里写作 `高达68.4%`。
_DECISION_MARKERS = (
    "## 📌 执行摘要",
    "# 📋 决策清单",
    "## 📊 核心指标一览表",
    "## 决策判定",
    "## 风险与对策",
    "## 成本测算",
    "## 单位经济",
    "## 市场规模测算",
    # ⚠️ 以下三节是 2026-09-16 补的——**它们都是"会被拿去行动"的节**，
    # 却不在决策区里，于是落在其中的发现只判 WARN、够不到 BLOCK：
    #   · 「行动清单（怎么做）」——清单里的数字直接进执行；
    #   · 「关键假设与可推翻变量」——它定义的就是"什么情况下这个结论不成立"；
    #   · 「最小验证计划（14天）」——投多少钱、盯哪几个指标。
    # 病案：闸门自己抓到过一条「R7 来源资格错配」，那条错误的来源标注**正写在行动清单里**，
    # 却因为这一节不在表内，只被标了警告。**闸门的覆盖范围由这张表决定**，
    # 表漏一节，就等于那一节里的问题永远够不到阻断。
    "## 行动清单",
    "## 关键假设与可推翻变量",
    "## 最小验证计划",
    # ⚠️ 2026-09-17 再补两节，理由同上——**闸门的覆盖范围由这张表决定**：
    #   · 「## 目标市场」——它里面就是**表2：核心目标市场规模容量测算**，
    #     病案：那一行印着 `9.3亿 × 18.5% × 42.0% × 20.5 × 4.8 = 758.4 亿元`
    #     （实算 71.1 亿，差 10.7 倍），R9 抓到了，却因为这一节不在表内只判 WARN。
    #   · 「## 进入壁垒」——行动清单里的门槛数字直接引它。
    "## 目标市场",
    "## 进入壁垒",
)

# 判定等级的可降级顺序。**只允许向下**：Go 是唯一能被闸门动的一档。
_VERDICT_ORDER = {"Go": 0, "Validate": 1, "No-Go": 2}


def _decision_zone(md: str) -> str:
    """决策区文本（各标记节起、到下一个同级标题止的并集）。

    取并集而不是"首章之前"：单位经济与市场规模测算都在正文深处，
    但它们和摘要一样直接进决策——按物理位置切会把它们漏掉。
    """
    text = md or ""
    if not text:
        return ""
    out: list[str] = []
    lines = text.split("\n")
    grab = False
    for ln in lines:
        if ln.startswith("#"):
            if any(ln.startswith(m) or m in ln for m in _DECISION_MARKERS):
                grab = True
            else:
                # ⚠️ **只有 `#`/`##` 级标题才结束决策区，`###` 及更深的不结束**
                # （2026-09-17 实跑病案，本轮最根性的一处）。
                #
                # 原实现是**见到任何 `#` 开头就重判**，于是 `###` 子标题会把刚打开的区域关掉：
                #     行 N   : ## 市场规模测算        ← 标记命中，grab = True
                #     行 N+1 : ### TAM：潜在总市场规模测算  ← 不含 "## 市场规模测算"，grab = False
                #   → **整个 TAM/SAM/SOM 段根本不在决策区里**
                # 后果：那一段里的高风险数字（如 TAM 164,970 亿元）**永远够不到阻断**，
                # 闸门只会判 WARN，而它自己还不知道自己漏了。
                # 报告结构里 `## 市场规模测算` 底下**第一个子标题就是 `###`**——
                # 也就是说**这一节从打开的那一刻起就是空的**。
                #
                # 判据改成**标题层级**：决策区是 `##` 级的节，`###` 是它的内容。
                # 只有同级或更高级（`#`/`##`）的非标记标题才是真的进入了另一节。
                _lvl = len(ln) - len(ln.lstrip("#"))
                if _lvl <= 2:
                    grab = False
        if grab:
            out.append(ln)
    return "\n".join(out)


def _norm_ws(s: str) -> str:
    """去掉**所有空白**（含全角空格、不换行空格）。"""
    # 竖线也要去——同一天踩出的**第三种变体**：`164,970亿元` 在报告表格里写作
    # `| 164,970 | 亿元 |`，数值与单位分居两格，裸比不中。而表格恰恰是决策区里
    # 数字最集中的地方（核心指标表 / TAM 表 / 成本表全是表格）。去掉竖线等价于
    # "把相邻单元格接起来读"——对一个数字 token 来说这正是正确语义。
    return re.sub(r"[\s　 |]+", "", s or "")


def _hits_in_zone(zone: str, token: str) -> bool:
    """token 是否落在决策区。token 为空 → 不算命中（宁漏不误）。

    三件事，前两件是实测踩出来的：

    ① **比较必须去空白**（2026-09-17 实跑病案）。原实现是裸 `token in zone`——
       判定结果取决于中间有没有一个空格：RedTeam 产出 `45-65 万元`（有空格）→ False，
       正文实际写作 `45-65万元`（无空格）→ True。
       本仓库在冲突扫描上修过同一个病（"主干归一没去空格 → 判据退化成逐字相同"）。

    ② **复合项必须拆开逐组件比**（同日第二例，比亚迪跑）。冲突类条目的 token 是
       `28.3% vs 12.7%` 这种**复合串**——**整串去比永远不命中**，
       于是"逐组件查 zone"这一步被整个跳过。而该冲突的**一侧 `12.7%` 就写在执行摘要里**：
       **决策区实际已被争议数字污染，却只判了 WARN。**
       同理 `527万辆 vs 327.6万辆`。
       拆分只在**明确的分隔符**上做（` vs ` / `、`），**不在 `/` 上拆**——
       `0.8 家/10万人` 是一个数，按 `/` 拆会把它拆成两个无意义的片段。
    """
    t = _norm_ws(token)
    if not t or not zone:
        return False
    if t in zone:
        return True
    _z = _norm_ws(zone)
    if t in _z:
        return True
    # ② 复合项：拆出组件逐个体检（任一侧落进决策区，就算这个争议进了决策区）
    for _part in re.split(r"\s*vs\s*|、", str(token or "")):
        _p = _norm_ws(_part)
        if len(_p) >= 2 and (_p in zone or _p in _z):
            return True
    return False


_DISTINCTIVE_NUM = re.compile(
    r"\d[\d,]*(?:\.\d+)?(?:\s*[-–—~]\s*\d[\d,]*(?:\.\d+)?)?\s*(?:[%％]|万元|亿元|万亿|元|万人|家)"
)
"""有辨识度的数值 token：**必须带单位**。

为什么必须带单位：不带单位的裸数字在报告里满地都是（`2024`、`100`、页码、章节号），
拿它去决策区里找必然命中——那样这一层就会退化成"凡是 LLM 报了问题就阻断"，
而**模型产出的发现不该有这种权力**。带上单位（百分数/金额/数量）之后，
它在决策区里出现就几乎只可能是同一个数。
"""


def _distinctive_numbers(s: str) -> list[str]:
    """抽取有辨识度的数值 token（去空白、全角百分号归一）。"""
    out: list[str] = []
    for m in _DISTINCTIVE_NUM.finditer(s or ""):
        t = _norm_ws(m.group(0)).replace("％", "%")
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out


# TAM/SAM/SOM 的取值抽取：标签后 40 字内第一个"数字+亿/万亿"。# 刻意**只认这三个标签**——报告里 `亿元` 满地都是，放宽就会抽到隔壁指标上去。
_TIER_LABEL = {
    "tam": r"TAM",
    "sam": r"SAM",
    "som": r"SOM",
}
_TIER_NUM = __import__("re").compile(r"([\d,]+(?:\.\d+)?)\s*(万亿|亿)(?![人户家个名次台辆杯吨亩平])")
# ⚠️ 末尾那个**否定断言是必需的**（2026-09-16 修）。
# 病案：原文写 `([\d,]+(?:\.\d+)?)\s*(万亿|亿)`，于是「人口基数 **9.3 亿人**」里的
# `9.3 亿` 被当成了一个市场规模取值。后果不是"少抓一次"，是**凭空造出一次 BLOCK**：
# 人口 9.3 亿 > 市场规模几十亿？比例关系被打乱 → 判定"三层不嵌套" →
# 闸门置顶横幅指控**我们自己的测算引擎算错了**（那正是它对外的卖点）。
# **假阳性比漏报更伤**：它让一个本来"从不误报"的闸门变得不可信。
# 单位后面跟的是人/户/家/个…这类计件量词时，这个数不是钱，不是市场规模。

# **"情景/区间"框定词**（2026-09-18，本规则上线当天实测逼出来的）：
# 第一版判据是"同一个层级出现 ≥2 个不同值就是缺陷"，跑全部 24 份历史报告——
# **24/24 全命中**。逐份读原文才发现：命中的全是**合法且专业**的写法：
#   「（SOM）介于 33 亿元至 80 亿元之间，中性预期约为 54 亿元」
#   「保守情景下 SOM 可能降至 33 亿元」「乐观情景下…推动 SOM 达到 80 亿元」
# ——**情景分析正是本产品对外主张的方法论之一**，把它判成"报告自相矛盾"
# 等于用自家卖点攻击自家产品（本仓库在"重复动作检测"上吃过同型的亏）。
# 所以判据收紧为：**只有 ≥2 个【未被情景框定】的取值**才算缺陷。
# 判据只认字面，不做语义推断——**宁可漏报，不可常态误报**。
_FRAME_RE = __import__("re").compile(
    r"保守|中性|乐观|悲观|情景|情形|区间|介于|上限|下限|范围|敏感|压力测试|"
    r"基准情景|目标值|力争|约在.*至|至\s*\d|~\s*\d|—\s*\d|"
    # 第二组（同日第二轮补）：**敏感性分析的写法**。补之前 24 份里仍有 10 份命中，
    # 逐份读原文发现全是这类句子：
    #   「若 SAM 渗透率从 30% 下调至 24%，SAM 将缩减至约 602 亿元」
    #   「若实际调研显示…频次低于15杯，则 TAM 缩水 50% 以上」
    # ——假设推演是**本产品对外主张的方法论之一**，同样不许判成"报告自相矛盾"。
    r"若|如果|假设|假定|下调|上调|缩减|压缩|降至|升至|下降至|上升至|波动|"
    r"缩水|腰斩|翻番|变动|情形下|情形中|"
    # 第三组（2026-09-18 第三轮，来自外部审计 B8 的**误报**）：
    # 病案：闸门把 `TAM=[4655, 5000]` 判成"同一个层级两个数"，而 5,000 是正文
    # 「**2030 年前后有望突破 5,000 亿元**」的**远期值**——那**不是第二个当期 TAM**，
    # 是同一份报告合法的"当期 + 远期"写法。**这条是误报**，而同一分支对 SAM 的判定是真报，
    # 一真一假说明判据缺了"远期/年份"这一类排除。
    r"有望|预计|预测|远期|未来|规划|将达|突破|超过|超越|"
    r"到\s*20\d\d|20\d\d\s*年前后|20\d\d\s*年将|20\d\d\s*年达"
)
# **只报"彼此接近"的取值**（同日第三轮）：
# 差值在 35% 以内的两个未框定取值，几乎只可能是**同一个数被写成了两遍**（真·自相矛盾）；
# 而相差数倍的（87 vs 1088、60 vs 753、937 vs 4218）几乎必然是不同区域/不同年份/不同品类口径，
# 那些是**正当的**，报了就是把专业写法判成缺陷。这条把"跨口径多值"整体排除在外。
_TIER_DUP_TOL = 0.35


def _tier_scan(report: str) -> dict[str, list[tuple[float, bool]]]:
    """→ {tier: [(值, 是否被"情景/区间"框定)]}。**只扫正文**（见 `_tier_values`）。"""
    import re as _re

    # **只扫正文**（与 R1/遗漏层/指标台账同一把刀）：审计附录里有引擎自己讨论
    # TAM/SAM/SOM 的段落，把它算进来就是"引擎自己的产物被算进它审计的集合"。
    _text = report or ""
    try:
        from core.report_html import split_audit_tail

        _b, _t = split_audit_tail(_text)
        if _b and len(_b) > 200:
            _text = _b
    except Exception:
        pass

    out: dict[str, list[tuple[float, bool]]] = {}
    for key, lab in _TIER_LABEL.items():
        vals: list[tuple[float, bool]] = []
        for m in _re.finditer(lab, _text):
            if _re.match(r"\s*/", _text[m.end():m.end() + 2]):
                continue
            if m.start() > 0 and _text[m.start() - 1] == "/":
                continue
            seg = _text[m.end():m.end() + 40]
            # **窗口不许跨句**（2026-09-16）。40 字符足以越过一个句号进入下一句——
            # 「TAM…。潜在人口基数 9.3 亿人」这种写法里，后半句的人口会被读成 TAM。
            # 取值只在**同一句内**找，跨句一律不认（宁可少抓，不可错抓）。
            for _sep in ("\n", "。", "；", ";", "！", "？", "|"):
                _i = seg.find(_sep)
                if _i != -1:
                    seg = seg[:_i]
            nm = _TIER_NUM.search(seg)
            if not nm:
                continue
            v = float(nm.group(1).replace(",", ""))
            if nm.group(2) == "万亿":
                v *= 10000
            # **是否被"情景/区间"框定**：见 `check_sizing_nesting` 的病案——
            # 一份报告合法地给出「保守 33 / 中性 54 / 乐观 80」是**专业写法**，
            # 把三值当成"自相矛盾"就是把行业标准做法判成缺陷。
            _w = _text[max(0, m.start() - 30):m.end() + 40]
            vals.append((v, bool(_FRAME_RE.search(_w))))
        if vals:
            out[key] = vals
    return out


def _tier_values(report: str) -> dict[str, list[float]]:
    """从交付文本里抽 TAM/SAM/SOM 的**全部**取值（单位统一折算为亿元，去重升序）。

    为什么要做这一层文本复核，而不是只信引擎：引擎的越界标记
    （`agents.researchers._assert_tier_order`）藏在多层调用之下，要一路 plumb 到交付节点；
    而**这条性质本来就写在交付物里**——直接验文本既零依赖、又恰好验的是"读者会看到的那个数"。
    两层互补：引擎侧防"算错"，文本侧防"算错了还印出来"。

    ⚠️ **取全部、不取首个**（2026-09-18 修，本轮最重的一条）。
    病案：这份报告一边写「中国咖啡行业 TAM 约为 5,371 亿元，SAM 约为 879 亿元」，
    另一边在市场规模测算节写「下沉市场咖啡服务的可服务市场规模（SAM）约为 **928 亿元**」，
    而核心指标一览表锚的是 879。**同一个 SAM 两个值**，只抽首个的实现
    只看到 879 → `879 < 5371` → 判定"三层嵌套成立" → 闸门放行。
    **报告在自己的决策区里自相矛盾，而闸门报告"没问题"。**
    这与 v0.9.81 那条同根：判定只看了一个代表值，而缺陷在"有两个"。

    跳过**斜杠连写**（`TAM/SAM/SOM` 三合一写法）：那里紧跟的是另一个标签，不是数值。
    """
    return {k: sorted({v for v, _f in vs}) for k, vs in _tier_scan(report).items()}


def check_sizing_nesting(report: str) -> str:
    """TAM/SAM/SOM 三层必须严格递减。返回违规描述（空串=没问题/抽不到）。

    病案（2026-09-15 深访实跑）：SAM 算成 904 亿，而正确值 651 亿——
    **944 < 4687 所以"递减"成立**，任何只查"SAM 有没有大于 TAM"的规则都放行。
    真正出问题的是**基数**（SAM 用了全部城镇人口 9.3 亿，TAM 用了潜在人群 6.696 亿），
    而基数是中间量、不印在报告里，只能由引擎侧保证。
    所以这里明确定位为**第二道**：它抓的是"三层明显不嵌套"这一档，
    引擎侧（`_assert_tier_order`）才是治本的那道。两层都在，才叫"可被复核"。
    """
    v = _tier_values(report)
    if not v:
        return ""
    _names = {"tam": "TAM", "sam": "SAM", "som": "SOM"}

    # —— ① 同层两个值：**未被情景框定的值**才算 ——
    # 病案（2026-09-18）：同一份报告的正文里 SAM 既是 879 亿（市场规模测算节 + 核心指标表）
    # 又是 928 亿（同一节的公式 `5.5亿人 × 15% × 75杯/年 × 15元/杯 ≈ 928亿元`）。
    # 两者只差 5.3%——**任何按"跨度阈值"设的规则都会放行**，而它是硬错：
    # 读者在决策区看到两个 SAM，无法判断该信哪个；下游的 SOM=6%×SAM 也随之下不来。
    #
    # ⚠️ 判据**必须排除情景/区间写法**：第一版不排除，24 份历史报告 **24/24 全命中**，
    # 而命中的全是「保守 33 / 中性 54 / 乐观 80」这类**专业写法**。
    # 现在只看**没被框定**的取值：一份报告里出现 ≥2 个"就这么一个数"的取值才是缺陷。
    _scan = _tier_scan(report)
    _dup: dict[str, list[float]] = {}
    for k, vs in _scan.items():
        _bare = sorted({x for x, _f in vs if not _f})
        if len(_bare) < 2:
            continue
        # 只报"彼此接近"的：见 `_TIER_DUP_TOL`。
        if (_bare[-1] / _bare[0] - 1.0) > _TIER_DUP_TOL:
            continue
        _dup[k] = _bare
    if _dup:
        _d = "；".join(
            f"{_names[k]}={'／'.join(f'{x:,.0f}' for x in s)} 亿元" for k, s in sorted(_dup.items())
        )
        return (
            f"报告对**同一个市场层级给出了多个取值**：{_d}。"
            "市场规模是一个数——同一个标签在一份报告里指两个数，"
            "读者（以及下游按份额推算的 SOM）无法判断该信哪个。"
            "这不是口径分歧（那是正当的、可并列的），也不是情景分析"
            "（带『保守/中性/乐观/区间』的写法已排除在外），而是**报告对自己矛盾**："
            "请统一到一个值，或把另一个明确改写成不同区域/年份/品类口径并说明差异。"
            f"（全篇抽到的取值：{'；'.join(f'{_names[k]}={s}' for k, s in sorted(v.items()))}）"
        )

    # —— ② 三层嵌套（原有判据，仅在每层唯一时适用）——
    if len(v) < 3:
        return ""
    _s = {k: s[0] for k, s in v.items()}
    if _s["sam"] > _s["tam"] or _s["som"] > _s["sam"]:
        return (
            f"报告印出的三层不满足 SAM⊆TAM：TAM={_s['tam']:,.0f} 亿 → "
            f"SAM={_s['sam']:,.0f} 亿 → SOM={_s['som']:,.0f} 亿"
        )
    return ""


def evaluate(
    *,
    report: str = "",
    rule_findings: list[dict[str, Any]] | None = None,
    adv_issues: list[dict[str, Any]] | None = None,
    omission: dict[str, Any] | None = None,
    quality_report: dict[str, Any] | None = None,
    sizing_violation: str = "",
    verdict: str = "",
) -> dict[str, Any]:
    """→ {level, reasons, downgraded_from, downgraded_to, blockers}

    level: PASS / WARN / BLOCK
    reasons: 人类可读的阻断理由（每条带"哪一层发现的"）
    """
    try:
        zone = _decision_zone(report)
        blockers: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []

        # —— ① 市场规模层：层级越界（SAM⊄TAM）或**同层多值**（同一个标签两个数）——
        # **两个来源必须分开报**（2026-09-18 修回来）：`sizing_violation` 是**引擎自报**
        # （我们自己算出来的事实）；`check_sizing_nesting` 是**文本复核**（从交付物里读出来的）。
        # 前者归"测算引擎"，后者归"市场规模层（文本复核）"——把两者混成一层，
        # 就等于抹掉了"是我们算错了，还是我们写错了"这个区别（`_fulltest` 第 41 项锁着这条）。
        _nest = sizing_violation or check_sizing_nesting(report)
        if _nest:
            # **两种发现的 why 必须与判定同源**：原先写死"三层不满足 SAM⊆TAM"，
            # 而本函数现在还报"同层两个值"——用前者的诊断语描述后者，
            # 就是本仓库反复出现的"审计层的话与判定不同源"。
            _is_dup = "同一个市场层级" in _nest
            if _is_dup:
                _layer = "市场规模层（文本复核）"
                _why = (
                    "报告对**同一个市场层级**（TAM/SAM/SOM）给出了两个数。"
                    "它不是口径分歧（那是正当的、可并列的），也不是情景分析"
                    "（带『保守/中性/乐观/区间/敏感性』的写法已在判据里排除），"
                    "而是**报告对自己矛盾**——而下游的 SOM 正是按 SAM 的百分比推算的，"
                    "基数不定，SOM 就定不下来。"
                )
            elif sizing_violation:
                _layer = "测算引擎"
                _why = (
                    "系统自算的市场规模三层不满足 SAM⊆TAM——这是**我们自己的计算引擎**出的错，"
                    "而『能把机械性错误下沉到确定性代码』正是对外卖的卖点之一。"
                )
            else:
                _layer = "市场规模层（文本复核）"
                _why = (
                    "从**交付物文本**里读出来的三层不满足 SAM⊆TAM。"
                    "引擎侧（`_assert_tier_order`）治本、这里防「算错了还印出来」，两层互补。"
                )
            blockers.append({"layer": _layer, "what": _nest, "why": _why})

        # —— ② 规则层：high 风险项落在决策区（R7 来源资格错配最典型）——
        for f in (rule_findings or []):
            if not isinstance(f, dict):
                continue
            # ⚠️ **`level` 与 `severity` 都要认**（2026-09-17 修，本轮最重的一条断链）。
            # R1–R7 用 `level`，R8/R9 用 `severity`——**同一个概念两个字段名**。
            # 病案：R8（正文自协商取值）与 R9（印出的算式算不出结果）两条规则
            # 从落地那一刻起就是**死的**：它们把 `severity: high` 写得清清楚楚，
            # 而这里读的是 `level` → 永远取到空串 → 永远进不了 blockers →
            # **只有"记录"，没有"消费"**。而本闸门的文档第一句就写着
            # 「规则层 high 风险项落在决策区 → BLOCK」。同一份报告里其余各层
            # （RedTeam/质检/测算）都在正常进出，唯独规则层这个入口是空的。
            # 修法：两个字段名都认。**不改成只认一个**——那会把另一批规则的
            # 静默失效原样保留下来，而下一次加规则的人还会踩同一脚。
            lv = str(f.get("level") or f.get("severity") or "").lower()
            if lv not in ("high", "高"):
                continue
            sn = str(f.get("snippet") or "")
            if _hits_in_zone(zone, sn) or _hits_in_zone(zone, f.get("title") or ""):
                blockers.append({
                    "layer": f"规则层 {f.get('rule', '')}".strip(),
                    "what": str(f.get("title") or "") + "：" + str(f.get("detail") or "")[:80],
                    "why": str(f.get("advice") or "")[:80],
                })
            else:
                warnings.append({"layer": f"规则层 {f.get('rule','')}".strip(),
                                 "what": str(f.get("title") or "")})

        # —— ③ RedTeam：『高』风险数字落在决策区 ——
        for i in (adv_issues or []):
            if not isinstance(i, dict):
                continue
            risk = str(i.get("risk") or "").strip().lower()
            if not (risk.startswith("高") or risk.startswith("high")):
                continue
            num = str(i.get("number") or "")
            if _hits_in_zone(zone, num):
                blockers.append({
                    "layer": "RedTeam",
                    "what": f"「{num}」被独立校验官判为高风险，而它出现在决策区",
                    "why": str(i.get("issue") or "")[:80],
                })
            else:
                warnings.append({"layer": "RedTeam", "what": f"「{num}」高风险（不在决策区）"})

        # —— ④ 质检层：已判 BLOCK ——
        q = quality_report or {}
        if str(q.get("level") or "").upper() == "BLOCK":
            blockers.append({
                "layer": "质检层",
                "what": "；".join(str(x) for x in (q.get("fatal_issues") or [])[:3]) or "质检判定 BLOCK",
                "why": "质检层已经判定不可交付，闸门据此维持阻断。",
            })

        # —— ⑤ 遗漏层：裸断言过多**不阻断**，但记 WARN ——
        # 刻意不设阈值卡 BLOCK：遗漏层是**启发式**（见 core/omission.py 的边界说明），
        # 拿一个启发式读数去阻断交付，等于把"可能误报"升级成"真的不给你报告"。
        _om = (omission or {}).get("omitted")
        if isinstance(_om, int) and _om > 0:
            warnings.append({"layer": "遗漏层", "what": f"正文有 {_om} 处裸断言（无来源、未自陈不确定）"})

        # —— ⑥ LLM 语义复核：『断言无证据支撑 / 数据自相矛盾』落进决策区 ——
        #
        # 这是本闸门**第一次消费模型产出的发现**，所以先把它为什么现在才接进来写清楚：
        # 语义复核是**唯一**能抓住"这句话根本没有来源"的一层——规则层数得清数字、对不齐语义，
        # RedTeam 只审它自己挑出来的关键数字。它检出的东西此前**只把质检等级抬到 FLAG**，
        # 再经附录披露一次，**没有任何通路能影响交付**。
        # 病案（2026-09-17 蜜雪冰城跑）：它检出「超过82%的县域消费者拒绝单价高于15元」
        # **缺乏证据支持**，而同一份报告的核心指标表把这个 82% 挂在「艾瑞咨询 L3 2024」名下、
        # 执行摘要拿它当第一条依据——**一条没有证据的断言，带着一个具体机构的名字，进了决策区**。
        # 那正是本闸门存在的理由，却因为没人接线而不归它管。
        #
        # **前置机械条件**（模型说了不算的保证）：该条发现里必须出现一个有辨识度的数值
        # token（百分数 / 金额 / 数量），且该 token **逐字出现在决策区里**。
        # 为什么不直接比对它的引号原文：模型的引文几乎从不与正文逐字相同——
        # 同一病案里校验官引「超过82%的县域消费者明确拒绝单价高于15元」，
        # 正文写的是「82%的县域消费者拒绝单价高于15元的茶饮」，**逐字匹配必然失配**。
        # 而数字是逐字稳定的，且"决策区里有没有这个数"本来就是本闸门其余五层在用的判据。
        #
        # 代价如实说：这条通路**只能接住带数字的无源断言**；纯文字的无源断言仍只进附录。
        for _iss in [str(x) for x in ((quality_report or {}).get("llm_issues") or [])]:
            _hit = next(
                (t for t in _distinctive_numbers(_iss) if _token_in_line(zone, t)), ""
            )
            if _hit:
                blockers.append({
                    "layer": "LLM语义复核",
                    # 措辞刻意只说**机械核验到的那件事**：这个数逐字出现在决策区。
                    # 不说"它指向的 85% 落在决策区"——决策区里的 85% 可能是**另一个** 85%
                    # （病案：语义复核说的 85% 是蜜雪渗透率目标，决策区里的 85% 是 TAM 渗透率假设）。
                    # 把机械事实说成语义指向，就是在替读者下一个我们没验过的判断。
                    "what": f"独立语义复核判定此处断言缺证据/自相矛盾，且这条发现里的「{_hit}」逐字出现在决策区：{_iss[:70]}",
                    "why": "语义复核此前只能把质检抬到 FLAG，够不到闸门；现按机械条件接入："
                           "该条发现里那个有辨识度的数（百分数/金额）**逐字出现在决策区**。"
                           "（闸门只核对该数在不在决策区，不核对该条发现说的就是那一处。）",
                })
            else:
                warnings.append({"layer": "LLM语义复核", "what": _iss[:60]})

        level = "BLOCK" if blockers else ("WARN" if warnings else "PASS")
        out: dict[str, Any] = {
            "level": level,
            # **`executed` 必须与 level 分开记**（2026-09-16）：`except` 分支返回的也是
            # `level="PASS"`，不看这个位就分不出「查过且干净」与「闸门自己崩了」。
            "executed": True,
            "blockers": blockers,
            "warnings": warnings,
            "reasons": [f"[{b['layer']}] {b['what']}" for b in blockers],
            "zone_chars": len(zone),
        }

        # —— 降级：只降不升 ——
        v = str(verdict or "").strip()
        if level == "BLOCK" and _VERDICT_ORDER.get(v, 1) < _VERDICT_ORDER["Validate"]:
            out["downgraded_from"] = v
            out["downgraded_to"] = "Validate"
        else:
            out["downgraded_from"] = ""
            out["downgraded_to"] = ""
        return out
    except Exception as e:  # 闸门自身绝不做单点故障
        logger.warning("决策完整性闸门执行失败（按 PASS 放行，但**必须标明未执行**）: %s", e)
        # ⚠️ 放行是有意的（闸门不该成为新的单点故障），但**放行必须是看得见的**：
        # `executed=False` 会让 `render_banner` 印出「本次未能执行」的置顶条，
        # 而不是让一份没被核过的报告看起来像一份干净的报告。
        return {"level": "PASS", "executed": False, "blockers": [], "warnings": [], "reasons": [],
                "downgraded_from": "", "downgraded_to": "", "error": str(e)}


def render_banner(gate: dict[str, Any], verdict: str = "") -> str:
    """闸门结论 → 报告**置顶横幅**（BLOCK/WARN 才有；PASS 返回空串）。

    为什么必须置顶：附录是给"想看的人"看的，横幅是给"必须知道的人"看的。
    一份被拦过的报告，如果读者要翻到第 1000 行才知道"这次的 Go 不算数"，
    那这道闸门就只是又一个自证清白的摆设。

    ## 「未执行」必须与「PASS」分开说（2026-09-16）

    本函数原先只看 `level`：`except` 分支返回的是 `level="PASS"`，
    于是**闸门自己崩掉**与**闸门真的查过且干净**印出完全相同的报告——
    零横幅、判定照旧。这与本仓库已经修过三次的同一个病同型：
    「全 skip 印 0.0%」→ 改成返回 `None`；「未取得 ≠ 没变化」；
    「未执行也必须出声」。**一个没跑过的检查，读者必须看得出来它没跑过。**
    """
    if (gate or {}).get("executed") is False:
        return (
            "> ⚠️ **决策完整性闸门：本次未能执行**"
            f"（{str((gate or {}).get('error') or '未知原因')[:120]}）\n"
            ">\n"
            "> **这不等于「本次没有问题」**——是这道闸门这次没跑起来，"
            "判定行**未经闸门核对**。请把下面的结论当作「未经过决策安全闸门」来读。"
        )
    lv = str((gate or {}).get("level") or "PASS")
    if lv == "PASS":
        return ""
    _df, _dt = gate.get("downgraded_from") or "", gate.get("downgraded_to") or ""
    if lv == "BLOCK":
        lines = [
            "> 🛑 **决策完整性闸门：本次交付已被阻断（BLOCK）**",
        ]
        if _df and _dt:
            lines.append(
                f"> 判定已由 **{_df}** 降级为 **{_dt}**——**本次不建议据此直接进入**。"
                "降级不是模型改口，是闸门按确定性规则执行的："
                "下列高风险项落在决策区（执行摘要/核心指标/决策依据/测算）内。"
            )
        else:
            lines.append(
                f"> 本次判定为 **{verdict or '—'}**，未被上调；"
                "但下列高风险项落在决策区，请先处理再据此行动。"
            )
        lines.append(">")
        for b in (gate.get("blockers") or [])[:5]:
            lines.append(f"> - **[{b.get('layer','')}]** {b.get('what','')}")
            if b.get("why"):
                lines.append(f">   - 为什么算阻断：{b['why']}")
        # **截断必须自陈**（本仓库铁律：静默截断读起来像"全覆盖"）。
        # 病案（2026-09-17 元气森林跑）：抬头写「阻断 8 项」，正文只列了 5 行，
        # 中间没有任何提示——**读者数不出来 = 这个数无法被核对**，
        # 而这一节的全部意义就是"拦了几项、拦的是什么"可以被核对。
        _nb = len(gate.get("blockers") or [])
        if _nb > 5:
            lines.append(f"> - …**另有 {_nb - 5} 项未逐条列出**（共 {_nb} 项；完整清单见文末审计附录）")
        lines.append(">")
        lines.append(
            "> **闸门不修改数字本身**——它只拒绝让这些数字以『可以直接行动』的口吻交付。"
            "处理方式见文末审计附录的对应条目。"
        )
        return "\n".join(lines)
    # WARN：只陈述，不改判定
    _n = len(gate.get("warnings") or [])
    return (
        f"> ⚠️ **决策完整性闸门：WARN**（{_n} 项非决策区问题）——"
        "本次判定未被改动。发现问题但不落在决策区的，写在文末审计附录里，"
        "**不进入决策叙事**；具体条目见附录。"
    )


_DASH_NORM = str.maketrans({"-": "-", "–": "-", "—": "-", "－": "-", "~": "-", "～": "-"})


def _token_in_line(line: str, token: str) -> bool:
    """token 是否作为**一个独立的数**出现在这一行里（数字边界 + 破折号归一）。

    为什么不复用上面的 `_hits_in_zone`：那个函数是"这行里有没有提到这个数"，
    判据是**裸子串**，用在"要不要把这条争议算进决策区"上够用（宁松勿漏，
    漏判的代价是闸门不出声）。但**往交付物里写一句指控**是另一回事——
    写错一句，读者看到的就是"系统在冤枉一个数字"，而那句话是我们自己加的。
    两处要求的证据强度不同，所以两把尺子，并且各自注明用途。

    病案（2026-09-17 元气森林跑，交付报告里当场可见）：
        闸门把
            「…转向强调『电解质/维生素』等功能成分，以解决 **68.2%** 运动人群…的顾虑」
        标成"本句依赖被阻断的数字「**8.2%**」"——因为 `"8.2%" in "68.2%"` 成立。
        **`8.2%`（气泡水增速）与 `68.2%`（运动人群占比）是两个不同的数**；
        同一跑里还有 `「0.2元」`（来自 `0.15-0.2元`）。
    判据：命中处**左边不能是数字或小数点，右边不能是数字**；破折号先归一再比
    （报告里 `0.15-0.2元` 与 `0.15–0.2元` 两种写法并存）。
    """
    t = _norm_ws(token).translate(_DASH_NORM)
    if not t or not line:
        return False
    _l = _norm_ws(line).translate(_DASH_NORM)
    for _part in re.split(r"\s*vs\s*|、", t) or [t]:
        _p = _part.strip()
        if len(_p) < 2:
            continue
        for m in re.finditer(re.escape(_p), _l):
            _b, _a = _l[m.start() - 1: m.start()], _l[m.end(): m.end() + 1]
            if not _b.isdigit() and _b != "." and not _a.isdigit():
                return True
    return False


def _blocked_tokens(gate: dict[str, Any]) -> list[str]:
    """BLOCK 里那些被点名的数字 token（按长度降序：先匹配更具体的）。"""
    out: list[str] = []
    for b in (gate or {}).get("blockers") or []:
        m = re.findall(r"「([^」]{2,40})」", str(b.get("what") or ""))
        for t in m:
            t = _norm_ws(t)
            if t and t not in out:
                out.append(t)
    out.sort(key=len, reverse=True)
    return out


# 建议/祈使句的判据。**必须同时满足**：句内有建议性动词、且含被阻断的数字。
# 刻意**不**包含"应/需/必须"这类过宽的字——它们会命中"若日均销量 < 200 杯 → No-Go"
# 这种**条件判据**（那是分析，不是建议），而误标一条分析句比漏标一条建议更伤。
_ACTION_VERBS = ("建议", "优先选择", "优先执行", "优先锁定", "务必", "应避免", "应聚焦",
                 "目标设定为", "确保在", "不得低于", "控制在")
# **反向误杀守卫**（2026-09-18，外部审计 Gemini 单独点名，且它就在交付件上）。
#
# 病案：行动清单里写着
#     「建议产品定价锚定 15–20 元，**放弃 25 元以上**高端溢价」
# ——这句话的落点是"**不要碰** 25 元"，而闸门因为句中有 `建议`（动作词）与 `25元`（被阻断数字），
# 就在它下面盖了一行 `⛔ 本句依赖被阻断的数字「25元」，不得直接执行`。
# **系统把一条完全正确的建议当场枪毙了**，而且用的是我们自己写的指控句。
#
# 这与 `_token_in_line` 那条（`8.2% in 68.2%`）是同一族：**往交付物里写一句指控，
# 写错的代价远高于少写一句**。判据：命中处**左邻 16 字**内出现规避义动词 → 这一句
# 是"别碰这个数"，不是"依赖这个数"，**不标**。
_AVOID_CTX = ("放弃", "避免", "规避", "远离", "不要", "不宜", "不应", "不推荐",
              "禁止", "警惕", "防范", "舍弃", "排除", "退出", "不碰", "别碰")


def _avoiding_context(line: str, token: str) -> bool:
    """这一行是在**劝阻**使用该数字，而不是**依赖**它。

    两侧都先 `_norm_ws`（去空白）再定位，保证下标与判断用的是同一个字符串——
    否则 `15–20 元，放弃 25元` 里空格会让左邻窗口错位。
    """
    try:
        l = _norm_ws(line)
        t = _norm_ws(token)
        if not t:
            return False
        i = l.find(t)
        if i < 0:
            return False
        return any(w in l[max(0, i - 16):i] for w in _AVOID_CTX)
    except Exception:
        return False


def _mark_blocked_advice(md: str, gate: dict[str, Any]) -> tuple[str, list[str]]:
    """把**直接依赖被阻断数字的建议句**就地标出来 → (新文本, 被标的行)。

    病案（2026-09-17 蜜雪冰城跑，三份外部评审同时点名）：
        置顶横幅写着「本次交付已被阻断（BLOCK）」，
        同一屏下方的**第一条建议**却是
            「建议优先选择初始投资 8–12 万元（存疑·待核）的轻资产加盟模型……
              以确保在县域低客单环境下实现 <12 个月的投资回收」
        ——而 `8–12万元` 正是这次 BLOCK 的阻断项之一。
    **闸门自己承诺的是「只拒绝让这些数字以『可以直接行动』的口吻交付」，
    而一条「建议优先选择…以确保…」正是整份报告里最可直接行动的那句话。**
    它此前只降了最上层的判定标签，没有碰下层那句建议——
    于是产品对外宣称"拦住了"，读者拿到的却是"照这个预算开店"。
    这与本仓库修过多次的同一个病同型：**上层改了，下层没改，而两层在读者眼里是同一件事。**

    只做**追加**、绝不改写数字：既不替读者判断哪个值才对（我们没有那个依据），
    也不让那句话以祈使句的形态原样站着。表格行(`|`)与已标过的行不碰——前者加了会破表，
    后者是为了幂等。
    """
    text = md or ""
    toks = _blocked_tokens(gate)
    if not text or not toks:
        return text, []
    zone = _decision_zone(text)
    if not zone:
        return text, []
    zlines = set(zone.split("\n"))
    _MARK = "- ⛔ **〔闸门：本句依赖被阻断的数字"
    lines = text.split("\n")
    out_lines: list[str] = []
    marked: list[str] = []
    for i, ln in enumerate(lines):
        # 幂等判据是**下一行**有没有标记（标记是追加在建议句之后的独立一行）——
        # 只查本行含不含标记会漏：本行永远是原来的建议句，第二次调用照样合格、照样再插一条。
        _nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (ln in zlines and not ln.lstrip().startswith(("|", "#", ">"))
                and not _nxt.lstrip().startswith(_MARK)
                and any(v in ln for v in _ACTION_VERBS)):
            hit = next(
                (t for t in toks if _token_in_line(ln, t) and not _avoiding_context(ln, t)), ""
            )
            if hit:
                marked.append(ln.strip()[:110])
                out_lines.append(ln.rstrip())
                out_lines.append(
                    f"  {_MARK}「{hit}」，不得直接执行〕**"
                    "——请先完成文末审计附录里的核验，或改用已核实的数据重写本句。"
                )
                continue
        out_lines.append(ln)
    return "\n".join(out_lines), marked


def apply_to_report(md: str, gate: dict[str, Any], verdict: str = "") -> tuple[str, str]:
    """把闸门结论**写进报告**：(新正文, 新判定)。

    只做三件事，件件都要落在纸上——**闸门不动手，就等于没有闸门**：

    1. **就地标记被阻断的建议句**（2026-09-17 加，见 `_mark_blocked_advice`）：
       横幅说"已阻断"、下面的建议却照旧教人执行——**那是"打码式假阻断"**。
    2. **置顶横幅**：插在 AIGC HTML 注释之后（那是法定标识，必须在最前）、
       正文一切内容之前。读者第一屏就看到，而不是翻到附录才知道。
    3. **降级判定**：把「决策判定」那一节里的加粗判定词改掉，并补一行说明是谁改的。
       只在 `gate["downgraded_to"]` 非空时执行——**只降不升**由 `evaluate` 保证。

    幂等：重复调用不再叠加（横幅查重、建议句查标记、判定词已是目标值则跳过）。
    任何异常返回原文 —— 闸门绝不阻断交付。
    """
    text = md or ""
    if not text:
        return text, verdict
    out = text
    new_verdict = verdict
    try:
        # **先就地标记、再插横幅**：横幅要把"被标了哪几句"一并报出来，
        # 否则读者只看得到"已阻断"，看不到"阻断落在哪句建议上"。
        out, _marked = _mark_blocked_advice(text, gate)
        banner = render_banner(gate, verdict)
        if _marked:
            banner += (
                "\n>\n> **下列可执行建议已就地标记为『不得直接执行』**"
                "（闸门只做追加标注，不改写数字——我们没有依据替你判断哪个值才对）：\n"
                + "\n".join(f"> - {m}" for m in _marked[:5])
            )
        if banner and "决策完整性闸门" not in out[:4000]:
            # 插在首行 HTML 注释之后；没有注释就插在最前
            lines = out.split("\n", 1)
            if lines and lines[0].startswith("<!--"):
                out = lines[0] + "\n\n" + banner + "\n" + (lines[1] if len(lines) > 1 else "")
            else:
                out = banner + "\n\n" + out
        _to = str(gate.get("downgraded_to") or "")
        _frm = str(gate.get("downgraded_from") or "")
        if _to and _frm:
            out2, _done = _rewrite_verdict_line(out, _frm, _to)
            if _done:
                out = out2
            elif (f"判定已由闸门降级为 {_to}" in out) or (f"闸门降级·原判 {_frm}" in out):
                # **幂等**：上一次交付已经写明了这次降级——要么补过显式说明（说明改写失败那次），
                # 要么判定行本身已带上〔闸门降级·原判 Go〕的标记（改写成功那次）。
                # 两种都算"已经做过"，重复调用不再叠一遍。
                # （闸门可能被调用两次：落档一次、前端再取一次。）
                pass
            else:
                # **改不到那一行时不许只管自己说**（2026-09-16）。
                # 病案：`_rewrite_verdict_line` 找不到「决策判定」节里那个加粗判定词就返回
                # `_done=False`，而**横幅早已插进去了**（上面那段）——于是第一屏写着
                # 「判定已由 Go 降级为 Validate」，正文里的判定行却仍是 **Go**，
                # 数据层（`decision_verdict`）也仍是 Go。三处里两处说没降、
                # 而唯一说降了的那处是横幅。**读者会信横幅，然后按 Go 行动。**
                #
                # 修法：判定**照降**（闸门的结论是确定性的，不该因为排版没匹配上就作废），
                # 同时在正文里补一段显式说明，让"正文—横幅—数据层"三处一致。
                logger.warning("闸门降级未能就地改写判定行，改为追加显式降级说明")
                _note = (
                    "\n\n> 🛑 **判定已由闸门降级为 "
                    f"{_to}**（原判 {_frm}）。\n"
                    "> 本节未能就地改写判定词（正文版式与预期不符），"
                    f"**请以 {_to} 为准**——上方判定行中的「{_frm}」已作废。\n"
                )
                _ls = out.split("\n", 1)
                if _ls and _ls[0].startswith("<!--"):
                    out = _ls[0] + _note + (_ls[1] if len(_ls) > 1 else "")
                else:
                    out = _note.lstrip("\n") + out
            # **判定一律以闸门为准**：无论就地改写成没成功，数据层都必须拿到降级后的值。
            # （原先只在 `_done` 为真时才更新 `new_verdict`——那是把"排版匹配成功"
            # 当成了"降级是否成立"的前提。）
            new_verdict = _to
    except Exception as e:
        # **不许原样静默交付**（2026-09-16）：原实现直接 `return text, verdict`，
        # 于是"闸门算出了 BLOCK、但把结论写进正文这一步失败了"的结果是——
        # 读者拿到一份**没有横幅、判定仍是 Go** 的报告，与"闸门放行"完全无法区分。
        # 这与 `evaluate` 的 fail-open 是两件事：那个是"没查"，这个是"查出来了但没写上去"，
        # 后者更危险（我们知道有问题，而读者不知道）。
        # 这里退而求其次：只补一行纯文本披露（不依赖任何可能再次失败的渲染逻辑）。
        logger.warning("闸门结论写进报告失败（改为插入一行纯文本披露）: %s", e)
        try:
            _lv = str((gate or {}).get("level") or "")
            if _lv in ("BLOCK", "WARN") and "决策完整性闸门" not in (text or "")[:4000]:
                _note = (
                    f"> ⚠️ **决策完整性闸门：本次判定为 {_lv}，但结论未能写入正文**"
                    f"（渲染失败：{type(e).__name__}）。请以审计附录中的条目为准。\n\n"
                )
                _ls = text.split("\n", 1)
                if _ls and _ls[0].startswith("<!--"):
                    text = _ls[0] + "\n\n" + _note + (_ls[1] if len(_ls) > 1 else "")
                else:
                    text = _note + text
        except Exception:
            pass
        return text, verdict
    return out, new_verdict


def _rewrite_verdict_line(md: str, frm: str, to: str) -> tuple[str, bool]:
    """在「决策判定」节内把加粗的判定词 `frm` 改成 `to`，并补一行"谁改的"。

    只在**决策判定那一节内**找（到下一个 `## ` 标题为止）：判定词在别的节里
    也可能出现（如"若…则 No-Go"的假设表），全局替换会把那些一起改掉——
    那是**篡改分析内容**，不是闸门该做的事。
    """
    import re as _re

    _i = md.find("## 决策判定")
    if _i < 0:
        return md, False
    _j = md.find("\n## ", _i + 5)
    seg = md[_i:_j if _j > 0 else len(md)]
    # 加粗判定词：`**✅ Go（建议进入）**` / `**🟡 Validate（…）**` / `**Go**`
    pat = _re.compile(r"\*\*([^\n*]{0,8}?)" + _re.escape(frm) + r"([^\n*]{0,40}?)\*\*")
    m = pat.search(seg)
    if not m:
        return md, False
    _note = (
        f"\n\n> 🛑 **本判定由「决策完整性闸门」从 {frm} 降级为 {to}**（确定性规则，非模型改口）："
        "有高风险项落在决策区。详见报告顶部横幅与文末审计附录。"
    )
    _new = f"**{to}（尚不确定，建议小规模验证）**〔闸门降级·原判 {frm}〕"
    seg_new = seg[:m.start()] + _new + _note + seg[m.end():]
    return md[:_i] + seg_new + md[(_j if _j > 0 else len(md)):], True


def describe() -> str:
    return (
        "决策完整性闸门：把各层（规则/RedTeam/质检/测算/遗漏）已产出的发现按"
        "『是否落在决策区』归类，落进去的高风险项触发 BLOCK，并把判定从 Go 降级为 Validate；"
        "只降不升。补的是『发现问题』与『控制问题』之间缺的那一步——"
        "它不新增检测能力，它给已有检测接上一个动作。"
    )
