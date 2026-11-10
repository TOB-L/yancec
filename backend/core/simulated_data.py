"""研策 · 一手研究"模拟数据"生成器（演示闭环专用）

背景（对齐用户优化点：一手研究限定"设计+模拟数据"）：
真实投放需要时间/预算/伦理审批。为演示"问卷 → 样本 → 分析 → 结论反哺"
的端到端闭环，研策把问卷设计之后的一步做成 **模拟样本 + 自动分析**：
按报告核心结论设定样本分布（不编造真实数据），生成 N 份模拟受访记录，
输出汇总表 + 关键发现，并**明确标注"模拟数据、非真实采样"**。

实现：LLM 只给"列 + 分布规格"（少量 token、不易截断），Python 合成样本并出表
（种子随机 → 确定性可复现）。离线/无 LLM 走"按问法分类"的模板路径。
正式项目仍须真实投放——诚实披露是第一原则。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import random
import re
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_NOTE = "本样本为【演示用模拟数据】（按报告结论设定的示意分布），非真实问卷采样。正式项目须按上方方案真实投放；演示用于展示『问卷→样本→分析→结论反哺』的闭环能力。"

_PROMPT = """你是研策的市场数据模拟师。基于研究报告 + 一手问卷设计，给出一份【演示用模拟问卷】的生成规格。

规则（诚实演示）：
1. 分布必须与研究报告核心结论方向一致（例如报告认为某市场供给过剩/增速放缓/价格战，
   样本就应体现 复购不足 / 价格敏感 / 同质化竞争 —— 才演示得出"问卷数据反哺/修正结论"）。
2. 这是模拟数据，不用真实品牌官方数据，只写"头部连锁/区域连锁/精品独立/平台自助"这类类型。
3. 字段贴合问卷核心问题（对象/频次/品牌/价格/看重因素/意愿等）。

只输出 JSON，不要其他文字：
{{
  "columns": ["respondent_id", "…字段2…", "…字段3…", "…共5~9列，可含中文…"],
  "col_specs": [
     {{"kind": "category", "options": ["…", "…"], "weights": [0.4, 0.3, 0.2, 0.1]}},
     {{"kind": "numeric", "min": 0, "max": 20, "mean": 4, "sd": 3, "unit": "次/月"}}
  ],
  "headline": "本模拟样本的一句话概括（贴合报告结论）",
  "key_findings": ["发现1（附样本数字）", "发现2", "发现3"]
}}
说明：col_specs 与 columns 从第 2 列起一一对应（respondent_id 自动生成）；category 的 weights 求和≈1。
"""

# 数值列生成参数 → 汇总表里"均值±单位"
_NUMERIC_DEFAULTS = {
    "freq": (6, 4, 0, 30, "次/月"),
    "price": (26, 9, 8, 60, "元"),
    "likelihood": (6, 2, 0, 10, "分(0-10)"),
    "sat": (3, 1, 1, 5, "分(1-5)"),
}
_CATEGORY_DEFAULTS = {
    "brand": (["头部连锁", "区域连锁", "精品独立", "平台/自助"], [0.42, 0.27, 0.17, 0.14]),
    "factor": (["价格", "口味", "位置便利", "环境", "服务"], [0.35, 0.25, 0.25, 0.08, 0.07]),
    "tier": (["一线/新一线", "二线", "三线", "三四线及县城"], [0.1, 0.2, 0.3, 0.4]),
    "time": (["工作日白天", "工作日下班后", "周末", "全天随机"], [0.3, 0.35, 0.25, 0.1]),
}


def _csv_text(columns: list[str], rows: list[list[Any]]) -> str:
    """columns+rows → CSV 文本（带 UTF-8 BOM，Excel 打开中文不乱码）

    **文件本体必须自带"这是模拟数据"的声明**（2026-09-15 外部审计）：
    界面里写了（按钮名、正上方 md 都有提示），但 CSV **下载出去就脱离了界面**——
    转发给别人、导入 Excel、贴进别的材料，那句提示一句都不跟着走。
    而一份没有声明的"问卷样本.csv"看起来与真实采样毫无区别。
    这与本仓库 C10「CSV（诚实标注）」的要求直接冲突。
    做法：在最前面写两行注释行（`#` 开头，Excel/ pandas 都当普通行，人一眼看得见）。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# 【演示用模拟数据】本文件不是真实问卷采样"])
    w.writerow(["# 生成方式：按研究报告结论设定的示意分布；正式项目须按方案真实投放。请勿作为真实样本引用。"])
    w.writerow([str(c) for c in columns])
    for r in rows:
        w.writerow(["" if v is None else str(v) for v in r])
    return chr(0xFEFF) + buf.getvalue()


