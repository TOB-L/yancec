"""研策 · 最小复购看板——用主办方点名的四个指标给自己的运营记账

## 为什么是这四个

赛道培训里，讲者（11 年人力资源商业化背景）明确建议参赛者把这些带上台：

> 「最小复购看板：**任务成功率 / 有效复用率 / 人工接管率 / 成功结果成本**」

它们不是随便四个数，而是一组相互制衡的诊断：
- 只看成功率 → 可以靠"什么都判 Validate"刷高；
- 只看成本 → 可以靠"降级成模板"刷低；
- 只看复用率 → 可以靠"什么都说是复用"刷高。

所以四个必须一起报，而且**每个都必须写清分母与缺测口径**。

## 本模块的纪律（与产品其余部分同一条）

1. **分母只含"测得出来的那一部分"**。早期账本里的行没有 `cost_cny`/`took_over`
   这些字段——那不是"没接管"，是"没记"。把 None 当 False 会把缺测伪装成 0%，
   比不报更糟。所以每一项都同时给出 `n`（有效样本）与 `total`（总账本数）。
2. **算不出来就说算不出来**。未配模型单价 → `cost_per_success = None` +
   一句"未配置单价"，绝不拍一个看起来专业的数字。
3. **成功 ≠ 交付**。决策清单走兜底模板的报告不计入"成功"分子——它形式上交付了，
   但那一节不是基于证据链的判定。分母口径写在 `definition` 里，可被第三方按同一
   口径复算（`core/runstats.read_ledger()` 是公开的原语）。

## 与「资产飞轮」叙事的关系

「越用越懂这个行业」这句话在 `reuse_rate` 里第一次有了分子与分母。
此前它是**话术**：行业卡确实在沉淀（`industry_kb.json` 有卡），
但"复用了几次"从来没记过账。
"""
from __future__ import annotations

from typing import Any

# 少于这个样本数就不给结论，只给原始计数（早期样本的百分比没有信息量）
MIN_SAMPLE = 3


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _process_fields_unreliable(r: dict[str, Any]) -> bool:
    """这一行的**过程字段**是不是被已知缺陷写坏的（v0.9.83 之前的流式路径）。

    ## 判据是**结构性自相矛盾**，不是人工标注

    病案（2026-09-16）：流式路径把 deliver 节点的**返回增量**当成 state 喂给
    `_build_run_stats`（见 graph.py 组装处病案），于是 `budget_spent / rework_count /
    search_rounds / stop_card / sop_fallback / asset_reused` 全部落成默认值。

    受影响的账本行会呈现一个**不可能的组合**：

        llm_tokens.calls > 0        ← 真的调了 79 次模型（这个计数是独立计量的，可靠）
        budget_spent == 0           ← 却一分预算没花
        search_rounds == 0          ← 却一次检索都没发生（同一行 citation_count=24）

    **79 次模型调用 + 0 积分 + 0 检索轮**在物理上不可能，所以这个判据是可推导的，
    不需要谁手工去标"这行可疑"。这也正是本仓库一贯的做法：**用结构判据，
    不用人工清单**（人工清单已经在这一个病上漏过五次）。

    ⚠️ **只否掉过程字段，不否掉整行**：`llm_tokens / citation_count / domain_count /
    elapsed_s / delivered` 与那条缺陷无关（token 是进程级计量器独立记的），
    仍然有效——把它们一起丢掉，等于用一个缺陷制造第二个缺陷。
    """
    _tok = r.get("llm_tokens") or {}
    try:
        _calls = int(_tok.get("calls") or 0)
    except Exception:
        _calls = 0
    return bool(
        _calls > 0
        and not float(r.get("budget_spent") or 0)
        and not int(r.get("search_rounds") or 0)
    )


