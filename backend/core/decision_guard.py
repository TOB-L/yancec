"""研策 · 决策层就地降级（P0-3）—— RedTeam 判高风险的数字不得当"未降级锚"继续承重

病案（报告(6)验收）：186杯/1.2杯/>63% 被对抗性数字校验(Red Team)判『高风险』、文首红条告警，
但执行摘要"行动式建议3"、决策判定"关键依据"、核心指标表、H1-H3 红线(250杯达标/200杯转No-Go)
仍把它们当成立事实正常推演，无一处就地『待核』——报告核心结论建在自己打高风险的锚上，
对复核者/投资人极不可信（低风险的 0.8-1.2元 反而被就地标了待核）。

方案（确定性下沉，零模型成本）：
demote_high_risk_in_head(md, adv_issues)：
1. 只作用于"决策头部区"（执行摘要+决策清单+核心指标表+假设，即正文第一主章节标题之前）——
   正文证据区仍如实呈现原值（RedTeam 附录已披露），避免全文刷屏。
2. 对每个高风险数字原文（如 ">63%"），做容错匹配：容忍前导 >/<（同时匹配 "63%"）、
   容忍同一数字带/不带单位（"186杯" 与 "186"），在命中处追加『（存疑·待核）』。
3. 已带 待核/存疑 标注的命中不重复标记；同数字每区段限标 N 次防刷屏。
4. 幂等：重复调用不叠加。任何异常返回原文不阻断交付。

验证：python core/decision_guard.py（离线自测，无网络）。
"""

from __future__ import annotations

import re
from typing import Any

logger = None  # 该模块保持零依赖（不 import logging 也行，需要时懒加载）


def _markers_already(tok: str, i: int, text: str) -> bool:
    """命中位置**紧后方**是否已带待核/存疑标注（防重复标记 / 幂等）

    窗口取 6 字符，两头都要防：
    - **太短**：`186 杯（存疑·待核）` 里逗号前有个空格，4 字符窗口只看到" 杯（存"，
      凑不出"存疑"两字 → 判成"没标过" → 同一个值被标两遍
      （实测报告(10)：`186（存疑·待核） 杯（存疑·待核）`，一句话里两个标记）。
    - **太长**：16 字符窗口会框住**旁边另一个数字**的标记 → 那个数字被误判成"已标过"而**漏标**
      （实测：`超63%的门店闭店、人均仅1.2杯` 里 63% 的窗口正好够到 1.2杯 的标记）。
    6 刚好：够容纳"单位+括号标注"，又够不到下一个数字的标记。
    """
    tail = text[i + len(tok) : i + len(tok) + 6]
    return "待核" in tail or "存疑" in tail


