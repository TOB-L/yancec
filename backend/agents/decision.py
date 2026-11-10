"""决策SOP 生成 —— 决策型交付（v3 §6.7）

把"市场报告"升级为"决策清单"：
- 该不该做（决策）
- 怎么做（行动建议）
- 预算多少（成本测算）
- 风险在哪（风险预警）

每条建议标注证据依据（对应证据链的哪条来源），让决策可查证。
这是"市场可行性"和"赛道维度·可验收性"的实锤。

真实模式：LLM 基于证据链生成决策SOP
兜底模式：规则模板（保证离线/无Key可演示）
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import Evidence
from core.llm import get_main_llm, has_main_llm

logger = logging.getLogger(__name__)

# 「兜底是静默的」是本模块踩过的最贵的坑（v0.9.75）：
# 原实现里 evidence_summary 的构造一旦抛异常（例如证据是 dict 而非 Evidence 对象），
# 就被 `except Exception: pass` 整个吞掉、直接返回模板 —— **报告其余部分一切正常，
# 只有「决策清单」这一节变成了"未调用大模型"的空壳，而没有任何人会发现。**
# 修法：① 构造逐条容错 ② 异常分因记录 ③ 把原因与"是否兜底"暴露给调用方，
# 由交付层在报告头显式告警。三者缺一，这个坑还会再回来。
_LAST_FALLBACK: dict[str, str] = {"reason": "", "detail": ""}


def sop_status() -> dict[str, str]:
    """上一次 generate_decision_sop 的状态。`reason` 为空 = 走了真实 LLM 路径。

    调用方（graph.node_decision_sop）必须读它：**决策清单降级不许静默**。
    """
    return dict(_LAST_FALLBACK)


def _evidence_line(i: int, ev: Any) -> str:
    """把一条证据渲染成 prompt 里的一行。**dict / pydantic 双形态通吃**。

    本仓库这是第三次踩「Evidence 双形态」（同步路径是 pydantic 实例、部分路径是 dict）。
    前两次的修法都是"调用前归一"，这次直接在渲染处容错——少一个必踩的坑。
    """
    claim = ""
    src_title = "未知"
    src_level = "?"
    try:
        if isinstance(ev, dict):
            claim = str(ev.get("claim") or "")
            srcs = ev.get("sources") or []
            if srcs and isinstance(srcs[0], dict):
                src_title = str(srcs[0].get("title") or srcs[0].get("publisher") or "未知")
                src_level = str(srcs[0].get("level") or "?")
        else:
            claim = str(getattr(ev, "claim", "") or "")
            srcs = getattr(ev, "sources", None) or []
            if srcs:
                src_title = str(getattr(srcs[0], "title", None) or "未知")
                _lv = getattr(srcs[0], "level", None)
                src_level = str(getattr(_lv, "value", _lv) or "?")
    except Exception:
        pass  # 单条渲染失败不该毁掉整份 prompt
    return f"- [{i}] {claim[:60]} (来源:{src_title}, 等级:{src_level})"

_SOP_SYSTEM_PROMPT = """你是"研策"的决策分析师。基于调研报告的证据链，为用户生成一份"决策清单"（Decision SOP）。

输入：用户的原始问题 + 带证据链的调研报告。
输出：Markdown 格式的决策清单，包含 6 个部分——

## 决策判定（Go / No-Go / Validate）
- 从三选一中给出明确结论：**✅ Go（建议进入）** / **❌ No-Go（不建议）** / **🟡 Validate（尚不确定，建议小规模验证）**
- 用一句话说出判定理由，再列 2-3 条关键依据（每条标注 [证据编号/来源等级]）
- 附"置信度"（高/中/低）和"最大不确定性"（一句话说清最该担心什么）
- 规则：证据不足、关键数据冲突、测算参数不自洽时，一律判 **Validate** 而不是 Go/No-Go

## 最小验证计划（14天，把结论变成行动）
- **若输入带有【系统已生成的验证任务清单】，该清单 vt_01… 是唯一事实来源（负责人/预算/KPI/停止条件一律以它为准，不得改动数字）**；本节只写 3 条以内的"执行顺序与优先级"，逐条引用 vt 编号（如"先做 vt_01 用户深访再开 vt_02 渠道测试"），**禁止重复罗列人数/预算/金额等数字——避免与系统清单出现两套数字互相矛盾**
- 若输入**没有**系统清单，才自行设计 2-4 个验证任务并用表格列出：任务 | 负责人角色 | 截止时间 | 预算 | 成功指标 | 未达标时的停止条件
- 示例任务类型：目标用户访谈 / 小预算广告测试 / 样品与渠道测试 / 成本核实
- 验证计划必须具体到"第几天做什么"，不要写"进一步调研"这种空话