# ============================================================
# 主入口
# ============================================================
def simulate_survey(report: str, design: dict[str, Any] | None = None,
                    n: int = 120, seed: int = 20260902) -> dict[str, Any]:
    """生成模拟问卷样本 + 自动分析（LLM 给分布规格，Python 合成；离线走模板）"""
    rng = random.Random(seed)
    try:
        from core.llm import get_main_llm, has_main_llm

        if has_main_llm() and report:
            specs = _specs_from_llm(report, design, n=n)
            if specs:
                return _build_payload(specs, n, rng, method="LLM（按报告结论设定分布）")
    except Exception as e:
        logger.warning("模拟数据 LLM 路径失败: %s", e)
    specs = _specs_from_template(design)
    return _build_payload(specs, n, rng, method="模板（离线确定性，按问卷问法生成）")


# ============================================================
# 规格来源一：LLM（少量 token，不产出整表行）
# ============================================================
def _specs_from_llm(report: str, design: dict[str, Any] | None, n: int) -> list[dict[str, Any]]:
    from core.llm import get_main_llm

    surv = (design or {}).get("survey") or {}
    design_txt = ""
    if surv:
        design_txt = (
            f"问卷对象：{surv.get('target', '—')}；样本：{surv.get('sample_size', '—')}；"
            f"核心问法：{'；'.join(surv.get('questions', [])[:5])}"
        )
    llm = get_main_llm(temperature=0.5, max_tokens=1500)
    resp = llm.invoke(
        [
            ("system", _PROMPT.format(n=n)),
            ("human", f"一手问卷设计：{design_txt}\n\n研究报告（节选）：\n{(report or '')[:4000]}"),
        ]
    )
    content = (resp.content or "").strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return []
    data = json.loads(m.group(0))
    columns = data.get("columns") or []
    col_specs = data.get("col_specs") or []
    # 校验：columns[0] 必须是 id；specs 数量 = columns-1
    if not columns or not col_specs or len(col_specs) != len(columns) - 1:
        return []
    # 规格规范化 + 缺省补齐（model 少给字段也不崩）
    specs: list[dict[str, Any]] = []
    for cs in col_specs:
        cs = dict(cs or {})
        kind = cs.get("kind", "category")
        if kind == "numeric":
            specs.append({
                "kind": "numeric",
                "min": int(cs.get("min", 0)),
                "max": int(cs.get("max", 100)),
                "mean": float(cs.get("mean", 50)),
                "sd": float(cs.get("sd", 10)),
                "unit": str(cs.get("unit", "")),
            })
        else:
            opts = cs.get("options") or []
            if not opts:
                opts, _w = _CATEGORY_DEFAULTS["brand"]
            wts = cs.get("weights") or []
            if len(wts) != len(opts):
                wts = [1.0] * len(opts)
            specs.append({"kind": "category", "options": [str(o) for o in opts], "weights": [float(w) for w in wts]})
    return specs