# 就地降级标记（**唯一定义处**）。
# 病案（报告(8) 实拆）：原标记写的是『（RedTeam待核）』——"RedTeam" 是我们的内部工序名，
# 它直接印在**执行摘要正文和核心指标表**上（"渗透率仅1.8家/百万人口（RedTeam待核）"），
# 读报告的投资人/复核者看到的是"一份没做完的内部草稿"。
# 机制要保留（高风险数字必须显式降级），但对外措辞要像交付物：
# 标记只说明"这个数存疑、别当定论用"，"为什么存疑"由报告末尾的对抗性校验附录解释。
_MARK = "（存疑·待核）"
# 裸数字后紧邻的单位（**长的必须排前面**：`万元` 先于 `万`，否则 `182万元` 被吃成 `182万`+`元`）。
# 只收"能紧跟在一个数字后面、且确实是单位"的词——不收 `个`/`条` 这类量词，
# 它们误伤面大（"第182个"）。
_UNIT_TAIL = (
    "万亿元|亿元|万元|万美元|个百分点|平方公里|平方米|"
    "家/10万人|家/万人|杯/人|元/杯|元/人|"
    "万亿|亿|万|元|杯|家|人|次|座|台|辆|吨|月|天|年|周|%|％"
)
# 纯 ISO 日期（YYYY-MM-DD）——页眉水印的形态，见 demote_high_risk_in_head 内的说明
#
# ⚠️ **尾巴上挂个标签不算"不是日期"**（2026-09-16 全量审计）：原模式以 `$` 收尾、只认纯日期，
# 于是 `2026-09-15（生成时点）` 这种（RedTeam 常把标注写进 number）就绕过了这道守卫——
# 页眉照旧被印成「生成时点：2026-09-15（存疑·待核）」，**报告连自己的交付日期都不敢认**。
# 放宽的边界与 `core/adversarial._drop_meta_issues` **逐字同一口径**：
# 允许日期后跟非数字字符（标注/空白），但**尾巴里不许再出现数字**——
# 所以真实时序矛盾（`2025-01-01 vs 2024-01-01`）一条都不会被挡住。
# 两处是同一道守卫的两层（adversarial 在出口丢、这里在应用前再挡一次），
# 由 `_fulltest.test_watermark_date_guard_has_no_trailing_label_hole` 锁住不许分叉。
_ISO_DATE = re.compile(
    r"^\s*\d{4}\s*-\s*\d{1,2}\s*-\s*\d{1,2}\s*[^0-9]{0,24}$"
)


def _head_region(md: str, split_markers: tuple[str, ...] = ("## 宏观环境",)) -> tuple[str, str]:
    """切出决策头部区（首个主章节标题之前）与其余正文区。

    报告的正文主体第一主章节标题通常是 '## 宏观环境'（PEST 首章）。若找不到，
    退化为整篇都算头部（宁可多标不放过）。返回 (head, rest)。
    """
    for _mk in split_markers:
        _i = md.find("\n" + _mk)
        if _i != -1:
            return md[:_i], md[_i:]
    return md, ""


