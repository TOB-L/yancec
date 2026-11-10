"""研策 · 引用蕴涵核验（claim 级）——从"链接活着"升级到"内容真的支持这句话"

## 这一层补的是什么洞

`core/verification.py` 回答的是**链接层**：这条 URL 还活着吗、证书可信吗、页面是不是
变成了软 404/词典页。它证明不了任何结论——**链接活着 ≠ 那句话被这一页支持**。

而这恰恰是主流 AI 搜索的主要失败模式：给出来的引用大多**能打开**，
但打开之后与它写的那句话**无关、或干脆相反**。外部审计（arXiv:2604.03173，
10 家商用模型 / 69,557 条引用）测到的引用错误率区间是 **11.4%–56.8%**。
所以「我们的链接通过率高」在说明桌上会被一句话问穿：
**"链接能打开，跟你的结论有什么关系？"**

这一层就是回答那句话的：对每一条「正文主张 ↔ 该主张所引来源的原文摘录」跑一次
LLM 判定，输出三态（support / partial / unrelated / contradict），并给出
**通过率**与逐条理由。它是本数据源层**唯一一处真正的 LLM 环节**——
其余环节全是正则与确定性规则，因为那些地方要的是可复算；
而"这句话和这段原文是不是一个意思"是**语义判断**，正则做不了，只能上模型。

## 为什么它敢叫"核验"而不是"自评"

判定用的是 **Source.excerpt（起诉前抓下来的原文摘录）**，不是让模型重新回忆。
摘录在取证时落库（`agents/researchers.py`，最长 400 字符），**事后不可被报告正文改写**——
所以这是"拿当初的原文"对"现在写的话"，而不是让模型给自己打分。

## 诚实边界（三条，写进附录）

1. **覆盖率**：只有"带原文摘录的来源"可比对。没摘录的引用**不计入通过率**，
   也不冒充通过——它们进 `unjudged`，单独列出。
2. **未判定 ≠ 通过**：模型调用失败/解析失败的条目一律进 `unjudged`。
   把失败算成通过，就是本产品一直在批评的"静默带病出干净账单"。
3. **判定者是模型**：它也会错。所以每条判定都附**理由 + 原文引用片段**，
   让人工能在 30 秒内推翻它——可推翻的结论才叫可核验的结论。

## 成本与开关

按批调用（默认 6 对/次，最多 4 批并发），默认最多 24 对。无模型时整段跳过（不出空表）。
`ENTAILMENT_ENABLED=0` 可全局关闭。
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# 三态 + 未判定 + **仅登记线索**。**顺序有意义**：support 才算通过。
VERDICTS = ("support", "partial", "unrelated", "contradict", "unjudged", "index_only")
_PASS = ("support",)
_WARN = ("partial",)
_FAIL = ("unrelated", "contradict")

# "登记线索"标记：取证层明确标注"这条只证明某文件存在、未抽取正文"。
# 它**不是一条主张**，拿它去比"原文支不支持主张"是问错了问题——
# 判它 ❌ 会把自己的取数短板算成引用错误（虚报错误率），
# 判它 ✅ 更是撒谎（它什么都没证明）。所以单列一类、**不计入通过率**。
_INDEX_MARK = "登记线索"

_VERDICT_LABEL = {
    "support": "✅ 支撑",
    "partial": "⚠️ 部分支撑",
    "unrelated": "❌ 不相关",
    "contradict": "❌ 相悖",
    "unjudged": "• 未判定",
    "index_only": "🔖 仅登记线索",
    "time_anchor": "🕐 判定作废（时间锚）",
}

# ============================================================
# 时间锚：**不许用"这个日期不可能存在"当判定理由**
# ============================================================
# 病案（2026-09-18 实跑，用户在界面里跑出来的那一份）：
#   国家统计局 2026-09-15 发布的「2026年1—8月份社会消费品零售总额增长1.1%」，
#   被核验官判成 **❌ 不相关**，理由原文：
#     「摘录含未来时间（2026年），属虚构或错误数据，非真实官方发布，无法作为事实依据。」
#   而当时系统日期是 **2026-09-18** —— 那是一条**已经发布三天的真实官方数据**。
#
# 这与 v0.9.81 修过的那条是同一个病：**模型拿自己的知识截止去否定系统时钟**。
# 当时修的是 RedTeam（`core/adversarial.py`）与辩论裁定（`core/debate.py::_filter_time_anchor`），
# **蕴涵核验这一层没有**——于是同一个病在这里复发，而且后果更难看：
# 报告会把"国家统计局的真实发布"公开印成"虚构数据"。
#
# 判据刻意做成**机械的、只认措辞**的，与辩论层同源：
# 只有裁定理由里出现"该日期不可能存在/内容虚构"这类**现实时间信念**时，才撤销该判定。
# **撤销 ≠ 判通过**：它进 `time_anchor` 一档，既不算通过也不算失败，并单独印出条数与理由。
_TIME_DENIAL = re.compile(
    r"虚构|未来时间|含未来|属于未来|尚未发生|尚未发布|还没发生|并未发布|"
    r"非真实官方发布|超出(?:训练|知识)|训练截止|知识截止|日期不存在|不存在的日期|"
    r"尚未存在|尚不存在"
)


def _filter_time_denial(hit: dict[str, Any]) -> tuple[str, str, str]:
    """裁定建立在"该日期不可能存在"之上 → 作废该判定（**不改成通过**）。

    与 `core/debate.py::_filter_time_anchor` 同一条纪律：
    **系统时钟是硬锚，来源存在性由原文摘录决定，而判定方无法访问外网**——
    所以"这个日期不可能有数据"这类指控在结构上不可证伪，不能作为判定依据。
    """
    v = str(hit.get("verdict") or "")
    why = str(hit.get("why") or "")
    if v not in ("unrelated", "contradict"):
        return v, why, str(hit.get("quote") or "")
    m = _TIME_DENIAL.search(why)
    if not m:
        return v, why, str(hit.get("quote") or "")
    logger.warning("蕴涵判定按时间锚作废：命中「%s」→ 由 %s 撤销", m.group(0), v)
    return (
        "time_anchor",
        f"⚠️ 已按**时间锚**作废原判定「{v}」：原理由建立在「{m.group(0)}」这类"
        "**现实时间信念**上。**你的知识截止早于本系统时钟**——凡是你认为「未来」的日期，"
        "先按真实存在对待，只比对摘录本身与主张是否一致。"
        "**本条既不计入通过率，也不计入失败。**",
        "",
    )


def _date_anchor_line() -> str:
    """给核验官的**系统日期锚**。不写死日期，取运行时当天。"""
    try:
        from datetime import date as _d

        t = _d.today().isoformat()
    except Exception:
        t = ""
    return (
        f"\n\n**当前系统日期：{t}**。你的训练数据截止**早于**本系统时钟，"
        "所以**不要用「这个日期不可能有数据 / 内容虚构」当判定理由**——"
        "凡是你认为「未来」的日期，一律先按真实存在对待，"
        "只比对**摘录本身**与主张在数字/单位/时点/口径/主体上是否一致。"
        "摘录里出现的日期晚于你的知识截止，**不构成任何减分项**。"
    )


# 行业参照（外部研究）。**只作量级参照，不作胜负结论**——语料不同、判定人不同。
INDUSTRY_BASELINE = (
    "**行业参照（都是外部研究结论，本报告未独立复核其原始数据）**——"
    "这一层之所以必须给出参照，是因为「我们的支撑率是多少」在 2026 年已经可以横向比了，"
    "自说自话的数字站不住：\n"
    "> · **URL 层**：10 家商用大模型/深度研究智能体、**53,090–168,021 条**引用，"
    "幻觉 URL **3%–13%**、整体无法解析 **5%–18%**"
    "（arXiv:2604.03173，2026-04）。\n"
    "> · **主张层**：**761,495** 个可评估的「引用 ↔ 断言」配对，**30.6%** 的引用扭曲了其来源、"
    "**27.1%** 来自领域不匹配的来源（arXiv:2605.28565，2026-05）。\n"
    "> · **系统间差异**：ICLR 2026 的 **DeepTRACE** 用语句级分析评测多家 deep research 产品，"
    "引用准确率区间 **40%–80%**。\n"
    "> · **遗漏是主导失败模式**：Google AI Overviews 的 **98,020** 条 atomic claim 实测中，"
    "**11.0%** 不被所引页面支持，而失败**主要形式是「该引而没引」而非「引错」**"
    "（arXiv:2605.14021，2026-05）。\n"
    "> · **可对齐的口径**：DeepResearch Bench 的 **FACT** 框架把「被正确支持的引用占比」"
    "定义为 Citation Accuracy——本节口径与它同层。\n"
    "> 口径差异必须说清：上述研究问的是「引用是否真实存在、是否支撑原句」，"
    "与本节的「正文主张 ↔ 原文摘录」比对同层，故可作**量级参照**；"
    "但语料不同、判定人不同（他们是人工+规则，本节是模型判定并公开理由），"
    "**不是同一把尺子量出来的两个数，请勿直接换算成胜负**。"
    "另需注意 HALLMARK（arXiv:2607.18360）的结论：**决定引用核验器能否部署的是"
    "假阳性率而非召回率**——所以本节同时报「未判定/登记线索」的数量，不把它们算作通过。"
)

_JUDGE_PROMPT = """你是"研策"的**引用蕴涵核验官**。判断一组「正文主张 ↔ 引用来源原文摘录」中，
**该摘录是否真的支撑该主张**。这是引用可信度的最后一道闸：链接能打开不代表内容支持结论。

