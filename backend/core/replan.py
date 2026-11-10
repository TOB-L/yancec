"""研策 · 动态重规划——发现缺口时，**自己决定补一轮调查**

## 这一层为什么是"自主"最硬的证据

一个固定流水线和一个自主智能体，在"跑得顺"的时候看起来一模一样。
区别只在**出问题的那一瞬间**：流水线继续往下跑、把缺口留在报告里当"数据缺口披露"；
自主智能体**回头补一轮**。

而"回头补一轮"这件事必须是**它自己判断出来的**，不能是"我们预设了第三波"。
所以本模块的分工写死：

- **触发**（要不要补）：确定性规则，零成本、可复算、可核对 —— `detect_gaps()`
- **内容**（补什么）：LLM 提议 —— `propose_subtasks()`
- **裁决**（能不能补）：规则校验 —— 类型白名单 / 去重 / 数量上限 / 档位上限
- **执行**：复用既有子代理（不新写一条取证链路）

## 为什么触发用规则而不是模型

"要不要再查一轮"如果交给模型，会得到两种坏结果：要么永远说"够了"（省事的倾向），
要么永远说"不够"（把预算烧光）。而**缺口是客观事实**，不需要判断力：

- 必需源层没命中（要 L1 官方，实际只有媒体转述）—— 这是最硬的缺口，规则一看便知
- 某个维度一条证据都没取回
- 存在未仲裁的冲突

模型只在**"补什么"**上发挥——那是真正需要判断力的部分（该换什么角度、换什么检索词）。
**把可判定的交给规则，把需要判断的交给模型**，这条分工本身就是"工程可信"的一部分。

## 诚实边界（写进附录）

1. **补一轮不等于补上了**。补充轮同样可能空手而归；空手而归会如实记进决策链
   （"补检后仍未获得 X，已在缺口披露中标注"），**不许因为"我们努力过了"就把它说成已覆盖**。
2. **补充轮产出独立章节，不与既有维度合章**。`SUPPLEMENT_PREFIX` 的作用是让
   `_chapter_key` 不撞车——**撞车的后果不是"多一章"，而是"换掉原来那一章"**
   （同键多轮会按"留首轮/留末轮"规则二选一）。所以前缀是**机制需要**，不是展示需要。
   ⚠️ **诚实纠正**：渲染时 `_clean_section_title` 会把"补充调查『』"这一层剥掉，
   所以**章节标题上看不出它是后补的**（`## 下沉市场用户真实反馈`）。
   "这一章是补充轮产出的"这件事由**自主决策链与调用台账**承载，不由标题承载——
   标题保持客户可读的干净形态。原本文档里"读者能一眼看出是后补的"是过度承诺，已改。
3. **有上限**。档位越高补得越多（标准 2 / 深入 3 / 定制 3），且**只补一轮**——
   无上限的自我补检会不收敛，而报告有交付时间。
4. **补了没补到，必须留下痕迹**。`node_summarize` 对普通空维度是静默跳过的，
   但对补充轮空产出会**显式写一节"本轮补充调查未取得可用内容"**，
   并明说"该缺口仍然存在"。理由：「试过没查到」是信息，「没提」不是。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 补充轮产出章节的标题前缀。**必须与既有维度名不撞车**，
# 否则 `_chapter_key` 会把它判成"同一维度的又一轮"，按"留首轮/留末轮"规则
# 反而会把原有章节挤掉（结果不是"补了一章"，而是"换了一章"）。
SUPPLEMENT_PREFIX = "补充调查"

# 档位 → 补充轮最多新增的子任务数（0 = 不补）
# 键必须与 `core.models.DepthLevel` 的值**逐字一致**（free/standard/deep/custom）。
# 踩过：写过 `"quick": 0` —— 档位名根本不存在，`.get()` 落到默认值，
# 于是"免费档不跑"这句话是假的（免费档照样跑）。测试里已加断言锁死。
_MAX_NEW_BY_DEPTH = {"free": 0, "standard": 2, "deep": 3, "custom": 3}

_AGENT_TYPES = ("industry", "competition", "user", "statistics")

_PROPOSE_PROMPT = """你是"研策"的首席研究员。第一轮取证已完成，现在请你**审视缺口**，决定要不要补一轮定向调查。

规则（务必遵守）：
1. 只针对**下面列出的具体缺口**提补充方向，不要趁机扩大研究范围。
2. 每个补充方向必须是一个**可检索的具体问题**，不是"再查一下XX"这种废话。
3. 检索词要**换角度**：首轮没查到，多半是检索词角度不对（比如首轮查"咖啡 门店数"没结果，
   就该换"连锁咖啡 门店总数 统计公报"或"XX公司 年报 门店"），**不要重复首轮的检索词**。
