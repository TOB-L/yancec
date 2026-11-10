"""核心指标对比图 → PNG bytes（供 docx/pdf 内嵌）

纪律（与前端 metricRows / HTML 作品页一致）：只有"同单位 ≥2"才画柱状图；
异量纲数字不共轴（把 28.6% 与 14.7 个月画一根轴 = 误导，不做）。
matplotlib 缺失时所有调用返回 []，调用方（导出器）静默跳过，不影响导出。
"""
from __future__ import annotations

import io
import re
from typing import Any

# 只对"来源机构/年份"齐备的『核心指标一览表』出图（表头含 来源机构 或 年份），
# 避免把单位经济的"指标项目|数值|单位|备注"误当核心指标（其数值列不带单位、无年份）。
_METRICS_HDR_MARK = ("来源机构", "年份")

# **同一份报告，换个导出格式，图数必须一样。**
#
# 病案（2026-09-16 全量审计）：三个导出面各写了一个字面量——
# `report_docx._MAX_CHARTS = 12` · `app.py` 的 PDF 预算 `{"left": 4}` · md 路径 `max_charts=16`。
# 于是同一份报告导 Word 有 12 张图、导 PDF 只有 4 张，而两处的注释还在互相打脸：
# app.py 写「与 Word 对齐：上限 4 张」，report_docx.py 写「PDF 已放到 12」。
# **注释互相引用的时候，只要没有单一出处，两边迟早各说各话。**
# 现在收口到这一个常量，三个导出面都从这里取——数字只有一个地方能改。
MAX_EXPORT_CHARTS = 12


def is_metrics_header(cells: list[str]) -> bool:
    joined = "".join(cells)
    return any("指标" in joined and "数值" in joined and (k in joined) for k in _METRICS_HDR_MARK)


def _num(raw: str) -> float | None:
    r"""取数。**必须收符号。**

    病案（2026-09-16 全量审计）：原正则 `([\d,]+(?:\.\d+)?)` 里没有符号位，
    `-15万` 被读成 **`15万`** —— 于是**亏损被画成一根向上的柱子**：
    「图里全是增长，表格里写着亏损」，而图比表更抓眼。
    这是本仓库"假阳性比漏报更伤"那一条在图表层的同型：**一根画反的柱子比不出图更糟**。

    `0` 不再被丢掉：`0 家门店` 是一个读数，不是缺失。丢掉它是**静默截断**。
    """
    m = re.search(r"([-−–—+＋]?)\s*([\d,]+(?:\.\d+)?)", raw or "")
    if not m:
        return None
    try:
        v = float(m.group(2).replace(",", ""))
    except Exception:
        return None
    return -v if m.group(1) in ("-", "−", "–", "—") else v


# 可信度/期间标注（不是单位）：`(估算)` `(2023全年)` `(L5)` `（存疑·待核）`…
_ANNOT_RE = re.compile(
    r"(估算|待核|预计|预测|约|左右|L\d|存疑|专家判断.*|.*(全年|年度|财年|年报|披露|数据期))"
)


