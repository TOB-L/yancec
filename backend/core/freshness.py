"""研策 · 检索新鲜度门禁（D2 / Q0）—— 治"决策报告数据停在旧年、漏掉近一年重大事件"

病案（N6）：生成时点 2026-09 的星巴克下沉报告，核心依据停在 2023-2024，
对 2025-11 星巴克首次让渡中国业务控制权（博裕）这类最大变量零提及。
根因：检索词锚定旧叙事 + 无"近 12 个月来源"检测 → 旧文章挤掉新动态。

方案（确定性下沉，不加 LangGraph 节点 / 不改 TaskState / 不碰 <ref> 编号）：
1. staleness_gap()：扫 sources[].publish_date / title / publisher / excerpt 与 claim 里的年份，
   取"最新来源年份"，算与当前年份的差距；一律剔除 >now_year 的年份（2030 是预测目标期）。
   无任何可靠年份 → None（不硬判）。
2. is_stale()：双判据——有可靠年份则年份优先（gap>=2 视为旧）；无可靠年份的
   **决策/时效敏感题**（该不该/能不能/进入/开店/下沉/趋势/为什么…）回退判定为"需补检"。
   理由：决策报告必须有时效新鲜度，拿不准时宁补一轮（块是提示不是断言，成本=1 次检索）。
3. build_freshness_block()：is_stale()==True 时补检近一年动态，产出"时效补检"md 块：
   - v0.9.47 双探针：主探针"主题头 + 最新动态/近一年"之外，再补一枪经济事件定向
     （"出售/收购/股权/转让"）——N6 病案里博裕交易对"最新动态"泛词不敏感，定向词才命中。
   - v0.9.47 不再静默消失：决策题一旦触发，必有可见产物——命中 → 真实线索列表（真实可点、
     明示"非系统生成结论、正文请点开核验"）；搜到页但无一条过主题/时效门 → 输出**诚实"补检
     未命中"提示**（并说明不得静默视为"无重大变化"）；检索彻底失败/空（无法区分故障与无结果）
     → 仍返回 "" 不硬塞。
   - v0.9.47 全程 INFO 日志（各探针返回条数/候选/命中行数），再出问题能定位。
4. 链接照常走 deliver 既有 verify_urls + 坏链清洗；补检无可用结果不阻断交付。

验证：python core/freshness.py（内置离线自测，mock 检索，无网络）。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"(20[0-2][0-9])")

# 决策/时效敏感题型词：无可靠年份时的回退判据（命中=拿不准该不该补检 → 补）
_DECISION_KW = [
    "该不该", "能不能", "应不应该", "是否值得", "值得", "值不值得", "机会",
    "进入", "开店", "加盟", "扩张", "布局", "下沉", "能不能做",
    "为什么", "趋势", "未来", "增速", "放缓", "前景", "建议",
]

# 经济事件定向词（双探针 q2）：治 N6 病案——重大股权交易/出售对"最新动态"泛词不敏感
_EVENT_KW = ["出售", "收购", "股权", "转让", "私有化", "控制权", "易主", "接盘", "并购", "融资"]


def is_decision_question(question: str) -> bool:
    """是否决策/时效敏感题（该不该/能不能/进入/开店/下沉/趋势/为什么…）。

    公开给 graph.node_deliver 的门禁复用（时效块空→诚实标记），避免两处 keyword 漂移。
    """
    _q = (question or "").strip()
    return bool(_q) and any(_k in _q for _k in _DECISION_KW)


def _years_in(text: str, now_year: int) -> list[int]:
    """文本里 ≤now_year 的 20xx 年份（预测目标期 >now_year 一律剔除）"""
    out: list[int] = []
    for _m in _YEAR_RE.finditer(text or ""):
        _y = int(_m.group(1))
        if 2000 <= _y <= now_year:
            out.append(_y)
    return out


def staleness_gap(evidences: list[Any] | None, now_year: int | None = None) -> int | None:
    """核心证据"最新来源年份"与当前年份的差距。

    年份来源（按优先级）：sources[].publish_date 前4位（真实 YYYY-MM-DD 才算）→
    sources[].title/publisher/excerpt → evidence.claim。一律剔除 >now_year 预测期年份。
    返回 None → 无任何可靠年份信息（不硬判）；n>=2 → 明显偏旧。
    """
    if now_year is None:
        now_year = date.today().year
    years: list[int] = []
    for ev in (evidences or []):
        try:
            _claim = str(getattr(ev, "claim", "") or "")
            # 总是扫 claim 年份（真实 Evidence 即便带 sources，claim 往往也含数据年份）
            years.extend(_years_in(_claim, now_year))
            for _s in getattr(ev, "sources", None) or []:
                _pd = str(getattr(_s, "publish_date", "") or "").strip()
                _ok_pd = len(_pd) >= 10 and _pd[:4].isdigit()  # 只要真日期（YYYY-MM-DD…），"实时"/"" 不算
                if _ok_pd:
                    _y = int(_pd[:4])
                    if 2000 <= _y <= now_year:
                        years.append(_y)
                        continue
                _blob = " ".join(
                    [
                        str(getattr(_s, "title", "") or ""),
                        str(getattr(_s, "publisher", "") or ""),
                        str(getattr(_s, "excerpt", "") or "")[:200],
                    ]
                )
                years.extend(_years_in(_blob, now_year))
        except Exception:
            pass
    if not years:
        return None
    return now_year - max(years)


def is_stale(
    evidences: list[Any] | None,
    question: str,
    now_year: int | None = None,
) -> bool:
    """是否触发时效补检。

    决策/时效敏感题（该不该/能不能/进入/开店/下沉/趋势/为什么…）+ 有真实证据 → **无条件补检**：
    2026-09-08 v0.9.45 修正——此前按证据最新年份判"够新鲜"会漏检：个别 2026 来源（如跨界价格战
    新闻）会掩盖"核心指标停在 2023-24 / 近一年重大事件缺席（2025-11 星巴克-博裕）"。
    决策报告必须做近一年动态例行补检，成本=1 次真实检索，块内标题来自检索、非系统断言。
    描述/问答类题仍走年份门禁（gap>=2 才补），省检索成本。
    空证据 = 本次检索本身已降级（不补检，避免为一份空报告多花一次检索）。
    """
    if now_year is None:
        now_year = date.today().year
    _q = (question or "").strip()
    _is_decision = bool(_q) and any(_k in _q for _k in _DECISION_KW)
    _evs = [e for e in (evidences or []) if e is not None]
    if _is_decision:
        return bool(_evs)  # 决策题 + 有证据 → 无条件补检
    g = staleness_gap(evidences, now_year=now_year)
    if g is not None:
        return g >= 2
    return False


def _subject_head(question: str) -> str:
    """问题主题头（≤16 字，截问号前、去常见套话）——与 researchers 里同思路的本地轻量版"""
    try:
        from agents.researchers import _subject_head as _sh

        _h = (_sh(question) or "").strip()
        if _h:
            return _h
    except Exception:
        pass
    _q = re.split(r"[？?。！!，,]", (question or "").strip())[0]
    for _p in ["我想", "我要", "请问", "帮我", "分析一下", "调研一下", "看看", "想知道"]:
        _q = _q.replace(_p, "")
    return (_q or (question or ""))[:16]


# 时效关键词：标题命中越多越像"近一年动态"（宁多勿漏，反正会点开核验）
_TIMELY_KW = [
    "2025", "2026", "出售", "收购", "股权", "融资", "门店", "增速", "下降", "放缓",
    "最新", "发布", "报告", "协议", "完成", "合作", "计划", "目标", "上调", "下调",
    "开店", "闭店", "政策", "新规", "季度", "财报",
]


def _overlap_score(title: str, subject: str) -> int:
    """主题相关：subject 与 title 的中文 2-gram 重叠计数（防检索到全跑题文）"""
    if not subject:
        return 0
    _sg = {title[i : i + 2] for i in range(len(title) - 1)}
    _ss = {subject[i : i + 2] for i in range(len(subject) - 1)}
    return len(_sg & _ss)


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _search_configured() -> bool:
    """是否有真实检索通道（博查/Bing Web API）——无则跳过补检（宁缺勿误，防离线测试撞网落到 Bing 抓取）"""
    try:
        from core.config import get_settings

        _s = get_settings()
        return bool(getattr(_s, "bocha_api_key", "") or getattr(_s, "bing_search_api_key", ""))
    except Exception:
        return False


def _score_title(title: str, subject: str) -> int:
    """候选取舍分：主题 2-gram 重叠×2 + 时效词命中 + 近两年年份强信号"""
    sc = _overlap_score(title, subject) * 2
    sc += sum(1 for _k in _TIMELY_KW if _k in title)
    if "2025" in title or "2026" in title:
        sc += 3
    return sc


def _probe(query: str, subject: str) -> tuple[list[tuple[int, str, str]], bool]:
    """单探针补检索 → ([(score,title,url)], 是否有任何原始返回)。

    返回元组第二项为 had_raw：用于区分"完全空（故障/无结果，返回 '' 不硬塞）"
    与"有返回但无一条过主题/时效门（输出诚实'未命中'提示）"。
    """
    try:
        from mcp.search import web_search_urls

        raw = web_search_urls(query, n=8) or []
    except Exception as e:
        logger.info("新鲜度补检索失败(%s): %s", query[:18], e)
        return [], False
    had_raw = bool(raw)
    try:
        from agents.researchers import _is_clean_url as _clean
    except Exception:
        _clean = lambda u: True  # 健康闸门不可用则放行（后续 deliver 端仍有 verify + 坏链清洗兜底）
    out: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for _r in raw:
        if not isinstance(_r, dict):
            continue
        _u = str(_r.get("url") or "").strip()
        _t = str(_r.get("title") or _r.get("site_name") or "").strip()
        if not (_u.startswith("http") and _t and len(_t) >= 6):
            continue
        if _u in seen:
            continue
        seen.add(_u)
        # 健康闸门（词典/聚合/黑名单 → 弃）
        try:
            if not _clean(_u):
                continue
        except Exception:
            pass
        out.append((_score_title(_t, subject), _t, _u))
    logger.info("时效补检探针 '%s': raw=%d → 候选=%d", query[:22], len(raw), len(out))
    return out, had_raw


def build_freshness_block(
    question: str,
    evidences: list[Any] | None,
    max_items: int = 4,
    _now: date | None = None,
) -> str:
    """新鲜度门禁主入口：偏旧/决策敏感 → 补检近一年动态 → 返回"时效补检"md 块（空串=未触发/检索彻底失败）。

    _now：仅测试注入；生产用 date.today()。
    """
    today = _now or date.today()
    if not is_stale(evidences, question or "", now_year=today.year):
        return ""
    if not _search_configured():
        return ""  # 无真实检索 key → 不做补检（不落到 Bing 抓取，保离线确定性）
    subj = _subject_head(question)
    g = staleness_gap(evidences, now_year=today.year)
    latest_year = (today.year - g) if g is not None else None
    # 双探针（v0.9.47）：主探针(泛"最新动态") + 经济事件定向(治 N6 博裕病案：泛词不敏感)
    c1, had1 = _probe(f"{subj} 最新动态 {today.year} 近一年", subj)
    c2, had2 = _probe(f"{subj} {' '.join(_EVENT_KW[:4])} {today.year}", subj)
    had_raw = had1 or had2
    # 合并去重（同 URL 留高分）
    best: dict[str, tuple[int, str, str]] = {}
    for _it in (c1 + c2):
        _sc, _t, _u = _it
        if _u not in best or _sc > best[_u][0]:
            best[_u] = _it
    cands = sorted(best.values(), key=lambda x: -x[0])
    # v0.9.47 放宽硬门槛：不再强制"标题必须含时效词"，改为综合分 >=2
    # （主题强相关 2-gram>=1 即 2 分；或含任一时效词）——防合法近一年新闻因标题不带"2025/门店"字样被滤空
    rows = [(t, u) for _sc, t, u in cands if _sc >= 2][:max_items]
    # ⚠️ 这些链接会**直接写进交付报告的正文**，所以必须与正文引用过**同一套门**
    # （2026-09-14 审计）。原先一个门都不过：栏目首页、七年前的旧文、低质农场
    # 只要综合分 >=2 就能出现在《时效补检》块里——而这一块的标题就是"以下为**最新**线索"，
    # 一个 2019 年的链接挂在那里，比不补检更伤。
    try:
        from core.source_quality import screen_citation

        _kept = []
        for _t, _u in rows:
            try:
                _why = screen_citation(_u, _t, "", industry="", question=question)
            except Exception:
                _why = None
            if _why:
                logger.info("时效补检·来源门拦下 %s：%s", _u[:70], _why)
                continue
            _kept.append((_t, _u))
        rows = _kept
    except Exception as _e:
        logger.info("时效补检·来源门异常(降级放过)：%s", _e)
    logger.info("时效补检合计候选=%d 命中行=%d", len(cands), len(rows))
    # 措辞自适应：真偏旧 → 明说偏旧；决策题无条件补检但年份不算旧 → 说"例行补检"，不谎称检测到偏旧
    _q = (question or "").strip()
    _is_decision = bool(_q) and any(_k in _q for _k in _DECISION_KW)
    if g is not None and g >= 2:
        _lead = f"系统检测到核心来源最新到 **{latest_year} 年**（距当前 ≥2 年），已补检近一年公开动态"
    elif _is_decision:
        _lead = "决策型调研例行补检近一年公开动态（确保不漏近一年重大事件，如股权出售/并购/政策变化）"
    else:
        _lead = "已自动补检近一年公开动态"
    if not rows:
        if not had_raw:
            return ""  # 两探针均无返回（故障/无结果，无法区分）→ 不硬塞，宁缺勿假
        # 有检索返回但无一过主题/时效门 → 诚实披露，不静默视为"无重大变化"
        logger.info("时效补检: 有返回但无可用线索, 输出诚实提示")
        return "\n".join(
            [
                f"> 🔍 **时效补检（新鲜度门禁）**：本报告{_lead}，补检已执行，"
                "但返回结果中**无一条通过主题/时效核验**的可点线索——"
                "**不得据此静默视为'近一年无重大变化'**：若行业恰逢重大股权交易/并购/政策变更，"
                "请在正式决策前人工定向补查一次。",
                "",
                f"> 数据时点纪律：判断时点 {today.isoformat()}。",
            ]
        )
    lines = [
        f"> 🔍 **时效补检（新鲜度门禁）**：本报告{_lead}。以下为检索到的最新线索——"
        "**标题来自真实检索、非系统生成结论，正文请点开原文核验**：",
        "",
    ]
    for _t, _u in rows:
        _t = re.sub(r"\s+", " ", _t).strip()
        # **印出期**（2026-09-14 审计）：这一块叫"最新线索"，却只给标题和域名、
        # 一个日期都不印——读者无从判断"最新"是相对什么而言。
        # 抽不到就写「期未标注」，**不编一个日期**。
        _per = ""
        try:
            from core.data_passport import extract_period, extract_period_from_url

            _per = extract_period(_t) or extract_period_from_url(_u) or ""
        except Exception:
            _per = ""
        _tag = _per or "期未标注"
        lines.append(f"- 🔗 [{_t[:46]}]({_u})（{_domain_of(_u)[:30]} · {_tag}）")
    lines += _consumption_notice(rows, subj)
    lines += [
        "",
        f"> 数据时点纪律：判断时点 {today.isoformat()}；如引文核验发现死链将自动转文字，请以点开的原文为准。",
    ]
    return "\n".join(lines)


SUBJECT_TENSION_SENTINEL = "<!--YANCEC-SUBJECT-TENSION-->"
"""正文结论处要挂的哨兵。