def build_board(depth: str = "", rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """汇总复购看板四指标。`depth` 可传中文档位名或英文码（内部先归一）。

    病案（2026-09-15 外部审计）：docstring 原先写"传 'deep'/'standard'"，而账本里实际
    混着 `standard`（graph 写枚举值）与 `标准`（app.py 经 MODE_ALIAS 转中文）两种写法，
    这里又按**精确相等**筛选 → 按档位筛选**各丢一半**，同一个看板能算出两个不同的复用率。
    现在两端都归一：写入点用 `runstats.canon_depth`，读入侧也走它——
    **两处都归一，才不会因为某一侧漏改而重新分裂。**
    """
    if rows is None:
        try:
            from core.runstats import read_ledger

            rows = read_ledger()
        except Exception:
            rows = []
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if depth:
        try:
            from core.runstats import canon_depth

            _want = canon_depth(depth)
        except Exception:
            _want = str(depth)
        rows = [r for r in rows if canon_depth(str(r.get("depth") or "")) == _want]
    total = len(rows)

    # ---- ⓪ 过程字段被已知缺陷写坏的行：**从过程口径里剔除并报出条数** ----
    # （token/引文/耗时口径不受影响，见 `_process_fields_unreliable` 的说明）
    _bad_proc = [r for r in rows if _process_fields_unreliable(r)]
    _good = [r for r in rows if not _process_fields_unreliable(r)]
    _excl_note = ""
    if _bad_proc:
        _excl_note = (
            f"另有 **{len(_bad_proc)} 行**的**过程字段**由 v0.9.83 之前的流式路径缺陷写成"
            "（`budget_spent`/`search_rounds`/`degraded`/`took_over`/`reused_asset` 落成默认值），"
            "已从下列过程口径中剔除——判据是**结构性自相矛盾**（模型调用次数 > 0 而积分与检索轮为 0）。"
            "这些行的 token / 引文 / 耗时口径不受该缺陷影响，仍计入成本类指标。"
        )

    # ---- ① 任务成功率：分母 = **有交付字段的行**（老行没有 delivered，不进分母） ----
    s_rows = [r for r in _good if r.get("delivered") is not None]
    delivered = sum(1 for r in s_rows if r.get("delivered"))
    degraded = sum(1 for r in s_rows if r.get("degraded"))
    # 成功 = 交付了 **且** 没有降级（口径写在 definition 里，可复算）
    success = sum(1 for r in s_rows if r.get("delivered") and not r.get("degraded"))

    # ---- ② 有效复用率：命中已沉淀行业资产/研究经验的运行占比 ----
    r_rows = [r for r in _good if r.get("reused_asset") is not None]
    reused = sum(1 for r in r_rows if r.get("reused_asset"))

    # ---- ③ 人工接管率：质检 BLOCK → 停止卡片（主动停止是产品能力，不是故障） ----
    t_rows = [r for r in _good if r.get("took_over") is not None]
    took = sum(1 for r in t_rows if r.get("took_over"))

    # ---- ④ 成功结果成本：只对"配了单价且成功"的行求和 ----
    c_rows = [r for r in rows if isinstance(r.get("cost_cny"), (int, float))]
    cost_sum = round(sum(float(r["cost_cny"]) for r in c_rows), 4) if c_rows else None
    # 成本口径的「成功」同样要剔除过程字段坏掉的行——`degraded` 在这些行上恒 False，
    # 会把一次**降级交付**算成正常成功交付（这正是那条缺陷最坏的后果：方向偏"更好"）。
    ok_with_cost = [
        r for r in c_rows
        if r.get("delivered") and not r.get("degraded") and not _process_fields_unreliable(r)
    ]
    cost_per_success = (
        round(sum(float(r["cost_cny"]) for r in ok_with_cost) / len(ok_with_cost), 4)
        if ok_with_cost
        else None
    )
    # **并发抹账的行不进 token 口径**（2026-09-15）：计量器是进程级全局，
    # 两个任务同时跑会互相抹账——落账时若检测到"计量器被别的任务重置过"，
    # `llm_shared` 会被标上。那一笔的 token 数**不是本次的实测值**，
    # 拿它算"单份成本"就是把混过的数当实测值报出去（比报不出来更糟）。
    _attributable = [r for r in rows if not r.get("llm_shared")]
    tok_rows = [r for r in _attributable if isinstance(r.get("llm_tokens"), dict) and r["llm_tokens"]]
    tok_avg = None
    if tok_rows:
        _tot = [int((r["llm_tokens"] or {}).get("total_tokens") or 0) for r in tok_rows]
        _tot = [x for x in _tot if x > 0]
        tok_avg = int(sum(_tot) / len(_tot)) if _tot else None
    # 单价未配置时的**成本口径代理**：token 数是硬事实（每次调用实测），
    # 只是在换算成钱之前缺一个外部单价。此前这里直接显示"—（未测）"——
    # 那是把"我们没换算"说成了"我们没测"，比不给数更糟。有 tokens 就报 tokens。
    # 成本口径实际用到的**行**（不只是数值）——下面判"跨档混合"要用它们的 depth
    ok_cost_rows = [
        r for r in _attributable          # 并发行的 token 不可归因
        if r.get("delivered") and not r.get("degraded")
        and not _process_fields_unreliable(r)   # `degraded` 在这些行上不可信 → "成功"判不出来
        and isinstance(r.get("llm_tokens"), dict)
        and int((r["llm_tokens"] or {}).get("total_tokens") or 0) > 0
    ]
    ok_tok = [int((r["llm_tokens"] or {}).get("total_tokens") or 0) for r in ok_cost_rows]
    tok_per_success = int(sum(ok_tok) / len(ok_tok)) if ok_tok else None
    # **本次成本口径跨了几个档位**（>1 时该均值没有业务含义，见 K17）
    _cost_depths = {str(r.get("depth") or "") for r in (ok_with_cost if cost_per_success is not None else ok_cost_rows)}
    _depth_mixed = len(_cost_depths - {""}) > 1

    _el = [float(r.get("elapsed_s") or 0) for r in rows if r.get("elapsed_s")]
    elapsed_avg = round(sum(_el) / len(_el), 1) if _el else None

    _cost_metric = {
        "label": "成功结果成本",
        "value": cost_per_success,
        "unit": "元/份",
        "num_ok": len(ok_with_cost) if cost_per_success is not None else len(ok_tok),
        "definition": "仅对**配置了模型单价**且**成功**的运行求均值。"
        "单价是外部输入（不同模型/时期价格差异大），写死一个价会把成本变成看起来精确的猜测，"
        "所以未配置时不给钱数——但**token 是每次调用的实测值，照报**。",
        "note": "",
        # 与三个比率项保持**同一字段形状**：前端按 `x.low_sample` 统一渲染角标，
        # 少一个键就会让"成本项永远不显样本不足"变成一个没人注意的口径漏洞。
        "low_sample": (len(ok_with_cost) if cost_per_success is not None else len(ok_tok))
        < MIN_SAMPLE,
        # 前端据此直接渲染，不在前端复算口径。**单位与数值分开给**：
        # 合成一个字符串会在前端拼接处出现"15.6 万 tokens tokens/份"这种重复。
        "display": (
            f"¥{cost_per_success}"
            if cost_per_success is not None
            else (f"{tok_per_success / 10000:.1f} 万" if tok_per_success else "—")
        ),
        "display_unit": "元/份" if cost_per_success is not None else (
            "tokens/份" if tok_per_success else ""
        ),
        # 有几笔因"计量器被并发重置"被排除在成本口径外——**如实说出来**，
        # 否则读者看到的分母比账本行数小，却不知道为什么。
        "shared_excluded": sum(1 for r in rows if r.get("llm_shared")),
        # **跨档位求均值是被 K17 明文禁止的**（"标准档 3 笔 ≈10.0 万 / 深入档 1 笔 =21.0 万 /
        # 禁跨档求均值——没有业务含义"）。而本指标在不传 `depth` 时恰恰会跨档平均：
        # 实测 标准3笔(99,626) + 深入1笔(210,181) 混出 **127,264**，而材料一度就把这个
        # 12.7 万当成"单份成本"印出去了。**一个我们自己禁掉的算法，不该在看板上默认生效。**
        # 这里不改变返回值形状（前端四张卡按同一形状渲染），而是把口径写在 display_note 上，
        # 并且**如实说出这是几档混合**。
        "depth_mixed": _depth_mixed,
        "display_note": (
            "" if cost_per_success is not None else (
                "未配置模型单价 → 以 **token 用量**作成本口径（同为实测值，只差一次外部换算）"
                if tok_per_success
                else "本次运行未取得 token 用量（账本行早于 v0.9.75 计量上线）"
            )
        ) + (
            f"　⚠️ **本数值跨了 {len(_cost_depths - {''})} 个档位**（{('、'.join(sorted(_cost_depths - {''})))}），"
            "按 K17 口径**不得作为「单份成本」引用**——档位不同，工作量本就不同，"
            "跨档平均没有业务含义。要看可引用的单份成本，请按档位分别取（传 `depth`）。"
            if _depth_mixed else ""
        ),
    }
    # ---- 样本充足性：**按各指标自己的分母判**，不能用账本行数代替 --------------
    # 病案（2026-09-14 端到端体检）：此处原先写 `sample_ok = total >= MIN_SAMPLE`。
    # 账本 4 行即判"样本充足"，而三个比率的**真实分母都是 1**（4 行里有 3 行早于
    # v0.9.75，没记这些字段、压根不进分母）。于是界面在"样本充足"的旗号下展示
    # `100.0%`——一个 n=1 的百分比，配一句"样本 4 笔"的说明，**看起来像成绩**。
    # 账本行数 ≠ 指标分母。判充足的必须是**各指标自己的分母**，取最小值。
    _dens = [len(s_rows), len(r_rows), len(t_rows)]
    # 「成功结果成本」也是四个指标之一，它的分母同样要进"有效分母"的计算。
    # 病案（2026-09-15 外部审计）：`_dens` 原先只有三个比率的分母，于是
    # caveat 里那句「各指标的有效分母最少只有 N 笔」**把成本那一项漏掉了**——
    # 界面上可能出现"样本充足"配一个分母更小的成本数，两句话对不上。
    if cost_per_success is not None:
        _dens.append(len(ok_with_cost))
    _dens_ok = [d for d in _dens if d > 0]
    _effective_n = min(_dens_ok) if _dens_ok else 0
    _low_labels = [
        _lbl
        for _lbl, _d in (
            ("任务成功率", len(s_rows)),
            ("有效复用率", len(r_rows)),
            ("人工接管率", len(t_rows)),
        )
        if 0 < _d < MIN_SAMPLE
    ]
    if cost_per_success is not None and len(ok_with_cost) < MIN_SAMPLE:
        _low_labels.append("成功结果成本")
    _unmeasured = [
        _lbl
        for _lbl, _measured in (
            ("任务成功率", bool(s_rows)),
            ("有效复用率", bool(r_rows)),
            ("人工接管率", bool(t_rows)),
        )
        if not _measured
    ]
    if not ok_with_cost and tok_per_success is None:
        _unmeasured.append("成功结果成本")
    # 充足 = 每一项都测到了 **且** 每一项的分母都够
    sample_ok = bool(_dens_ok) and not _low_labels and not _unmeasured
    if sample_ok:
        _caveat = (
            f"账本 {total} 笔，各项有效分母均 ≥{MIN_SAMPLE}。每项的分母只含**该项测得出来**的行，"
            "早期账本缺字段的行不进分母（把缺测当 0，等于把「没记」伪装成「没有」）。"
        )
    else:
        _caveat = (
            f"账本共 {total} 笔，但**各指标的有效分母最少只有 {_effective_n} 笔**——"
            "账本行数 ≠ 指标分母：早期行没记这些字段，就不进分母。"
        )
        if _low_labels:
            _caveat += (
                f"不足 {MIN_SAMPLE} 笔的指标：{'、'.join(_low_labels)}——"
                "**这些百分比还没有信息量**，只能当作埋点已通的证明，不能当成绩读。"
            )
        if _unmeasured:
            _caveat += f"尚未埋点：{'、'.join(_unmeasured)}。"
    if _excl_note:
        # **剔除必须说出来**（本仓库纪律：静默截断会被读成"全都算进去了"）
        _caveat += " " + _excl_note

    return {
        "total": total,
        "excluded_bad_process_fields": len(_bad_proc),
        "effective_n": _effective_n,
        "depth_filter": depth or "全部档位",
        "metrics": {
            "task_success_rate": {
                "label": "任务成功率",
                "value": _rate(success, len(s_rows)),
                "num": success,
                "den": len(s_rows),
                "low_sample": 0 < len(s_rows) < MIN_SAMPLE,
                "definition": "成功 = 产出可交付报告 **且** 决策清单未走兜底模板。"
                "降级交付形式上完成、但那不是基于证据链的判定，故不计入成功。",
                "note": f"其中 {degraded} 笔因决策清单降级未计为成功" if degraded else "",
                "display": (
                    "—" if not s_rows else f"{success / len(s_rows) * 100:.1f}%"
                ),
                "display_unit": "",
                "display_note": "" if s_rows else "账本行早于 v0.9.75，未记「是否交付」字段",
            },
            "reuse_rate": {
                "label": "有效复用率",
                "value": _rate(reused, len(r_rows)),
                "num": reused,
                "den": len(r_rows),
                "low_sample": 0 < len(r_rows) < MIN_SAMPLE,
                "definition": "本次调研命中**已沉淀的行业卡或研究经验**即计一次复用"
                "（账本字段 `reused_asset`——注意它与图状态键 `asset_reused` 不是同一个名字，"
                "落账时由 graph 改名；写定义串时要用**账本侧**的名字，否则第三方按它复算第一步就卡住）。"
                "这是「越用越懂这个行业」唯一的账。",
                "note": "",
                "display": "—" if not r_rows else f"{reused / len(r_rows) * 100:.1f}%",
                "display_unit": "",
                "display_note": "" if r_rows else "账本行早于 v0.9.75，未记「是否复用资产」字段",
            },
            "takeover_rate": {
                "label": "人工接管率",
                "value": _rate(took, len(t_rows)),
                "num": took,
                "den": len(t_rows),
                "low_sample": 0 < len(t_rows) < MIN_SAMPLE,
                "definition": "质检判定 BLOCK → 挂停止卡片交还人工。"
                "**主动停止是产品能力，不是故障**——它出现在这里，是为了让「拦住过几次」可核对。",
                "note": "",
                "display": "—" if not t_rows else f"{took / len(t_rows) * 100:.1f}%",
                "display_unit": "",
                "display_note": "" if t_rows else "账本行早于 v0.9.75，未记「是否人工接管」字段",
            },
            "cost_per_success": _cost_metric,
        },
        "context": {
            "avg_elapsed_s": elapsed_avg,
            "avg_total_tokens": tok_avg,
            "tokens_per_success": tok_per_success,
            "cost_sum_cny": cost_sum,
            # **这句均值的档位构成**：跨 >1 档时必须由渲染面标出来（K17 禁跨档求均值）。
            # 放在这里而不是让 `render_md` 再读一遍账本——同一次汇总只读一次账，
            # 免得将来两侧口径各改一半。
            "avg_tokens_depths": sorted({str(r.get("depth") or "") for r in rows} - {""}),
        },
        "sample_ok": sample_ok,
        "caveat": _caveat,
    }


def render_md(board: dict[str, Any] | None = None) -> str:
    """看板 → markdown（进报告附录 / PPT / 说明稿）。无样本也如实出，不印空壳。"""
    b = board or build_board()
    m = b["metrics"]
    c = b["context"]
    lines = [
        "# 📈 最小复购看板（任务成功率 / 有效复用率 / 人工接管率 / 成功结果成本）",
        f"口径：{b['depth_filter']}｜账本 **{b['total']}** 笔 · 各指标有效分母最少 **{b.get('effective_n', 0)}** 笔"
        "｜数据源：`data/run_stats.jsonl`（每笔运行一行，可被第三方按同一口径复算）",
        "",
        "| 指标 | 值 | 分子/分母 | 说明 |",
        "|---|---|---|---|",
    ]

    def _fmt(x):
        # 用服务端算好的 display：口径只在一处定义，前后端不各算一遍
        if x.get("display") and x["display"] != "—":
            _u = x.get("display_unit") or ""
            return f"{x['display']}" if _u in ("", "元/份") else f"{x['display']} {_u}"
        return "—（未测）"
    _t = m["task_success_rate"]
    _r = m["reuse_rate"]
    _k = m["takeover_rate"]
    _c = m["cost_per_success"]

    def _lown(x) -> str:
        """样本不足时把分母**贴在数字旁边**——否则报告里被单独摘出来引用的一定是那个裸百分比"""
        return " ⚠️样本不足" if x.get("low_sample") else ""

    lines.append(
        f"| 任务成功率 | **{_fmt(_t)}**{_lown(_t)} | {_t['num']}/{_t['den']} | {_t['definition']} |"
    )
    lines.append(
        f"| 有效复用率 | **{_fmt(_r)}**{_lown(_r)} | {_r['num']}/{_r['den']} | {_r['definition']} |"
    )
    lines.append(
        f"| 人工接管率 | **{_fmt(_k)}**{_lown(_k)} | {_k['num']}/{_k['den']} | {_k['definition']} |"
    )
    lines.append(
        f"| 成功结果成本 | **{_fmt(_c)}**{_lown(_c)} | 成功 {_c['num_ok']} 笔 | {_c['definition']} |"
    )
    lines.append("")
    _ctx = []
    if c.get("avg_elapsed_s") is not None:
        _ctx.append(f"平均耗时 {c['avg_elapsed_s']}s")
    if c.get("avg_total_tokens"):
        # ⚠️ **这一行也必须标出跨档**（2026-09-16）。它是**全部行**的平均（含各档位），
        # 而 K17 明文禁止跨档求均值——实测这个位置印的正是 **127,264**，
        # 也就是材料里那个被禁的「12.7 万」。一个我们自己在材料里禁掉的数，
        # 不该由我们自己的渲染器原样吐出来（下一份材料照抄它就合规了，而它是错的）。
        _depths_ctx = list(c.get("avg_tokens_depths") or [])
        _ctx.append(
            f"平均 {c['avg_total_tokens']:,} tokens"
            + (f"（**跨 {len(_depths_ctx)} 个档位混合**：{'、'.join(_depths_ctx)}——"
               "禁作为单份成本引用，按档位看请传 `depth`）" if len(_depths_ctx) > 1 else "")
        )
    if c.get("cost_sum_cny") is not None:
        _ctx.append(f"累计成本 ¥{c['cost_sum_cny']}")
    if _ctx:
        lines.append("**过程读数**：" + "｜".join(_ctx))
        lines.append("")
    # 缺测原因逐条注明（"未测"与"没记"是两回事，读者要知道是哪一种）
    _notes = [f"{v['label']}：{v['display_note']}" for v in m.values() if v.get("display_note")]
    if _notes:
        lines.append("**未测原因**：" + "；".join(_notes))
        lines.append("")
    lines.append(f"> ⚠️ {b['caveat']}")
    lines.append(
        "> **这一节不证明什么**：它证明的是「这四个指标有定义、有分母、能被复算」，"
        "**不证明它们好看**。样本还小，任何百分比都应当被当作埋点已通的证据，而不是成绩。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "最小复购看板：从 run_stats 账本汇总主办方点名建议带上台的四个指标"
        "（任务成功率 / 有效复用率 / 人工接管率 / 成功结果成本），"
        "每项写清定义与分母、缺测单独标注、算不出的一律给 None 而非猜测值。"
    )