def _unit_of(raw: str, header: str = "") -> str:
    """取单位。**表头里的括号单位优先**于单元格尾巴。

    病案（2026-09-11 拆真报告）：表头写「2025年预估营收 (亿元)」、单元格写 `320.0 (估算)`，
    旧逻辑从单元格尾巴取 → 单位成了 **"估算"**，图题写成「（估算）」——
    比不出图更糟：出来一张**标错单位的图**。

    病案（2026-09-16 全量审计）：**markdown 强调符与脚注标记也不是单位**。
    实测从真报告里抽出来的"单位"有 `*注4*`（原文 `302.5 *注4*`）、
    `2023全年`（原文 `249.0 (2023全年)`）、`2023实披露`、`「瑞幸2023年年报」`。
    它们不光把图题写成「（*注4*）」，还会**把同一列拆成多个单位组**——
    每组都不足 2 行，于是**整列一张图都出不来**，而那正是该列最大值所在的行。
    """
    h = re.search(r"[（(]\s*([^（()）]{1,8})\s*[)）]", header or "")
    if h and not re.fullmatch(r"(估算|待核|预计|预测|约|左右|L\d)", h.group(1).strip()):
        return h.group(1).strip()[:6]
    m = re.search(r"([\d,]+(?:\.\d+)?)", raw or "")
    unit = raw[m.end():] if m else ""
    # **尾巴上的括号：像单位就用它，像可信度标注就丢掉**（2026-09-16）。
    # 病案：表头是「市场份额 (估算)」时，表头括号命中标注 → 放弃表头单位、改从单元格尾巴取，
    # 而尾巴 `~15.0% (估算)` 里那个括号**又是标注**。旧实现只做去括号与截断，
    # 得到 `'% (估算'` —— 于是同一列被拆成 `'% (估算'`（星巴克/库迪/Manner/Tims）
    # 与 `'%'`（瑞幸 37.2%，**单行，整组被丢**）两组：**市场份额最大的那家从图上消失**，
    # 图题还写成「（% (估算）」。
    _p = re.search(r"[（(]\s*([^（()）]{1,8})\s*[)）]", unit)
    if _p:
        _inner = _p.group(1).strip()
        unit = _inner if (_looks_like_unit(_inner) and not _ANNOT_RE.fullmatch(_inner)) else unit[:_p.start()]
    unit = re.sub(r"^[（(【\s]*", "", unit)   # 去前置括号/空白（低34%、约28.6% 里的字）
    unit = re.sub(r"[（(【】)）\s]*$", "", unit)
    unit = re.sub(r"^(约|约合|高于|低于|低|高)?", "", unit)
    unit = unit.lstrip(".")
    # ---- 以下三条都是 2026-09-16 补的"标注 ≠ 单位"清洗 ----
    unit = re.sub(r"[*_`「」『』]", "", unit)          # markdown 强调符 / 书名号
    if re.search(r"(19|20)\d{2}", unit):
        return ""                                     # 含年份 → 是数据期或出处，不是单位
    unit = re.sub(r"^(注|见|参考|资料来源)?\s*\d{1,3}$", "", unit)   # 脚注引用：注4 / [3] / ①
    unit = re.sub(r"^\[\d{1,3}\]$", "", unit)
    # 单元格尾巴若是"估算/待核/预计"这类**可信度标注**而非单位 → 视为无单位，
    # 让同表多行仍能按"同单位"聚在一起（而不是被这些字拆散或冒充成单位）
    if re.fullmatch(r"(估算|待核|预计|预测|约|左右|L\d|专家判断.*|.*(全年|年度|财年|年报|披露))", unit or ""):
        return ""
    return unit[:6]


# 表头文字列（不是数值列）：命中即跳过该列
_TEXT_HDR = ("说明", "依据", "来源", "机构", "备注", "口径", "定位", "特征", "对策", "风险", "章节", "备注说明", "等级",
             # 分类/标注列：这些列里的"数字"是**编号不是量级**（L1官方/L5假设/中/低），
             # 画成柱状图毫无意义——实测把"数据级别""类型""建议核验范围"都画成了图。
             "级别", "类型", "可信度", "核验", "模式", "策略", "状态", "标签", "年份")

# 单位必须"像单位"：含这些字且够短。防把「高线核心市场 (一线/新一线)」的括号内容当单位。
_UNIT_CHARS = ("元", "亿", "万", "%", "％", "家", "人", "杯", "个", "次", "倍", "天", "月", "年",
               "吨", "台", "辆", "平", "公里", "米", "分", "点", "美元", "港元", "人/", "家/", "次/")
# **审计附录**起点：到了这里就**不再出图**。
# 病案（2026-09-11 拆真报告）：审计表（置信度台账/源路由审计/数字护照/配对矩阵/RedTeam）
# 被逐张画成柱状图——把"核心理由""实际拿到""条数"当指标画柱状图，是**报告里最扎眼的硬伤**。
_AUDIT_HEAD = re.compile(
    r"^#{1,4}\s*[^#]*?(数据与方法说明|质检与可验证性|结论置信度披露|数据缺口披露|数据源体系|结论状态|"
    r"引文核验报告|规则层自检|对抗性数字校验|证据链台账)"
)


def _looks_like_unit(s: str) -> bool:
    s = (s or "").strip()
    return bool(s) and len(s) <= 8 and any(c in s for c in _UNIT_CHARS)


# 「值列」的表头——**这一列的量纲写在行名里，不在表头里**。
# 只有这种列才需要按"指标主题"把行拆成子族（见 `_group_rows` 的说明）。
_GENERIC_VALUE_HDRS = ("", "数值", "值", "数据", "金额", "指标值", "本期数值", "本期值",
                       "数值/金额", "数值（估算）")