判定四选一（**严格**，宁可判轻不可判过）：
- `support`：摘录**明确支撑**主张。关键数字、单位、时点、口径、主体范围全部一致。
- `partial`：**方向对但有出入**——数字量级或口径有差、时点偏旧/偏新、少了关键限定词、
  或摘录只覆盖主张的一部分（如主张讲全国、摘录只讲某市）。
- `unrelated`：摘录与主张**没有实质关系**（引错页、摘录是导航/目录/免责声明、
  或只是同一主题但不是这句话的依据）。
- `contradict`：摘录与主张**相反**（如主张"增长"而摘录是下降，主张"门店 3500 家"而摘录是 6000 家）。

判定要点（逐条查，别跳）：
1. **数字**：数值、单位（亿/万/%）、时间范围是否与主张一致？量级差一档就是 partial 或 contradict。
2. **时点**：摘录的数据期与主张声称的年份是否一致？把 2023 的数说成 2025 的 → 至少 partial。
3. **口径**：全国 vs 某省、含加盟 vs 不含加盟、营收 vs 总交易额（GMV）——口径不同即 partial。
4. **主体**：是这家公司/这个行业吗？张冠李戴 → unrelated。
5. **摘录本身的形态**：摘录可能是结构化抽取结果（含"数据表1""本地计算"等字样），
   只要其中的数字/事实能支撑主张就算 support，不要因为它不是散文就判 unrelated。
