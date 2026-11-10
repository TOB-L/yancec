"""研策 · 工具自主选择——让智能体**自己决定这次该用哪几个数据源**

## 为什么这一层必须真的改变行为，否则就是装饰

`SourceSpec.feeds` 这一列从建表起就写着"这个源喂哪些研究维度"，
但全仓搜一遍会发现：**它只被渲染进"数据源体系"面板，从来没参与过任何路由决策。**
（`grep '\.feeds'` 的两处命中，一处是 API 序列化，一处是面板文案。）

于是取证层的实际行为是：`fetch_authority` 按**自己的一套触发词**依次判断每个源族要不要打，
与"这次子任务到底需要什么"没有直接关系。这不是错，但它意味着
**"智能体在挑工具"这句话是假的**——它没有挑，是固定顺序在跑。

这一步要把它变成真的：
- 模型**从已接入的源里挑**（按 `feeds` 与该子任务维度的相关性过滤出候选）；
- 规则**强制并集**进 `CLAIM_ROUTES` 要求的必需层对应的源（模型选漏了也不许降级）；
- 选出来的名单**真的传进取证层**，决定这次打哪些源族。

## 三道护栏（缺一条都会出事）

1. **fail-open**：无模型 / 解析失败 / 名单为空 → 返回 `None`，取证层退回"全打"的既有行为。
   **宁可多打几个源，也不能因为一次选工具失败而让报告缺证据。**
2. **必需层强制并集**：`CLAIM_ROUTES` 说这类结论必须有 L1/L2 的源，那么该层的源
   **必须进名单**，无论模型选了没有。这是"模型提议 + 规则裁决"里规则的实权部分。
3. **跳过必留痕**：被跳过的源族要在**调用台账**里留一行"工具计划未选中，本次跳过"。
   不记的话，台账里"0 条"会被读成"这个源死了/回空了"——
   **主动不查**和**查了没查到**是两件事，混在一起就是对读者的误导。

## 与 `route_query` 的分工

- `route_query`（既有）：**定层级**——这类结论**必须**用什么层级的源（L1/L2/…）。是"资格线"。
- 本模块：**选具体工具**——在合格范围里，这次实际打哪几个。是"战术选择"。
  两者是串联关系：先有资格线，再在线上挑。本模块**读取** `route_query` 的结果做强制并集，
  **不修改**它。

## 作用范围（**别overclaim**）

名单**实际约束的是"权威源直取"那一层**（`fetch_authority`：巨潮/SEC/统计局/地域统计局/
行业协会/政府采购/上证e互动/国际组织）——这一层是"主动发请求去取"，跳过的收益最直接。

**检索层（通义千问 / 博查 / Bing）始终运行**，不受本名单约束：它们是"广域发现"，
按子任务挑掉它们会让报告失去覆盖面，收益为负。

所以对外表述应当是「**智能体自己决定这次去哪些权威源取证**」，
**不是**「智能体自己决定用哪些工具」——后者会让读者以为检索层也被挑了。措辞上别越界。

## 聚合项与成员项的别名（实跑踩中）

注册表里地域统计源记的是**一条聚合项 `region_stats`**（面板上显示为一个源），
而取证层按 `tjj_*` 逐个判定（杭州/南京/宁波…各自独立）。
于是模型选了 `region_stats`——一个最自然的选择——`_want("tjj_hangzhou")` 却为 False，
**16 个地方统计局被全体跳过**。已在 `authority_fetch._want` 里加双向别名归并。
这条不加，工具选择就不是"少打几个源"，而是"把整个地域源层静默关掉"。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 一次子任务最多打几个源族（并发请求与耗时都随它线性增长）
MAX_TOOLS = 5
# 提供给模型的候选上限（太多会让它挑花眼，也拖长 prompt）
MAX_CANDIDATES = 14

_PICK_PROMPT = """你是"研策"的取证调度员。请为下面这个**具体子任务**挑选本次该调用哪些数据源。