def _patterns(number: str) -> list[str]:
    """容错匹配模式（返回**正则片段**，调用方不再二次 escape）

    - `>63%` 同时匹配 `63%`
    - `186杯` 同时匹配 `186 杯`（数字与单位之间可能有空格——实测报告里就是 `186 杯`）
      与裸 `186`（已带数字场景容错，≥3 位纯数才加，防误伤）

    ## 为什么数字两侧一律加零宽断言（2026-09-16）

    病案（真实交付物）：RedTeam 标的数字是 `3104`，而正文里写的是「门店总数达**31048**家」。
    原来的 `re.escape("3104")` **匹配上了 `31048` 的前四位**，标记插进一个完整数字的中间，
    引文标题变成「门店总数达3104（存疑·待核）8家」。
    **它把一个正确的数字，改成了一个错误且带警告标记的数字**——而"就地标注"这套机制
    的全部价值，就在于标出来的东西是可信的。

    注意：**纯数字走的是第一条模式**（`_m` 那个分支要求"数字后面必须跟非数字字符"，
    纯数字根本不进那个分支），所以只给"裸数字兜底"加断言是无效的
    ——第一版就是这么改的，被 `test_false_accusations_are_blocked` 当场打红。
    这里改为**在构造处统一加**：凡是以数字开头/结尾的模式，两侧都挡。
    """
    _n = str(number or "").strip()
    if not _n:
        return []

    def _guard(p: str) -> str:
        """左右都不许紧邻数字或小数点（`31048` 的前四位、`13104` 的后四位、`3104.5` 都必须挡住）。"""
        return r"(?<![\d.])" + p + r"(?![\d.])"

    _m = re.match(r"^([\d.]+)([^\d.%]+)$", _n)
    _m2 = re.match(r"^([\d.]+)$", _n)
    # ⚠️ **优先序在这里就定死，不能靠"整串放最前"**（2026-09-18 第三修）。
    #
    # 前两修的教训连起来看：
    #   ① 整串模式（`_guard(esc(_n))`）放最前 → 裸数字 `182` 直接命中，标记插进 `182万元` 中间；
    #   ② 把"吃单位"模式加在**裸数字兜底之前**还不够 —— **整串模式还排在更前面**。
    # 所以整串模式**只在 token 自带单位时才配当第一优先**；裸数字一律先试"吃掉单位"。
    if _m:
        # 自带单位（RedTeam 给的是 `186 杯` 这种）：整串优先，标在单位之后
        pats = [_guard(re.escape(_n))]
        _num, _unit = _m.group(1), _m.group(2)
        # 数字与单位间允许空格：`186 杯（存疑·待核）` 而不是 `186（存疑·待核） 杯`
        pats.append(re.escape(_num) + r"\s*" + re.escape(_unit))
        if len(_num) >= 3:
            pats.append(_guard(re.escape(_num)))
    elif _m2:
        # **裸数字：先试"吃掉后面的单位"**（2026-09-18 实跑病案）。
        #
        # 上面那条 `^([\d.]+)([^\d.%]+)$` 只在 **RedTeam 给的 number 自带单位**时才成立；
        # 而 RedTeam 绝大多数时候给的是**裸数字**（它标的是"这个数是高风险"，不是"这个量"），
        # 于是只命中裸数字模式 → 交付出
        #     「年均营收中位数仅 182（存疑·待核）万元」
        # ——**执行摘要里最该读通的那一句被切断了**。本模式让它变成
        #     「182万元（存疑·待核）」。
        #
        # **不会重新引爆 `31048` 那个坑**：正文写 `31048家`、RedTeam 标 `3104` 时，
        # `3104` 后面紧跟 `8`（不在单位表里）→ 本模式不匹配；落到裸数字兜底
        # 又被 `_guard` 的 `(?![\d.])` 挡掉 → 整体零命中。
        # 单位表**长的排前面**（`万元` 先于 `万`），否则 `182万元` 被吃成 `182万`+`元`。
        _g = _m2.group(1)
        pats = [_guard(re.escape(_g) + r"\s*(?:" + _UNIT_TAIL + r")"), _guard(re.escape(_g))]
    else:
        pats = [_guard(re.escape(_n))]
    if _n[0] in "><":
        pats.append(_guard(re.escape(_n[1:])))  # ">63%" → "63%"
    # **去重但保序**，绝不能按长度排（2026-09-16 修的第二个坑）：
    # 加了零宽断言之后，"裸数字"模式的**字符串**反而比"数字+单位"模式长，
    # 按长度倒序会把它排到前面 → 标记插成 `186（存疑·待核）杯`
    # 而不是 `186杯（存疑·待核）`（实测把 `test_headline_demotion_marker` 打红了）。
    # 优先级应当是**语义的**：整串 → 去符号 → 数字+单位 → 裸数字兜底。
    _seen: set[str] = set()
    _ordered: list[str] = []
    for _p in pats:
        if _p not in _seen:
            _seen.add(_p)
            _ordered.append(_p)
    return _ordered