6. **不许用推算凑出支撑**（v0.9.77 加，来自一次校准实测）：
   若主张里的数字**没有直接出现在摘录中**，而是要靠你**自己算一遍**才能对上
   （如摘录给"某市 4.2 万家、占全国两成"，主张写"全国约 18.6 万家"——
   4.2÷20%≈21 万，于是判 support），**这不算 support**。
   理由：**推算的每一步都是你自己的假设**（那个"两成"是否精确、口径是否可比、四舍五入到哪一位），
   读者看到的是"原文这么说"，而实际上原文没这么说。
   这种情况判 `partial`，并在 `why` 里写明「需推算，原文未直述」。

对每一对输出一个对象：
{"i": <编号>, "verdict": "support|partial|unrelated|contradict", "why": "<≤45字，说明判定依据>", "quote": "<从摘录中截取的支撑或反驳片段，≤60字；没有则空串>"}

**只输出 JSON 数组**，不要任何其他文字、不要 markdown 代码块。"""


def _enabled() -> bool:
    return str(os.environ.get("ENTAILMENT_ENABLED", "1")).strip() not in ("0", "false", "False", "")


def _as_dict(x: Any) -> dict[str, Any] | None:
    """证据条目 → dict。

    **必须归一化，不能假设是 dict**（2026-09-14 实跑踩中）：
    同一份 `state["evidences"]` 在两条路径上是**两种类型**——
    流式路径（`run_task_stream`）会把 Evidence 覆盖成 dict；
    同步路径（`run_task` / `/api/execute` / `_fulltest`）传的是 **pydantic `Evidence` 实例**。
    只测了其中一条路径，另一条就会抛 `'Evidence' object has no attribute 'get'`
    （`core/audit_bundle.py` 里为同一个坑写过同样的注释——这是本仓库第二次踩它）。
    """
    if isinstance(x, dict):
        return x
    try:
        return x.model_dump(mode="json")
    except Exception:
        return None


def build_pairs(evidences: list[dict[str, Any]] | None, max_pairs: int = 24) -> list[dict[str, Any]]:
    """证据 → 可比对的 (主张, 原文摘录) 对。

    排序策略：**带数字的主张优先**。核验资源有限时，优先核那些"写错了会出错的"——
    纯定性主张（"竞争激烈"）判错了代价低，而"门店 3500 家""增速 6.4%"判错了会直接误导决策。
    """
    out: list[dict[str, Any]] = []
    for _raw in evidences or []:
        ev = _as_dict(_raw)
        if not ev:
            continue
        claim = str(ev.get("claim") or "").strip()
        if not claim or claim.startswith("【反方】"):
            continue
        for s in ev.get("sources") or []:
            if not isinstance(s, dict):
                continue
            ex = str(s.get("excerpt") or "").strip()
            if len(ex) < 40:  # 太短没有比对价值（多为标题回填），不硬凑
                continue
            # 登记线索（索引条目）不参与判定：它没有"主张"可比，硬判两头都是撒谎
            _idx = _INDEX_MARK in ex or _INDEX_MARK in claim
            out.append(
                {
                    "claim": claim[:300],
                    "excerpt": ex[:700],
                    "title": str(s.get("title") or "")[:80],
                    "publisher": str(s.get("publisher") or "")[:40],
                    "url": str(s.get("url") or "")[:200],
                    "level": str(s.get("level") or ""),
                    "query": str(ev.get("query") or "")[:80],
                    "verdict": "index_only" if _idx else "",
                    "why": (
                        "引用页为**登记线索（索引元数据）**，未抽取正文，不存在可比对的主张——"
                        "不计入通过率；需要数值请改走该主体的结构化财务/年报正文抽取"
                    ) if _idx else "",
                    "quote": "",
                }
            )
    # 数字密度高者优先（数字多的主张更可能是硬结论），同级保持原顺序（稳定性）
    out.sort(key=lambda p: -len(re.findall(r"\d", p["claim"])))
    return out[: max(1, int(max_pairs))]


def _parse_json_array(txt: str) -> list[dict[str, Any]] | None:
    """从模型输出里抠出 JSON 数组（容忍 ```json 围栏与前后废话）"""
    if not txt:
        return None
    s = txt.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else None
    except Exception:
        pass
    m = re.search(r"(?s)\[.*\]", s)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else None
    except Exception:
        return None


def _judge_batch(batch: list[dict[str, Any]], idx0: int) -> dict[int, dict[str, Any]]:
    """一批（≤6 对）→ {全局下标: {verdict, why, quote}}。失败返回 {}（**不冒充通过**）"""
    from core.llm import get_main_llm, has_main_llm

    if not has_main_llm():
        return {}
    payload = []
    for j, p in enumerate(batch):
        payload.append(
            {
                "i": j + 1,
                "主张": p["claim"],
                "引用来源": (p["publisher"] or p["title"] or "未标注"),
                "来源层级": p["level"],
                "原文摘录": p["excerpt"],
            }
        )
    user = (
        "请逐对判定下列引用是否支撑对应主张。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    for attempt in (1, 2):
        try:
            llm = get_main_llm(temperature=0.0, max_tokens=1400)
            msg = llm.invoke(
                [
                    {"role": "system", "content": _JUDGE_PROMPT + _date_anchor_line()},
                    {"role": "user", "content": user if attempt == 1 else user + "\n\n只输出 JSON 数组。"},
                ]
            )
            arr = _parse_json_array(getattr(msg, "content", "") or "")
            if not arr:
                continue
            got: dict[int, dict[str, Any]] = {}
            for it in arr:
                if not isinstance(it, dict):
                    continue
                try:
                    j = int(it.get("i")) - 1
                except Exception:
                    continue
                if not (0 <= j < len(batch)):
                    continue
                v = str(it.get("verdict") or "").strip().lower()
                if v not in ("support", "partial", "unrelated", "contradict"):
                    continue
                got[idx0 + j] = {
                    "verdict": v,
                    "why": str(it.get("why") or "")[:120],
                    "quote": str(it.get("quote") or "")[:160],
                }
            if got:
                return got
        except Exception as e:
            logger.warning("引用蕴涵判定失败（第%d次，该批转未判定）: %s", attempt, e)
    return {}


def judge_pairs(pairs: list[dict[str, Any]], batch: int = 6, workers: int = 4) -> list[dict[str, Any]]:
    """并发分批判定（就地写回 pairs 的 verdict/why/quote）。未判定的保持 verdict='unjudged'。

    `index_only`（登记线索）**不进模型**：它没有可比对的主张，送去判定既浪费调用、
    又会得到一个无意义的四态结论。原样保留，由 `summarize` 单列。
    """
    if not pairs:
        return pairs
    todo = [p for p in pairs if p.get("verdict") != "index_only"]
    if not todo:
        return pairs
    batch = max(1, min(10, int(batch)))
    batches = [(i, todo[i : i + batch]) for i in range(0, len(todo), batch)]
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as ex:
            futs = [ex.submit(_judge_batch, b, i) for i, b in batches]
            merged: dict[int, dict[str, Any]] = {}
            for f in futs:
                try:
                    merged.update(f.result(timeout=180) or {})
                except Exception as e:
                    logger.warning("引用蕴涵批次异常（该批转未判定）: %s", e)
    except Exception as e:
        logger.warning("引用蕴涵判定整体失败（全部转未判定）: %s", e)
        merged = {}
    for i, p in enumerate(todo):
        hit = merged.get(i)
        if hit:
            # **时间锚过滤**（2026-09-18）：裁定若建立在"该日期不可能存在"上 → 作废该判定。
            # 见 `_filter_time_denial` 的病案——不修它，报告会把国家统计局的真实发布
            # 公开印成"虚构数据"。
            _v, _w, _q = _filter_time_denial(hit)
            if _v != str(hit.get("verdict") or ""):
                p["verdict_before_filter"] = str(hit.get("verdict") or "")
            p["verdict"] = _v
            p["why"] = _w
            p["quote"] = _q
        else:
            p["verdict"] = "unjudged"
            p["why"] = "未完成判定（模型不可用/调用或解析失败）——**不计入通过率，也不视为通过**"
            p["quote"] = ""
    return pairs


def summarize(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """判定结果 → 计数与通过率。

    分母口径（三件事必须分开，混在一起就是数字骗人）：
    - **已判定**（四态齐全）→ 进分母
    - **未判定**（模型失败）→ 不进分母，也不算通过
    - **仅登记线索** → 不进分母：它没有可比对的主张，"判错/判对"都是问错了问题
    """
    _four = ("support", "partial", "unrelated", "contradict")
    judged = [p for p in pairs if p.get("verdict") in _four]
    unjudged = [p for p in pairs if p.get("verdict") == "unjudged"]
    index_only = [p for p in pairs if p.get("verdict") == "index_only"]
    # **时间锚作废**单列一档（2026-09-18）：原理由是"这个日期不可能存在"这类
    # 现实时间信念——既不该算通过，也不该算失败。**撤销不是洗白**，理由照印。
    time_anchor = [p for p in pairs if p.get("verdict") == "time_anchor"]
    counts = {k: sum(1 for p in judged if p.get("verdict") == k) for k in _four}
    n = len(judged)
    return {
        "pairs": len(pairs),
        "judged": n,
        "unjudged": len(unjudged),
        "index_only": len(index_only),
        "time_anchor": len(time_anchor),
        "counts": counts,
        "pass_rate": round(counts["support"] / n, 4) if n else 0.0,
        "support_rate": round((counts["support"] + counts["partial"]) / n, 4) if n else 0.0,
        "items": pairs,
    }


def run(evidences: list[dict[str, Any]] | None, max_pairs: int = 24, batch: int = 6) -> dict[str, Any] | None:
    """端到端：证据 → 配对 → 并发判定 → 汇总。

    返回 `None` = **未能执行**（开关关闭 / 无模型 / 配不出任何可比对的对）。
    调用方必须把 None 当"没做"来披露，而不是当"全部通过"——
    这是本模块存在的前提，别在调用点把它抵消掉。
    """
    if not _enabled():
        logger.info("引用蕴涵核验：ENTAILMENT_ENABLED=0，跳过")
        return None
    try:
        from core.llm import has_main_llm

        if not has_main_llm():
            logger.info("引用蕴涵核验：无可用主模型，跳过（不冒充通过）")
            return None
    except Exception:
        return None
    pairs = build_pairs(evidences, max_pairs=max_pairs)
    if not pairs:
        logger.info("引用蕴涵核验：无「带原文摘录」的可比对条目，跳过")
        return None
    judge_pairs(pairs, batch=batch)
    rep = summarize(pairs)
    logger.info(
        "引用蕴涵核验：已判定 %d/%d 对，通过率 %.1f%%（±部分支撑后 %.1f%%）",
        rep["judged"], rep["pairs"], rep["pass_rate"] * 100, rep["support_rate"] * 100,
    )
    return rep


def _row(p: dict[str, Any]) -> str:
    claim = (p.get("claim") or "").replace("|", "／").replace("\n", " ")
    why = (p.get("why") or "").replace("|", "／").replace("\n", " ")
    src = (p.get("publisher") or p.get("title") or "未标注").replace("|", "／")
    return f"| {_VERDICT_LABEL.get(p.get('verdict'), '•')} | {claim[:70]} | {src[:24]} | {why[:60]} |"


def format_appendix_md(rep: dict[str, Any] | None) -> str:
    """核验结果 → 报告附录。`rep is None`（未执行）**也出一段如实说明**——
    不出声的缺席，读者会默认"这块没问题"。"""
    if not rep:
        return "\n".join(
            [
                "\n\n---\n\n# 🧷 引用蕴涵核验（主张 ↔ 原文）",
                "> **本次未执行**：需要可用的主模型且证据中带原文摘录；本次条件不满足。"
                "**这不等于引用全部通过**——本节的缺席只是缺席，不构成任何背书。",
            ]
        )
    c = rep["counts"]
    lines = [
        "\n\n---\n\n# 🧷 引用蕴涵核验（主张 ↔ 原文）",
        f"对 **{rep['pairs']}** 组「正文主张 ↔ 其所引来源的原文摘录」做了逐条判定"
        f"（已完成 **{rep['judged']}** 组，未完成 {rep['unjudged']} 组，"
        f"另有 {rep.get('index_only', 0)} 组为登记线索不参与判定"
        + (f"，**{rep.get('time_anchor', 0)} 组按时间锚作废**" if rep.get("time_anchor") else "")
        + "）："
        f"✅ 明确支撑 **{c['support']}** · ⚠️ 部分支撑 {c['partial']} · "
        f"❌ 不相关 {c['unrelated']} · ❌ 相悖 {c['contradict']}",
        "",
        *(
            [
                f"> 🕐 **本组有 {rep['time_anchor']} 条判定被「时间锚」作废**：核验官给出的理由"
                "是「该日期不可能存在 / 内容虚构」这类**现实时间信念**——而**核验官的知识截止"
                "早于本系统时钟**，它认为「未来」的日期里，有相当一部分是因为它没见过。"
                "这类指控在结构上不可证伪，故撤销该判定。"
                "**撤销不等于判通过**：它们既不计入通过率，也不计入失败，理由是照印的。"
                "（同一个病此前在对抗校验层修过一次，2026-09-18 在这一层复发并补上。）",
                "",
            ]
            if rep.get("time_anchor")
            else []
        ),
        f"**引用支撑率（明确支撑 / 已判定）= {rep['pass_rate'] * 100:.1f}%**"
        f"（计入部分支撑为 {rep['support_rate'] * 100:.1f}%）"
        if rep["judged"]
        else "**本次无可判定的主张**（所有可配对的引用页都是登记线索/索引元数据）——"
        "**这不等于通过**，只是没有可比对的内容。",
        "",
        # **这个数最容易被读大**（GPT 评审 2026-09-15 单独点名）。
        # 「引用支撑率 92.9%」放在一份 2.4 万字的报告里，读者会顺手记成"报告 92.9% 可信"——
        # 而它的分母只是**被选入核验的那 28 对**，与"报告里有多少句话有据"是两件事。
        # 更危险的是这份报告自己的遗漏层同时报着"另有 17 处裸断言"——
        # **两个数并排看才拦得住误读**。
        # 所以这里把"分母是什么、不是什么"写死在数字下面，不靠读者自己推。
        "> ⚠️ **这个数不是『报告可信度』**：分母是**被选入核验的 claim-source 对**"
        f"（本次 {rep['judged']} 对），不是报告的全部断言。"
        "准确读法是：**「在核对过的那些引用里，有多少条被原文支撑」**。"
        "报告里还有大量没有引用、因此**根本不进这一层**的断言行——"
        "它们的规模由《遗漏层》单独报出（两者必须并排看才不会被误读）。"
        "另外这一层只判『引用是否支撑原句』，**不判该句本身是否成立**。",
        "",
        "**判定方法**：把取证时落库的**原文摘录**（最长 400 字符，事后不可被正文改写）"
        "与该条主张交给独立判定的模型逐对比对，按「数字 / 单位 / 时点 / 口径 / 主体」五项核，"
        "输出四态之一并附理由与原文片段——**每条判定都可被人工在 30 秒内推翻**。",
        "",
        "| 判定 | 正文主张 | 引用来源 | 判定依据 |",
        "| :--- | :--- | :--- | :--- |",
    ]
    # 先列问题项（读者最该看的），再列通过项（抽样，不全量刷屏）
    bad = [p for p in rep["items"] if p.get("verdict") in _FAIL or p.get("verdict") == "unjudged"]
    warn = [p for p in rep["items"] if p.get("verdict") in _WARN]
    good = [p for p in rep["items"] if p.get("verdict") in _PASS]
    for p in bad + warn:
        lines.append(_row(p))
    for p in good[:6]:
        lines.append(_row(p))
    if len(good) > 6:
        lines.append(f"| … | 其余 {len(good) - 6} 组判定为「明确支撑」，不再逐条列出 | | |")
    _idx = [p for p in rep["items"] if p.get("verdict") == "index_only"]
    if _idx:
        lines.append("")
        lines.append("### 🔖 仅登记线索（不参与通过率）")
        lines.append(
            f"另有 **{len(_idx)}** 条引用页是**登记线索**（索引条目/申报元数据，未抽取正文）："
            "它们只证明「该文件存在」，**不构成任何数值或事实的依据**。"
            "这类条目**既不算通过、也不算失败**——拿它去比「原文支不支持主张」是问错了问题；"
            "列在这里，是因为**读者有权知道报告里有几条引用其实只是指路牌**。"
        )
        lines.append("")
        lines.append("| 判定 | 正文主张 | 引用来源 | 说明 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for p in _idx[:8]:
            lines.append(_row(p))
        if len(_idx) > 8:
            lines.append(f"| … | 其余 {len(_idx) - 8} 条同类，不再逐条列出 | | |")
    lines.append("")
    lines.append(
        "> **这一节证明什么**：报告里这些关键主张，**其引用来源的原文确实（或确实不）支持它**——"
        "不是「链接能打开」（那是上一节的事），而是「打开之后那句话站得住」。"
    )
    lines.append(
        "> **这一节不证明什么**：①判定者是模型，也会出错——所以每条都附了理由与原文片段，"
        "**可以被推翻的结论才是可核验的结论**；②只覆盖「带原文摘录」的来源"
        f"（本次 {rep['judged']} 组），**未覆盖的不计入通过率、也不视为通过**；"
        "③「部分支撑」多因口径/时点/范围有出入，属**需人工确认**，不要当成已核实。"
    )
    lines.append("")
    lines.append("> " + INDUSTRY_BASELINE)
    return "\n".join(lines)