原则：
1. **按能不能拿到这个子任务要的东西来挑**，不是按"哪个源更权威"——权威但问不出这个问题的源没有用。
2. 每个源的 `feeds` 是它**能提供**的研究维度，`note` 写了它的**边界与限制**（务必读，边界比能力更重要）。
3. 挑 2-{mx} 个。**宁少勿滥**：打一个用不上的源既浪费请求、也会往台账里灌噪音。
4. 若某个源虽然相关但 note 里写明"仅覆盖 XX"而本子任务不在 XX 范围内，**不要选它**。
5. 只输出 JSON，不要其他文字。

输出格式：
{{"tools": ["源id1", "源id2"], "why": "一句话说明为什么是这几个（≤60字）"}}

**研究问题**：{question}
**本子任务**：{subtask}
**主结论类型**：{claim_label}（要求 {required_layer} 层：{required_label}）

**可选数据源**（id ｜ 名称 ｜ 层级 ｜ 能提供 ｜ 边界）：
{cands}
"""


@dataclass
class ToolPlan:
    """一次取证的工具选择结果（可审计对象）"""

    tools: list[str] = field(default_factory=list)      # 选中的源 id（有序）
    why: str = ""
    trigger: str = "llm"                                 # llm / rule / hybrid
    forced: list[str] = field(default_factory=list)      # 规则强制并集进来的
    dropped: list[str] = field(default_factory=list)     # 模型选了但不存在/未接入的

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "why": self.why,
            "trigger": self.trigger,
            "forced": list(self.forced),
            "dropped": list(self.dropped),
        }


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


def _candidates(subtask_desc: str, queries: list[str], required_layer: str) -> list[Any]:
    """候选源 = 已接入的源里，`feeds` 与"本子任务文本"有交集的那些。

    交集判定用**维度词是否出现在子任务描述/检索词里**，不是把 `feeds` 当精确键去查——
    因为子任务描述是模型自由生成的（"调研『市场规模测算』（方法论：TAM/SAM/SOM…）"），
    不会有哪个字段能精确对上。这是最容易写成"永远匹配不到"的一步。
    """
    try:
        from core.source_registry import connected_sources

        srcs = connected_sources()
    except Exception:
        return []
    text = f"{subtask_desc or ''} " + " ".join(queries or [])
    scored: list[tuple[int, Any]] = []
    for s in srcs:
        _hit = sum(1 for f in (s.feeds or ()) if f and f in text)
        _lay = 2 if (required_layer and s.layer == required_layer) else 0
        scored.append((_hit + _lay, s))
    # 有命中的排前面；一条都没命中的也保留（子任务描述可能与维度词措辞不同），
    # 但排在后面——否则"描述换个说法"就会导致候选为空、工具选择直接失效。
    scored.sort(key=lambda x: -x[0])
    return [s for _n, s in scored[:MAX_CANDIDATES]]


def select_tools(
    question: str,
    subtask_desc: str,
    queries: list[str] | None = None,
    required_layer: str = "",
    claim_label: str = "",
) -> ToolPlan | None:
    """选工具。**返回 None = 未执行**（调用方必须 fail-open 退回默认行为）。

    为什么把"未执行"和"选了个空名单"分开：前者是"没挑，所以全打"，
    后者是"挑了，一个都不打"——后者会让这一轮取证颗粒无收。
    这两种情况的处置完全相反，混在一个返回值里必然出事。
    """
    cands = _candidates(subtask_desc, list(queries or []), required_layer)
    if not cands:
        return None
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm():
            return None
        cand_txt = "\n".join(
            f"- {s.id} ｜ {s.name} ｜ {s.layer} {s.layer and ''}"
            f" ｜ 能提供：{'、'.join(s.feeds) or '（未标注）'}"
            f" ｜ 边界：{(s.note or '')[:64]}"
            for s in cands
        )
        llm = get_main_llm(temperature=0.2, max_tokens=500)
        msg = llm.invoke(
            [
                {"role": "system", "content": "只输出 JSON，不要任何其他文字。"},
                {
                    "role": "user",
                    "content": _PICK_PROMPT.format(
                        mx=MAX_TOOLS,
                        question=(question or "")[:160],
                        subtask=(subtask_desc or "")[:200],
                        claim_label=claim_label or "（未判定）",
                        required_layer=required_layer or "（未要求）",
                        required_label=_layer_label(required_layer),
                        cands=cand_txt,
                    ),
                },
            ]
        )
        obj = _parse_obj(getattr(msg, "content", "") or "")
    except Exception as e:
        logger.warning("工具选择失败（fail-open，退回默认取证）: %s", e)
        return None
    if not isinstance(obj, dict):
        return None

    valid = {s.id for s in cands}
    picked: list[str] = []
    dropped: list[str] = []
    for t in obj.get("tools") or []:
        t = str(t).strip()
        if not t or t in picked:
            continue
        (picked if t in valid else dropped).append(t)
    # ---- 规则强制并集：必需层的源一个都不能少（模型选漏了也不许降级）----
    forced: list[str] = []
    if required_layer:
        try:
            from core.source_registry import connected_sources

            for s in connected_sources():
                if s.layer == required_layer and s.id not in picked:
                    picked.append(s.id)
                    forced.append(s.id)
        except Exception:
            pass
    if not picked:
        # 模型回了个空名单或全是无效 id → 视为"未执行"，fail-open
        logger.info("工具选择未产出有效名单（模型输出：%s），退回默认取证", str(obj)[:120])
        return None
    # **强制源必须在截断之前就落位**（2026-09-15 修，来自一次外部对抗审计）。
    # 病案：`picked` 的顺序是「模型选的在前、规则强制的追加在末尾」，而这里写作
    # `picked[: max(MAX_TOOLS, len(forced))]`——模型选满 MAX_TOOLS 个时，
    # 切片恰好把**追加在末尾的强制源**整段切掉（`max()` 只在 len(forced) ≥ MAX_TOOLS 时才救得回来）。
    # 后果：对外承诺的「结论类型要求的层级由规则强制并集，模型选漏了也不许降级」，
    # 在最需要它的场景下**静默失效**——报告里写着 6 个权威源必选，真正调用的只剩 5 个模型自选。
    # 这类"承诺与执行不一致"正是本项目最不能出的错：它不报错，只是不生效。
    # 修法：**强制源先落位**，可选源只吃剩下的额度；forced 本身超出上限时，上限让位于 forced。
    _forced_set = set(forced)
    _optional = [t for t in picked if t not in _forced_set]
    _final = forced + _optional[: max(0, MAX_TOOLS - len(forced))]
    return ToolPlan(
        tools=_final,
        why=str(obj.get("why") or "")[:140],
        trigger="hybrid" if forced else "llm",
        forced=forced,
        dropped=dropped,
    )


def _layer_label(layer: str) -> str:
    try:
        from core.source_registry import LAYERS

        return LAYERS.get(layer, "")
    except Exception:
        return ""


def describe() -> str:
    return (
        "工具自主选择：按子任务的 `feeds` 相关性筛出候选数据源，由模型挑选本次实际调用哪几个"
        "（读每个源的边界说明，宁少勿滥），再由规则强制并集进 CLAIM_ROUTES 要求的必需层源。"
        "无模型/解析失败一律 fail-open 退回默认取证；被跳过的源族在调用台账里留一行，"
        "以免把「主动不查」误读成「查了没查到」。"
        "**作用范围**：只约束「权威源直取」那一层；广域检索层（千问/博查/Bing）始终运行——"
        "对外表述是「自己决定去哪些权威源取证」，不是「自己决定用哪些工具」。"
    )