def _generic_value_header(h: str) -> bool:
    """表头是不是"泛值列"（数值/金额/值…）——即量纲在行名而不在表头。

    病案（2026-09-16 全量审计）：`_group_rows` 原先**无条件**对百分号列做"按指标主题
    拆子族"，于是 `品牌|市占率` 这种**同一指标、不同主体**的表也被拆——
    主体名之间不共享连续 2 字 → 每个主体自成一族 → 单行族被整族丢掉，
    实测「市场份额」图只剩库迪 5% 与 Tims 1.5%，**其余品牌的数据点凭空消失**。
    拆族只对"行名就是指标名"的泛值列有意义。
    """
    _h = re.sub(r"[*`\s（）()【】]", "", h or "")
    return _h in _GENERIC_VALUE_HDRS or _h.startswith("数值") or _h.startswith("指标值")


def _is_numeric_cell(raw: str) -> bool:
    """这个单元格是不是**数值型**，而不是"散文里恰好带了个数字"。

    病案：表「反转阈值」里的 `日均销售额跌破 **6,500元**（导致LTV/CAC < 3）` 被当成数值画进图；
    区间值 `¥80万 - ¥120万` 被当成 80 画成一根柱子——**看图的人会直接读错**。
    """
    import re as _re

    t = _re.sub(r"[*_`\s]", "", raw or "")
    if not t or len(t) > 18:            # 数据单元格都很短；散文长句直接排除
        return False
    # 区间（80万-120万 / 3~5家 / -15万 ~ -20万）不是单值——画成一根柱子会让人直接读错。
    # 注意单位字可能夹在中间（"80万-¥120万"），负数区间还有前置负号（"-15万 ~ -20万"），
    # 故 `[¥￥$+\-]?` 必须带上——少了它 "−15万 ~ −20万" 会被读成"15"单值画进图里。
    if _re.search(r"\d[\d,\.]*\s*[万亿千百]?\s*[-‐-―~～至]\s*[¥￥$+\-]?\s*\d", t):
        return False
    # **先把千分位逗号归一**，否则 "7,500+" 里的逗号会被当成句读判成非数值（实测踩过）
    t2 = _re.sub(r"(?<=\d),(?=\d{3})", "", t)
    # 只允许"紧跟在数值后的单位/标注"这一种括号。
    # `[%％]?[+＋]?` 不能少：实测「约7,000+ (估算)」里的 `+` 把括号挡在了数值后面，
    # 标注剥不掉 → 残留的 `（` 被当成句读 → 整列判成非数值 → 品牌对比表**一张图都出不来**
    # （那张表是整份报告里最该出图的一张）。
    t2 = _re.sub(r"([\d,\.]+[%％]?[+＋]?)[（(][^）)]{1,8}[)）]", r"\1", t2)
    if any(ch in t2 for ch in "。；;，,、（）()【】？！±"):
        return False
    # 字母紧挨数字 = **编号不是量级**（L1官方/L5假设/Q1/H1/T2）——
    # 不挡掉就会把等级列画成柱状图，图上写着"数据级别：L1=1、L2=2"，是报告里最外行的图。
    if _re.search(r"[A-Za-z]\d", t2):
        return False
    return bool(_re.search(r"\d", t2))


