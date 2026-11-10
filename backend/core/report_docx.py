"""研策 · 专业版 Word 导出（对照咨询级市场调研报告模板）

模板骨架（样例：《Starbucks China Market Research Report 2026.docx》）：
  封面（品牌 + 中文题名 + 英文副题 + 研究时点）→ 正文（数字编号章节 + 高亮框 +
  规范表格 + 行内加粗）→ 页脚页码。
输入：报告 markdown（研策 report_final）。所有 md 记号被净化为 Word 原生样式，
不再出现 "**/#/|" 之类源码符号。
"""
from __future__ import annotations

import re
from datetime import date
from io import BytesIO

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

_CN_FONT = "微软雅黑"
_BRAND = RGBColor(0x1F, 0x4E, 0x8C)     # 深蓝（标题/品牌）
_GRAY = RGBColor(0x6B, 0x72, 0x80)      # 次级文字
_ACCENT_BG = "DEE9F7"                    # 高亮框底色（浅蓝）
_HEAD_BG = "EEF3FA"                      # 表格表头底色
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿←-⇿⬀-⯿️‍]"
)
_CN_NUM = "零一二三四五六七八九十"


def _cn_num(n: int) -> str:
    """1→一，11→十一，21→二十一…（够报告章节用）"""
    if n <= 0:
        return "零"
    if n < 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + (_CN_NUM[n - 10] if n > 10 else "")
    tens, ones = divmod(n, 10)
    return _CN_NUM[tens] + "十" + (_CN_NUM[ones] if ones else "")


def _set_cn_font(obj, name: str = _CN_FONT) -> None:
    """同时设置 ascii/eastAsia 字体（中文不乱码）"""
    try:
        obj.name = name
    except Exception:
        pass
    try:
        rpr = obj.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn("w:eastAsia"), name)
    except Exception:
        pass


def _strip_md(text: str) -> str:
    """段落级净化：emoji/ref/span/链接/加粗记号 全部转成纯文本词面"""
    t = re.sub(r"<ref>(\d+)</ref>", r"[\1]", text or "")
    t = re.sub(r"</?span[^>]*>", "", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)  # 链接只留文字
    t = _EMOJI.sub("", t)
    t = t.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", t).strip()


def _add_runs(p, text: str) -> None:
    """行内富文本：**加粗** → Word bold run（净化为正规排版，无源码记号）"""
    t = re.sub(r"<ref>(\d+)</ref>", r"[\1]", text or "")
    t = _EMOJI.sub("", t)
    parts = re.split(r"(\*\*[^*]+\*\*)", t)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = p.add_run(part[2:-2])
            r.bold = True
            _set_cn_font(r.font)
        else:
            r = p.add_run(part.replace("`", ""))
            _set_cn_font(r.font)


