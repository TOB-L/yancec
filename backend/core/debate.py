"""研策 · 多角色辩论——让被质疑的一方**有机会反驳**

## 为什么单向审查不够

`core/adversarial.py` 的 Red Team 是**单向**的：怀疑论官提出质疑，报告照单全收，把
"（RedTeam待核）"打上去就结束了。这有三个问题：

1. **质疑方永远赢**。它是被告也是法官，提出来的每一条都被当成真问题记进报告。
2. **被质疑方没有申辩权**。一条数字被标"高风险"，可能只是因为校验官没看到支撑它的那一页。
   单向流程里这个误判**没有任何机会被纠正**。
3. **读者拿不到结论**。报告最后给的是"这里有争议"，而不是"这个争议**解决了没有、怎么解决的**"。

真辩论补的正是这三件事：**质疑 → 申辩 → 裁定**，而且**每一轮都必须给出立场**
（撤回 / 让步 / 维持），不许含糊过去。

## 三个角色（三个独立调用，不是让一个模型自说自话）

- **质疑方**（Skeptic）：给定主张 + 该主张所引来源的原文摘录 → 提出**最强的一条**反对意见。
  要求给出**可证伪的具体理由**（口径/时点/量级/主体/逻辑），不许"感觉不太对"。
- **申辩方**（Analyst）：只看得到质疑 + 原始材料 → 要么**用证据驳回**，要么**承认并给出修正表述**。
  明确要求：**驳回不了就认**。硬撑的申辩比认输更糟——它会把一个真问题洗成假问题。
- **裁定方**（Judge）：看完整轮交锋 → 判 `反驳成立 / 部分成立 / 不成立`，并给出**收敛后的表述**。

分成三个独立调用（而不是"一个 prompt 里让模型扮演双方"）是刻意的：
同一次生成里，模型会**自动让两方和解**（它会朝着"看起来平衡"的方向写），
而我们要的恰恰是**分歧被真实地暴露出来**。分开调用才会真的对撞。

## 三态输出（都要如实进报告）

- `converged_support`：质疑被驳回，**原表述维持**（并公开驳回理由）
- `converged_revise`：申辩方让步，**结论修正**（公开修正后的表述与让步理由）
- `unresolved`：**分歧未消解** —— 这条最该被看见：它意味着"这份报告里有一个我们内部
  都没吵清楚的问题"，必须标进结论状态与缺口披露，**不许悄悄按原表述交付**

## 成本与边界

按 claim 并发（默认最多 3 条）、每条 3 次调用（质疑/申辩/裁定），档位越高跑得越多。
**只辩"关键数字类主张"**——纯定性表述（"竞争激烈"）辩不出结果，白花钱。
无模型 / 任一轮失败 → 整条转 `unresolved` 并在附录写明"辩论未完成"，
**绝不当成"没有争议"**（这是本模块与单向 Red Team 最根本的区别）。
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

MAX_CLAIMS_BY_DEPTH = {"free": 0, "standard": 2, "deep": 3, "custom": 3}
_ROUNDS = 1  # 质疑→申辩 算一轮；当前固定一轮（多轮收益递减，成本线性增长）

VERDICT_LABEL = {
    "converged_support": "✅ 质疑被驳回·原表述维持",
    "converged_revise": "✏️ 申辩方让步·结论已修正",
    "unresolved": "⚠️ 分歧未消解",
    # **第五态（2026-09-18 审计补）**：质疑方自己就说"未见实质问题"——这**不是申辩胜出**。
    # 补它之前，这一支被归进 `converged_support`，于是同一节里汇总行印"质疑被驳回 3"、
    # 而三条明细里两条写的是"质疑方自陈未见实质问题（非「申辩胜出」）"——
    # **汇总与明细自相矛盾，且方向一致地让"驳回率"虚高**。
    # 这正是本模块自己最反对的那种形态：审计层的话与判定不同源。
    "no_objection": "• 质疑方未提出实质质疑（非「申辩胜出」）",
    "not_run": "• 未执行",
}

_SKEPTIC = """你是"研策"的**质疑方**。下面是一条出现在报告里的关键主张，以及它引用的来源原文摘录。

你的任务：提出**最强的一条**反对意见。

要求：
1. 必须是**可证伪的具体理由**——口径不同 / 时点错位 / 量级不合理 / 主体张冠李戴 / 逻辑跳跃，
   五选一或几项，并**指出具体是哪个数字或哪个词出的问题**。