## 关键假设与可推翻变量（Red Team Review）
- 列出 2-3 个**最关键假设**（如渗透率、客单价、获客成本、市场增速）
- 对每个假设写：它成立，结论才成立；**哪个变量变化多大，结论就会反转**（如"CAC 若翻倍→LTV/CAC 低于 3→No-Go"）
- 给"必须监控的先行指标"（哪个数字一变就该警惕）
- 目的：不是替用户找支持结论的证据，而是**主动找能推翻自己的证据**

## 行动清单（怎么做）
- 分步行动建议（第一步做什么，第二步做什么），每条标注依据

## 成本测算（预算多少）
- 启动预算区间 + 构成（依据公开数据）
- 标注"为估算值，需实际核验"

## 风险与对策
- 3-5 个关键风险 + 每个的对策 + 依据

规则：
1. 每个结论必须标注证据依据，格式"依据：报告[章节名] + 来源名/信源等级"（对应证据链编号 [i]）
2. 数据是估算/推测的必须明确标注"为估算值，需实际核验"
3. 绝不编造证据，没有依据的判断标"专家判断，待验证"
4. 成本测算用 markdown 表格呈现（构成项/金额区间/依据），不用纯文字罗列
5. 风险每一条都必须配对策，不要只列风险不给解法
6. 禁止以"启示""总之""综上所述"等模板句结尾
7. 只输出决策清单，不要其他内容
"""


def generate_decision_sop(
    raw_question: str,
    report: str,
    evidences: list[Evidence],
    vt_summary: str = "",
) -> str:
    """生成决策SOP（真实模式 LLM + 兜底模板）。

    vt_summary：系统已生成的验证任务清单（vt_01…）文本，作为"最小验证计划"一节的唯一事实来源，
    避免 LLM 另造一套人数/预算与清单打架（v0.9.43 一致性修复）。

    **降级不静默**：任何原因走到兜底都会写进 `sop_status()`，调用方须在报告头告警。
    """
    _LAST_FALLBACK["reason"] = ""
    _LAST_FALLBACK["detail"] = ""

    def _fallback(reason: str, detail: str = "") -> str:
        _LAST_FALLBACK["reason"] = reason
        _LAST_FALLBACK["detail"] = detail[:300]
        if reason != "no-llm":  # 无模型是既定档位，不是异常；异常要留日志
            logger.warning("决策清单降级为兜底模板｜原因=%s｜%s", reason, detail[:200])
        return _fallback_sop(raw_question, evidences, vt_summary=vt_summary)

    if not has_main_llm():
        return _fallback("no-llm", "未配置主模型（离线/无 Key）")
    if not (report or "").strip():
        # 这一条以前也会静默兜底：报告正文为空 = 上游出了事，必须让上面知道
        return _fallback("empty-report", "report_draft 为空——上游汇总环节可能已失败")

    try:
        llm = get_main_llm(temperature=0.3, max_tokens=2500)
        # 逐条渲染（_evidence_line 双形态通吃）；单条坏掉不再连坐整段
        evidence_summary = "\n".join(
            _evidence_line(i, ev) for i, ev in enumerate((evidences or [])[:10])
        )
        vt_block = (
            "\n\n【系统已生成的验证任务清单（唯一事实来源，禁止改数字/另造清单）】\n" + vt_summary
            if vt_summary
            else ""
        )
        resp = llm.invoke(
            [
                ("system", _SOP_SYSTEM_PROMPT),
                (
                    "human",
                    f"用户问题：{raw_question}\n\n调研报告摘要：\n{report[:3000]}\n\n证据链：\n{evidence_summary}{vt_block}",
                ),
            ]
        )
        content = (resp.content or "").strip()
        if content and len(content) > 50:
            return content
        return _fallback("short-output", f"模型返回 {len(content)} 字（阈值 50）")
    except Exception as e:  # 调用异常不再吞：记因、留日志、照实暴露给交付层
        return _fallback("llm-error", f"{type(e).__name__}: {e}")


def build_validation_tasks(
    raw_question: str,
    verdict: str = "Validate",
    evidence_count: int = 0,
) -> list[dict[str, Any]]:
    """构建结构化验证任务清单（Action Layer，对齐培训"收集→判断→行动→复核"）。

    确定性模板（"确定性下沉"）：结构由代码保证、评审可复现——
    不依赖 LLM 自由发挥，避免"看起来能行动、实际没法验收"。
    每个任务：ID / 任务 / 负责人 / 截止 / 预算 / KPI / 停止条件 / 状态 / 依据。

    ## 关于入参（2026-09-16）

    `evidence_count` 原先**被调用方算好传进来、却从未使用**
    （`graph.py` 写的是 `evidence_count=len(state.get("evidences", []))`，
    算完就丢）。现在把它写进每个任务的「依据」里——这是**真实且有用**的信息：
    一份被单独摘出去执行的验证清单，应当说得出它背后的证据量级。
    传 0（未提供）时**不印**，免得印出"共 0 条证据"这种自我否定的话。

    `raw_question` 保留在首位仅为**兼容既有位置传参**
    （`tests/test_reliability.py` 用 `build_validation_tasks("测试", ...)` 调用）；
    模板是确定性固定结构，不消费问题文本。这一条已登记在
    `_fulltest._PW_DEAD_PARAM_OK` 里，不是遗漏。
    """
    _basis = f"（本次调研共 {evidence_count} 条证据）" if evidence_count else ""
    return [
        {
            "id": "vt_01",
            "task": "目标用户访谈（5-10人）",
            "owner": "市场",
            "deadline": "Day 3",
            "budget": "¥500",
            "kpi": "≥50% 受访者表达明确需求",
            "stop_condition": "无人愿意深度对话",
            "status": "待执行",
            "evidence": "依据：用户画像/需求章节" + _basis,
        },
        {
            "id": "vt_02",
            "task": "小预算广告/渠道测试",
            "owner": "增长",
            "deadline": "Day 7",
            "budget": "¥3,000",
            "kpi": "测试转化率达到假设下限",
            "stop_condition": "转化率明显低于盈亏线",
            "status": "待执行",
            "evidence": "依据：获客/单位经济章节" + _basis,
        },
        {
            "id": "vt_03",
            "task": "成本与供应链核实",
            "owner": "供应链",
            "deadline": "Day 10",
            "budget": "¥0",
            "kpi": "拿到 ≥2 家真实报价",
            "stop_condition": "报价均超预算",
            "status": "待执行",
            "evidence": "依据：成本测算章节" + _basis,
        },
    ]


def extract_verdict(sop: str) -> str:
    """从决策清单文本里提取 Go / No-Go / Validate 判定（Decision Memory 用）。

    只认带 ✅/❌/🟡 图标的"决策判定"行，避免 Red Team 章节里出现的
    "→No-Go"（如"CAC 翻倍→No-Go"）被误判为最终结论。

    ⚠️ **原先这句话只写在注释里，代码并没有做到**（2026-09-16 全量审计）：
    三个模式各带一条**全局裸词**分支（`|No-Go（不建议）`、`|Validate（尚不确定`、`|Go（建议进入）`），
    而 `re.search` 扫的是**整篇 sop**。于是：
      · Red Team 章节里一句「若 CAC 翻倍→**No-Go（不建议）**」，就把一份 **Go** 报告判成 No-Go；
      · 而且 No-Go 排在最前——**先命中先返回**，真判定行在后面也救不回来。
    不是措辞问题：判定会被写进**决策记忆与决策回访基线**（`register_baseline`），
    一份 Go 报告的回访基线记着 No-Go，后面所有"当初的判断今天还站得住吗"都跟着错。

    现在的判据：
      ① 只扫**前 60 行**——决策判定按设计是置顶的（交付时"决策判定置顶"），
         而风险/RedTeam 章节在后面，全文搜索必然串味；
      ② 一行里**出现两个以上判定图标**就跳过——那是模型照抄题干
         「**✅ Go（建议进入）** / **❌ No-Go（不建议）** / 🟡 **Validate…**」那种选项行，
         不是结论；
      ③ 先要求**图标紧跟判定词**（结论行的写法），再退回"本行只有一个判定词"的宽松匹配。
    """
    import re

    if not sop:
        return "Validate"
    _ICON = {"✅": "Go", "❌": "No-Go", "🟡": "Validate"}
    _lines = (sop or "").split("\n")[:60]

    def _sole_icon(ln: str) -> str:
        _hits = [k for k in _ICON if k in ln]
        return _hits[0] if len(_hits) == 1 else ""

    # 第一遍：图标紧跟判定词（`- **✅ Go（建议进入）**：…`）
    for ln in _lines:
        _k = _sole_icon(ln)
        if not _k:
            continue
        if re.search(re.escape(_k) + r"\s*\**\s*" + re.escape(_ICON[_k]), ln):
            return _ICON[_k]
    # 第二遍：本行只提到一个判定词（模型偶尔漏图标）
    for ln in _lines:
        _k = _sole_icon(ln)
        if not _k:
            continue
        if re.search(r"\**\s*" + re.escape(_ICON[_k]), ln):
            return _ICON[_k]
    return "Validate"


def build_monitor_conditions(industry: str = "", verdict: str = "Validate") -> list[str]:
    """构建"持续监控"触发条件（Market Monitor：市场变了→重新评估）。

    确定性模板：不依赖 LLM；每次决策存档时生成，供"重新评估"判断何时触发。
    """
    base = [
        f"{industry or '目标市场'}出现重大政策/法规变化",
        "头部竞品价格下降 15% 以上",
        "获客成本(CAC)显著超过测算假设",
        "市场规模数据出现新的官方统计",
    ]
    if verdict == "No-Go":
        base.insert(0, f"{industry or '目标市场'}出现结构性利好（如新政策放开）")
    return base[:4]


def _fallback_sop(raw_question: str, evidences: list[Evidence], vt_summary: str = "") -> str:
    """兜底决策SOP：基于证据数量生成模板（无LLM时保证有内容）。

    vt_summary：系统已生成的验证任务清单文本（v0.9.43 一致性：兜底版也从它渲染，
    避免"最小验证计划"与结构化 vt_01..03 出现第二套人数/预算）。
    """
    ev_count = len(evidences)

    # 从 vt_summary（"- vt_01｜任务｜负责人:X｜截止:..｜预算:..｜KPI:..｜停止:.."）渲染成表格
    vt_md = ""
    if vt_summary:
        _rows = []
        for _ln in (vt_summary or "").splitlines():
            _s = (_ln or "").strip()
            if not _s.startswith("- vt_"):
                continue
            _p = [x.strip() for x in _s.lstrip("- ").split("｜")]

            def _f(_i: int) -> str:
                return _p[_i].split(":", 1)[1].strip() if len(_p) > _i and ":" in _p[_i] else ""

            if len(_p) >= 2:
                _rows.append(
                    f"| {_p[0]} | {_p[1]} | {_f(2)} | {_f(3)} | {_f(4)} | {_f(5)} | {_f(6)} |"
                )
        if _rows:
            vt_md = (
                "## 最小验证计划（14天，把结论变成行动）\n\n"
                "（本兜底版验证任务以系统生成的清单 vt_01… 为唯一事实来源，此处仅引用编号，不另造数字）\n\n"
                "| 编号 | 任务 | 负责人 | 截止 | 预算 | KPI | 停止条件 |\n"
                "|---|---|---|---|---|---|---|\n"
                + "\n".join(_rows)
            )
    if not vt_md:
        vt_md = """## 最小验证计划（14天，把结论变成行动）

