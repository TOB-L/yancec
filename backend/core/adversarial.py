"""研策 · 对抗性数字校验（Red Team Review）——数据护城河核心

对齐 30x-mckinsey 的"Analyst + 怀疑论EM双角色"：
- 分析师产出报告后，由独立"怀疑论校验官"重审每个关键数字
- 检查：口径一致 / 单位混淆 / 假设过度乐观 / 重复计算 / 信源层级 / 时序错误
- 输出"数字核验报告"（哪些存疑、风险等级、修正建议）追加到报告附录

"敢挑战结论"是付费点，也是"麦肯锡级可信"与"AI废话"的分水岭。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_ADVERSARIAL_PROMPT = """你是"研策"的独立怀疑论校验官（Red Team）。你的职责是"敢挑战结论"：
假设报告里每个关键数字都可能是错的，找出最可能错的地方。绝不当捧哏。

审查这份市场调研报告中的关键数字，按以下清单逐项排查：
1. 口径一致性：同一指标在不同章节是否口径一致？（如杭州门店数：3500家 vs 6000家 vs 10000家）
2. 单位混淆：是否把"亿元"当"万元"、把"杯/天"当"杯/周"、把"万亿"当"亿"？
3. 假设过度乐观：渗透率/频次/份额/增速等估算假设是否偏离常识或自相矛盾？（如人均120杯/年 vs 全国人均12杯/年）
4. 重复计算：同一数据是否被多次计入导致虚高？
5. 信源层级：关键数字的信源是否足够权威（官方/机构）？L5估算是否被当成了事实陈述？
6. 时序错误：数据年份是否错位（把旧政策当新政、把2024当2025）？
7. 逻辑矛盾：结论是否与报告内其他数字冲突？（如"市场规模大"却"门店密度过高难以盈利"）

对每个发现的问题，输出 JSON 数组：
[{"number": "出问题的数字", "context": "出现位置/上下文", "issue": "问题描述", "risk": "高/中/低", "suggestion": "修正建议"}]

规则：
- 只输出 JSON 数组，不要其他文字
- 最多输出 6 条最严重的问题
- 没有确定的问题时输出 []（不要为凑数编造问题）

**你无权质疑的三件事（越界一律不许输出）**：
- **你的训练数据截止时间不等于当前时间。** 报告里的日期（生成时点、数据发布日、检索日）
  来自系统时钟与真实联网检索，比你"感觉现在是哪一年"可靠。**禁止**以"当前真实时间为
  20XX 年""我的训练数据截止较早""该日期属于未来"为由，把报告里的日期判为时序穿越或虚构。
  你唯一能就日期提的异议，是**报告内部两个日期互相矛盾**（如正文说 2025 年、表里写 2024 年）。
- **报告头的元信息不是待审的"关键数字"**：「生成时点」「引擎基线版本」「内容编号」
  「AIGC 标识」是交付水印，不参与数字对抗校验。**禁止把生成日期本身列为高风险数字。**
- **已实测核验过的来源，不许只凭印象否定。** 若某数字标注了来源机构与发布日期，
  而报告的证据链显示该来源**已通过 HTTP 实测**（链接层核验通过），则不得在没有任何
  反方证据的情况下判其为"虚构/不存在"；要提异议必须给出**具体的反方数据或链接**。
  拿不出反方证据时，宁可输出 [] —— **"未发现确定问题"是可接受的结论，
  "编一条看起来专业的质疑"不是**。