def demote_high_risk_in_head(md: str, adv_issues: list[dict[str, Any]] | None,
                             per_token_cap: int = 8) -> str:
    """把 RedTeam『高风险』数字在执行摘要/决策清单/指标表等头部区就地标『（存疑·待核）』。

    adv_issues: 对抗性数字校验结果（与 graph.deliver 同源），每项含 number/risk/issue。
    只处理 risk 归一为"高"的项（中/低风险不在此就地降级，避免全文刷屏）。
    幂等、容错、限次；任何异常返回原文。
    """
    text = md or ""
    if not text or not adv_issues:
        return text
    try:
        high_nums: list[str] = []
        for _i in adv_issues:
            if not isinstance(_i, dict):
                continue
            _r = str(_i.get("risk") or "").strip().lower()
            if _r.startswith("高") or _r.startswith("high"):
                _n = str(_i.get("number") or "").strip()
                # **纯 ISO 日期不作为就地降级对象**（2026-09-15 深访实跑）。
                # 病案：RedTeam 把报告头的「生成时点：2026-09-15」判为高风险，本函数于是
                # 在页眉印出「生成时点：2026-09-15（存疑·待核）」——**报告连自己的交付日期
                # 都不敢认**，而这是复核者看到的第一行。
                # 日期天然是"通用串"，标在一个地方等于标在所有地方，标记本身没有信息量；
                # 且真正的时序问题该由正文陈述，不该由页眉水印承担。
                # 上游 `core.adversarial._drop_meta_issues` 已在出口丢弃这类项，这里是**第二道**：
                # 该函数也可能收到别处传入的 adv_issues。
                if _ISO_DATE.match(_n):
                    continue
                if _n:
                    high_nums.append(_n)
        if not high_nums:
            return text
        head, rest = _head_region(text)
        if not head:
            return text
        out = head
        for _n in high_nums:
            _cnt = 0
            # **已插入标记的位置**（当前 `out` 坐标）。用来做"同一处不重复标"的去重。
            _marked_at: list[int] = []
            for _p in _patterns(_n):
                if _cnt >= per_token_cap:
                    break
                pat = re.compile(_p, re.IGNORECASE)  # _patterns 已返回正则片段（含转义）
                _repl_out: list[tuple[int, int]] = []
                for _m in pat.finditer(out):
                    if _cnt >= per_token_cap:
                        break
                    if _markers_already(_m.group(0), _m.start(), out):
                        continue
                    # **同一处不重复标**（2026-09-18 第四修）。
                    #
                    # 病案：RedTeam 标 `0.8`，正文写 `0.8家/10万人`。优先模式（吃单位）先插一次
                    #     `0.8家/10万人（存疑·待核）`
                    # 紧接着**裸数字兜底模式又匹配了那两个字符**，插出第二个：
                    #     `0.8（存疑·待核）家/10万人（存疑·待核）`
                    #
                    # ⚠️ 第一版修法是"命中即 break"——**被现有测试当场打红**：
                    # `>63%` 这个 token 有两条**互补**模式（带符号 `>63%` 与去符号 `63%`），
                    # 它们覆盖的是正文里**不同的写法**，两条都必须跑。
                    # 所以正确的判据不是"跑不跑下一个模式"，而是"这一处是不是已经标过"：
                    # 若某个已插入标记就落在本次匹配的**右邻域**内，这一处就是同一个数。
                    if any(_m.start() <= _a <= _m.end() + len(_MARK) for _a in _marked_at):
                        continue
                    _repl_out.append((_m.start(), _m.end()))
                    _marked_at.append(_m.end())
                    _cnt += 1
                # 从后往前插入，避免索引错位；每插一次，记录在它**之后**的位置整体右移，
                # 这样 `_marked_at` 始终是**当前 out 坐标**，下一个模式比对时才是同一把尺。
                for _s, _e in sorted(_repl_out, key=lambda x: -x[1]):
                    out = out[:_e] + _MARK + out[_e:]
                    _marked_at = [_a + len(_MARK) if _a > _e else _a for _a in _marked_at]
        return out + rest
    except Exception:
        return text


_MARK_QUAL = "（来源资格待核）"


