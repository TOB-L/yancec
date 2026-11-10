"""研策 · 重复动作检测（Repeat Guard）——把"Agent 在打转"变成一条可核对的读数

## 为什么这一层必须存在，而且判据必须小心

Agent 最典型的失败模式不是"答错"，是**在一个无效的检索词上无限打转**：
预算烧光、轮次用尽，报告里却什么也没多出来。
既有护栏（返工 ≤2、预算 80% 门、检索轮上限）管的都是**总量**，
没有一条管**同一个动作被重复执行**。

但"重复检索"这个信号**不能直接当成浪费**——本系统里有两类重复是**刻意设计**的：

- **反证检索**：决策/风险类问题会追加"原词 + 风险/失败/反方"；
- **时效补检**：核心来源偏旧时用双探针补近一年。

把这两类报成"打转"，就是**把设计说成缺陷**。所以判据不是"重复了几次"，而是：

    **同一个检索词被重复调用，且其中有 ≥2 次返回 0 条新证据。**

——第一种是"又问了一遍"，第二种是"问了一遍什么都没得到，还接着问"。
后者才是空转的签名。

## 本层的输出是**读数**，不是**闸门**

它不改任何执行行为，只把这件事算出来并进审计附录。
理由是纪律性的：**改变失败行为的改动，在没有一次真实运行验证之前不许上线**
（本仓库已有 `sizing_violation` 全仓无写入方、`human_confirm` 假门两次病案）。
所以这一层先做"看得见"，"拦不拦"留给有真实样本之后再说。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 归一化：检索词的"同一个"是**语义上的同一个**，不是字符串相等。
# 去掉空白与全半角标点后按小写比对——`星巴克 下沉`、`星巴克下沉`、`星巴克，下沉`
# 是同一次检索，不该被算成三次。
_PUNCT = re.compile(r"[\s\-—_·|｜,，。.、;；:：!！?？'\"“”‘’()（）\[\]【】《》]+")


def _norm(q: str) -> str:
    return _PUNCT.sub("", str(q or "")).lower()


def detect(search_debug: list | None, *, min_empty: int = 2) -> dict:
    """扫一遍检索诊断 → 空转读数。**纯函数、零模型、零网络、不改行为。**

    `search_debug` 的元素形如 `(子任务id, 检索词, 证据数)`。

    返回：
        {"total": 总调用, "distinct": 去重后检索词数, "groups": [...空转组...],
         "wasted": 空转组涉及的调用数, "ok": bool}
    无空转 → `groups` 为空、`ok` 为 True（**"没发现"也要有一个明确的值**，
    不能让调用方分不清"没查"与"查了没有"）。
    """
    rows = [r for r in (search_debug or []) if isinstance(r, (list, tuple)) and len(r) >= 3]
    out: dict = {"total": len(rows), "distinct": 0, "groups": [], "wasted": 0, "ok": True}
    if not rows:
        return out
    buckets: dict[str, list[tuple[str, str, int]]] = {}
    for sid, q, n in rows:
        try:
            cnt = int(n)
        except Exception:
            cnt = 0
        buckets.setdefault(_norm(q), []).append((str(sid), str(q), cnt))
    out["distinct"] = len(buckets)
    for _key, items in buckets.items():
        if len(items) < 2:
            continue
        empty = [it for it in items if it[2] <= 0]
        if len(empty) >= min_empty:
            out["groups"].append({
                "query": items[0][1][:60],
                "calls": len(items),
                "empty": len(empty),
                "subtasks": sorted({it[0] for it in items})[:4],
            })
            out["wasted"] += len(items)
    out["groups"].sort(key=lambda g: -g["calls"])
    out["ok"] = not out["groups"]
    return out


def format_md(res: dict) -> str:
    """进审计附录。**没发现就印一行"未发现"**——不印会被读成"没查"。

    另外必须写清一件事：本层**不区分**刻意重复（反证检索 / 时效补检）与空转，
    只按"重复且多次为空"给读数。**把它当结论用是错的**——它是一条线索。
    """
    if not res or not res.get("total"):
        return ""
    _head = "### 🔁 检索重复度（Agent 有没有在同一个词上打转）"
    if res.get("ok"):
        return (
            f"\n{_head}\n\n> 本次共 **{res['total']}** 次检索、去重后 **{res['distinct']}** 个检索词，"
            f"**未发现空转**（没有出现「同一检索词重复 ≥2 次且其中 ≥2 次无返回」的组合）。\n"
        )
    lines = [
        f"\n{_head}", "",
        f"> 本次共 **{res['total']}** 次检索、去重后 **{res['distinct']}** 个检索词。"
        f"检出 **{len(res['groups'])}** 组空转候选，涉及 **{res['wasted']}** 次调用。", "",
        "| 检索词 | 调用次数 | 其中无返回 | 涉及子任务 |",
        "| :--- | ---: | ---: | :--- |",
    ]
    for g in res["groups"][:5]:
        lines.append(f"| {g['query']} | {g['calls']} | {g['empty']} | {'、'.join(g['subtasks'])} |")
    lines += [
        "",
        "> ⚠️ **这是一条线索，不是一个结论**：本层**不区分刻意重复与空转**——"
        "反证检索（原词+风险）与时效补检（双探针补近一年）**是有意设计的重复**，"
        "它们也会出现在上表里。判据只是「同一检索词重复 ≥2 次且其中 ≥2 次无返回」，"
        "请结合子任务类型判断。",
    ]
    return "\n".join(lines)


def describe() -> str:
    return (
        "重复动作检测：扫检索诊断，按「同一检索词重复 ≥2 次且 ≥2 次无返回」给出空转读数，"
        "进审计附录。纯确定性、零模型成本；只报读数不改执行行为。"
    )