4. 明显查不到的（例如需要付费数据库、需要企业内部数据）**不要提**——那属于"缺口披露"，不属于补充调查。
5. 最多 {n} 条；一条都提不出就返回空数组（这是允许的，也是诚实的）。

输出 JSON 数组，不要任何其他文字：
[{{"topic": "补充调查的主题（≤18字，会作为章节标题）",
   "agent_type": "industry|competition|user|statistics",
   "queries": ["检索词1", "检索词2"],
   "why": "这条补充针对的是哪个缺口、换了什么角度（≤50字）"}}]

**提问**：{question}

**已完成的子任务**（不要重复覆盖）：
{existing}

**检测到的缺口**（只针对这些）：
{gaps}
"""


def _depth_of(state: dict[str, Any]) -> str:
    return getattr(getattr(state.get("plan"), "depth", None), "value", "") or "standard"


def detect_gaps(state: dict[str, Any]) -> list[dict[str, Any]]:
    """确定性缺口检测（零模型成本、可复算）。

    只报**能被客观判定**的缺口——"感觉这块不够深"不是缺口，"要 L1 结果一条 L1 都没有"才是。
    """
    gaps: list[dict[str, Any]] = []

    # ① 必需源层未命中：最硬的缺口（结论类型决定了必须用什么源，没拿到就是没拿到）
    try:
        routes = (state.get("subtask_results") or {}).get("_routes") or {}
        for sid, r in routes.items():
            if not isinstance(r, dict):
                continue
            if r.get("claim_key") and not r.get("satisfied"):
                gaps.append(
                    {
                        "kind": "route_unsatisfied",
                        "label": f"必需源层未命中：{r.get('claim_label') or r.get('claim_key')}",
                        "detail": (
                            f"子任务[{sid}] 的结论类型是「{r.get('claim_label') or ''}」，"
                            f"要求 {r.get('required_layer')}（{r.get('required_label') or ''}），"
                            f"实际只拿到 {('/'.join(r.get('hit_layers') or []) or '无')}"
                        ),
                        "severity": "high",
                        "hint": f"换检索角度去找 {r.get('required_layer')} 层的一手来源",
                    }
                )
    except Exception as e:
        logger.warning("缺口检测[源路由]失败: %s", e)

    # ② 某个维度一条证据都没取回
    try:
        for k, v in (state.get("subtask_results") or {}).items():
            if k.startswith("_") or k.endswith("__raw"):
                continue
            if isinstance(v, str) and v.strip().startswith("## ") is False and "检索失败" in v:
                gaps.append(
                    {
                        "kind": "dimension_failed",
                        "label": f"维度检索失败：{k}",
                        "detail": str(v)[:160],
                        "severity": "high",
                        "hint": "该维度的检索整轮失败，需换通道或换检索词重取",
                    }
                )
    except Exception as e:
        logger.warning("缺口检测[维度]失败: %s", e)

    # ③ 证据总量偏薄（不足 6 条支撑不了任何决策题）
    try:
        n_ev = len(state.get("evidences") or [])
        if n_ev and n_ev < 6:
            gaps.append(
                {
                    "kind": "thin_evidence",
                    "label": f"证据总量偏薄（{n_ev} 条）",
                    "detail": "决策型报告通常需要 ≥6 条可溯源证据支撑；当前证据不足以覆盖主要结论",
                    "severity": "medium",
                    "hint": "需要从更多角度补取证",
                }
            )
    except Exception as e:
        logger.warning("缺口检测[证据量]失败: %s", e)

    # ④ 存在未仲裁的冲突（数据打架却没人处理）
    try:
        for cf in state.get("conflict_results") or []:
            if isinstance(cf, dict) and not cf.get("arbitration") and cf.get("conflict_detail"):
                gaps.append(
                    {
                        "kind": "conflict_open",
                        "label": f"未仲裁冲突：{str(cf.get('claim') or '')[:30]}",
                        "detail": str(cf.get("conflict_detail") or "")[:160],
                        "severity": "medium",
                        "hint": "冲突未解决说明缺少一个能定分止争的权威来源",
                    }
                )
    except Exception as e:
        logger.warning("缺口检测[冲突]失败: %s", e)

    # 按严重度排序（high 在前），并去重（同 label 只留一条）
    _seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for g in sorted(gaps, key=lambda x: 0 if x.get("severity") == "high" else 1):
        if g["label"] in _seen:
            continue
        _seen.add(g["label"])
        out.append(g)
    return out


def _parse_json_array(txt: str) -> list[dict[str, Any]] | None:
    if not txt:
        return None
    s = re.sub(r"^```(?:json)?\s*", "", txt.strip())
    s = re.sub(r"\s*```$", "", s)
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return v
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


def propose_subtasks(
    state: dict[str, Any], gaps: list[dict[str, Any]], max_new: int
) -> list[dict[str, Any]]:
    """LLM 提议 + 规则校验 → 可执行的补充子任务（失败返回空 list，绝不阻断交付）"""
    if max_new <= 0 or not gaps:
        return []
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm():
            logger.info("动态重规划：无可用主模型，跳过（缺口仍会进『数据缺口披露』）")
            return []
        _st = list(state.get("plan").subtasks or [])
        existing = "\n".join(f"- {s.description[:70]}" for s in _st[:12]) or "（无）"
        gap_txt = "\n".join(
            f"- [{g.get('severity')}] {g.get('label')}：{g.get('detail')}（建议：{g.get('hint')}）"
            for g in gaps[:6]
        )
        llm = get_main_llm(temperature=0.3, max_tokens=900)
        msg = llm.invoke(
            [
                {"role": "system", "content": "只输出 JSON 数组，不要任何其他文字。"},
                {
                    "role": "user",
                    "content": _PROPOSE_PROMPT.format(
                        n=max_new,
                        question=(state.get("raw_question") or "")[:200],
                        existing=existing,
                        gaps=gap_txt,
                    ),
                },
            ]
        )
        arr = _parse_json_array(getattr(msg, "content", "") or "") or []
    except Exception as e:
        logger.warning("动态重规划提议失败（不影响交付）: %s", e)
        return []

    # ---- 规则校验（模型提议 + 规则裁决）----
    from graph import _chapter_key  # 局部导入：graph 反向依赖本模块，避免循环

    _existing_keys = {_chapter_key(s) for s in (state.get("plan").subtasks or [])}
    _seen_queries: set[str] = set()
    for _s in state.get("plan").subtasks or []:
        for _q in _s.search_queries or []:
            _seen_queries.add(str(_q).strip().lower())

    out: list[dict[str, Any]] = []
    for it in arr:
        if not isinstance(it, dict) or len(out) >= max_new:
            continue
        # 截断取**词边界**（2026-09-17）：同族写法 `[:24]` 在 router 那边把提问
        # 拦腰截成了一句话里两个残句（实跑病案见 `agents/router._subject_of`）。
        # 这里 topic 是模型自己拟的短标题，风险低得多，但**同一个写法不该留两处**——
        # 按边界回退一格，别切出"瑞幸2024Q"这种半截 token。
        topic = str(it.get("topic") or "").strip()
        if len(topic) > 24:
            _cut = topic[:24]
            _m = re.search(r"[A-Za-z0-9]+$", _cut)
            topic = (_cut[: _m.start()] if _m and _m.start() > 0 else _cut).rstrip("，,、：:；; ")
        at = str(it.get("agent_type") or "").strip()
        queries = [str(q).strip() for q in (it.get("queries") or []) if str(q).strip()][:4]
        why = str(it.get("why") or "").strip()[:120]
        if not topic or at not in _AGENT_TYPES or not queries:
            continue
        # 检索词去重：首轮查过的词再查一遍是纯浪费（而且会命中同一批缓存）
        queries = [q for q in queries if q.lower() not in _seen_queries][:4]
        if not queries:
            continue
        desc = f"{SUPPLEMENT_PREFIX}『{topic}』"
        _st_probe = type("_P", (), {"description": desc, "id": ""})()
        if _chapter_key(_st_probe) in _existing_keys:
            continue  # 与既有维度撞章 → 会挤掉原章节，丢弃
        out.append({"description": desc, "agent_type": at, "queries": queries, "why": why})
        _seen_queries.update(q.lower() for q in queries)
    return out


def run(state: dict[str, Any]) -> dict[str, Any]:
    """端到端：检测 → 提议 → 裁决。返回 {gaps, proposed, planned, depth, max_new}。

    **不执行**执行（执行在 graph 层，因为需要复用子代理与状态通道）。
    分成两半是为了让"决策"与"执行"可以分别测试——决策是可复算的纯函数，执行不是。
    """
    depth = _depth_of(state)
    max_new = _MAX_NEW_BY_DEPTH.get(depth, 2)
    gaps = detect_gaps(state)
    if max_new <= 0 or not gaps:
        return {"gaps": gaps, "proposed": [], "planned": [], "depth": depth, "max_new": max_new}
    planned = propose_subtasks(state, gaps, max_new)
    return {"gaps": gaps, "proposed": planned, "planned": planned, "depth": depth, "max_new": max_new}


def describe() -> str:
    return (
        "动态重规划：首轮取证后做确定性缺口检测（必需源层未命中 / 维度检索失败 / 证据偏薄 / 未仲裁冲突），"
        "有缺口时由模型提议定向补充方向、规则裁决（类型白名单/检索词去重/不撞章/数量与档位上限），"
        "再复用既有子代理补一轮。补充轮产出独立章节（标题带『补充调查』）以便读者识别，"
        "补检未果会如实记进决策链与缺口披露。"
    )