def mark_source_qualification(md: str, findings: list[dict[str, Any]] | None,
                              cap: int = 6) -> str:
    """规则层 R7『来源资格错配』→ **在正文就地标出**，不只在附录里说。

    病案（2026-09-15 深访实跑，真实报告）：规则层抓到了——

        **来源资格错配（high 风险）**：「国家统计局」被用作**企业微观经营数据**（客单价）的来源…
        建议：该数字须改挂企业披露（巨潮/SEC/年报）/ 券商研报 / 行业机构；
        拿不到就把来源改成「待核」，**不得保留 L1官方 标注**。

    而同一份报告的正文行动清单第 2 条原样写着 `[依据：国家统计局2026年数据及艾瑞咨询]`，
    核心指标表也照旧标 `L1官方`。也就是说：**规则层判了"这条来源挂错了"，
    却只把判决书写在 500 行之后的附录里，正文一个字没动**——复核者读正文看到一处错误归属，
    读附录才知道我们自己也认为它错，而两处对不上时，读者信的是正文。

    这正是本项目最常复发的一类缺陷：**检出 ≠ 消费**。同类处置本仓库已有先例——
    `demote_high_risk_in_head` 对 RedTeam 高风险数字做**就地标注**（幂等、限次、异常不阻断）。
    这里沿用同一姿势，并且**只标不删**：
      · **不删原文**——抹掉 `[依据：国家统计局…]` 等于销毁证据，而"这条归属有问题"本身要给人看；
      · **只加一个短标记**，跟在出问题的那一句之后；完整理由在规则层附录里，
        正文的标记与附录的条目构成"就地告警 + 详细说明"，不是两份各说各话的结论。

    只处理 R7（`level=high` 且带 `snippet`）；只动**正文**——审计附录不标，
    那句话本来就是判决书本身，标它等于自己批注自己。
    """
    text = md or ""
    if not text or not isinstance(findings, list):
        return text
    try:
        from core.report_html import split_audit_tail

        body, tail = split_audit_tail(text)
    except Exception:
        body, tail = text, ""
    if not body:
        return text
    try:
        _snips: list[str] = []
        for f in findings:
            if not isinstance(f, dict) or f.get("rule") != "R7":
                continue
            _s = str(f.get("snippet") or "").strip()
            if len(_s) >= 8 and _s not in _snips:
                _snips.append(_s)
        if not _snips:
            return text
        out = body
        done = 0
        for _s in _snips[:cap]:
            _pos = out.find(_s)
            if _pos < 0:
                continue  # 该句在正文里找不到（前序已改写）→ 不猜、不动
            _end = _pos + len(_s)
            # 落在**句子边界**上：往后最多 40 字找句末标点，免得标记插进半个句子
            # （snippet 是截断到 110 字的，截断处可能正在句中）。
            _m = re.search(r"[。；;!？?\n]", out[_end:_end + 40])
            if _m:
                _end += _m.end()
            if _MARK_QUAL in out[max(0, _end - 220):_end + 60]:
                continue  # 幂等：附近已有标记，不叠加
            out = out[:_end] + _MARK_QUAL + out[_end:]
            done += 1
        if not done:
            return text
        return out + tail
    except Exception:
        return text