2. **不许**说"感觉不太对""可能不够准确""建议进一步核实"这类没有信息量的话。
3. 若你确实找不到实质问题，就如实说 `"weak": true` 并说明"未见实质问题"——
   **编一条似是而非的质疑比不提更糟**，它会让读者对整个质疑机制失去信任。
4. **你的训练数据截止时间不等于当前时间**。报告的日期来自系统时钟与真实联网检索，
   比你"感觉现在是哪一年"可靠。**禁止**以"当前真实时间为 20XX 年""该日期属于未来"
   "AI 训练数据截止较早"为由指控时点穿越或信源虚构。你就**时点**能提的异议只有一种：
   报告内部两个日期互相矛盾，或**来源原文里的时点**与主张的时点对不上。
   ⚠️ **换壳同样禁止**（2026-09-17 实跑踩中）：**"该数据尚不存在""官网上没有这一年的数据"**
   **"这条链接是虚构的"——凡是以"现在还没到那一天"为前提的指控，换多少种说法都禁止。**
   一份已经落进来源原文摘录的数据，它的存在性由**摘录**决定；你无法访问外网去核实
   "官网上有没有"，所以"官网查无此数"这个理由**在结构上就不成立**。
5. 只输出 JSON，不要其他文字。

输出：{{"objection": "反对意见（≤80字，必须点名具体数字/口径）", "type": "口径|时点|量级|主体|逻辑",
       "severity": "high|medium|low", "weak": false}}

**主张**：{claim}
**引用来源**：{source}
**来源原文摘录**：{excerpt}
"""

_ANALYST = """你是"研策"的**申辩方**。有人对报告里的一条主张提出了质疑。

你的任务：**要么用证据驳回，要么承认并给出修正表述**。

要求：
1. 只能依据下面给出的原始材料申辩，**不许引入新材料**（引入了也无从核实）。
2. **驳回不了就认**。硬撑的申辩比认输更糟——它会把一个真问题洗成假问题。
   如果质疑成立，直接给出**修正后的表述**（写清改成什么），不要只是"承认有不足"。
3. **但"认"必须是证据上的认，不是态度上的认。** 若质疑的理由本身站不住——
   例如质疑方说"当前时间不是某年，所以这份数据是虚构的"——**要驳回，不要顺着认**。
   一条已经落到来源原文摘录里的数字，其存在性由**摘录**决定，不由质疑方对"现在是哪一年"
   的印象决定；把一份真实发布过的数据改写成"虚构/预测"，**是本环节可能造成的最严重失真**，
   比硬撑一条错数字更糟。
4. 只输出 JSON，不要其他文字。

输出：{{"rebuttal": "申辩内容或让步说明（≤90字）", "concede": true/false,
       "revised": "若让步，给出修正后的完整表述；不让步则留空"}}

**主张**：{claim}
**来源原文摘录**：{excerpt}
**质疑**：{objection}（类型：{otype}，严重度：{severity}）
"""

_JUDGE = """你是"研策"的**裁定方**。下面是围绕一条主张的一轮交锋，请给出裁定。

要求：
1. 判 `rebuttal_holds`：申辩是否**真正驳倒了**质疑（只看证据与逻辑，不因为申辩写得长就成立）。
2. 给出**收敛后的表述**：无论哪方赢，读者最终该看到的那句话是什么。
3. 若双方各执一词、现有材料不足以判定 → `verdict` 给 `unresolved`，并**说清卡在哪里**。
4. ⚠️ **时间锚（裁定方此前没有这一条，是本轮实跑最重伤的根因）**：
   **系统时钟日期是硬锚。** 若质疑建立在"当前时间尚未到达该日期""官网上不存在这一年的数据"
   "该链接为虚构或未来时点"这类**现实时间信念**之上——**一律不得支持该质疑**，
   `rebuttal_holds` 必须判 true。
   理由：数据的真实性由**来源原文摘录**决定，不由任何一方对"现在是哪一年"的印象决定；
   而**你无法访问外网**，所以"官网查无此数"在结构上不可证伪。
   把一份真实发布过的数据改判成"虚构/未来"，是本环节能造成的最严重失真。
5. 只输出 JSON，不要其他文字。

