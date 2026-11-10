"""研策 · MR-Bench 轻量回归版（Market Research Benchmark · smoke 版）

对齐五份方案共识 + 目标大赛四关键词（任务理解/自主规划/工具调用/结果交付）：
- 8 个真实行业问题，覆盖 exploratory/descriptive/causal 三类
- 对每个问题做确定性回归：问题类型路由 + MECE 树 + 因果假设
- 输出"四关键词"评分表（任务理解/自主规划/工具调用/交付）——同时是参赛自查工具

用法（PowerShell）：
  cd backend
  python mrbench.py

说明：这是轻量回归版（Router/规划层确定性校验，不跑全量 LLM）。
深版（10 题 + 全文评价 + GPT 对照）上线前补。Demo 主案例在用例 #1。
"""

from __future__ import annotations

import sys
from unittest import mock

sys.path.insert(0, ".")

# (问题, 期望类型, 期望是否决策/因果)
_CASES: list[tuple[str, str, str]] = [
    ("咖啡连锁品牌下沉三四线城市该不该做？下沉门店增速为什么放缓？", "causal", "决策+因果"),      # Demo 主案例
    ("中国现制咖啡市场规模多大？竞争格局如何？", "descriptive", "统计描述"),                        # 描述
    ("社区养老服务市场有什么机会？要不要进入？", "exploratory", "议题树"),                          # 探索
    ("人形机器人减速器国产化，价格对采购决策有没有影响？", "causal", "因果"),                       # 因果
    ("杭州茶饮市场主要玩家是谁？格局如何？", "descriptive", "统计描述"),                            # 描述
    ("AI Agent 创业该进什么细分行业？", "exploratory", "议题树"),                                  # 探索
    ("广告投放对咖啡销量是否有效？ROI 为什么波动？", "causal", "因果"),                             # 因果
    ("低空经济赛道要不要进入？政策对商业化有什么影响？", "causal", "决策+因果"),                    # 决策+因果
]


def _score(rows: list[dict]) -> None:
    """输出四关键词评分表"""
    print("\n" + "=" * 70)
    print("MR-Bench 轻量回归版 · 四关键词评分")
    print("=" * 70)
    print(f"{'#':<3}{'类型':<12}{'任务理解':<8}{'自主规划':<8}{'工具调用':<8}{'交付':<6}总分")
    total = 0
    for i, r in enumerate(rows, 1):
        s = r["task"] + r["plan"] + r["tool"] + r["deliver"]
        total += s
        print(f"{i:<3}{r['type']:<12}{r['task']:<8}{r['plan']:<8}{r['tool']:<8}{r['deliver']:<6}{s}")
    print("-" * 70)
    print(f"平均分：{total / max(len(rows), 1):.1f}/20（五档自评：各维度 0-5）")
    print("> 四关键词对齐目标大赛赛道维度：①任务理解 ②自主规划 ③工具调用 ④结果交付")


def main() -> int:
    from agents.router import _detect_question_type, generate_plan

    passed = 0
    rows = []
    for q, exp_type, _label in _CASES:
        got = _detect_question_type(q)
        ok_type = got == exp_type
        task = 5 if ok_type else 1  # 任务理解：类型路由正确

        # 规划：离线生成计划（确定性，不调 LLM），校验 MECE 树 + 假设
        plan = None
        plan_ok = False
        hyp_ok = True
        with mock.patch("agents.router.has_main_llm", return_value=False):
            try:
                plan = generate_plan(q, {"depth": "标准"})
                tree_n = len(getattr(plan, "problem_tree", []) or [])
                plan_ok = tree_n >= 2
                # 因果类问题必须带假设；非因果类不应有假设（分流正确）
                if got == "causal":
                    hyp_ok = bool(getattr(plan, "hypotheses", []))
                else:
                    hyp_ok = not bool(getattr(plan, "hypotheses", []))
            except Exception:
                plan_ok = False
        plan_pts = 5 if plan_ok else 1  # 自主规划：MECE 树
        if got == "causal" and not hyp_ok:
            plan_pts = min(plan_pts, 2)  # 因果无假设 → 扣分

        # 工具调用：计划内含带检索词的子任务（= 会触发检索/网页工具链）
        sub_queries = (
            [st.search_queries for st in (getattr(plan, "subtasks", None) or [])]
            if plan is not None else []
        )
        tool_ok = bool(plan is not None and sub_queries and any(sq for sq in sub_queries))
        tool = 5 if tool_ok else 2

        # 交付：标准档计划覆盖可验收维度数
        dims = list(getattr(plan, "dimensions", None) or []) if plan else []
        deliver = min(5, 2 + len(dims) // 2)

        tree_n = len(getattr(plan, "problem_tree", []) or []) if plan else 0
        if ok_type and plan_ok and hyp_ok and tool_ok:
            passed += 1
            verdict = "✅"
        else:
            verdict = "❌"
        print(f"{verdict} [{got}→期望{exp_type}] {q[:34]}… 树{tree_n}分支 假设{'有' if plan and getattr(plan, 'hypotheses', []) else '无'}")
        rows.append({"type": exp_type, "task": task, "plan": plan_pts, "tool": tool, "deliver": deliver})

    _score(rows)
    print(f"\n回归通过：{passed}/{len(_CASES)}")
    return 0 if passed == len(_CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