def _shade(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _add_field(paragraph, instr: str) -> None:
    """Word 域（页码 PAGE）"""
    r = paragraph.add_run()
    fld1 = OxmlElement("w:fldChar"); fld1.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText"); instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    fld2 = OxmlElement("w:fldChar"); fld2.set(qn("w:fldCharType"), "separate")
    t = OxmlElement("w:t"); t.text = "1"
    fld3 = OxmlElement("w:fldChar"); fld3.set(qn("w:fldCharType"), "end")
    r._r.append(fld1); r._r.append(instr_el); r._r.append(fld2)
    r._r.append(t); r._r.append(fld3)
    _set_cn_font(r.font)


def _cover(doc: Document, question: str, title: str) -> None:
    for _ in range(5):
        doc.add_paragraph()
    brand = doc.add_paragraph()
    brand.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rb = brand.add_run("研策 YANCEC · 市场调研数字员工")
    rb.font.size = Pt(12)
    rb.font.color.rgb = _BRAND
    rb.bold = True
    _set_cn_font(rb.font)

    main = question or title or "市场调研报告"
    # 长问题折行：按 20 字一行居中排
    for i in range(0, len(main), 20):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(main[i:i + 20])
        r.font.size = Pt(24)
        r.bold = True
        r.font.color.rgb = RGBColor(0x17, 0x24, 0x33)
        _set_cn_font(r.font)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rs = sub.add_run("决策型市场调研报告 · Decision-grade Market Research")
    rs.font.size = Pt(11)
    rs.font.color.rgb = _GRAY
    _set_cn_font(rs.font)

    doc.add_paragraph()
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rm = meta.add_run(f"研究时点：{date.today().isoformat()}　｜　数据截止：检索当日公开来源（口径见附录）")
    rm.font.size = Pt(10)
    rm.font.color.rgb = _GRAY
    _set_cn_font(rm.font)
    doc.add_page_break()


def _add_chart_pics(doc, tgt) -> int:
    """任意"可绘图的数据表"之后内嵌柱状图（同单位分组 + 异义守卫；缺 matplotlib 静默跳过）。

    返回实际插入的图数（供调用方计数上限）。
    """
    try:
        from core.report_charts import metric_chart_pngs
    except Exception:
        return 0
    try:
        pngs = metric_chart_pngs(tgt)
    except Exception:
        return 0
    added = 0
    for k, it in enumerate(pngs):
        try:
            doc.add_picture(BytesIO(it["png"]), width=Cm(15.5))
            if doc.paragraphs:
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = cap.add_run(
                f"图{k + 1}　数据对比（{it.get('unit') or '—'}）｜数据来源：见对应表格来源列"
            )
            r.font.size = Pt(8.5)
            r.font.color.rgb = _GRAY
            _set_cn_font(r.font)
            added += 1
        except Exception:
            continue
    return added


def _ensure_footer(doc: Document) -> None:
    footer = doc.sections[0].footer
    fp = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run("研策 · 市场调研数字员工　第 ")
    r.font.size = Pt(8); r.font.color.rgb = _GRAY
    _set_cn_font(r.font)
    _add_field(fp, " PAGE ")
    r2 = fp.add_run(" 页")
    r2.font.size = Pt(8); r2.font.color.rgb = _GRAY
    _set_cn_font(r2.font)


def _collect_table(lines: list[str], i: int) -> tuple[list[list[str]], int]:
    block = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        block.append(lines[i].strip())
        i += 1
    rows = []
    for ln in block:
        cells = [c.strip().strip("*`").strip() for c in ln.strip().strip("|").split("|")]
        rows.append(cells)
    return rows, i


def build_report_docx(
    md: str, title: str = "研策调研报告", question: str = "", auto_chapter: bool = True
) -> bytes:
    """markdown 报告 → 专业版 Word（封面 + 编号章节 + 高亮框 + 规范表格 + 页码）

    `auto_chapter=False` → **不套报告的章节编号**，标题按原层级出。
    用于方案书 / 作品简述这类**非报告文档**：它们的 H1 是文档标题本身，
    套上"一、二、"会把标题也编进去，二级再变"二、0. 一句话定位"——
    读起来像排版事故（实测踩过：拿本函数直接渲染参赛方案书就是这个结果）。
    报告要编号是因为咨询级模板以"一~十三"分章；方案书不需要。
    """
    doc = Document()
    # 默认字体
    normal = doc.styles["Normal"]
    normal.font.name = _CN_FONT
    normal.font.size = Pt(10.5)
    rpr = normal.element.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    rf.set(qn("w:eastAsia"), _CN_FONT)

    _cover(doc, question, title)
    _ensure_footer(doc)
    _chart_count = 0
    # 图表总数上限：**市场调研报告本就该"一表一图"**。
    # ⚠️ 这个数**不再写字面量**（2026-09-16）：原先这里写 12、`app.py` 的 PDF 写 4、
    # md 路径写 16 —— 同一份报告导 Word 12 张、导 PDF 4 张，而两处注释还各自声称
    # "与对方对齐"。**注释互相引用而没有单一出处，两边迟早各说各话。**
    from core.report_charts import MAX_EXPORT_CHARTS as _MAX_CHARTS

    lines = (md or "").split("\n")
    chapter = 0  # 一级章节序号（对应 '#' 或 '##' 标题行）
    in_fence = False  # 是否在 ``` 围栏代码块内（见下方围栏分支）
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        # ── 围栏代码块（2026-09-18 新增）─────────────────────────────────
        # 病案：本函数此前**完全没有围栏概念**，`#` 开头的行一律按 markdown 标题渲染。
        # 后果实测于《产品说明书》的 Word 成品：§15.2 的复算命令块里
        # **10 行 `#` 注释全部变成 Word 标题**——导航窗格里多出 10 个假条目，
        # 而那个块是**给人照着敲的命令**，不是文档结构。
        # 更坏的是它同时把 `#` 行的**语义**也改了：读者在导航栏看到
        # 「本件读数：21 份 × 12 类 = 0 项」当标题，会以为是文档的一节。
        # 判据：围栏内**不解析任何 markdown**，等宽字体原样出。
        if s.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            p = doc.add_paragraph()
            r = p.add_run(raw.rstrip())
            r.font.name = "Consolas"
            r.font.size = Pt(9)
            r.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
            i += 1
            continue
        # 表格块
        if s.startswith("|") and "|" in s[1:]:
            rows, i = _collect_table(lines, i)
            if len(rows) >= 2:
                ncols = max((len(r) for r in rows), default=1)
                tgt = []
                for r in rows:
                    if len(r) == ncols and all(
                        re.fullmatch(r":?-{2,}:?|", c.strip()) for c in r
                    ):
                        continue
                    tgt.append([(r[c] if c < len(r) else "") for c in range(ncols)])
                if tgt:
                    tbl = doc.add_table(rows=len(tgt), cols=ncols)
                    tbl.style = "Table Grid"
                    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                    for ri, row in enumerate(tgt):
                        for ci in range(ncols):
                            cell = tbl.cell(ri, ci)
                            cp = cell.paragraphs[0]
                            _add_runs(cp, _strip_md(row[ci]))
                            for run in cp.runs:
                                run.font.size = Pt(9.5)
                                _set_cn_font(run.font)
                            if ri == 0:
                                for run in cp.runs:
                                    run.bold = True
                                _shade(cell, _HEAD_BG)
                    doc.add_paragraph()
                    hdr0 = "".join(tgt[0]) if tgt else ""
                    is_metrics = (
                        bool(hdr0)
                        and "指标" in hdr0
                        and "数值" in hdr0
                        and ("来源机构" in hdr0 or "年份" in hdr0)
                    )
                    # 任意"可绘图的数据表"（首列名称 + 某列单位一致 ≥2 行）都在表后内嵌图，
                    # 不再只认核心指标表——治"整份报告只有一张柱状图"。不可绘的表自然返回 0 张。
                    if _chart_count < _MAX_CHARTS:
                        _chart_count += _add_chart_pics(doc, tgt)
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            depth = len(m.group(1))
            text = _strip_md(m.group(2))
            if not text:
                i += 1
                continue
            if depth <= 2:
                # 一级/二级标题都作"章节"编号（摘要/正文模块/附录平级，类似样例一~十三）
                p = doc.add_heading(level=1 if (auto_chapter or depth == 1) else 2)
                if auto_chapter:
                    chapter += 1
                    r = p.add_run(f"{_cn_num(chapter)}、{text}")
                    r.bold = True
                    r.font.size = Pt(15)
                else:
                    # 非报告文档：标题原样出，只按层级区分字号
                    r = p.add_run(text)
                    r.bold = True
                    r.font.size = Pt(16 if depth == 1 else 13.5)
                r.font.color.rgb = _BRAND
                _set_cn_font(r.font)
            else:
                p = doc.add_heading(level=2)
                r = p.add_run(text)
                r.font.size = Pt(12.5)
                r.font.color.rgb = RGBColor(0x2B, 0x3A, 0x4D)
                _set_cn_font(r.font)
            i += 1
            continue
        # 引用 → 高亮框（1×1 表格 + 浅蓝底，样例"核心结论/关键观察"式 callout）
        if s.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = " ".join(q for q in quote_lines if q)
            if body:
                call = doc.add_table(rows=1, cols=1)
                call.alignment = WD_TABLE_ALIGNMENT.CENTER
                cell = call.cell(0, 0)
                _shade(cell, _ACCENT_BG)
                cp = cell.paragraphs[0]
                _add_runs(cp, _strip_md(body))
                for run in cp.runs:
                    run.font.size = Pt(10.5)
                    _set_cn_font(run.font)
                doc.add_paragraph()
            continue
        # 分隔线/列表/普通段落
        if re.fullmatch(r"-{3,}|\*{3,}", s):
            i += 1
            continue
        bullet = False
        if s.startswith(("- ", "* ", "• ")):
            bullet = True
            s = s[2:]
        elif s.startswith("> "):
            s = s[2:]
        if bullet:
            p = doc.add_paragraph(style="List Bullet")
        else:
            p = doc.add_paragraph()
        _add_runs(p, _strip_md(s))
        i += 1

    buf = BytesIO()
    # 隐式标识（法定）：文件元数据里写"AI 生成合成内容 / 服务提供者 / 内容编号"。
    # Word 是可以被单独转发的一份文件——正文里的显式标识可能被截图裁掉，
    # 元数据不会。失败只丢元数据，不影响文件。
    try:
        from core.ai_label import apply_file_meta

        apply_file_meta(doc.core_properties, md, title=title, question=question)
    except Exception:
        pass
    doc.save(buf)
    return buf.getvalue()