def _group_rows(md_cells_rows: list[list[str]], header: list[str] | None = None) -> list[dict[str, Any]]:
    """逐**数值列**分组出图（每列按单位分族，只留 ≥2 行同单位的）

    2026-09-11 修：原实现只取 `cells[1]` 一列 → 「品牌|营收|市占率|增速|门店数」这类
    多数值列表格**只出 1 张图**（其余三列全丢）；且图题恒为"核心指标对比"。
    现在每列各出各的图，图题用该列真实表头——这正是"报告只有开头一两张图"的根因之一。
    """
    hdr = header or []
    ncol = max((len(r) for r in md_cells_rows if r), default=0)
    # **单位可能写在旁边一列**（`指标项目|数值|单位|备注`）。
    # 病案（2026-09-16 全量审计）：只从单元格尾巴与表头括号取单位时，这种表的
    # 每一行单位都是空 → 全部落进同一个空单位组 → 图上出现一根 4,687 的柱
    # 紧挨一根 45 的柱（前者是亿元、后者是杯/人/年）——**异量纲共轴**，
    # 正是本模块开头那句"不做"的事，而它是由我们自己的取单位逻辑造成的。
    _unit_col = next((j for j, h in enumerate(hdr) if "单位" in str(h)), -1)
    per_col: list[dict[str, Any]] = []
    for c in range(1, ncol):
        if c == _unit_col:
            continue
        htxt = (hdr[c] if c < len(hdr) else "").strip()
        if htxt and any(k in htxt for k in _TEXT_HDR):
            continue
        # 表头既长又给不出单位 → 大概率是**描述列**（如「高线核心市场 (一线/新一线)」），跳过
        _hu = _unit_of("", htxt)
        if len(htxt) > 10 and not _looks_like_unit(_hu):
            continue
        gathered: dict[str, list[dict[str, Any]]] = {}
        for cells in md_cells_rows:
            if not cells or len(cells) <= c:
                continue
            name = (cells[0] or "").strip()[:14]
            raw = (cells[c] or "").strip()
            if not _is_numeric_cell(raw):
                continue
            v = _num(raw)
            if v is None:
                continue
            unit = _unit_of(raw, htxt)
            # 旁边那列写了单位就用它（且必须"像单位"——那一列也常写"—"/"占比"这类非单位）
            if 0 < _unit_col < len(cells):
                _uc = re.sub(r"[*`\s]", "", (cells[_unit_col] or ""))
                if _looks_like_unit(_uc):
                    unit = _uc[:6]
            label = re.sub(r"[（(【].*$", "", raw).strip()
            gathered.setdefault(unit, []).append(
                {"name": name, "value": v, "raw": raw, "unit": unit, "label": label or raw[:10]}
            )
        for unit, rows in gathered.items():
            if len(rows) < 2:
                continue
            # 同单位 ≠ 同语义：**只有当量纲写在行名里（泛值列）时**才按"指标主题"拆子族。
            # 表头本身就是指标名（市占率/毛利率/门店数）时，各行是同一指标的不同主体，
            # 拆了反而把主体名之间不共享 2 字的行全部丢光（见 `_generic_value_header`）。
            if "%" in (unit or "") and _generic_value_header(htxt):
                _subs = _split_by_subject(rows)
                _solo: list[dict[str, Any]] = []
                for sub in _subs:
                    if len(sub) >= 2:
                        per_col.append({"col": c, "title": htxt, "unit": unit, "rows": sub[:6],
                                        "rows_total": len(sub)})
                    else:
                        # **单行族不再整族丢弃**（2026-09-16）。
                        # 丢弃是静默的：8 行的表只画出 4 行，读者以为"这张表就 4 行"。
                        # 单行族**合并成一张图**，图题写明"其余单行指标"——
                        # 同单位共轴（本模块的准入规则就是"同单位"），每根柱各自带名，
                        # 读者看到的是读数，不是被隐去的读数。
                        _solo.extend(sub)
                if _solo:
                    per_col.append({"col": c, "title": htxt, "unit": unit,
                                    "rows": _solo[:6], "rows_total": len(_solo), "solo": True})
                continue
            per_col.append({"col": c, "title": htxt, "unit": unit, "rows": rows[:6],
                                 "rows_total": len(rows)})
    return per_col


def _two_grams(name: str) -> set[str]:
    s = re.sub(r"[\s，,。．.、:：()（）【】%％]", "", name or "")
    return {s[i : i + 2] for i in range(max(0, len(s) - 1))}