| 任务 | 负责人角色 | 截止 | 预算 | 成功指标 | 未达标停止条件 |
|---|---|---|---|---|---|
| 目标用户访谈（5-10人） | 市场 | Day 3 | ¥500 | 半数以上表达明确需求 | 无人愿意深度对话 |
| 小预算广告/渠道测试 | 增长 | Day 7 | ¥3,000 | 测试转化率达到假设下限 | 转化率明显低于盈亏线 |
| 成本与供应链核实 | 供应链 | Day 10 | ¥0 | 拿到2家真实报价 | 报价均超预算 |
"""
    return f"""## 决策判定（Go / No-Go / Validate）

- 🟡 **Validate（建议小规模验证）**：基于本次调研（共 {ev_count} 条证据），证据不足以直接下 Go/No-Go 定论。
- 置信度：中（当前为模板兜底输出，正式运行将基于证据链给出个性化判定）
- 最大不确定性：关键测算参数与一手数据尚未核验

{vt_md}
## 行动清单（怎么做）

1. 核验关键数据（门店数/规模）是否满足决策需求
2. 按上面的验证计划先跑 14 天最小验证，再决定是否进入
3. 结合自身资源（预算/团队）制定执行计划

## 成本测算（预算多少）

- 启动预算需基于具体行业数据测算（当前为兜底模板）
- 标注：为估算值，需实际核验

## 风险与对策

1. 数据时效性风险 → 关注最新行业报告
2. 竞争加剧风险 → 差异化定位
3. 需求不确定性 → 小规模试水（即上面 14 天验证计划）

> 说明：此为兜底模板输出（未调用大模型），正式运行将基于证据链生成个性化决策清单。
"""