为什么光把提示摆在报告头还不够（2026-09-17）：这一块的位置是**报告头部**，
而读者（和复核者）真正会读的是**执行摘要那句结论**。同一份报告里，
结论说"元气森林线下失速"、头部摆着"元气森林 2025 +26%"——两者相距几百行，
**结构上读者不会把它们连起来**。所以命中主体级反证时，除了就地披露，
再打一个哨兵，由 deliver 节点把同一句提醒钉到结论处。
"""


def _consumption_notice(rows: list[tuple[str, str]], subj: str = "") -> list[str]:
    """**这一块自己声明它没有被正文消费**——以及哪些线索必须人工并入。

    病案（2026-09-15 深访实跑，真实报告）：本模块的建模块理由（见文件头 N6）就是
    「生成时点 2026-09 的星巴克下沉报告，对星巴克让渡中国业务控制权这类最大变量零提及」。
    这次实跑**同一个病案原样复现**——补检确实抓到了「星巴克股权易主」并列在报告头部，
    而 22 万字的正文对「股权」二字**一次都没有提及**，核心结论仍是
    "星巴克等品牌应谨慎下沉"（一个前提是"星巴克自己决策"的结论）。

    为什么单靠"置顶展示"治不好：`build_freshness_block` 在 **deliver 节点**才运行，
    而各章正文在那之前早已写完——**这些线索在结构上不可能被正文采用**。
    把它摆在报告最前面，读者会以为分析已经吸收了它；实际没有。
    不把这件事说破，"补检"就从一道防线退化成一句免责声明。

    所以这里只做两件**确定性、可核对**的事，不做花哨的实体抽取：
      ① 说清结构事实——本块的检索发生在正文写作完成之后，故线索不可能已进入正文分析；
      ② 把含**重大公司事件词**（出售/收购/股权/易主/控制权…）的线索逐条点名，
         提醒人工确认正文结论是否仍然成立。判据复用 `_EVENT_KW`，与定向探针同源，
         不新增词表（**同一动作两处实现**正是本项目最常复发的一类缺陷）。
    """
    if not rows:
        return []
    _major = [(t, u) for t, u in rows if any(k in (t or "") for k in _EVENT_KW)]
    out = [
        "",
        "> ⚠️ **这一块没有被正文消费（如实说明）**：以上线索的检索发生在**各章正文写作完成之后**，"
        "因此它**不可能已经进入正文的分析与结论**。它的作用是把近一年动态摆到决策者面前，"
        "**不等于报告已经把它们算进去了**——请人工按需并入。",
    ]
    if _major:
        out.append(
            f"> 🔺 **其中 {len(_major)} 条是公司层面的重大事件**（股权/控制权/并购类），"
            "**这类事件会直接推翻以'该公司自己决策'为前提的结论**，务必先确认正文结论是否仍然成立："
        )
        for _t, _u in _major:
            _t = re.sub(r"\s+", " ", _t or "").strip()
            out.append(f"> 🔺 [{_t[:44]}]({_u})")
    # ★ **主体级反证**（2026-09-17 加，Claude A3 / GPT §18）：
    # 上面那条只判"是不是公司层面的重大事件"，**不判"讲的是不是本案这家公司"**。
    # 病案（元气森林跑）：补检抓到《元气森林 2025 年业绩同比增长 26%》，
    # 而正文结论正建立在"元气森林线下失速（-6.3%，2024Q1）→ 不建议重仓线下"之上。
    # 老的提示只说"有 3 条公司层面重大事件"——读者不会把它和"元气森林会不会已经好起来了"连起来。
    # **同一个主体、同一份报告、一个说跌一个说涨**，这才是必须点名的那一种。
    # 判据刻意只做机械的一件事：标题里出现**题干主体头**（≥2 字）。不做实体抽取、不猜同义。
    _s = (subj or "").strip()
    if _s and len(_s) >= 2:
        _mine = [(t, u) for t, u in rows if _s in (t or "")]
        if _mine:
            out.append(
                f"> 🔺🔺 **其中 {len(_mine)} 条讲的就是本案主体「{_s}」本人**——"
                "本报告的结论以该主体的经营表现为前提，而这几条线索**发生在正文成稿之后**："
                "**它们与正文结论是否存在张力，必须先确认，再据此行动**。"
                "（本条由系统的「主体级反证」检查给出，非模型判断。）"
            )
            out.append(SUBJECT_TENSION_SENTINEL)
            for _t, _u in _mine:
                # 归一化**先落成局部变量再进 f-string**：f-string 表达式里带反斜杠
                # 在 Python 3.12(PEP 701) 才合法，3.11 直接 SyntaxError——而
                # pyproject 声明的是 `requires-python = ">=3.11"`。写成内联版时，
                # 任何 3.11 解释器 import 本模块即炸，整个时效补检 + 主体级反证一起下线。
                # （2026-09-18 审计：本文件此前在 3.11 下 `ast.parse` 抛 SyntaxError。）
                _tt = re.sub(r"\s+", " ", _t or "").strip()[:44]
                out.append(f"> 🔺🔺 [{_tt}]({_u})")
    return out


# ============================================================
# 离线自测（无网络；python core/freshness.py）
# ============================================================
def _selftest() -> int:
    import os
    import sys
    from unittest import mock

    # `python core/freshness.py`（backend 目录下）时 sys.path[0]=core/，需补 backend 根以 import mcp/researchers
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_here)  # core/ → backend/
    if _root not in sys.path:
        sys.path.insert(0, _root)

    class _FakeEv:
        def __init__(self, claim: str = "", dates: list[str] | None = None, titles: list[str] | None = None):
            self.claim = claim
            _dates = dates or []
            _titles = titles or []
            _n = max(len(_dates), len(_titles))
            self.sources = [
                _FakeSrc(_dates[i] if i < len(_dates) else "", _titles[i] if i < len(_titles) else "")
                for i in range(_n)
            ]

    class _FakeSrc:
        def __init__(self, publish_date: str, title: str = ""):
            self.publish_date = publish_date
            self.title = title
            self.publisher = ""
            self.excerpt = ""

    ok = 0

    def _check(name: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            print(f"[{name}] ✗ 断言失败")
            sys.exit(1)
        ok += 1
        print(f"[{name}] ✓")

    def _ev(*args, **kw):
        return [_FakeEv(*args, **kw)]

    # 1) 偏旧（真日期 2024）→ gap==2
    evs_old = [_FakeEv("星巴克下沉", ["2024-06-01", "2023-11-01"])]
    _check("偏旧 gap==2", staleness_gap(evs_old, now_year=2026) == 2)
    # 1b) 标题带旧年（无真日期）也判旧
    _check("标题年份判旧", staleness_gap(_ev("", titles=["星巴克2024年门店报告", "GfK2023"]), now_year=2026) == 2)
    # 2) 有近一年来源 → gap<=1（不触发）
    evs_new = [_FakeEv("最新动态", ["2026-03-01"])]
    _check("近一年 gap==0", staleness_gap(evs_new, now_year=2026) == 0)
    # 3) 无年份 + 描述题 → None 且不触发；无年份+有真实证据+决策题 → 触发（回退判据）
    _check("无年份 None", staleness_gap([], now_year=2026) is None)
    _check("描述题不触发", is_stale(_ev("市场规模是多少"), "咖啡市场规模是多少", now_year=2026) is False)
    _check("空证据不触发", is_stale([], "星巴克下沉该不该做", now_year=2026) is False)
    _check("决策题回退触发", is_stale(_ev("", titles=["星巴克门店报道", "县域咖啡观察"]), "星巴克下沉三四线城市该不该做", now_year=2026) is True)
    # 4) 检索彻底空（无法区分故障/无结果）→ ""（不硬塞）
    with mock.patch("mcp.search.web_search_urls", return_value=[]), mock.patch(
        "core.freshness._search_configured", return_value=True
    ):
        b = build_freshness_block("星巴克下沉该不该做", evs_old, _now=date(2026, 9, 8))
    _check("检索空→空串", b == "")
    # 5) 决策题无条件补检：有近一年来源也例行补检；双探针命中时效文 → 线索块（含经济事件定向命中）
    hits_a = [{"url": "https://example.com/x", "title": "2026 星巴克最新动态 出售股权"}]
    with mock.patch("mcp.search.web_search_urls", side_effect=lambda q, n=3: hits_a), mock.patch(
        "core.freshness._search_configured", return_value=True
    ):
        b2 = build_freshness_block("星巴克下沉该不该做", evs_new, _now=date(2026, 9, 8))
    _check("决策题够新也补检→非空", bool(b2))
    _check("措辞=例行补检", "例行补检" in b2)
    _check("措辞不谎称偏旧", "检测到核心来源" not in b2)
    _check("含经济事件线索", "出售股权" in b2)
    # 5b) 经济事件定向探针专门命中博裕式新闻（N6 病案）：标题含 出售/2025 → 应进块
    def _q2_only(q, n=3):
        if "出售" in q and "收购" in q:
            return [{"url": "https://finance.sina.com.cn/boyu", "title": "星巴克中国2025年11月出售股权 博裕牵头的财团收购 60%"}]
        return []
    with mock.patch("mcp.search.web_search_urls", side_effect=_q2_only), mock.patch(
        "core.freshness._search_configured", return_value=True
    ):
        b3 = build_freshness_block("星巴克等咖啡品牌下沉三四线城市该不该做", evs_old, _now=date(2026, 9, 8))
    _check("经济事件定向命中(N6病案)", bool(b3) and "博裕" in b3 and "finance.sina.com.cn" in b3)
    # 5c) 泛词探针只回旧文、定向探针命中 → 仍产块（双探针并集防单探针滤空）
    def _q_mix(q, n=3):
        if "出售" in q:
            return [{"url": "https://cd.nbd.com.cn/4135903", "title": "2025-11 星巴克中国控制权交易 卖方与买方 股权"}]
        return [{"url": "https://example.com/old", "title": "2023年咖啡行业市场规模分析报告"}]
    with mock.patch("mcp.search.web_search_urls", side_effect=_q_mix), mock.patch(
        "core.freshness._search_configured", return_value=True
    ):
        b4 = build_freshness_block("星巴克下沉该不该做", evs_old, _now=date(2026, 9, 8))
    _check("单探针旧文+定向命中→并集产块", bool(b4) and "cd.nbd.com.cn" in b4)
    # 6) 偏旧 + 双探针只回词典/词条(真实链路 _is_clean_url 域名闸先滤) → 诚实"未命中"提示
    #    （有返回但无一过主题/时效门；不再静默空）
    _dict_only = [{"url": "https://zh.wiktionary.org/x", "title": "咖啡是什么意思"}, {"url": "https://zh.wiktionary.org/star", "title": "星巴克(词条)"}]

    def _clean_mock(u: str) -> bool:
        return "wiktionary" not in (u or "")

    with mock.patch("mcp.search.web_search_urls", side_effect=lambda q, n=3: _dict_only), mock.patch(
        "core.freshness._search_configured", return_value=True
    ), mock.patch("agents.researchers._is_clean_url", side_effect=_clean_mock):
        b5 = build_freshness_block("星巴克下沉该不该做", evs_old, _now=date(2026, 9, 8))
    _check("词典文被滤", "咖啡是什么意思" not in (b5 or "") and "wiktionary" not in (b5 or ""))
    _check("有返回无可用→诚实提示", "无一条通过" in (b5 or "") and "静默视为" in (b5 or ""))
    # 6b) 真命中：时点纪律 + 真实链接 + "非系统生成结论"标注齐全
    _hits = [
        {"url": "https://finance.sina.com.cn/a1", "title": "星巴克出售中国股权 2025 博裕 60%"},
        {"url": "https://example.com/b2", "title": "咖啡下沉市场 2026 最新动态 门店"},
    ]
    with mock.patch("mcp.search.web_search_urls", side_effect=lambda q, n=3: _hits), mock.patch(
        "core.freshness._search_configured", return_value=True
    ):
        b6 = build_freshness_block("星巴克等咖啡品牌下沉三四线城市该不该做", evs_old, _now=date(2026, 9, 8))
    _check("命中→非空", bool(b6))
    _check("含最新年份", f"{2024}" in b6)  # latest_year = 2026-2 = 2024
    _check("含真实链接", "finance.sina.com.cn" in b6)
    _check("含非结论标注", "非系统生成结论" in b6)
    _check("含数据时点纪律", "数据时点纪律" in b6)
    # **这一块必须自陈"没有被正文消费"**（2026-09-15 深访实跑）：
    # 补检在 deliver 节点才跑，正文在那之前就写完了 → 线索在结构上不可能已进入分析。
    # 不说破的话，"置顶展示"会被读成"报告已经算进去了"——那正是 N6 病案换个样子复发。
    _check("自陈未被正文消费", "没有被正文消费" in b6)
    _check("说明结构原因（写作完成之后）", "写作完成之后" in b6)
    # 含重大公司事件词的线索要逐条点名（判据复用 `_EVENT_KW`，不新增词表）
    _check("重大事件逐条点名", "股权" in b6 and "务必先确认正文结论" in b6)
    # 反向锁：**没有**重大事件词时不许无中生有地点名
    with mock.patch("mcp.search.web_search_urls", side_effect=lambda q, n=3: [
        {"url": "https://example.com/c1", "title": "咖啡下沉市场 2026 最新动态 门店"},
        {"url": "https://example.com/c2", "title": "三四线城市咖啡消费 2026 最新趋势"},
    ]), mock.patch("core.freshness._search_configured", return_value=True):
        _b7 = build_freshness_block("星巴克等咖啡品牌下沉三四线城市该不该做", evs_old, _now=date(2026, 9, 8))
    _check("无重大事件时不点名", "没有被正文消费" in _b7 and "务必先确认正文结论" not in _b7)

    print(f"\n新鲜度门禁自测：{ok} 项全过（离线，无网络）。")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_selftest())