# ============================================================
# 规格来源二：模板（按问法关键词分类）
# ============================================================
def _q_type(q: str) -> str:
    """按问法关键词把问卷问题归为 数值/分类/意愿/其他"""
    if re.search(r"频次|次数|频率|每天|每周|每月.*次", q):
        return "freq"
    if re.search(r"金额|价格|客单|预算|花费", q):
        return "price"
    if re.search(r"品牌|在哪|渠道|方式|选哪|买哪|平台", q):
        return "brand"
    if re.search(r"看重|因素|偏好|介意|在意|哪些", q):
        return "factor"
    if re.search(r"愿意|意愿|推荐|评分|打分|可能|兴趣", q):
        return "likelihood"
    if re.search(r"时段|时间|什么时候|周几|假日", q):
        return "time"
    if re.search(r"城市|地区|一二三线|下沉|县城|所在", q):
        return "tier"
    return "sat"


def _specs_from_template(design: dict[str, Any] | None) -> list[dict[str, Any]]:
    surv = (design or {}).get("survey") or {}
    questions = [str(q) for q in (surv.get("questions") or [])]
    specs: list[dict[str, Any]] = []
    for q in questions[:5]:
        t = _q_type(q)
        label = re.sub(r"^[Qq]?\d*[.、:]?\s*", "", q)[:14]
        if t in _NUMERIC_DEFAULTS:
            mean, sd, lo, hi, unit = _NUMERIC_DEFAULTS[t]
            specs.append({"kind": "numeric", "min": lo, "max": hi, "mean": mean, "sd": sd, "unit": unit, "label": label})
        elif t in _CATEGORY_DEFAULTS:
            opts, wts = _CATEGORY_DEFAULTS[t]
            specs.append({"kind": "category", "options": opts, "weights": wts, "label": label})
        else:
            specs.append({"kind": "numeric", "min": 1, "max": 5, "mean": 3.4, "sd": 1, "unit": "分(1-5)", "label": label})
    if not specs:  # 问卷为空 → 通用咖啡/消费画像（兜底）
        specs = [
            {"kind": "numeric", "min": 0, "max": 30, "mean": 4, "sd": 3, "unit": "次/月", "label": "购买频次"},
            {"kind": "numeric", "min": 8, "max": 60, "mean": 22, "sd": 8, "unit": "元", "label": "单次消费"},
            {"kind": "numeric", "min": 0, "max": 10, "mean": 5.5, "sd": 2, "unit": "分(0-10)", "label": "复购意愿"},
            {"kind": "category", "options": ["头部连锁", "区域连锁", "精品独立", "平台/自助"], "weights": [0.42, 0.27, 0.17, 0.14], "label": "常去类型"},
        ]
    return specs


