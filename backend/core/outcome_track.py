"""研策 · 决策回访——数字员工要为自己说过的话负责

## 为什么它比"再做一个功能"重要

**员工与工具的分界线是：员工会回来对账，工具不会。**

一个有记忆的研究员，做完判断时会记下"我建议你盯这三件事"，然后过一阵子回来看
这三件事动了没有。而本系统里的 `data/outcome_baselines.json` 到今天为止
**零代码读取**——那份埋点只是一份静态文件。
也就是说：「决策记忆 / 资产飞轮 / 越用越懂」这些叙事，**此前一个都没有代码支撑**。

本模块把它接上：对每条在途基线，逐条去查当初写下的**监控条件**今天动没动。

## 三态输出（与 `core/entailment.py` 同一条纪律）

- `changed`：查到可指认的进展（附是哪一条、原文片段、URL）
- `unchanged`：查了，但未发现指向该条件的新进展
- `no_result`：检索通道不可用或零返回 → **未取得**

**`no_result` 绝不写成 `unchanged`。** 把"没查到"写成"没变化"，
正是本产品一直在批评的那种静默编造——而且在这个功能里尤其致命：
回访的意义就是"当初的判断今天还站不站得住"，用假的"没变化"去支持它，
等于自己给自己盖章。

## 另外三条纪律

1. **只回访、不改写原判断**。回访问的是"监控条件动了没有"，不是"重新做一次研究"。
   原判定与基线值原样保留在 `outcome_baselines.json` 里——
   **篡改历史等于没有记忆**，而"有记忆"正是这个模块唯一的存在理由。
2. **判定者是模型，且必须公开依据**。每条回访都带"依据哪条结果、原文片段是什么"，
   让人工能在 30 秒内推翻。可推翻的结论才叫可核验的结论。
3. **不预测、不代为决策**。只给"动了 / 没动 / 没查到 + 依据"，
   **不写"所以你应该…"**——那是决策者的事，越界就变成了替客户拍板。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "outcome_baselines.json"
)
_REVIEWS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "outcome_reviews.jsonl"
)

VERDICT_LABEL = {
    "changed": "🔔 有进展",
    "unchanged": "· 未见变化",
    "no_result": "⚠️ 未取得",
    "error": "⚠️ 未取得（异常）",
}

_JUDGE = """你是"研策"的**决策回访官**。下面是研究员在【{since}】就某个商业问题写下的
**监控条件**之一，以及今天检索到的若干条公开结果。

你的任务：判断这些结果里**有没有指向该监控条件的新进展**。

三选一，**宁可判"未见变化"也不许把沾边的东西说成进展**：
- `changed`：有结果**明确**指向该条件的变化（如条件问"股权变更"而结果里有具体的股权交易报道）。
- `unchanged`：结果与该条件主题相关，但**没有**指向变化（如泛泛的行业综述、旧文重发）。
- `unclear`：结果与该条件**无关**，或信息不足以判断。

输出 JSON（只输出 JSON）：
{{"verdict": "changed|unchanged|unclear", "why": "一句话说清依据（≤80字）",
  "which": [指向该判断的结果序号，如 [1,3]；没有则空数组],
  "quote": "最能支撑判断的原文片段（≤120字；无则留空）"}}