"""


def adversarial_review(
    report: str,
    question: str = "",
    counter_evidence: str = "",
) -> list[dict[str, str]] | None:
    """对报告关键数字做对抗性校验 → 问题列表。

    counter_evidence：已知反方证据（来自反证检索的【反方】内容），
    让校验官用真实反证去挑战报告的乐观结论（P0 对抗性核查轮扩展）。
    三态返回（v0.9.44，防"校验失败却出干净账单"的静默带病）：
      None → 未能执行/失败（无 LLM 或调用/解析异常），调用方须显式提示人工复核；
      []   → 校验确已运行，未发现确定问题（可出干净账单）；
      list → 发现项。
    """
    if not report:
        return None  # 没有可审内容 = 未执行
    try:
        from core.llm import get_main_llm, has_main_llm

        if not has_main_llm():
            return None  # 无模型 = 未执行（不是"未发现问题"）
        llm = get_main_llm(temperature=0.4, max_tokens=1600)
        counter_txt = ""
        if counter_evidence:
            counter_txt = (
                "\n\n**已知反方证据（请据此重点挑战报告的乐观结论，看是否被低估风险/高估机会）**：\n"
                f"{counter_evidence[:1500]}"
            )
        # **喂全文的数字摘要，而不是报告前 8000 字符**（2026-09-14 审计）。
        # 病案：原写 `report[:8000]`，而实测成稿正文 12 万+ 字符 ——
        # 各章（市场规模测算/竞争格局/单位经济表/目标市场）里的数字**从未进入 Red Team 视野**，
        # 它们恰恰是最该被挑战的部分；但报告末尾照样盖章
        # "独立校验官复核关键数字，未发现确定的严重问题"。
        # 更糟的是风险红条与决策层就地降级都建立在这份不完整的 adv_issues 之上，
        # 于是"带病锚未降级"的老病案会原样复现。
        # 改用执行摘要同款手段 `build_number_digest`（全文数字摘录、按预算均匀取），
        # 它已经解决过"报告太长塞不进上下文"这个问题。
        try:
            from core.report_html import build_number_digest

            _material = build_number_digest(report, budget=14000) or report[:8000]
        except Exception:
            _material = report[:8000]
        resp = llm.invoke(
            [
                ("system", _ADVERSARIAL_PROMPT),
                ("human", f"调研问题：{question or '（未提供）'}\n\n报告全文（数字摘录，覆盖全文各章）：\n{_material}{counter_txt}"),
            ]
        )
        content = (resp.content or "").strip()
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            return None  # 没拿到 JSON 数组 = 无法校验，按"未完成"处理
        issues = json.loads(m.group(0))
        if not isinstance(issues, list):
            return None
        return _drop_meta_issues([i for i in issues if isinstance(i, dict) and i.get("issue")])[:6]
    except Exception as e:
        logger.warning("对抗性数字校验失败: %s", e)
        return None  # 失败 ≠ 干净，交给调用方显式提示


def _drop_meta_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉"把报告头水印当关键数字"的伪发现。**确定性闸门，不依赖提示词。**

    病案（2026-09-15 深访实跑，真实报告原文）：

        头部水印：**研策调研报告**｜引擎基线 v0.9.80｜生成时点：2026-09-15（存疑·待核）
        RedTeam 表：| 2026-09-15 | 报告头信息：生成时点2026-09-15 | 时序穿越/信源虚构。
                    当前真实时间为2024-2025年间（…AI训练数据截止较早…） | **高** |

    两处都错，而且错得不小：
      · **交付水印被当成待核数字**——报告连自己的生成日期都不敢认，读者第一屏就读到。
      · 论据是"校验官觉得现在是 2024-2025 年"——**拿模型的知识截止去否定系统时钟**。
    更糟的是它顺带把一条**已实测核验过**的当期数据拉下水：多角色辩论里那条
    「国家统计局 2026-01–08 社零」在引文核验里是 `stats.gov.cn HTTP 200 ✅`，
    却在辩论中被"申辩方承认质疑成立"，收敛成"该数据为虚构或预测"。
    **同一条证据链，前面说它活着、后面说它是编的**——这是核验体系自伤。

    提示词已写明三条禁令（见 `_ADVERSARIAL_PROMPT`），但那只是**建议**；
    模型换了、提示被改短了，这张表就会重新长出这一行。所以在**出口**再拦一道：
    `number` 是纯日期、且 `context` 指向报告头/水印/元信息 → 不是分析结论，丢弃。

    口径**刻意收窄**：只认"纯日期 + 元信息位置"这一种组合。
    「正文说 2025 年、表里写 2024 年」这类**真实的内部时序矛盾**必须继续报出来——
    收宽了会把真问题一起挡掉，那比误报更糟。
    """
    _META = ("生成时点", "报告头", "水印", "引擎基线", "内容编号", "AIGC", "元信息")
    # ⚠️ **尾巴上挂个标注就能绕过**（2026-09-16 全量审计）：原正则以 `$` 收尾且只认纯日期，
    # 于是把标签写进 `number` 字段（如 `2026-09-15（生成时点）`）就绕过了这道闸门，
    # 报告首屏照旧出现针对**水印日期**的红色高风险红条——正是这条闸门存在的理由。
    # 放宽的**边界仍然很紧**：允许日期后面跟非数字字符（括号标注/空白/文字），
    # 但**尾巴里不许再出现任何数字**——所以
    #   `2026-09-15（生成时点）` → 判为日期（丢弃）✓
    #   `2025 年 vs 2024 年`、`2024年（对比2023年）` → 尾巴有数字 → **照旧上报** ✓
    # 「正文说 2025 年、表里写 2024 年」这类真实的内部时序矛盾，一条都不会被挡掉。
    _DATE_ONLY = re.compile(
        r"^\s*[\d]{4}\s*[-/年.]\s*\d{1,2}\s*([-/月.]\s*\d{1,2}\s*日?)?\s*[^0-9]{0,24}$"
    )
    out = []
    for i in issues:
        num = str(i.get("number") or "").strip()
        ctx = str(i.get("context") or "")
        # 标签也可能就写在 `number` 里（上面那条绕过），所以两边都查
        if _DATE_ONLY.match(num) and (any(k in ctx for k in _META) or any(k in num for k in _META)):
            logger.info("RedTeam 元信息项已丢弃（水印日期不是待核数字）：%s | %s", num, ctx[:40])
            continue
        out.append(i)
    return out


def format_adversarial_md(issues: list[dict[str, str]]) -> str:
    """对抗性校验报告 → markdown（追加到报告附录）"""
    if not issues:
        return "### ✅ 对抗性数字校验（Red Team Review）\n\n独立校验官复核关键数字：**未发现确定的严重问题**。\n"
    lines = [
        "### ⚠️ 对抗性数字校验（Red Team Review）",
        "",
        "独立校验官对报告关键数字做了反方审查，发现以下需复核项（风险：高/中/低）：",
        "",
        "| 数字 | 位置 | 问题 | 风险 | 修正建议 |",
        "|---|---|---|---|---|",
    ]
    for i in issues:
        lines.append(
            f"| {str(i.get('number', ''))[:24]} | {str(i.get('context', ''))[:28]} | "
            f"{str(i.get('issue', ''))[:60]} | {i.get('risk', '中')} | {str(i.get('suggestion', ''))[:40]} |"
        )
    lines.append("")
    lines.append("> 上述问题由独立怀疑论校验官提出，用于交叉核验结论。建议人工复核后再用于正式决策。")
    return "\n".join(lines)