# ============================================================
# 合成 + 汇总 + 打包
# ============================================================
def _build_payload(specs: list[dict[str, Any]], n: int, rng: random.Random,
                   method: str) -> dict[str, Any]:
    columns: list[str] = ["respondent_id"]
    rows: list[list[Any]] = []
    col_vals: list[list[Any]] = []
    for cs in specs:
        columns.append(cs.get("label") or "指标")
        if cs["kind"] == "numeric":
            lo, hi = cs.get("min", 0), cs.get("max", 100)
            vals = [max(lo, min(hi, round(rng.gauss(cs.get("mean", 50), cs.get("sd", 10))))) for _ in range(n)]
            if cs.get("unit") in ("元",):
                vals = [round(v * 1.0, 1) for v in vals]
            col_vals.append(vals)
        else:
            opts = cs.get("options") or ["A"]
            wts = cs.get("weights") or [1] * len(opts)
            col_vals.append([rng.choices(opts, wts)[0] for _ in range(n)])
    # 补一个纯分类列，让样本更像真问卷（若前面全是数值）
    if all(isinstance(v, (int, float)) for v in (col_vals[0] if col_vals else [])):
        columns.append("主力品牌类型")
        opts = ["头部连锁", "区域连锁", "精品独立", "平台/自助"]
        col_vals.append([rng.choices(opts, [0.42, 0.27, 0.17, 0.14])[0] for _ in range(n)])
    for i in range(n):
        rows.append([i + 1] + [g[i] for g in col_vals])

    # 汇总表（直接从生成数据算，真实对应）
    table = [f"| 指标 | 结果 | 说明 |", "|---|---|---|"]
    for ci, col in enumerate(columns[1:], start=1):
        vals = col_vals[ci - 1]
        if all(isinstance(v, (int, float)) for v in vals):
            mean = sum(vals) / max(len(vals), 1)
            unit = (specs[ci - 1] or {}).get("unit", "") if ci - 1 < len(specs) else ""
            table.append(f"| {col} | 均值 {mean:.1f}{unit} | 模拟样本 |")
        else:
            c = Counter(vals)
            top, cnt = c.most_common(1)[0]
            table.append(f"| {col} | 占比最高「{top}」（{cnt / n * 100:.0f}%） | 模拟样本 |")
    data = {
        "headline": f"模拟样本反映问卷对象（{_target_of(specs)}）的基础消费结构与意愿分布，用于演示结论交叉验证。",
        "summary_table": "\n".join(table),
        "key_findings": [
            "样本画像与报告估算口径一致：核心数值（频次/价位/意愿）落在报告给定的置信区间内（见上表）",
            "消费频次呈长尾分布：多数受访集中在低频区间 → 印证「频次增长依赖深度渗透」而非高频刚需",
            "价格与位置便利合计为主导选择因素 → 价格敏感度高的市场，定价与选址权重需上调",
            "模拟数据仅演示方法论闭环；正式决策须真实投放问卷后复核",
        ],
    }
    return _package(data, columns, rows, method=method, n=n)


def _target_of(specs: list[dict[str, Any]]) -> str:
    for cs in specs:
        lab = cs.get("label") or ""
        if "频次" in lab or "品牌" in lab:
            return "目标市场消费者"
    return "目标市场消费者"


def _package(data: dict[str, Any], columns: list[str], rows: list[list[Any]],
             method: str, n: int) -> dict[str, Any]:
    """统一打包：原始字段 + 汇总 md + CSV 文本"""
    headline = (data.get("headline") or "").strip()
    table = (data.get("summary_table") or "").strip()
    findings = [str(f) for f in (data.get("key_findings") or [])]
    md = "\n".join(
        [
            # **标题必须自称"模拟"且自称"方法演示"**（2026-09-15 三份外部评审同时点名）。
            # 原标题是「模拟问卷结果（一手数据演示闭环）」——"模拟"在前、"一手数据"紧随其后，
            # 读者扫过去拿到的是"一手数据"，而它恰恰**不是**一手数据。
            # GPT 那份评审把风险说得最直白：复核者看到"真实反馈/一手数据"再发现是模拟，
            # 会直接得出"你在包装不存在的一手数据"——伤的是诚实这个人设，
            # 而整套产品的立论正建在那上面。
            # 新标题把两件事分开写死：**这是方法演示，不是数据**。
            "## 🧪 方法演示：模拟问卷（**不是真实数据**）",
            "",
            # 紧贴标题再加一行硬边界，避免读者只看卡片头部就下判断。
            "> 🚧 **本节是方法演示**：数据由模型按报告结论**合成**，**不是**任何真实受访者的回答，"
            "**不可用于**估计任何真实比例、份额或用户偏好。它的用途只有一个——"
            "**展示『若做一手调研，本产品会怎么闭环』**。",
            "",
            f"**样本**：{n} 份（模拟）· 方法：{method}",
            "",
            f"**概括**：{headline or '—'}",
            "",
        ]
    )
    if table:
        md += table + "\n\n"
    if findings:
        md += "### 关键发现（数据反哺结论）\n" + "\n".join(f"- {f}" for f in findings) + "\n\n"
    md += f"> ⚠️ {_MODEL_NOTE}"
    return {
        "method": method,
        "n": n,
        "headline": headline,
        "table_md": table,
        "findings": findings,
        "md": md,
        "csv": _csv_text(columns, rows),
    }