**监控条件**：{cond}
**原始问题**：{question}
**今天检索到的结果**：
{hits}
"""


def register(
    question: str,
    verdict: str,
    monitor_conditions: list[str] | None = None,
    industry: str = "",
    baseline_values: str = "",
    due_days: int = 90,
) -> dict[str, Any] | None:
    """交付时**自动登记**一条决策基线——让「系统会回来对账」这句话有代码支撑。

    ## 为什么必须自动登记（病案，2026-09-15 一次外部对抗审计）

    `outcome_baselines.json` 里的基线**全部是手工维护的**；而系统自己每一次交付
    都写下了 判定 / 监控条件 —— **却没有一条进入回访队列**。
    于是「员工会回来对账」这个叙事目前只对了一半：**对的是人写下的账，系统自己的账从没进过队列。**
    审计原话：这一处对「数字员工」叙事的提升，**远大于再加 5 个数据源**。

    ## 三条纪律

    1. **同一问题只登记一次**——复跑同一道题不重复建基线，否则回访列表会被同题刷屏，
       且多条 due_date 互相打架（哪条算数？）。
    2. **只在真交付时登记**——调用方负责保证这点（无判定/降级交付不调）。
    3. **失败绝不影响交付**——调用方包 try；本函数自身出错返回 None。
    """
    q = (question or "").strip()
    v = (verdict or "").strip()
    if not q or not v:
        return None
    # **离线测试不许写生产档案**（与 `core/runstats.py` 同一条闸门、同一个理由）：
    # `_fulltest` 的 e2e 会真的跑完整流水线 → 一路调到本函数 → 每跑一次测试
    # 就凭空多一条"决策基线"，回访列表会被测试题面刷屏。
    # 账本那边已经踩过一次（6 行被撑到 10 行），这里在**写入点**直接挡掉。
    #
    # 2026-09-16：判定实现收归 `core.safe_io`（此前这里内联抄了一份环境变量判断）。
    if safe_io.is_offline():
        logger.info("离线模式：本次交付不登记决策基线（question=%s）", q[:40])
        return None
    try:
        doc: dict[str, Any]
        try:
            with open(_PATH, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            doc = {}
        if not isinstance(doc, dict):
            doc = {"baselines": doc}
        bs = doc.setdefault("baselines", [])
        if not isinstance(bs, list):
            return None
        # ① 同题不重复登记
        for b in bs:
            if isinstance(b, dict) and str(b.get("question") or "").strip() == q:
                return None
        # ② 编号取现有最大值 +1（不是 len+1——手工基线删过的话 len 会撞号）
        _mx = 0
        for b in bs:
            m = str((b or {}).get("id") or "")
            if m.startswith("DB-"):
                try:
                    _mx = max(_mx, int(m[3:]))
                except ValueError:
                    pass
        rec = {
            "id": f"DB-{_mx + 1:02d}",
            "question": q[:200],
            "industry": (industry or "")[:30],
            "verdict": v,
            "decision_date": date.today().isoformat(),
            "baseline_values": (baseline_values or "")[:400],
            "monitor_conditions": [str(c).strip() for c in (monitor_conditions or []) if str(c).strip()][:6],
            "due_date": (date.today() + timedelta(days=int(due_days))).isoformat(),
            "status": "in_flight",
            # **来源必须可分辨**：「系统自己登的」与「人手工写的」不是一回事，
            # 回访报告要能分开报（否则又是一次"把两种东西混成一种"）。
            "source": "auto（交付时自动登记）",
        }
        bs.append(rec)
        # 原子写：先写临时文件再替换，避免写一半崩掉把手工基线也毁了
        _tmp = _PATH + ".tmp"
        with safe_io.open_prod(_tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        safe_io.replace(_tmp, _PATH)
        logger.info("决策基线已自动登记: %s verdict=%s", rec["id"], v)
        return rec
    except Exception as e:  # 登记失败绝不影响交付
        logger.warning("决策基线自动登记失败(不阻断交付): %s", e)
        return None


def load_baselines(path: str | None = None) -> list[dict[str, Any]]:
    """读决策基线。返回 `baselines` 列表（文件缺失/损坏 → 空表，不抛）。"""
    p = path or _PATH
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        bs = d.get("baselines") if isinstance(d, dict) else d
        return [b for b in (bs or []) if isinstance(b, dict)]
    except Exception as e:
        logger.warning("决策基线读取失败: %s", e)
        return []


def _judge(cond: str, question: str, since: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """把检索结果交给模型判三态。无模型 → 退化为"未取得"（绝不装成"没变化"）。"""
    if not hits:
        return {"verdict": "no_result", "why": "检索未取得任何结果（通道不可用或零返回）",
                "which": [], "quote": ""}
    try:
        from core.llm import get_quality_llm, has_main_llm

        if not has_main_llm():
            return {"verdict": "no_result", "why": "无可用模型，未做判定（**不等于没变化**）",
                    "which": [], "quote": ""}
        _lines = []
        for i, h in enumerate(hits[:5], 1):
            _t = str(h.get("title") or "")[:90]
            _u = str(h.get("url") or "")[:120]
            _sn = str(h.get("snippet") or h.get("content") or "")[:220]
            _lines.append(f"[{i}] {_t}\n    {_u}\n    {_sn}")
        llm = get_quality_llm(temperature=0.0)
        msg = llm.invoke([
            {"role": "system", "content": "只输出 JSON，不要任何其他文字。"},
            {"role": "user", "content": _JUDGE.format(
                since=since or "此前", cond=cond, question=question or "（未记录）",
                hits="\n".join(_lines))},
        ])
        import re

        _s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (getattr(msg, "content", "") or "").strip())
        _m = re.search(r"(?s)\{.*\}", _s)
        v = json.loads(_m.group(0)) if _m else {}
        _vd = str(v.get("verdict") or "").strip()
        if _vd == "unclear":
            _vd = "no_result"  # "判不出来"与"没查到"对读者是一件事：不能拿去支持原判断
        if _vd not in ("changed", "unchanged", "no_result"):
            _vd = "no_result"
        return {
            "verdict": _vd,
            "why": str(v.get("why") or "")[:160],
            "which": v.get("which") if isinstance(v.get("which"), list) else [],
            "quote": str(v.get("quote") or "")[:200],
        }
    except Exception as e:
        logger.warning("回访判定失败: %s", e)
        return {"verdict": "error", "why": f"{type(e).__name__}: {e}"[:120], "which": [], "quote": ""}


def _search(cond: str, industry: str) -> list[dict[str, Any]]:
    """为一条监控条件做一次真实检索（复用既有检索通道与全部合规闸门）。"""
    q = " ".join(x for x in (industry, cond, str(date.today().year)) if x).strip()
    try:
        from mcp.search import web_search_urls

        return list(web_search_urls(q, n=5) or [])
    except Exception as e:
        logger.info("回访检索失败（记未取得）: %s", e)
        return []


def review_one(b: dict[str, Any], max_conds: int = 3) -> dict[str, Any]:
    """回访一条基线：逐条查它的监控条件。**不改写原记录的任何字段。**"""
    conds = [str(c) for c in (b.get("monitor_conditions") or []) if str(c).strip()]
    rows: list[dict[str, Any]] = []
    for c in conds[:max_conds]:
        hits = _search(c, str(b.get("industry") or ""))
        j = _judge(c, str(b.get("question") or ""), str(b.get("decision_date") or ""), hits)
        # 归一化放在**这里**而不是只放在 `_judge` 里：`unclear`（判不出来）与
        # `no_result`（没查到）对读者是同一件事——**都不能拿去支持原判断**。
        # 只在一个函数里归一，等于把不变量的守卫交给"下一位实现者记得别返回 unclear"。
        _vd = str(j.get("verdict") or "").strip()
        if _vd not in ("changed", "unchanged", "no_result"):
            _vd = "no_result"
        rows.append({
            "condition": c[:120],
            "verdict": _vd,
            "why": j.get("why", ""),
            "quote": j.get("quote", ""),
            "evidence": [
                {"title": str(h.get("title") or "")[:90], "url": str(h.get("url") or "")[:200]}
                for h in hits[:3]
            ],
        })
    return {
        "id": str(b.get("id") or ""),
        "question": str(b.get("question") or "")[:120],
        "industry": str(b.get("industry") or ""),
        "verdict_original": str(b.get("verdict") or ""),
        "decision_date": str(b.get("decision_date") or ""),
        "due_date": str(b.get("due_date") or ""),
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
        "changed": sum(1 for r in rows if r["verdict"] == "changed"),
        "no_result": sum(1 for r in rows if r["verdict"] in ("no_result", "error")),
    }


def run(ids: list[str] | None = None, max_conds: int = 3, persist: bool = True) -> dict[str, Any]:
    """回访全部（或指定）在途基线。返回汇总 + 逐条结果。

    ⚠️ **`due_date` 不再是一个写了没人读的字段**（2026-09-16 全量审计）。
    病案：登记基线时同时写下 `decision_date` 与 `due_date`，而本函数**只按
    `status == "in_flight"` 筛**——于是**刚登记完的基线会被立刻"回访"**，
    拿今天的行情去回答"这条监控条件后来动了没有"，并**落盘成一条结论**。
    而本模块自己的收尾声明写着「原判断对不对，是应验日（due_date）到期后才有资格谈的事」。

    处理方式**不是**把未到期的滤掉——回访是**由人触发**的（材料里写的就是"到期后我按一下"），
    人有权提前查。**但系统必须说出来**：本次回访里有几条其实还没到应验日。
    悄悄按未到期状态回答、再把它当结论存进账本，才是那个真问题。
    """
    bs = [b for b in load_baselines() if str(b.get("status") or "in_flight") == "in_flight"]
    if ids:
        _want = {str(i) for i in ids}
        bs = [b for b in bs if str(b.get("id") or "") in _want]
    _today = datetime.now().date().isoformat()
    # 空 due_date（早期基线没写）= 不拦，但也不算"已到期"——它属于"没记应验日"
    _not_due = [b for b in bs if str(b.get("due_date") or "")[:10] > _today]
    reviews = [review_one(b, max_conds=max_conds) for b in bs]
    if persist and reviews:
        try:
            safe_io.makedirs(os.path.dirname(_REVIEWS))
            with safe_io.open_prod(_REVIEWS, "a", encoding="utf-8") as f:
                for r in reviews:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("回访记录落盘失败（不影响本次结果）: %s", e)
    return {
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(reviews),
        "changed": sum(r["changed"] for r in reviews),
        "no_result": sum(r["no_result"] for r in reviews),
        # 提前回访了几条：**必须能被读者看见**，否则"有进展 0 条"会被读成
        # "监控条件都没动"，而真实原因是"还没到该看的时候"。
        "not_due": len(_not_due),
        "not_due_ids": [str(b.get("id") or "") for b in _not_due],
        "reviews": reviews,
    }


def load_reviews(limit: int = 20) -> list[dict[str, Any]]:
    """读历史回访记录（最新在前）。"""
    out: list[dict[str, Any]] = []
    try:
        if not os.path.exists(_REVIEWS):
            return []
        with open(_REVIEWS, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    if isinstance(d, dict):
                        out.append(d)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("回访记录读取失败: %s", e)
    return list(reversed(out))[: max(1, int(limit))]


def _cell(s: Any, n: int) -> str:
    """markdown 表格单元格：**竖线与换行必须洗掉**（2026-09-16 全量审计）。

    病案：同一行里 `why`（依据）洗了 `|`、而 `condition`（监控条件）**没洗**——
    监控条件是我们自己从「14 天验证计划」里摘的，本来就常带 `|`-like 分隔或换行，
    于是那一行整个错列，读者看到的是被切碎的表。**同一行的两个格子用两套规则**，
    正是本仓库"同一动作两处实现"的经典形态。
    """
    t = str(s or "").replace("|", "/").replace("\n", " ").replace("\r", " ")
    return " ".join(t.split())[:n]


def format_md(rep: dict[str, Any] | None) -> str:
    """回访结果 → markdown。**未执行也出一段如实说明**（不出声的缺席会被读成"没问题"）。"""
    if not rep or not rep.get("reviews"):
        return "\n".join([
            "\n\n---\n\n# 🔁 决策回访（当初的判断，今天还站得住吗）",
            "> **本次未执行**：回访需要按监控条件逐条真实检索。"
            "**这不代表当初的判断已经应验**——本节缺席只是缺席。",
        ])
    lines = [
        "\n\n---\n\n# 🔁 决策回访（当初的判断，今天还站得住吗）",
        f"对 **{rep['count']}** 条在途决策基线做了回访（回访时点 {rep['reviewed_at']}）："
        f"🔔 有进展 **{rep['changed']}** 条 · ⚠️ 未取得 **{rep['no_result']}** 条。",
        # **提前回访必须自陈**（2026-09-16）：`due_date` 原先写了没人读，
        # 刚登记的基线会被立刻回访并落盘结论。不拦人提前查，但要说出来——
        # 否则"有进展 0 条"会被读成"监控条件都没动"，而真实原因是"还没到该看的时候"。
        (
            f"⚠️ **其中 {rep['not_due']} 条尚未到应验日（due_date）**"
            f"（{'、'.join(rep.get('not_due_ids') or [])[:60]}）——"
            "对这些条目，本次只回答「今天查得到什么」，**不构成应验结论**。"
            if rep.get("not_due")
            else ""
        ),
        "",
        "> **为什么这是一个「员工」才会做的事**：工具做完判断就结束了；"
        "员工会记下「我建议你盯这三件事」，过一阵子回来看它们动了没有。"
        "本节就是把这件事接上——**决策记忆此前只是一份静态文件，没有任何代码读它。**",
        "",
        "> **本节的三条纪律**：① 只回访、**不改写原判断**（原判定与基线值原样保留）；"
        "② **「未取得」绝不写成「未见变化」**（把「没查到」说成「没变化」，"
        "等于自己给自己盖章）；③ 不预测、不代为决策——只给「动了 / 没动 + 依据」。",
        "",
    ]
    for r in rep["reviews"]:
        lines.append(
            f"### {r['id']}｜{r['question'][:44]}"
            f"（原判定 **{r['verdict_original']}**，{r['decision_date']}）"
        )
        lines.append("")
        lines.append(f"| 监控条件 | 回访结论 | 依据 |")
        lines.append("| :--- | :--- | :--- |")
        for row in r["rows"]:
            _lab = VERDICT_LABEL.get(row["verdict"], row["verdict"])
            _why = _cell(row.get("why") or "", 110)
            lines.append(f"| {_cell(row.get('condition') or '', 50)} | **{_lab}** | {_why} |")
        lines.append("")
        for row in r["rows"]:
            if row["verdict"] == "changed" and row.get("evidence"):
                lines.append(f"- **🔔 有进展**：{row['condition'][:60]}")
                if row.get("quote"):
                    lines.append(f"  - 原文片段：{row['quote'][:150]}")
                for e in row["evidence"][:2]:
                    lines.append(f"  - [{e['title'][:60]}]({e['url']})")
        lines.append("")
    lines.append(
        "> **本节不证明什么**：回访只回答「当初写下的监控条件今天有没有可指认的进展」，"
        "**不回答「原判断对不对」**——那是应验日（due_date）到期后才有资格谈的事。"
        "且回访判定由模型给出，已附依据与原文片段，可被人工推翻。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "决策回访：对在途决策基线逐条重查其监控条件，输出「有进展 / 未见变化 / 未取得」三态，"
        "附依据与原文片段并落盘。不改写原判断、未取得绝不写成未见变化、不代为决策。"
    )
