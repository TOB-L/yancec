"""追问闭环 —— 从报告反推研究（v3 §4.2 端到端闭环演示点）

用户对报告里任何一句话发起"再深挖"：
1. 接收"待深挖的结论 + 用户补充意图"
2. 生成深挖子任务（LLM 判断该补查什么）
3. 检索新证据（RAG优先 + DeepSeek）
4. 把新证据并入原报告，重新生成对应章节

这是"报告不是终点，是可对话的活文档"的落地——
打通"调研 → 决策 → 再调研"的完整闭环。
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import get_main_llm, has_main_llm
from core.models import Evidence, Source, SourceLevel
from core.rag import add_asset


def _extract_industry(text: str) -> str:
    """行业取名——**委托给全仓唯一的那一份实现**（2026-09-16 全量审计）。

    病案：这里原先自带**第二份**行业词表（14 个硬编码词：咖啡/奶茶/宠物/零食/美妆…），
    命中不了就 `return "通用"`。而主流程用的是 `agents.researchers._extract_industry`
    （v0.9.80 才把"垃圾键"问题收口成的那一份）。
    两份词表的后果不是"叫法不一致"，是**资产找不回来**：
      · 深挖沉淀时，非这 14 个行业的主题一律落到 `通用` 这个键下；
      · 下一次同行业调研按**真实行业键**去检索沉淀资产 → **永远搜不到那些深挖结果**；
      · 于是"越用越懂这个行业"在这条路上是断的，而这恰恰是用户主动追问过、
        最该被记住的那部分资产（复用率也因此被系统性地压低）。
    **同一个动作两处实现**——本仓库最稳定的复发形态之一。
    """
    try:
        from agents.researchers import _extract_industry as _canon

        got = (_canon(text or "") or "").strip()
        if got:
            return got[:20]
    except Exception:
        pass
    return "通用"


def deep_dive(claim: str, user_intent: str = "", context: dict | None = None) -> dict[str, Any]:
    """对报告中的某句结论发起深挖

    流程：拆解深挖方向 → 检索新证据 → 整合深挖结论 → 沉淀资产
    返回：深挖结论 + 新证据 + 是否并入原报告的建议
    """
    industry = _extract_industry(claim)
    context = context or {}

    # 1. 生成深挖子任务（LLM 判断该补查什么）
    sub_questions = _plan_deep_dive(claim, user_intent)

    # 2. 检索新证据
    new_evidences: list[Evidence] = []
    new_contents: list[str] = []
    for q in sub_questions[:2]:  # 最多深挖2个方向
        content = _retrieve(q, industry)
        if content:
            new_contents.append(f"**{q}**：{content[:600]}")
            new_evidences.append(
                Evidence(
                    claim=f"深挖「{q}」: {content[:80]}",
                    query=str(q)[:120],
                    sources=[
                        Source(
                            title=f"[深挖检索] {q}",
                            url="DeepSeek检索",
                            level=SourceLevel.L2_SEMI_OFFICIAL,
                            publisher="DeepSeek",
                            excerpt=content[:300],
                            timeliness_score=0.8,
                            independence_score=0.6,
                            corroboration_count=1,
                        )
                    ],
                )
            )

    # 3. 整合深挖结论
    deep_conclusion = _integrate_deep_dive(claim, new_contents, user_intent)

    # 4. 沉淀资产
    for ev in new_evidences:
        src = ev.sources[0]
        add_asset(
            industry=industry,
            content=f"{ev.claim}\n{src.excerpt}",
            source_type="deep_dive",
            source_title=src.title,
        )

    return {
        "claim": claim,
        "deep_conclusion": deep_conclusion,
        "new_evidences": [ev.model_dump(mode="json") for ev in new_evidences],
        "sub_questions": sub_questions,
        "industry": industry,
    }


def _plan_deep_dive(claim: str, user_intent: str) -> list[str]:
    """LLM 判断该补查什么（真实模式），兜底规则"""
    if has_main_llm():
        try:
            llm = get_main_llm(temperature=0.3)
            resp = llm.invoke(
                [
                    (
                        "system",
                        "你是市场调研研究员。用户对一句调研结论想深挖，请列出2个最该补充的检索问题。只输出JSON数组，如[\"问题1\",\"问题2\"]。",
                    ),
                    ("human", f"结论：{claim}\n用户想了解：{user_intent or '更多细节'}"),
                ]
            )
            data = json.loads(resp.content.strip().removeprefix("```json").removesuffix("```"))
            return data[:2]
        except Exception:
            pass
    # 兜底
    return [f"{claim[:30]} 的详细数据与依据", f"{claim[:30]} 的发展趋势与风险"]


def _retrieve(query: str, industry: str) -> str | None:
    """检索：RAG 优先 + DeepSeek 兜底"""
    from mcp.search import search_with_llm
    from core.rag import search_assets

    # RAG 优先
    hits = search_assets(query, industry=industry, n_results=2)
    for hit in hits:
        if not isinstance(hit, dict):
            continue  # 防御：资产库偶发 None/异常条目
        if hit.get("content") and len(hit["content"]) > 50:
            return f"[历史资产] {hit['content'][:300]}"

    # DeepSeek 兜底
    return search_with_llm(query)


def _integrate_deep_dive(claim: str, new_contents: list[str], user_intent: str) -> str:
    """整合深挖结论（LLM 真实模式），兜底拼接"""
    if not new_contents:
        return f"针对「{claim[:50]}」的深挖暂未检索到新数据，建议换个角度再试。"

    if has_main_llm():
        try:
            llm = get_main_llm(temperature=0.3)
            data_str = "\n".join(new_contents)
            resp = llm.invoke(
                [
                    (
                        "system",
                        "你是市场调研研究员。基于新检索到的资料，对用户关注的结论做深入分析。输出结构化Markdown，标注数据来源。",
                    ),
                    ("human", f"原结论：{claim}\n用户想了解：{user_intent or '更多细节'}\n\n新检索资料：\n{data_str}"),
                ]
            )
            return resp.content
        except Exception:
            pass
    # 兜底
    return "\n\n".join(new_contents)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, ".")
    print("=== 追问闭环自测 ===")
    result = deep_dive("杭州咖啡市场规模约120-160亿元", user_intent="具体是哪些品牌贡献的")
    print("深挖方向:", result["sub_questions"])
    print("深挖结论前200字:", result["deep_conclusion"][:200])
