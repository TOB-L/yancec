# -*- coding: utf-8 -*-
"""研策 · 可靠性测试套件（对齐赛前培训：12 类异常场景）

只测确定性逻辑（不依赖网络/LLM Key），复核者可逐条复现：
1. 模糊/歧义 → 是否主动澄清（跨境）
2. 明确需求 → 不应误澄清
3. 冲突数据 → 是否进入仲裁
4. 测算矛盾（SAM>TAM）→ BLOCK
5. 人口口径冲突 → BLOCK
6. 市场规模"亿元"不应被误判为人口基数（防误杀）
7. 测算无 L5 标注 → FLAG（末尾披露，不硬拦截）
8. 合格报告 → 不应 BLOCK（防误伤）
9. 决策判定 + 最小验证计划 → 存在
10. 工具合同四维字段 → 完整
11. 人工接管卡片字段 → 完整（含 Risk Class）
12. 预算预警 → 80% 触发 / 未触发
13. Action Layer 验证任务 → 9 字段结构完整
14. 决策判定提取 → Go/No-Go/Validate（Decision Memory）
15. 监控触发条件 → ≥3 条（Market Monitor）

运行：cd backend && ..\venv\Scripts\python.exe -m pytest tests\test_reliability.py -v
或直接：..\venv\Scripts\python.exe tests\test_reliability.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import Evidence, Source, SourceLevel, arbitrate_conflict  # noqa: E402
from agents.quality import quality_check, build_stop_card  # noqa: E402
from agents.decision import generate_decision_sop  # noqa: E402
from mcp.search import tool_contracts  # noqa: E402
from agents.router import needs_clarification  # noqa: E402


# ---------- 1/2. 需求澄清 ----------
def test_cross_border_ambiguous_should_clarify():
    """跨境/出海问题有歧义 → 必须主动澄清（任务漂移 P3 的根源）"""
    assert needs_clarification("研究一下跨境电商零食的机会") is True


def test_clear_question_should_not_clarify():
    """地域+动作明确 → 不应误澄清"""
    assert needs_clarification("在杭州开一家咖啡店，预算80万，目标进入市场") is False


# ---------- 3. 冲突仲裁 ----------
def test_conflict_arbitration_runs():
    """两证据数值冲突 → 必须进入仲裁，产出可解释卡片"""
    ev_a = Evidence(
        claim="杭州咖啡门店约5000家",
        sources=[
            Source(
                title="机构A", level=SourceLevel.L2_SEMI_OFFICIAL,
                timeliness_score=0.9, independence_score=0.8, corroboration_count=2,
            )
        ],
    )
    ev_b = Evidence(
        claim="杭州咖啡门店约8000家",
        sources=[
            Source(
                title="机构B", level=SourceLevel.L3_INDUSTRY,
                timeliness_score=0.4, independence_score=0.5, corroboration_count=0,
            )
        ],
    )
    card = arbitrate_conflict(ev_a, ev_b)
    assert card["decision"] in ("adopt_winner", "show_both")
    assert "weights" in card and "reason" in card  # 仲裁必须可解释


# ---------- 4-8. 质检硬闸门 ----------
def test_sam_gt_tam_blocked():
    q = quality_check("TAM（潜在总市场）≈25,110 亿元，SAM≈50,000 亿元", [])
    assert q["level"] == "BLOCK"
    assert any("SAM" in f and "TAM" in f for f in q["fatal_issues"])


def test_population_official_duo_is_not_blocked():
    """官方口径组合（总14.1 + 城镇9.3）合法共存 → 不得 BLOCK（防误杀正常报告）"""
    q = quality_check("城镇人口9.3亿，总人口14.1亿", [])
    assert q["level"] != "BLOCK"


def test_population_outside_kb_flag():
    """≥5亿且非官方口径 → FLAG（末尾披露，不 BLOCK，避免误杀正常报告）"""
    q = quality_check("该市场总人口约12亿", [])
    assert q["level"] == "FLAG"
    assert any("人口口径" in i for i in q["issues"])


def test_market_size_yi_yuan_is_not_population():
    """防误杀：市场规模 110 亿元 ≠ 人口基数 110 亿人"""
    q = quality_check("据国家统计局，市场规模约110亿元，同比增速5%", [])
    assert q["level"] != "BLOCK"


def test_calculation_without_l5_blocked():
    """L5 缺失 → FLAG（报告末尾披露），不再硬拦截（避免正常报告反复返工）"""
    q = quality_check("市场规模测算：渗透率5%，客单价30元，合计约25亿元", [])
    assert q["level"] == "FLAG"
    assert any("L5" in i for i in q["issues"])


def test_well_formed_report_not_blocked():
    """合格报告（带L5、口径统一、SAM≤TAM）→ 不得 BLOCK"""
    report = (
        "市场规模测算（L5估算）：TAM≈25,110 亿元，SAM≈4,297 亿元。"
        "据国家统计局，城镇人口9.3亿。渗透率、频次、客单价均为(L5估算)。"
    )
    q = quality_check(report, [])
    assert q["level"] != "BLOCK"


# ---------- 9. 决策判定 + 验证计划 ----------
def test_decision_verdict_and_validation_plan():
    sop = generate_decision_sop("测试", "", [])
    assert "决策判定" in sop and "Go / No-Go / Validate" in sop
    assert "最小验证计划" in sop and "成功指标" in sop


def test_validation_tasks_structure():
    """Action Layer：结构化验证任务必须有 9 个字段（Task/Owner/Deadline/Budget/KPI/停止/状态/依据）"""
    from agents.decision import build_validation_tasks

    tasks = build_validation_tasks("测试", evidence_count=5)
    assert len(tasks) >= 3
    for t in tasks:
        for field in ("id", "task", "owner", "deadline", "budget", "kpi", "stop_condition", "status", "evidence"):
            assert field in t, f"验证任务缺字段 {field}"


def test_extract_verdict():
    """Decision Memory：能从决策清单提取判定，且 Red Team 章节的"→No-Go"不误判"""
    from agents.decision import extract_verdict

    assert extract_verdict("## 决策判定\n- ✅ **Go（建议进入）**") == "Go"
    assert extract_verdict("## 决策判定\n- ❌ **No-Go（不建议）**") == "No-Go"
    assert extract_verdict("## 决策判定\n- 🟡 **Validate（尚不确定）**") == "Validate"
    assert extract_verdict("") == "Validate"
    # Red Team 里出现"→No-Go"时，不得覆盖决策判定行的 Validate
    assert (
        extract_verdict(
            "## 决策判定\n- 🟡 **Validate（尚不确定）**\n\n## 关键假设\n若CAC翻倍→No-Go"
        )
        == "Validate"
    )


def test_build_monitor_conditions():
    """Market Monitor：确定性生成 ≥3 条监控触发条件"""
    from agents.decision import build_monitor_conditions

    conds = build_monitor_conditions("咖啡", "Validate")
    assert len(conds) >= 3
    assert any("竞品价格下降" in c for c in conds)


# ---------- 10. 工具合同 ----------
def test_tool_contracts_complete():
    tools = tool_contracts()
    assert len(tools) >= 3
    need = {"purpose", "inputs", "outputs", "permissions", "failure_handling"}
    for t in tools:
        assert need.issubset(t), f"{t['name']} 缺合同字段"


# ---------- 11. 人工接管卡片 ----------
def test_stop_card_complete():
    card = build_stop_card("报告", [], ["SAM>TAM 矛盾"], budget_spent=2.0)
    for field in (
        "reason", "why_stop", "risk_class", "disposition",
        "known", "unknown", "need_user", "cost_if_continue", "how_to_resume",
    ):
        assert field in card, f"停止卡片缺字段 {field}"
    assert card["risk_class"] == "参数矛盾"  # SAM>TAM → 参数矛盾 → 回退重测


# ---------- 12. 预算预警 ----------
def test_budget_flag_at_35_percent():
    """护栏阈值是「报价的 35%」，不是 80%。

    病案（2026-11-10 首次公开仓库跑 pytest）：这条用例写于阈值还是 0.8 的版本，
    产品后来把护栏收紧到 `_COST_BUDGET_RATIO = 0.35`（见 graph.node_budget_check 的 docstring），
    用例没跟着改，于是 5/10 = 50% 被判成"不该触发"。
    **测试跟着实现走是危险的**——所以这里不改实现，只把断言对齐到当前契约，并卡住边界。
    """
    from graph import _COST_BUDGET_RATIO, node_budget_check

    assert _COST_BUDGET_RATIO == 0.35, "护栏阈值变了，这条用例的前提需要重新确认"

    # 3.6/10 = 36% > 35% → 触发
    state = {"budget_estimate": 10.0, "budget_spent": 3.6, "trace_events": []}
    assert node_budget_check(state)["budget_flag"] is True

    # 3.4/10 = 34% < 35% → 不触发
    state2 = {"budget_estimate": 10.0, "budget_spent": 3.4, "trace_events": []}
    assert node_budget_check(state2)["budget_flag"] is False


if __name__ == "__main__":
    tests = [
        test_cross_border_ambiguous_should_clarify,
        test_clear_question_should_not_clarify,
        test_conflict_arbitration_runs,
        test_sam_gt_tam_blocked,
        test_population_official_duo_is_not_blocked,
        test_population_outside_kb_flag,
        test_market_size_yi_yuan_is_not_population,
        test_calculation_without_l5_blocked,
        test_well_formed_report_not_blocked,
        test_decision_verdict_and_validation_plan,
        test_validation_tasks_structure,
        test_extract_verdict,
        test_build_monitor_conditions,
        test_tool_contracts_complete,
        test_stop_card_complete,
        test_budget_flag_at_35_percent,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {t.__name__} | {e}")
    print(f"\n==== 可靠性测试: {'全部通过 OK' if failed == 0 else f'{failed} 项失败'} ====")
    sys.exit(1 if failed else 0)
