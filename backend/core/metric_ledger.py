"""指标台账：报告里每个数字的**结构化归属**——一套数据，两个用途

① **一致性核对**：同一指标在报告不同位置给出不同数值 → 交付前如实列出。
   同维度并列成章的问题已由"同维度合并"从源头消灭，摘要与正文的错位由"正文背书校验"兜住；
   剩下的是**章内/跨章**同一个指标被写了两次而数值不同（模型自己在不同段落里改了口）。
   这类冲突只靠事后检出——那就把它检得干干净净、写在明处，而不是假装没有。

② **指标血缘 / 跨报告锁值**：每次调研的指标落盘成时序库（`data/metrics.jsonl`），
   下次同行业调研时作为**历史锚**注入写作上下文——同一指标跨报告可比、可追溯，
   数值若变化必须写明"修正：X→Y（原因）"。这是"数据护城河"从**散文**（行业基线 KB）
   升级到**结构化指标**的那一步。

设计纪律（防误报比防漏报重要）：
- 指标名归一**只去标点空白**，**保留年份与限定词**——"2024年门店数"与"2025年门店数"
  是两个指标，把它们并成一个再报"数值冲突"就是纯噪声。
- 只在"同一指标名 + 两个不同数值"时才算冲突；单位不同、口径不同不去猜，如实并列。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_STORE = os.path.join(_DIR, "metrics.jsonl")
_MAX_READ = 4000          # 读取上限（防文件无限增长拖慢）
_TABLE_ROW_MAX_NAME = 22  # 指标名超过这个长度基本是句子，不是指标

# **必须收负号**（2026-09-14 审计）：原来只认 `[\d,]`，`-1.4%` 被读成 `+1.4%`。
# 后果有三层：① 一处 -1.2%、一处 +1.2% 的真冲突因"只有一个值"被静默放过；
# ② 错误符号随 ingest 永久写进 data/metrics.jsonl；③ 跨报告锁值锚拿着反号数值去比"修正：X→Y"。
_NUM_RE = re.compile(r"([-−+]?[\d,]+(?:\.\d+)?)")


def _norm(name: str) -> str:
    """指标名归一：只去标点/空白/星号，**保留年份与限定词**"""
    t = re.sub(r"[*`\s]", "", str(name or ""))
    t = re.sub(r"[，,。.、；;：:（）()【】\[\]\"'“”‘’]", "", t)
    return t.strip()


_CJK_RUN = re.compile(r"[一-鿿]{5,}")


def _cell_value(raw: str) -> tuple[float, str] | None:
    """单元格 → (数值, 单位)。只认"短数值"，散文/区间/标签一律不认。

    和 report_charts._is_numeric_cell 同一套判定思路：区间（¥80万-¥120万）画成单值、
    或把"日均销售额跌破6,500元（导致LTV/CAC<3）"当成一个数，都会产出**假冲突**。

    **判"这是数值不是标签"的关键是连续中文的长度**：真数值单元格里剩下的中文只有单位和
    少量标注（"亿元"、"家/百万人口"、"（L5估算）"），连续中文不会超过 4 个字；
    而标签单元格（"三线以下新店投资回收期18个月"、"四线城市单店年均营收"）里有一长串中文。
    实测这一条把 `下沉市场效能`/`宏观定价环境` 这类"标签当指标"的假冲突全滤掉了。
    """
    t = re.sub(r"[*`\s]", "", str(raw or ""))
    t = t.replace("−", "-").replace("－", "-")   # U+2212 数学减号 / 全角连字符
    if not t or len(t) > 24:
        return None
    if _CJK_RUN.search(t):
        return None
    if re.search(r"\d[\d,\.]*\s*[万亿千百]?\s*[-‐-―~～至]\s*[¥￥$]?\s*\d", t):
        return None
    t2 = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    # 去掉"紧跟在数值（含百分号）后的括号标注"：(L5估算)/(估算)/（RedTeam待核）……
    # 少了 `[%％]?` 会把 `12% (L5估算)` 判成非数值——实测这一条让 SOM 三行
    # 错位去取第三列的"测算结果亿元"，报出 12% → 131 这种名不副实的冲突。
    t2 = re.sub(r"([\d,\.]+[%％]?)[（(][^）)]{1,10}[)）]", r"\1", t2)
    if any(ch in t2 for ch in "。；;，,、（）()【】？！"):
        return None
    m = _NUM_RE.search(t2)
    if not m:
        return None
    # **数字左边紧挨着拉丁字母 → 不是数值**。病案：核心指标表的「数据级别」列写着 `L3`/`L1`，
    # `_NUM_RE` 会把那个 3 抠出来，于是台账里多出一条「艾瑞咨询 = 3」——
    # 它还会跟别的行凑成"同一来源内部不一致"的**假冲突**。
    # 同一条也顺手挡住了 `CR5`/`T4`/`Q1` 这类标签被读成 5/4/1。
    if m.start() > 0 and re.match(r"[A-Za-z]", t2[m.start() - 1]):
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = t2[m.end():].strip()[:8]
    return (v, unit)


def _tables(md: str) -> list[tuple[str, list[list[str]]]]:
    """把 markdown 切成 [(最近的标题, 表格行)]——标题用于把冲突定位到章节，读者才找得到"""
    out: list[tuple[str, list[list[str]]]] = []
    title = ""
    cur: list[list[str]] = []
    for ln in (md or "").split("\n"):
        s = ln.strip()
        if s.startswith("#"):
            if cur:
                out.append((title, cur))
                cur = []
            title = re.sub(r"^#+\s*", "", s)[:40]
            continue
        if s.startswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                continue  # |---|---| 分隔行
            cur.append(cells)
        elif cur:
            out.append((title, cur))
            cur = []
    if cur:
        out.append((title, cur))
    return out


def _plausible_label(cell: str) -> bool:
    """这个单元格像"指标名"吗（而不是另一个数值）"""
    s = str(cell or "").strip("*` ")
    if not s or len(s) > _TABLE_ROW_MAX_NAME:
        return False
    if not re.search(r"[一-鿿]", s):
        return False
    if _NUM_RE.match(s):          # 以数字开头（12% (L5估算)）→ 是数值不是名字
        return False
    # **缺失标记不是指标名**（2026-09-16）。
    # 病案：头部玩家对比表里两行的「2023年营收」格写着 `未公开`；表头没有"指标"列时
    # 指标名回退到左邻格，于是台账里出现 indicator=`未公开` 的条目——
    # 报告在一个**不存在的指标**上印出「数值不一致」，而且这条
    # 随 ingest 永久落库（`GET /api/metrics?q=未公开` 能查到、
    # `locked_anchor` 还会把它当"本行业已归档指标"注入后续写作上下文）。
    # `未公开` 不是"这个指标的值没公开"，而是"这一格没有内容"——两者不能同形。
    if re.fullmatch(
        r"(未公开|未披露|未单独披露|未披露具体|不适用|不披露|暂无|待补|待核|无数据|无|—|–|-|/|N/?A|n/?a)",
        s,
    ):
        return False
    return _cell_value(s) is None  # 本身能被读成数值的，就不是名字


# 「泛值列」列头：量纲在行名里而不在列头里，把这种列头并进指标名没有信息量。
_GENERIC_COL_HDR = ("", "数值", "值", "数据", "金额", "指标值", "本期值", "本期数值", "测量值", "数量")


# **对比/变化类列**：值是"两个数之间的关系"，不是"某个指标的取值"。
# 把它们收进台账 = 让同一个指标凭空多出几个"互相打架"的取值（真病案：差异倍数/幅度 那列）。
# 判据用列名，不用值——值靠不住（`+3.7%` 与普通的增速指标长得一样）。
_DELTA_COLS = (
    "对比", "同比", "环比", "变化", "差异", "倍数", "幅度", "增减", "升降", "vs", "VS", "较上",
)


def build_ledger(md: str) -> list[dict[str, Any]]:
    """全文表格 → 指标台账 [{indicator, key, value, unit, raw, chapter, source, period}]

    **指标名取"紧挨在数值左边的那一格"**，而不是无脑取第一列：
    实测 `| 下沉市场效能 | 三线以下新店投资回收期 | 28.5个月 | 四线城市单店年均营收 | 162万元 |`
    这种"一个标签行里塞了好几对 指标:数值"的表格，按第一列取名会把 28.5 和 162 归到
    `下沉市场效能` 名下，报出纯属捏造的"冲突"。
    若左边那格自己也是数值（=该行有第二个无数值的数值列），跳过它——宁缺勿错。

    `source` 只从**表头里明确写着"来源机构/数据来源"的那一列**取（不是"数值右边那格"）：
    后者会把 `四线城市单店年均营收` 当成来源。表头没有来源列就留空——
    **留空是有信息的**（"这一处没标来源"），比填一个错的好。
    """
    ledger: list[dict[str, Any]] = []
    # 审计附录（结论状态/配对矩阵/数据源体系/引文核验…）里的表是**诊断表不是分析指标**：
    # 实测它们的"✅ 满足 | 51"、"国家统计局 | L1官方"会被当成指标，报出纯属捏造的冲突。
    try:
        from core.report_html import split_audit_tail

        main_md, _audit = split_audit_tail(md or "")
    except Exception:
        main_md = md or ""
    for title, rows in _tables(main_md):
        hdr = rows[0] if rows else []
        src_col = next(
            (j for j, h in enumerate(hdr) if any(k in str(h) for k in ("来源机构", "数据来源", "来源"))),
            -1,
        )
        year_col = next((j for j, h in enumerate(hdr) if any(k in str(h) for k in ("年份", "数据期", "期间"))), -1)
        # **指标名所在的列**（2026-09-14 审计修正）。
        # 病案：原来只取"数值格左边一格"，而核心指标表的列序是
        # `指标|数值|来源机构|数据级别|年份|备注`（见 graph.py 的 _SUMMARY_METRICS_PROMPT）——
        # 扫到「年份」列时左邻是「数据级别」，于是台账里出现
        # `indicator='L2媒体与咨询', value=2026`（`_plausible_label` 因为含中文判 True，
        # 而 `_cell_value("L2媒体与咨询")` 因"数字紧邻拉丁字母"返回 None，反倒帮它过了关）。
        # 实测 `data/metrics.jsonl` 里已有 **29 条** indicator 以 `L<数字>` 开头——真冲突被这些噪声淹没，
        # 且错误指标名随 ingest 永久污染跨报告锚。
        ind_col = next(
            (j for j, h in enumerate(hdr) if any(k in str(h) for k in ("指标", "名称"))),
            -1,
        )
        _skip_cols = {c for c in (src_col, year_col) if c >= 0}
        # 「数据级别」列（L1-L5 标签）也必须跳过，否则它会同时污染指标名与来源
        for j, h in enumerate(hdr):
            if any(k in str(h) for k in ("数据级别", "级别", "层级")):
                _skip_cols.add(j)
            # 备注/说明类列是**文字**，但里面常出现数字开头的期间说法
            # （`7月当月`、`1—7月累计`）——`_cell_value` 会把 `7月当月` 读成数值 7。
            if any(k in str(h) for k in ("备注", "说明", "注释", "口径")):
                _skip_cols.add(j)
            # **对比/变化类列整列不进台账**（2026-09-15 深访实跑，Claude 评审点名）。
            # 病案：竞争格局表里有一列叫「差异倍数/幅度」，值是 2.3倍 / 12倍 / 54.7% / +3.7个百分点。
            # 它们被当成**同一指标的第二、第三个取值**，于是"指标一致性核对"节报出
            # 一批纯属捏造的"口径冲突"——而那一节的全部价值就是让人相信它报的是真冲突。
            # 判据：列名含对比/变化语义。**宁可少收一个指标，也不报一个假冲突**——
            # 本模块自己的纪律就是"防误报比防漏报重要"（真冲突被噪声淹没那么显眼，
            # 而假冲突会让人不再看这一节）。
            if any(k in str(h) for k in _DELTA_COLS):
                _skip_cols.add(j)
        # **一行的多个数值列 = 同一指标的几个口径，不是同一指标的几次读数**（2026-09-16）
        #
        # 病案：真实报告里那张表长这样——
        #   | 指标 | 一线城市基准值 | 三四线城市基准值 | 差异倍数 | 来源 |
        #   | 单店日均杯量 | 287 杯 | 126 杯 | 2.3倍 | 艾瑞咨询2024 / 反方检索2024 |
        # 有了"指标"列之后，第 212 行给**这一行的每一个数值格**都挂同一个指标名，
        # 列语义（一线/三四线）随列号一起丢掉 → 台账里 `单店日均杯量` 有两个值 →
        # 附录把它判成 🔴「**同一来源内部不一致（最高优先级，务必核对）**」。
        # 实测这样造出 **4 条假阳性**（单店日均杯量 / 客单价中位数 / 门店渗透率 / 闭店率），
        # 而那一节的全部价值就是让人相信它报的是真冲突——4 条噪声里埋着 1 条真的
        # （客单价 20 vs 15 元/杯），读者查三条都是我们自己的问题之后就不会再查第四条了。
        #
        # 判据：表里存在**≥2 个数值列**时，把列头并进指标名（`单店日均杯量（三四线城市基准值）`）。
        # 只并"非泛值列"的列头——`指标|数值|来源机构` 那种表只有一个数值列，不受影响，
        # 跨报告锁值锚的键也保持不变。
        _val_cols = [j for j in range(1, len(hdr)) if j not in _skip_cols and j != ind_col]
        _multi_val = len(_val_cols) >= 2
        for cells in rows:
            if len(cells) < 2:
                continue
            for i in range(1, len(cells)):
                # 来源/年份/级别列**整列不参与台账**。少了这一条，
                # 「年份」列里的 2025 会被当成一个数值，再挂到指标列名下 →
                # 同一个指标被记两条（6562亿元 与 2025），凭空造出"数值不一致"。
                if i in _skip_cols:
                    continue
                hit = _cell_value(cells[i])
                if not hit:
                    continue
                label = cells[ind_col] if 0 <= ind_col < len(cells) and ind_col != i else cells[i - 1]
                if i > 1 and not _plausible_label(label):
                    continue  # 左边不是名字（是另一个数值/空）→ 这列没有可归属的指标名
                name = label.strip("*` ")
                if not _plausible_label(name):
                    continue
                # 指标名落在来源/年份/级别列上 → 这是"列错位"，不是指标
                if ind_col < 0 and (i - 1) in _skip_cols:
                    continue
                if re.match(r"^L\d", name) or re.match(r"^T\d", name):
                    continue  # 数据级别标签（L2媒体与咨询 / T4）
                if name in ("指标", "名称", "数值", "数据级别", "备注"):
                    continue
                # 多数值列的表：把列头并进指标名（见 `_multi_val` 处的病案）。
                # 判据用**列头**而非列号——列号在读者那一端毫无意义。
                if _multi_val and 0 <= ind_col < len(cells) and ind_col != i:
                    _ch = (hdr[i] if i < len(hdr) else "").strip("*` ")
                    if _ch and _ch not in _GENERIC_COL_HDR and len(_ch) <= 16:
                        name = f"{name}（{_ch}）"
                v, unit = hit
                # **只丢 0，不丢负数**（2026-09-14 审计）。原来写的是 `if v <= 0: continue`，
                # 把全部负值整条丢出台账——而"同比下降 1.2%"在市场调研里是极常见的一类数字。
                # 后果：一处写"下降1.2%"、另一处写"增长1.2%"这种**符号相反的真冲突永远检不出**，
                # 台账自己也少了一大块（负增长指标集体缺席一致性核对）。
                if v == 0:
                    continue
                src = ""
                if src_col > i and src_col < len(cells):
                    src = cells[src_col].strip("*` ")[:24]
                year = cells[year_col].strip("*` ")[:12] if 0 <= year_col < len(cells) else ""
                raw = cells[i].strip()[:40]
                ledger.append(
                    {
                        "indicator": name,
                        "key": _norm(name),
                        "value": v,
                        "unit": unit,
                        "raw": raw,
                        "chapter": title,
                        "source": src,
                        # 数据期取法：**扫整行**，不只扫指标名与数值格。
                        # 病案：核心指标表把期间写在「备注」列（`1—7月累计` / `7月当月`），
                        # 只看指标名+数值时两行都读成"2026年"，于是「累计 287744 亿」与
                        # 「当月 37713 亿」被判成**同一来源内部打架**——其实它们都对，
                        # 差的只是期间。这种假冲突最伤信任：读者查完发现是我们的问题。
                        # 数据期扫**整行**（期间常写在"备注"列），但**不扫数值原文**
                        # ——`raw` 是数值格，`2019家`/`2019万辆` 会被年份 pattern 读成"2019年"，
                        # 然后盖过真实年份列，把真冲突降级成"趋势证据"。
                        "period": _period_of(year, name, " ".join(cells)),
                    }
                )
    return ledger


# ============================================================
# 数据期识别：把"数值不同"里"**期间**不同"的那一类先摘出来
# ------------------------------------------------------------
# 「1—7月累计社零 287744 亿」与「7月当月社零 37713 亿」数值差一个量级，
# 但它们**不是打架**——是同一指标的两个期间。这种"冲突"报出来，
# 读者要么被误导，要么从此不信这张表。识别出期间差异，把它归到"趋势证据"里。
# ============================================================
_PERIOD_PATS: tuple[tuple[re.Pattern[str], Any], ...] = (
    (re.compile(r"1\s*[—\-~～]\s*(\d{1,2})\s*月"), lambda m: f"1—{m.group(1)}月累计"),
    (re.compile(r"((?:19|20)\d{2})\s*年\s*1\s*[—\-~～]\s*(\d{1,2})\s*月"),
     lambda m: f"{m.group(1)}年1—{m.group(2)}月"),
    (re.compile(r"上半年"), lambda m: "上半年"),
    (re.compile(r"下半年"), lambda m: "下半年"),
    (re.compile(r"一季度|Q1"), lambda m: "一季度"),
    (re.compile(r"全年|年度"), lambda m: "全年"),
    (re.compile(r"(\d{1,2})\s*月(?:份|当月|末)?"), lambda m: f"{m.group(1)}月"),
    # **只认带「年」字的年份**：裸四位数字在正文里到处都是（2019家 / 2019万辆 / 2019亿元），
    # 少了这个「年」字约束，`门店总数 2019家` 会被读成"数据期 2019年"，
    # 再盖过真实年份列 → 真冲突被降级成"趋势证据"（这正是要修的那条）。
    (re.compile(r"((?:19|20)\d{2})\s*年"), lambda m: f"{m.group(1)}年"),
)


def _period_of(year_hint: str = "", *texts: str) -> str:
    """认**数据期**。（2026-09-14 审计重写）

    取"信息量更大"的那个，而不是简单分先后：
      · 「年份」列 → `2024年`（权威，但只到年）
      · 行内文本 → `1—7月累计` / `上半年` / `7月`（更具体）
    两者都有时**用更具体的那个**——不然「1—7月累计」与「7月当月」会被压成同一个
    「2026年」，两个都对却看起来像打架。

    纪律：文本里只认**带「年」字**的年份与明确期间。裸四位数字（`2019家`/`2019亿元`）
    **不是数据期**——认它会把真冲突降级成"趋势证据"。
    """
    y = ""
    m_y = re.search(r"((?:19|20)\d{2})", str(year_hint or ""))
    if m_y:
        y = f"{m_y.group(1)}年"
    t = " ".join(str(x or "") for x in texts)
    if not t.strip():
        return y
    for rx, fn in _PERIOD_PATS:
        m = rx.search(t)
        if not m:
            continue
        try:
            got = fn(m)
        except Exception:
            continue
        # 文本给的只是"年"→ 不比年份列更具体，交给年份列
        if re.fullmatch(r"(?:19|20)\d{2}年", got or ""):
            break
        return got
    return y


def find_conflicts(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一指标名出现 ≥2 个不同数值 → 冲突（保留各自章节，便于读者回查）

    每条冲突带 `code` / `note`（见 `classify_variants`）——
    **"数值不同"不等于"打架"**：数据期不同、口径不同都会让两个都对的数看起来矛盾。
    不加区分地一律报"冲突"，等于把真问题淹在噪声里。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in ledger or []:
        k = it.get("key") or ""
        if len(k) < 3:
            continue  # 太短的名字（"占比"/"数值"）并起来必是噪声
        groups.setdefault(k, []).append(it)
    out: list[dict[str, Any]] = []
    for k, items in groups.items():
        vals = {round(float(i["value"]), 4) for i in items}
        if len(vals) < 2:
            continue
        seen: list[dict[str, Any]] = []
        for i in items:
            if not any(abs(float(i["value"]) - float(s["value"])) < 1e-9 for s in seen):
                seen.append(i)
        # ## 先分类，再截断（2026-09-16 修）
        #
        # 这里原先写作 `seen = seen[:4]` **然后**才 `classify_variants(seen)` ——
        # 而 `classify_variants` 读的是**来源集合与数据期集合**（`{v["source"] for v in vs}`），
        # 不是前四个值本身。所以丢掉第 5 个不同值**会改变判定码**：
        # 例如前 4 处都来自 A 源（判 `same_source`「同一来源内部打架，最该先查」），
        # 而第 5 处来自 B 源——真实判定应是 `cross_source`（多半只是口径不同）。
        # **一个静默截断，把"多半是笔误"说成了结论。**
        # 现在：分类用**全量**去重值，截断只作用于**展示**，且截了多少必须写出来。
        code, note = classify_variants(seen)
        out.append(
            {
                "indicator": items[0]["indicator"], "key": k,
                "variants": seen[:4], "variants_total": len(seen),
                "code": code, "note": note,
            }
        )
    # 真冲突排前面（同类内按指标名）
    _rank = {"same_source": 0, "cross_source": 1, "unknown": 2, "caliber": 3, "period": 4}
    return sorted(out, key=lambda x: (_rank.get(x.get("code") or "", 9), x["indicator"]))


# 口径敏感指标：同一个词在不同来源下**分母/范围常常不同**，
# 这类指标"两处数值不同"最可能的原因就是口径，而不是谁写错了。
_CALIBER_SENSITIVE = (
    "市占率", "份额", "渗透率", "集中度", "CR5", "CR10", "覆盖率",
    "市场规模", "行业规模", "规模", "门店数", "客单价", "均价", "人均",
)


def classify_variants(vs: list[dict[str, Any]]) -> tuple[str, str]:
    """同指标的不同数值 → (判定码, 说明)。**先看数据期，再看来源**。

    判定码与处理方式：
    | code | 含义 | 报告里怎么讲 |
    |---|---|---|
    | `same_source` | **同一来源内部**数值不一致 | 最高优先级：多半是笔误或摘录错，必须查 |
    | `cross_source` | 不同来源给出不同数值 | 多数是口径差异，须核对各自口径 |
    | `caliber` | 口径敏感指标 + 多来源 | 大概率是口径不同→**不可比**，不是谁错 |
    | `period` | 数据期不同 | 口径一致、期间不同——**这恰恰是可用的趋势证据** |
    | `unknown` | 两处都没标来源 | 无法判断，列为待核 |
    """
    if len(vs) < 2:
        return "", ""
    periods = {str(v.get("period") or "") for v in vs}
    periods.discard("")
    if len(periods) >= 2:
        return (
            "period",
            f"数据期不同（{'、'.join(sorted(periods))}）——口径一致、期间不同，"
            "**这不是打架**，反而是可用的趋势证据",
        )
    srcs = {str(v.get("source") or "").strip() for v in vs}
    srcs.discard("")
    if len(srcs) == 1:
        return "same_source", f"**同一来源（{list(srcs)[0]}）内部数值不一致**——最该先查的一类，多半是笔误"
    if len(srcs) >= 2:
        nm = str(vs[0].get("indicator") or "")
        if any(k in nm for k in _CALIBER_SENSITIVE):
            return (
                "caliber",
                f"不同来源（{'、'.join(sorted(srcs))}）——「{nm}」是**口径敏感指标**，"
                "两处的统计范围/分母很可能不同，**大概率不可直接比较**，而不是谁写错了",
            )
        return "cross_source", f"不同来源（{'、'.join(sorted(srcs))}）给出不同数值，须核对各自口径"
    return "unknown", "两处都未标注来源，无法判断是口径问题还是笔误"


def format_appendix(conflicts: list[dict[str, Any]]) -> str:
    """冲突 → 报告附录（没有冲突就**一个字都不加**）

    按判定码**分节呈现**，而不是揉成一张"数值不一致"表：
    把"数据期不同"和"同一来源内部打架"并列，读者会以为两者同样严重——
    前者其实是一条趋势证据，后者才是必须立刻查的错。
    """
    if not conflicts:
        return ""
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in conflicts:
        groups.setdefault(c.get("code") or "unknown", []).append(c)
    lines = [
        "\n\n---\n\n# 🔎 指标一致性核对",
        f"本次报告有 **{len(conflicts)}** 项指标在不同位置给出了不同数值。"
        "但**「数值不同」不等于「打架」**——下面按原因分类列出，处理方式各不相同：",
    ]
    _BLOCKS = (
        ("same_source", "🔴 同一来源内部不一致（最高优先级，务必核对）"),
        ("cross_source", "🟠 不同来源数值不一致（核对口径）"),
        ("caliber", "🟡 口径敏感指标，多半不可比（不是错）"),
        ("period", "🟢 数据期不同（**这是趋势证据，不是冲突**）"),
        ("unknown", "⚪ 未标来源，无法判断"),
    )
    for code, head in _BLOCKS:
        items = groups.get(code) or []
        if not items:
            continue
        lines.append("")
        lines.append(f"**{head}** —— {items[0].get('note') or ''}")
        lines.append("")
        lines.append("| 指标 | 各处数值（所在章节 · 来源） |")
        lines.append("| :--- | :--- |")
        for c in items[:8]:
            variants = "；".join(
                f"{v['raw']}（{v['chapter'] or '未标章节'}"
                + (f" · {v['source']}" if v.get("source") else " · 未标来源")
                + (f" · {v['period']}" if v.get("period") else "")
                + "）"
                for v in c["variants"]
            )
            # **截断必须留痕**（2026-09-16）：分类用的是全量去重值，
            # 表里只列前 4 处——不写出来，读者会以为"这个指标总共就这 4 个数"，
            # 而判定码可能是被第 5 处决定的。表里少写一行，读者就会自己补一个错的。
            _extra = int(c.get("variants_total") or len(c["variants"])) - len(c["variants"])
            if _extra > 0:
                variants += f"；……等共 {c.get('variants_total')} 处不同数值"
            lines.append(f"| {c['indicator']} | {variants} |")
        if len(items) > 8:
            # 表头那句「本次报告有 N 项」用的是全量，这里必须说清本节被截到 8 行——
            # 否则又是一个"N 项里只列了 8 项、却看不出少了"的静默截断。
            lines.append("")
            lines.append(f"> 本节共 **{len(items)}** 项，上表列出前 8 项（其余同型，处理方式相同）。")
    lines.append("")
    lines.append(
        "> 处理建议：**先看数据期**（期间不同则两个都对，可直接当趋势用）；"
        "**再看来源**（同一来源内部不一致优先查，多半是笔误）；"
        "**最后看口径**（不同来源的规模/份额/门店数类指标，统计范围与分母常常不同）。"
        "正式引用前请人工确认。"
    )
    return "\n".join(lines)


# ============================================================
# 指标时序库（跨报告血缘 / 锁值锚）
# ============================================================
def ingest(ledger: list[dict[str, Any]], report_id: str = "", question: str = "", industry: str = "") -> int:
    """把本次台账落入时序库 → 写入条数（失败不影响交付）。

    ## 离线闸门（2026-09-16 补 · 第五次复发病案的第六处）

    本函数此前**没有** `YANCEC_OFFLINE` 闸门，由 `_gate_probe.py` 探针实测确认：
    置 `YANCEC_OFFLINE=1` 后 `metrics.jsonl` 仍被改写。

    为什么这一处与其它写点同级甚至更该拦：`metrics.jsonl` 是**跨报告的血缘库**，
    被 `locked_anchor()` 读回去当**锁值锚**注入下一次调研——与经验台账同一条污染路径，
    但更硬：台账只是"提示"，锁值锚是**直接写进提示词的数字约束**。
    测试用题（杭州开咖啡店）的指标一旦入库，下一次真实调研就会拿到它当"历史锚"。
    """
    if not ledger:
        return 0
    if safe_io.is_offline():
        logger.info("离线模式：本次不写生产指标时序库（question=%s）", str(question or "")[:40])
        return 0
    try:
        safe_io.makedirs(_DIR)
        ts = datetime.now().isoformat(timespec="seconds")
        n = 0
        with safe_io.open_prod(_STORE, "a", encoding="utf-8") as f:
            for it in ledger:
                # 指标名太短/纯符号的不入库（存进去只会污染锚）
                if len(it.get("key") or "") < 3:
                    continue
                f.write(
                    json.dumps(
                        {
                            "ts": ts,
                            "report_id": report_id,
                            "question": (question or "")[:80],
                            "industry": industry or "",
                            "indicator": it["indicator"],
                            "key": it["key"],
                            "value": it["value"],
                            "unit": it.get("unit") or "",
                            "chapter": it.get("chapter") or "",
                            "raw": it.get("raw") or "",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                n += 1
        return n
    except Exception as e:
        logger.warning("指标台账落库失败（不影响交付）: %s", e)
        return 0


def _read_all() -> list[dict[str, Any]]:
    if not os.path.isfile(_STORE):
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(_STORE, encoding="utf-8") as f:
            for ln in f.readlines()[-_MAX_READ:]:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("key"):
                    out.append(d)
    except Exception as e:
        logger.warning("指标时序库读取失败: %s", e)
    return out


def series(indicator: str, industry: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """某指标的跨报告序列（新的在前）——按 key 精确匹配，匹配不到再按包含匹配"""
    k = _norm(indicator)
    if not k:
        return []
    rows = [r for r in _read_all() if (not industry or r.get("industry") == industry)]
    exact = [r for r in rows if r.get("key") == k]
    hit = exact or [r for r in rows if k in (r.get("key") or "")]
    hit.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return hit[: max(1, min(limit, 200))]


def locked_anchor(industry: str, limit: int = 12) -> str:
    """本行业**已归档指标**锚块（供写作上下文注入）

    与 `core/company_anchor` 同一模式：一处沉淀、全章可见。指标同名只留**最近一次**取值，
    并要求新数据若与锚不同必须写成"修正：X→Y（原因）"——**锚是提示，不是枷锁**：
    新证据更权威时该改就改，但必须留下改动的痕迹，不能悄悄换个数。
    """
    if not industry:
        return ""
    rows = [r for r in _read_all() if r.get("industry") == industry]
    if not rows:
        return ""
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    picked: dict[str, dict[str, Any]] = {}
    for r in rows:
        picked.setdefault(r["key"], r)
        if len(picked) >= limit:
            break
    lines = [
        f"【{industry}·已归档指标（本产品历史调研沉淀）】",
        "以下指标在本行业既往调研中已确立口径。**优先复用同一数值**；"
        "若本次拿到更权威/更新的数据，可以修正，但必须显式写成「修正：上期 X → 本次 Y（原因：…）」，"
        "禁止同一指标在同一份报告里出现两个值而不作说明。",
    ]
    for r in picked.values():
        lines.append(
            f"- {r['indicator']} = {r.get('raw') or r.get('value')}"
            f"（{r.get('ts','')[:10]} 调研，章节：{r.get('chapter') or '—'}）"
        )
    return "\n".join(lines)


def describe() -> dict[str, Any]:
    rows = _read_all()
    return {
        "store": _STORE,
        "records": len(rows),
        "indicators": len({r.get("key") for r in rows}),
        "industries": sorted({r.get("industry") for r in rows if r.get("industry")})[:20],
    }