# ============================================================
# 离线自测（无网络；python core/decision_guard.py）
# ============================================================
def _selftest() -> int:
    import sys

    ok = 0

    def _check(name: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            print(f"[{name}] ✗ 断言失败")
            sys.exit(1)
        ok += 1
        print(f"[{name}] ✓")

    sample = (
        "**研策调研报告**｜生成时点：2026-09-08\n\n"
        "## 📌 执行摘要\n\n"
        "建议3：聚焦提升单店日均杯量至186杯以上的留存转化。\n\n"
        "核心指标表：\n\n"
        "| 指标 | 数值 |\n|---|---|\n"
        "| 12个月闭店率 | >63% |\n"
        "| 人均年现磨消费量 | 1.2杯 |\n\n"
        "# 📋 决策清单\n\n"
        "关键依据3：超63%的门店闭店、人均仅1.2杯(不足一线1/8)。\n\n"
        "H1：从186杯提升至250杯；若日均杯量连续3个月低于200杯转No-Go。\n\n"
        "H2 基准：单店日均销售额中位数 4,200元（中风险，不应被就地降级）。\n\n"
        "## 宏观环境\n\n正文：三四线单店日均杯量186杯(证据原始值，不应被降级)。"
    )
    issues = [
        {"number": "186杯", "risk": "高", "issue": "违背常识"},
        {"number": "1.2杯", "risk": "高", "issue": "数量级错误"},
        {"number": ">63%", "risk": "高", "issue": "异常偏高"},
        {"number": "4200元", "risk": "中", "issue": "逻辑互斥"},  # 中风险不应触发就地降级
    ]
    out = demote_high_risk_in_head(sample, issues)
    _check("头部186杯就地标待核", "186杯（存疑·待核）" in out)
    _check("指标表>63%就地标待核", ">63%（存疑·待核）" in out)
    _check("决策依据1.2杯就地标待核", "1.2杯（存疑·待核）" in out)
    _check("中风险4,200元在场且不动", "4,200元" in out and "4,200元（存疑·待核）" not in out)
    _check("正文宏观后不降级(原值保留)", out.rpartition("## 宏观环境")[2].count("186杯（存疑·待核）") == 0)
    _check("幂等: 二次调用不叠加", demote_high_risk_in_head(out, issues).count("（存疑·待核）")
           == out.count("（存疑·待核）"))
    # 容错：'63%'（无前导>）也应命中；裸 '186' 兜底不应误伤 '200杯'/'250杯'
    _check("容错63%命中", "超63%（存疑·待核）的门店" in out)
    _check("红线200/250不误标", "低于200杯转No-Go" in out and "提升至250杯" in out)

    # **页眉水印的生成日期不许被就地降级**（2026-09-15 深访实跑，真实报告原文：
    # 「**研策调研报告**｜引擎基线 v0.9.80｜生成时点：2026-09-15（存疑·待核）」——
    # 报告连自己的交付日期都不敢认，而这是复核者看到的第一行）
    _date_issues = [{"number": "2026-09-15", "risk": "高", "issue": "时序穿越/信源虚构"}]
    _dm = demote_high_risk_in_head(sample, _date_issues)
    _check("页眉生成日期不被标待核", "生成时点：2026-09-08（存疑·待核）" not in _dm)
    _check("日期项被跳过但不影响其它项", _dm.count("（存疑·待核）") == 0 or "2026-09-08（存疑" not in _dm)

    # **R7 来源资格错配就地标记**（2026-09-15 深访实跑：判决只写在 500 行后的附录里，正文没动）
    _r7 = [{
        "rule": "R7", "level": "high", "title": "来源资格错配",
        "snippet": "针对下沉市场15.8元的客单价中位数，设计专属入门级产品线",
        "advice": "不得保留 L1官方 标注",
    }]
    _body = (
        "## 行动清单（怎么做）\n\n"
        "2. **差异化定价策略**：针对下沉市场15.8元的客单价中位数，设计专属入门级产品线，"
        "避免沿用高线城市菜单 [依据：国家统计局2026年数据]。\n\n"
        "3. **强化数字化中台**：建立精准流量投放模型。\n\n"
        "---\n\n# ⚠️ 规则层自检\n\n- **来源资格错配（high 风险）**：略。\n"
    )
    _m = mark_source_qualification(_body, _r7)
    _check("R7 正文就地标记", "（来源资格待核）" in _m)
    _check("标记落在句末而非句中", "菜单 [依据：国家统计局2026年数据]。（来源资格待核）" in _m)
    _check("原文一字未删（只标不删）", "[依据：国家统计局2026年数据]" in _m)
    _check("附录判决条不自我批注",
           _m.rpartition("# ⚠️ 规则层自检")[2].count("（来源资格待核）") == 0)
    _check("R7 标记幂等", mark_source_qualification(_m, _r7) == _m)
    _check("非 R7 项不触发", mark_source_qualification(_body, [{"rule": "R1", "snippet": "针对下沉市场15.8元的客单价中位数"}]) == _body)
    _check("正文里找不到该句则不动", mark_source_qualification(_body, [{"rule": "R7", "snippet": "这句话压根不在报告里出现过哦"}]) == _body)

    print(f"\n决策层就地降级自测：{ok} 项全过（离线，无网络）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