输出：{{"rebuttal_holds": true/false, "verdict": "converged_support|converged_revise|unresolved",
       "final_statement": "收敛后的表述（≤100字）", "why": "裁定理由（≤80字）",
       "disagreement": "若未收敛，写清分歧点；否则留空"}}

**主张**：{claim}
**质疑**：{objection}
**申辩**：{rebuttal}
"""


def _parse_obj(txt: str) -> dict[str, Any] | None:
    if not txt:
        return None
    s = re.sub(r"^```(?:json)?\s*", "", txt.strip())
    s = re.sub(r"\s*```$", "", s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    m = re.search(r"(?s)\{.*\}", s)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _as_dict(x: Any) -> dict[str, Any] | None:
    """证据条目双形态归一（同步路径 pydantic / 流式路径 dict）——本仓库已踩过两次。"""
    if isinstance(x, dict):
        return x
    try:
        return x.model_dump(mode="json")
    except Exception:
        return None


def pick_claims(evidences: list[Any] | None, max_claims: int) -> list[dict[str, Any]]:
    """挑值得辩的主张：**带数字 + 有原文摘录** 的关键结论，按数字密度排序。

    纯定性表述（"竞争激烈"）辩不出结果——质疑方只能提"不够具体"，那是废话，
    白花钱还往附录里灌噪音。
    """
    out: list[dict[str, Any]] = []
    for _raw in evidences or []:
        ev = _as_dict(_raw)
        if not ev:
            continue
        claim = str(ev.get("claim") or "").strip()
        if not claim or claim.startswith("【反方】"):
            continue
        if len(re.findall(r"\d", claim)) < 2:
            continue  # 没数字 → 辩不出结果
        best = None
        for s in ev.get("sources") or []:
            if not isinstance(s, dict):
                continue
            ex = str(s.get("excerpt") or "").strip()
            if len(ex) >= 40 and (best is None or len(ex) > len(best.get("excerpt") or "")):
                best = {
                    "source": str(s.get("publisher") or s.get("title") or "未标注")[:40],
                    "excerpt": ex[:700],
                    "url": str(s.get("url") or "")[:200],
                }
        if not best:
            continue  # 没有原文 → 无处申辩也无处质疑
        out.append({"claim": claim[:300], **best})
    out.sort(key=lambda p: -len(re.findall(r"\d", p["claim"])))
    return out[: max(1, int(max_claims))]


def _call(prompt: str, max_tokens: int = 700) -> dict[str, Any] | None:
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm():
            return None
        llm = get_main_llm(temperature=0.3, max_tokens=max_tokens)
        msg = llm.invoke(
            [
                {"role": "system", "content": "只输出 JSON，不要任何其他文字。"},
                {"role": "user", "content": prompt},
            ]
        )
        return _parse_obj(getattr(msg, "content", "") or "")
    except Exception as e:
        logger.warning("辩论环节调用失败: %s", e)
        return None


def debate_one(c: dict[str, Any]) -> dict[str, Any]:
    """一条主张的一轮辩论：质疑 → 申辩 → 裁定。任一步失败 → unresolved（不当作"无争议"）。"""
    row: dict[str, Any] = {
        "claim": c["claim"],
        "source": c.get("source", ""),
        "url": c.get("url", ""),
        "objection": "",
        "otype": "",
        "severity": "",
        "rebuttal": "",
        "concede": None,
        "revised": "",
        "verdict": "unresolved",
        "final_statement": "",
        "why": "",
        "disagreement": "",
        "rounds": 0,
        # 出口过滤留痕（2026-09-17）：空 = 未过滤。**不隐藏被过滤的条目**——
        # 过滤本身也要能被读者看见，否则就成了另一种"静默消失"。
        "filtered": "",
        "filter_hit": "",
    }
    sk = _call(_SKEPTIC.format(claim=c["claim"], source=c.get("source", ""), excerpt=c.get("excerpt", "")))
    if not sk:
        row["why"] = "质疑方未产出（模型不可用/调用或解析失败）——**不计为无争议**"
        row["verdict"] = "not_run"
        return row
    row["objection"] = str(sk.get("objection") or "")[:200]
    row["otype"] = str(sk.get("type") or "")[:12]
    row["severity"] = str(sk.get("severity") or "")[:8]
    row["rounds"] = 1
    # 质疑方自认没找到实质问题 → 直接收敛，不必再走申辩（省一次调用，也更诚实）
    if sk.get("weak") or not row["objection"]:
        # 记 `no_objection` 而**不是** `converged_support`：没有质疑可驳回，
        # 把它算进"驳回"就是给系统记一笔它没挣到的功劳（见 VERDICT_LABEL 处的审计记录）。
        row["verdict"] = "no_objection"
        row["final_statement"] = c["claim"]
        row["why"] = "质疑方自陈未见实质问题（非「申辩胜出」）"
        return row

    an = _call(
        _ANALYST.format(
            claim=c["claim"], excerpt=c.get("excerpt", ""),
            objection=row["objection"], otype=row["otype"], severity=row["severity"],
        )
    )
    if not an:
        row["verdict"] = "not_run"
        row["why"] = "申辩方未产出——**辩论不完整，不计为无争议**"
        return row
    row["rebuttal"] = str(an.get("rebuttal") or "")[:220]
    row["concede"] = bool(an.get("concede"))
    row["revised"] = str(an.get("revised") or "")[:300]
    row["rounds"] = 2

    jd = _call(
        _JUDGE.format(claim=c["claim"], objection=row["objection"], rebuttal=row["rebuttal"])
    )
    if not jd:
        row["verdict"] = "not_run"
        row["why"] = "裁定方未产出——**辩论不完整，不计为无争议**"
        return row
    row["rounds"] = 3
    _v = str(jd.get("verdict") or "").strip()
    if _v not in ("converged_support", "converged_revise", "unresolved"):
        _v = "converged_revise" if row["concede"] else "converged_support"
    row["verdict"] = _v
    row["final_statement"] = str(jd.get("final_statement") or "")[:300] or (
        row["revised"] or c["claim"]
    )
    row["why"] = str(jd.get("why") or "")[:160]
    row["disagreement"] = str(jd.get("disagreement") or "")[:200]
    return _filter_time_anchor(row)


# ============================================================
# 时间锚的**确定性出口过滤**（2026-09-17 实跑病案）
# ============================================================
#
# 病案（本跑最重伤，逐字来自真实交付报告）：
#   质疑方：国家统计局官网域名 stats.gov.cn 下**不存在 2026 年的真实数据**，该链接为**虚构或未来时点**
#   裁定方：质疑指出数据时空逻辑悖论（2026 年发布 2025 年数据但**当前未到该时间**），属根本性证伪
#   结果：**申辩方（正确的一方）被驳回**，一条真实存在的统计局数据被判成"虚构"。
#
# 为什么提示词禁令不够：`_SKEPTIC` 条款 4 **早就有**这条禁令，`_ANALYST` 条款 3 也有对策，
# 但**裁定方没有任何时间锚**，模型就从裁定方那一侧绕了过去——**换了个壳，绕过了一条只写在
# 一个角色身上的规则**。本仓库对此已有定论：**只写在提示词里的禁令不是禁令**（RedTeam 那条
# "不得用知识截止否定系统时钟"当初也是先有提示词、后有确定性闸门才真正生效的）。
#
# 因此这里加**出口过滤**：不看提示词听没听话，直接检查**产出的文本**。
# 触发后不隐藏该条、不静默丢弃——**改判并留痕**（本仓库对"静默"的一贯纪律）。
_TIME_DENIAL = re.compile(
    r"未到该时间|未到该日期|尚未到达该时间|当前未到|还没到该时间|未(到|至)该(时间|日期)|"
    r"不存在\s*20\d\d\s*年|不存在该年|"
    r"尚不存在|还不存在|尚未(发布|公开|发布该)|"
    r"官网(上)?(并)?(查无|没有|不存在)|"
    r"未来时点|属于未来|时序穿越|训练(数据)?截止"
)


def _filter_time_anchor(row: dict[str, Any]) -> dict[str, Any]:
    """裁定结果若建立在"现在还没到那一天"之上 → 判为无效，改判支持，并留痕。

    判据来自**产出文本**而不是提示词是否被遵守。作用范围刻意收窄：
    **只有结论本身不是 `converged_support` 时才动手**——
    结论已经是"维持原主张"的，说明没有被这条错误推理伤到，不必改（少动一次就少一分风险）。
    """
    # 形状兜底：本函数可能被施加到不是 `debate_one` 造的 row 上（测试/回放），
    # 缺键时补空值，保证"过滤与否"这件事**两种情况下都读得到**。
    row.setdefault("filtered", "")
    row.setdefault("filter_hit", "")
    if row.get("verdict") in ("converged_support", "no_objection"):
        # `no_objection` 同样提前返回：**根本没有质疑可撤销**，过滤器无事可做。
        return row
    blob = " ".join(
        str(row.get(k) or "") for k in ("objection", "why", "disagreement", "final_statement")
    )
    m = _TIME_DENIAL.search(blob)
    if not m:
        return row
    row["filtered"] = "time_anchor"
    row["filter_hit"] = m.group(0)
    row["verdict_before_filter"] = row.get("verdict")
    row["verdict"] = "converged_support"
    row["final_statement"] = row.get("claim") or row.get("final_statement") or ""
    row["why"] = (
        f"⚠️ 已按**时间锚**过滤：原裁定建立在「{m.group(0)}」之上，"
        "而系统时钟是硬锚、来源存在性由原文摘录决定——**该结论不计为分歧**。"
    )[:160]
    row["disagreement"] = ""
    logger.warning("辩论结论按时间锚过滤：命中「%s」→ 由 %s 改判为维持原主张",
                   m.group(0), row["verdict_before_filter"])
    return row


def run(evidences: list[Any] | None, depth: str = "standard", max_claims: int | None = None) -> dict[str, Any] | None:
    """端到端：挑主张 → 并发辩论 → 汇总。返回 None = **未执行**（调用方须如实披露）。"""
    n = MAX_CLAIMS_BY_DEPTH.get(depth, 2) if max_claims is None else int(max_claims)
    if n <= 0:
        return None
    claims = pick_claims(evidences, n)
    if not claims:
        return None
    rows: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=min(3, len(claims))) as ex:
            for r in ex.map(debate_one, claims):
                rows.append(r)
    except Exception as e:
        logger.warning("辩论整体失败（不阻断交付）: %s", e)
    if not rows:
        return None
    cnt = {k: sum(1 for r in rows if r["verdict"] == k) for k in VERDICT_LABEL}
    # 被时间锚过滤掉的条数：**单独计数并披露**。它既不算"维持"也不算"未消解"——
    # 它是一条**因推理不合法而被撤销的质疑**，与前两者都不是一回事。
    _filtered = sum(1 for r in rows if r.get("filtered"))
    logger.info(
        "多角色辩论：辩了 %d 条（驳回 %d / 无实质质疑 %d / 修正 %d / 未消解 %d / 未完成 %d / 时间锚过滤 %d）",
        len(rows), cnt["converged_support"], cnt["no_objection"], cnt["converged_revise"],
        cnt["unresolved"], cnt["not_run"], _filtered,
    )
    return {
        "rows": rows,
        "total": len(rows),
        "counts": cnt,
        "time_filtered": _filtered,
        # 未消解 + 未完成都要"被看见"：前者是内部没吵清楚，后者是没吵成
        "open": cnt["unresolved"] + cnt["not_run"],
    }


def format_appendix_md(rep: dict[str, Any] | None) -> str:
    """辩论记录 → 报告附录。**未执行也出一段如实说明**（不出声的缺席会被读成"没争议"）。"""
    if not rep:
        return "\n".join(
            [
                "\n\n---\n\n# ⚔️ 多角色辩论（质疑 ↔ 申辩 ↔ 裁定）",
                "> **本次未执行**（档位未安排或无可辩材料）。**这不代表报告没有争议**——"
                "本节的缺席只是缺席，不构成任何背书。",
            ]
        )
    c = rep["counts"]
    lines = [
        "\n\n---\n\n# ⚔️ 多角色辩论（质疑 ↔ 申辩 ↔ 裁定）",
        f"对 **{rep['total']}** 条关键主张做了一轮完整辩论："
        f"✅ 质疑被驳回·原表述维持 **{c['converged_support']}** · "
        # 「质疑方未提出实质质疑」**单独列**，不并进"驳回"：没有反驳可驳回时记一笔胜出，
        # 是把系统的沉默当成系统的胜利（2026-09-18 审计）。
        + (f"• 质疑方未提出实质质疑 **{c['no_objection']}** · " if c.get("no_objection") else "")
        + f"✏️ 申辩方让步·结论已修正 **{c['converged_revise']}** · "
        f"⚠️ 分歧未消解 **{c['unresolved']}** · 未完成 {c['not_run']}"
        # 时间锚过滤单独列。**不许并进"维持"里**：它不是"质疑被驳回"（那要讲道理），
        # 是"这一轮质疑的推理不合法、整条撤销"——两者的可信度含义完全不同。
        + (f" · 🕐 时间锚过滤 {rep['time_filtered']}" if rep.get("time_filtered") else ""),
        "",
        *(
            [
                f"> 🕐 **本轮有 {rep['time_filtered']} 条裁定被「时间锚」过滤**：该结论建立在"
                "「当前时间尚未到达该日期 / 官网上没有这一年的数据 / 该链接为虚构」这类**现实时间信念**上。"
                "系统时钟是硬锚，来源存在性由**原文摘录**决定，而辩论双方**都无法访问外网**——"
                "所以这类指控在结构上不可证伪，判为无效并撤销该轮质疑（**已留痕，未隐藏**）。",
                "",
            ]
            if rep.get("time_filtered")
            else []
        ),
        "**为什么要有申辩环节**：单向的对抗校验里，**质疑方永远是赢家**——它提出来的每一条"
        "都被当成真问题记进报告，而被质疑的一方**没有申辩权**。一条数字被标「待核」，"
        "可能只是因为校验官没看到支撑它的那一页；单向流程里这个误判没有任何机会被纠正。"
        "本节的流程是**质疑 → 申辩 → 裁定**，每一轮都必须给出立场（撤回 / 让步 / 维持）。",
        "",
    ]
    for r in rep["rows"]:
        mark = VERDICT_LABEL.get(r["verdict"], "•")
        lines.append(f"### {mark}｜{r['claim'][:70]}")
        lines.append("")
        lines.append(f"- **引用来源**：{r.get('source') or '未标注'}")
        if r.get("objection"):
            lines.append(f"- **质疑方**（{r.get('otype') or '—'}·{r.get('severity') or '—'}）：{r['objection']}")
        else:
            lines.append("- **质疑方**：（未产出）")
        if r.get("rebuttal"):
            _c = "**承认质疑**" if r.get("concede") else "**驳回质疑**"
            lines.append(f"- **申辩方**（{_c}）：{r['rebuttal']}")
        if r.get("revised"):
            lines.append(f"- **修正后表述**：{r['revised']}")
        if r.get("why"):
            lines.append(f"- **裁定**：{r['why']}")
        if r.get("disagreement"):
            lines.append(f"- **分歧点**：{r['disagreement']}")
        # ⚠️ **没辩过的条目不许印「收敛后表述」**（2026-09-16 全量审计）。
        # 病案：`final_statement` 为空时这里回落到 `r['claim']`——于是**任一轮失败
        # （`not_run`）的条目**，附录里照样写着
        #     - **收敛后表述**：<原主张>
        # 读起来完全像"辩过了、维持原判"。而它其实**一轮都没跑完**。
        # 这一节的整个价值在于"每一轮的立场都留了痕"，**包括我们没吵清楚的那些**——
        # 把"没辩"排版成"辩完维持"，恰恰把它变成了它自己最反对的那种东西。
        if r.get("verdict") == "not_run":
            lines.append("- **本轮未完成**：质疑/申辩环节未产出结论，**这不是「维持原判」**——"
                         "该主张未经本节核验，请按未核处理。")
        else:
            lines.append(f"- **收敛后表述**：{r.get('final_statement') or r['claim']}")
        lines.append("")
    lines.append(
        "> **这一节证明什么**：报告里这些关键主张**经过了内部对撞**，且每一轮的立场（撤回/让步/维持）"
        "都留了痕——**包括我们没吵清楚的那些**。"
    )
    lines.append(
        "> **这一节不证明什么**：①辩手是模型，也会一起错；②一轮辩论不等于定论，"
        "「维持」只表示**在当前材料下质疑被驳回**，材料变了结论可能变；"
        "③标「分歧未消解 / 未完成」的条目**不应当作已核实**，它们是这份报告里明确的不确定点。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "多角色辩论：对带数字的关键主张做「质疑 → 申辩 → 裁定」一轮完整对撞"
        "（三个独立调用，避免同一模型自演和解），输出维持/修正/未消解三态与收敛后表述；"
        "未完成与未消解都如实进附录并计入不确定点。"
    )