def _split_by_subject(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按"共享连续 2 字"把同单位行拆成指标族（贪心：能并入已有族就并入）。"""
    subs: list[dict[str, Any]] = []  # {"grams": set, "rows": [...]}
    for r in rows:
        g = _two_grams(r.get("name") or "")
        placed = False
        for s in subs:
            if s["grams"] & g:
                s["grams"] |= g
                s["rows"].append(r)
                placed = True
                break
        if not placed:
            subs.append({"grams": set(g), "rows": [r]})
    return [s["rows"] for s in subs]


def _common_subject(names: list[str]) -> bool:
    """组内行名是否至少共享一个连续 2 字（判"同一指标族"）。保留供外部调用。"""
    grams: set[str] | None = None
    for nm in names:
        g = _two_grams(nm)
        grams = g if grams is None else (grams & g)
        if not grams:
            return False
    return bool(grams)


def metric_chart_pngs(md_cells_rows: list[list[str]], table_title: str = "") -> list[dict[str, Any]]:
    """→ [{unit, title, png(bytes)}]；matplotlib 缺失/异常一律返回 []"""
    header = md_cells_rows[0] if md_cells_rows else []
    body = md_cells_rows[1:] if md_cells_rows else []
    groups = _group_rows(body, header)
    if not groups:
        return []
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager as _fm, pyplot as plt

        _name = None
        for _fp in (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
        ):
            try:
                _fm.fontManager.addfont(_fp)
                _name = _fm.FontProperties(fname=_fp).get_name()
                break
            except Exception:
                continue
        plt.rcParams["font.sans-serif"] = ([_name] if _name else []) + [
            "Microsoft YaHei", "SimHei", "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    try:
        for g in groups:
            rows = g["rows"]
            fig, ax = plt.subplots(figsize=(7.4, 2.9), dpi=150)
            names = [r["name"][:10] for r in rows]
            vals = [r["value"] for r in rows]
            ax.bar(names, vals, color="#3b82f6", width=0.55)
            for x, r in zip(range(len(rows)), rows):
                # 负值柱向下长，标注要放到柱尖**下方**，否则文字压在柱子里
                _neg = r["value"] < 0
                ax.text(x, r["value"], r["label"], ha="center",
                        va="top" if _neg else "bottom", fontsize=8)
            # 图题用**该列真实表头 + 表名**。原实现恒为"核心指标对比"——
            # 一份报告里七八张图全叫同一个名字，等于没有标题。
            _ct = (g.get("title") or "").strip()
            _tt = (table_title or "").strip()
            if _ct and _tt:
                _cap = f"{_tt} · {_ct}"
            else:
                _cap = _ct or _tt or "指标对比"
            # 图题 = 表名 · 列名（单位）。**这个字符串同时用于两处**：画在图上的
            # `set_title`，以及 markdown 的 `![…]` alt 文本。原实现只在图上带单位、
            # alt 文本用不带单位的 `_cap`，于是同一张表里两组不同单位的图
            # **alt 文本一模一样**——.md 导出里读者看到两张同名图，无从分辨。
            # 病案（2026-09-15 深访实跑）：13 张图里有两对同名
            # （「📊 核心指标一览表 · 数值」×2、「数值/金额」×2），
            # 而图上其实分别标着（杯）与（%）——**信息在，只是没传到读者那一端**。
            _full = f"{_cap}（{g['unit'] or '—'}{'｜其余单行指标合并' if g.get('solo') else ''}）"[:46]
            # **标题里的 emoji/符号要先摘掉**：表名来自报告标题（如「📊 核心指标一览表」），
            # 而 matplotlib 用的中文字体（微软雅黑/黑体）没有这些码位，画出来是一个
            # 空心方框（tofu）。实测告警：`Glyph 128202 (\N{BAR CHART}) missing`。
            # 同一类事故此前在 PDF 法定标识上出现过（渲染成空白），已修；
            # 这里同法处理——**图上的标题必须真的能被看见**。
            _full = _glyph_safe(_full)
            ax.set_title(_full, fontsize=11)
            # ⚠️ 纵轴**必须容得下负值**（2026-09-16）：原实现写死 `set_ylim(0, max*1.18)`，
            # 而 `_num` 现在收负号了——一根 −15 万的柱子落在 0 以下会被**整个裁掉**，
            # 图上什么都不显示，读者只会看到一个少了一根柱的对比图。
            _lo = min(vals + [0.0])
            _hi = max(vals + [0.0])
            _span = (_hi - _lo) or (abs(_hi) or 1.0)
            ax.set_ylim(_lo - 0.02 * _span, _hi + 0.18 * _span)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=0.3)
            ax.set_axisbelow(True)
            ax.tick_params(axis="x", labelsize=8)
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
            # **同表内题名去重兜底**：万一列名与单位仍撞车（如两列的列名都是"数值"、
            # 单位都抽不出），加序号，让读者至少能指认"这是第几张"。
            _cap_final = _full
            _seen = {o["title"] for o in out}
            _k = 2
            while _cap_final in _seen:
                _cap_final = f"{_full[:42]} ({_k})"
                _k += 1
            # 画了几行 / 本组共几行，一并带给调用方（2026-09-16）：
            # `_group_rows` 里 `rows[:6]` 是**静默截断**——9 行的表只画 6 根柱，
            # 图题不写、图注不写，读者会以为"这张表就 6 行"，
            # 而上方表格明明是 9 行 → 正中"图里的点数 ≠ 表里的条数"那个老病。
            out.append({"unit": g["unit"], "title": _cap_final, "png": buf.getvalue(),
                        "rows_total": int(g.get("rows_total") or len(rows)),
                        "rows_drawn": len(rows)})
    except Exception:
        return []
    return out


# ============================================================
# markdown 内联图表（.md 导出用）
# ============================================================
_MD_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")

# 常见中文字体覆盖不到的码位（emoji / 图形符号 / 变体选择符）——留在图题里会被画成空心方框。
# 与 `app.py` 的 PDF 法定标识同一条纪律：**宁可少一个装饰符号，也不要一个看不见的方框**。
_NON_RENDERABLE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002B00-\U00002BFF]"
)


def _glyph_safe(s: str) -> str:
    """摘掉中文字体画不出来的字符（emoji/图形符号），并压掉多余空白。"""
    return re.sub(r"\s{2,}", " ", _NON_RENDERABLE.sub("", s or "")).strip()


def decorate_md_charts(md: str, max_charts: int = 12, skip_metrics_table: bool = True) -> str:
    """在 markdown 报告的可绘图数据表**后面**插入 base64 内联图表

    为什么必须做：`.md` 导出一直是**裸正文**——表格一个不少、图一张没有。
    用户导出 md 打开一看就是"纯文字文档"，而 docx/pdf 早就有图。**同一次调研，
    换个格式就没了图**，这是导出管线的不一致，不是能力缺失。

    markdown 原生支持 `![](data:image/png;base64,...)`，Typora / VSCode / GitHub /
    飞书 / 语雀 都能直接渲染，不依赖外部文件。
    """
    if not md or max_charts <= 0:
        return md
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = 0
    in_audit = False   # 进入审计附录后不再出图（见 _AUDIT_HEAD 说明）
    while i < len(lines):
        s = lines[i]
        if _AUDIT_HEAD.match(s.strip()):
            in_audit = True
        if s.strip().startswith("|") and s.count("|") >= 3:
            block: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].count("|") >= 3:
                block.append(lines[i])
                i += 1
            rows: list[list[str]] = []
            for b in block:
                cells = [c.strip() for c in b.strip().strip("|").split("|")]
                if _MD_SEP.match(b):          # 分隔行 |---|---|
                    continue
                rows.append(cells)
            # 表名 = 表格上方最近的标题/加粗行（渲染成图题的一部分，便于对照）
            _tname = ""
            for _j in range(len(out) - 1, max(0, len(out) - 4), -1):
                _c = re.sub(r"^[#>\s*]+", "", out[_j]).strip()
                _c = re.sub(r"\*+$", "", _c).strip()
                if 3 <= len(_c) <= 30:
                    _tname = _c
                    break
            out.extend(block)
            if n < max_charts and not in_audit and len(rows) >= 3:
                hdr_join = "".join(rows[0])
                if skip_metrics_table and "指标" in hdr_join and "数值" in hdr_join:
                    continue  # 核心指标表已在交付物头部作图，正文不重复
                try:
                    from core.report_charts import metric_chart_pngs as _mk
                except Exception:
                    _mk = None
                if _mk:
                    import base64

                    # **两道静默截断，都必须留痕**（2026-09-16）：
                    # ① `[:3]` —— 一张表有 4 个可绘列时，第 4 张图直接消失；
                    # ② `_group_rows` 里的 `rows[:6]` —— 9 行的表只画前 6 行。
                    # 两者原先都不写进任何地方，读者看到的是"图比表少"却说不出少了什么。
                    _all_ch = _mk(rows, _tname)
                    _shown = _all_ch[:3]
                    if len(_all_ch) > len(_shown):
                        out.append("")
                        out.append(
                            f"> 本表共可出 **{len(_all_ch)}** 张图，"
                            f"上列前 {len(_shown)} 张（防一份报告变图表堆）。"
                        )
                    for ch in _shown:
                        if n >= max_charts:
                            break
                        b64 = base64.b64encode(ch["png"]).decode("ascii")
                        out.append("")
                        out.append(f"![{ch['title']}](data:image/png;base64,{b64})")
                        _rt, _rd = int(ch.get("rows_total") or 0), int(ch.get("rows_drawn") or 0)
                        if _rt > _rd > 0:
                            out.append("")
                            out.append(
                                f"> 图中为 **{_rd}** 行，本组共 **{_rt}** 行"
                                f"（其余 {_rt - _rd} 行未画入，完整数据见上表）。"
                            )
                        out.append("")
                        n += 1
            continue
        out.append(s)
        i += 1
    return "\n".join(out)
