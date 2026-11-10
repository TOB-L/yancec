"""研策 · 情景与敏感性分析（Scenario & Sensitivity）——把咨询方法论里最硬的那一段变成确定性代码

## 为什么是这一层

传统咨询（MBB）的看家本领不是报告更长，而是**敏感性分析**：
结论不是"会不会成"，而是"**在什么条件下会翻，以及哪个条件最关键**"。
这正是"AI 写建议"与"专业判断"之间那道最后的沟。

而它之所以长期没做，是因为它需要**参数化 + 可重算**——
恰好我们已经有：`MarketSizingParams`（Pydantic 校验过的结构化参数）
+ `_compute_tam_sam_som`（确定性计算，LLM 不参与算数）。
**所以这一层不需要新提示词、不需要模型、不需要联网**——它是纯算术。

## 三条纪律（写完就锁）

1. **公式只有一处出处**。本模块**不复制任何公式**——计算函数由调用方传进来
   （`compute`）。仓库里已经有过"同一个公式三处实现"的病案，本模块不制造第四处。
2. **扰动幅度必须公开**。`rel` 是**我们定的**（默认 ±15%），不是行业标准——
   所以它必须印在输出里，而不是藏在代码里。**把一个自定参数说成方法论，就是我们最反对的那种事。**
3. **不许说满**。本层只报"在这组假设下、这些变量、这个幅度内，结论是否被击穿"。
   它**不预测概率**，也不给"最可能"——那需要真实分布，我们没有。

## 为什么方向是"减法"

敏感性分析的价值在于**削弱自信**：一个在 ±15% 内就翻的结论，
比一个"看起来数据很全"的结论**更值得被标出来**。
所以本层的输出会进决策区：**击穿红线 → 交给完整性闸门处理**（只降不升）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 默认扰动幅度。**这是我们定的，不是行业标准**——输出里必须原样印出这一句。
DEFAULT_REL = 0.15
# 单变量上下扰动的方向词（报告里用中文，避免"up/down"被读成"好坏"）
DIR_LABEL = {"down": "下浮", "up": "上浮"}


def _perturb(base: dict[str, float], key: str, factor: float) -> dict[str, float]:
    p = dict(base)
    try:
        p[key] = float(p[key]) * (1.0 + factor)
    except Exception:
        pass
    return p


def analyze(
    base: dict[str, Any],
    compute: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    variables: list[str] | None = None,
    rel: float = DEFAULT_REL,
    redline: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    metric_keys: tuple[str, ...] = ("tam", "sam", "som"),
    top_n: int = 5,
) -> dict[str, Any]:
    """情景 + 单变量敏感性。**纯函数、零模型、零网络。**

    · `base`      —— 基准参数（如 `MarketSizingParams` 转成的 dict）
    · `compute`   —— **调用方传进来的确定性计算函数**（公式唯一出处）
    · `variables` —— 要扰动的参数名；不给则取 base 里所有数值键
    · `redline`   —— 可选：给定指标 → (是否击穿, 说明)。**决策红线由调用方定义**
    · `top_n`     —— 敏感性表最多列几行（按影响力排序）

    返回结构固定，便于渲染与断言：
        {"ok": bool, "rel": float, "scenarios": {...}, "sensitivity": [...],
         "break": {...} | None, "notes": [...]}
    """
    out: dict[str, Any] = {
        "ok": False, "rel": rel, "scenarios": {}, "sensitivity": [], "break": None, "notes": [],
    }
    if not isinstance(base, dict) or not base:
        out["notes"].append("参数为空，未做情景分析")
        return out
    if not callable(compute):
        out["notes"].append("未提供确定性计算函数，未做情景分析")
        return out

    def _safe(p: dict[str, Any]) -> dict[str, Any] | None:
        """算不出来就返回 None——**不许拿一个半成品当作"情景"**。"""
        try:
            r = compute(p)
            return r if isinstance(r, dict) and r.get("tam") else None
        except Exception as e:
            logger.debug("情景计算失败（该情景丢弃）: %s", e)
            return None

    _b = _safe(base)
    if not _b:
        out["notes"].append("基准情景算不出，未做情景分析")
        return out
    out["ok"] = True
    out["scenarios"]["基准"] = _b

    if variables is None:
        variables = [k for k, v in base.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    variables = [v for v in variables if isinstance(base.get(v), (int, float))][:12]

    # ---- ① 三档情景：**所有变量同向**移动 ----
    # 为什么"同向"才是悲观/乐观：单变量各自乱动会互相抵消，得到一张"看起来稳"的表——
    # 那是假稳健。悲观 = 所有不利方向同时发生，这才是压力测试。
    for name, f in (("乐观", abs(rel)), ("悲观", -abs(rel))):
        _p = dict(base)
        for v in variables:
            _p = _perturb(_p, v, f)
        _r = _safe(_p)
        if _r:
            out["scenarios"][name] = _r

    # ---- ② 单变量敏感性：逐个动，量它对结论的影响力 ----
    rows: list[dict[str, Any]] = []
    for v in variables:
        _d, _u = _safe(_perturb(base, v, -abs(rel))), _safe(_perturb(base, v, abs(rel)))
        if not _d or not _u:
            continue
        # 影响力 = 该变量单独把 SOM 拉开的幅度（相对基准）。选 SOM 而不是 TAM：
        # **决策关心的是"我能拿到多少"，不是"盘子有多大"**。
        _base_som = _b.get("som") or 0
        _swing = 0.0
        if _base_som:
            _swing = abs((_u.get("som") or 0) - (_d.get("som") or 0)) / _base_som
        rows.append({
            "var": v,
            "down": _d.get("som"),
            "up": _u.get("som"),
            "swing": round(_swing, 4),
        })
    rows.sort(key=lambda r: -r["swing"])
    out["sensitivity"] = rows[:top_n]
    out["n_variables"] = len(rows)

    # ---- ③ 决策红线：有没有哪个情景把它击穿 ----
    if callable(redline):
        for name, r in out["scenarios"].items():
            try:
                broken, why = redline(r)
            except Exception:
                continue
            if broken:
                out["break"] = {"scenario": name, "why": str(why)[:120]}
                break

    out["notes"].append(
        f"扰动幅度 ±{abs(rel) * 100:.0f}% 为**本系统的设定**，不是行业标准；"
        f"情景为**同向**移动（悲观 = 全部往不利方向），它是一次**压力测试**，"
        f"不是概率预测。"
    )
    return out


def format_prompt_block(res: dict[str, Any]) -> str:
    """情景结果 → **给模型的指令块**（与 `format_md` 分开，不是抄一遍）。

    为什么要分两种渲染：`format_md` 是**给读者的发现**（带 `###` 标题与表格，
    准备进报告正文/附录）；而这里是**给模型的指令**（"这些数你可以引、怎么引"）。
    把读者版直接倒进提示词，等于让模型去"抄一段排版"——
    它可能抄对、也可能抄成一个不完整的表，而我们无从核对。
    **同一份数据、两个消费者、两种形态**：读者要的是结论，模型要的是约束。
    """
    if not res or not res.get("ok"):
        return ""
    _sc = res.get("scenarios") or {}
    _b, _o, _p = _sc.get("基准"), _sc.get("乐观"), _sc.get("悲观")
    if not (_b and _o and _p):
        return ""
    _lines = [
        "",
        "【情景与敏感性（同样由系统算好，正文可引用；**禁止自行改动或重算**）】",
        f"- 三档情景（全部参数**同向**变动 ±{res['rel'] * 100:.0f}%）："
        f"悲观 SOM ≈ {_p['som']:,} 亿元 · 基准 ≈ {_b['som']:,} 亿元 · 乐观 ≈ {_o['som']:,} 亿元",
    ]
    if res.get("sensitivity"):
        _top = res["sensitivity"][0]
        _lines.append(
            f"- 影响力最大的变量是 `{_top['var']}`"
            f"（它单独 ±{res['rel'] * 100:.0f}% 就使 SOM 变化约 {_top['swing'] * 100:.0f}%）"
        )
    if res.get("break"):
        _lines.append(
            f"- ⚠️ **「{res['break']['scenario']}」情景击穿了本层一致性红线**："
            f"{res['break']['why']}。正文**不得**按基准情景下结论，必须写明这一条。"
        )
    _lines.append(
        f"- 口径纪律：±{res['rel'] * 100:.0f}% 是**本系统的设定**、不是行业标准；"
        "情景是**压力测试**，不是概率预测——正文**禁止**写成「发生概率」或「最可能」。"
    )
    return "\n".join(_lines)


def format_md(res: dict[str, Any], metric_label: str = "SOM（可获得市场）") -> str:
    """情景与敏感性 → 报告片段。算不出来就一个字不印（**不印空表**）。"""
    if not res or not res.get("ok"):
        return ""
    lines = [
        "",
        "### 📉 情景与敏感性分析（结论在什么条件下会翻）",
        "",
        "| 情景 | " + " | ".join(k for k in ("tam", "sam", "som") if k in (res["scenarios"].get("基准") or {})) + " |",
    ]
    _keys = [k for k in ("tam", "sam", "som") if k in (res["scenarios"].get("基准") or {})]
    lines.append("| :--- | " + " | ".join("---:" for _ in _keys) + " |")
    for name in ("基准", "乐观", "悲观"):
        r = res["scenarios"].get(name)
        if not r:
            continue
        lines.append("| **" + name + "** | " + " | ".join(f"{r.get(k):,}" for k in _keys) + " |")
    if res.get("sensitivity"):
        lines += [
            "",
            f"**单变量敏感性**（逐个 ±{res['rel'] * 100:.0f}%，按对 {metric_label} 的影响力排序；共试了 {res.get('n_variables', 0)} 个变量）：",
            "",
            "| 变量 | 下浮后 " + metric_label.split("（")[0] + " | 上浮后 " + metric_label.split("（")[0] + " | 影响力 |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for row in res["sensitivity"]:
            lines.append(
                f"| `{row['var']}` | {row['down']:,} | {row['up']:,} | **{row['swing'] * 100:.1f}%** |"
            )
    if res.get("break"):
        lines += [
            "",
            f"> ⚠️ **有一个情景击穿了决策红线**：「{res['break']['scenario']}」情景下，"
            f"{res['break']['why']}。该结论**不得按基准情景陈述**。",
        ]
    for n in res.get("notes", []):
        lines += ["", f"> {n}"]
    return "\n".join(lines)


def describe() -> str:
    return (
        "情景与敏感性分析：在**结构化参数 + 确定性计算**之上做三档情景（基准/乐观/悲观，同向压力测试）"
        "与单变量敏感性排序，并判定是否有情景击穿调用方定义的决策红线。"
        "不引入新提示词、不依赖模型、可离线复算；扰动幅度为公开设定而非行业标准。"
    )
