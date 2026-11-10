"""端到端集成测试：验证完整状态机链路

覆盖：
1. 完整流程：提问 → 计划 → 执行 → 冲突仲裁 → 质检 → 交付
2. 冲突仲裁卡片生成（评审命门1）
3. 质检 + 返工逻辑（限2次）
4. Trace 事件完整
"""

import sys
import os

import pytest

# 端到端会真的构建状态机并跑完整链路，可能触网/调模型，默认不纳入 `pytest -q`。
# 需要时显式运行：pytest -m e2e
pytestmark = pytest.mark.e2e

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import Evidence, Source, SourceLevel
from graph import build_graph
from core.state import init_state


def _inject_conflict_evidence(state):
    """注入两个冲突证据：同一维度、数值差异>20%（模拟真实检索结果）"""
    # 证据A：官方来源，5000家
    ev_a = Evidence(
        claim="杭州咖啡门店数量约5000家",
        sources=[
            Source(
                title="杭州市统计局-2025餐饮业报告",
                url="https://tjj.hz.gov.cn",
                level=SourceLevel.L1_OFFICIAL,
                publisher="杭州市统计局",
                publish_date="2026-03-01",
                excerpt="全市咖啡门店约5000家",
                timeliness_score=0.9,
                independence_score=0.9,
                corroboration_count=2,
            )
        ],
    )
    # 证据B：自媒体，8000家
    ev_b = Evidence(
        claim="杭州咖啡门店数量约8000家",
        sources=[
            Source(
                title="某自媒体文章",
                url="https://example.com/coffee-shop-report",
                level=SourceLevel.L3_INDUSTRY,
                publisher="某自媒体",
                publish_date="2025-06-01",
                excerpt="杭州咖啡门店已超8000家",
                timeliness_score=0.3,
                independence_score=0.2,
                corroboration_count=0,
            )
        ],
    )
    state["evidences"] = [ev_a, ev_b]
    # 给 plan 一个占位子任务（否则 summarize 无内容）
    from core.models import SubTask

    state["plan"].subtasks = [
        SubTask(id="st_0", agent_type="industry", description="调研『竞争格局』：杭州咖啡门店数量")
    ]
    state["subtask_results"] = {"st_0": "杭州咖啡市场门店数量存在数据口径差异。"}
    return state


def test_full_flow_with_conflict():
    """完整链路：router → 执行 → 冲突仲裁 → 质检 → 交付"""
    graph = build_graph()
    state = init_state(
        "我想在杭州开一家咖啡店，能不能做，帮我看看市场机会，预算标准档",
        {"objective": "进入市场", "budget": "标准"},
    )
    state = _inject_conflict_evidence(state)

    # 编译了 checkpointer 的图必须带 thread_id（修复同步 invoke 崩溃）
    result = graph.invoke(state, config={"configurable": {"thread_id": "test_e2e"}})

    # 1. 流程走完
    assert result["status"] == "done"
    print("✓ 完整流程跑通，status = done")

    # 2. 冲突被检测并仲裁
    conflicts = result["conflict_results"]
    assert len(conflicts) >= 1, f"应检测到冲突，实际 {len(conflicts)}"
    card = conflicts[0]["card"]
    print(f"✓ 冲突仲裁生成: decision={card['decision']}, score_a={card['score_a']:.3f}, score_b={card['score_b']:.3f}")
    print(f"  理由: {card['reason']}")
    assert card["decision"] == "adopt_winner"
    assert card["winner"] == "A"

    # 3. 仲裁卡片数据完整（4维breakdown + 权重）
    assert "breakdown_a" in card and "breakdown_b" in card
    assert "weights" in card
    print(f"✓ 仲裁卡片数据完整（4维评分+权重+理由）")

    # 4. Trace 事件包含冲突仲裁节点
    trace_nodes = [ev["node"] for ev in result["trace_events"]]
    assert "check_conflict" in trace_nodes
    print(f"✓ Trace 包含 check_conflict 节点（共{len(trace_nodes)}个事件）")

    # 5. 交付物
    assert result["deliverables"], "应产出交付物"
    print(f"✓ 交付物生成: {[d['type'] for d in result['deliverables']]}")

    return result


def test_quality_rework():
    """质检返工逻辑：不通过 → 返工（限2次）"""
    from graph import node_quality_check, route_after_quality

    # 构造一个质量很差的报告（数字无来源、有推测词）
    bad_report = "杭州咖啡门店预计超8000家，增速可能达30%，投入约50万元。"
    state = init_state("测试", {"objective": "进入市场"})
    state["report_draft"] = bad_report
    state["evidences"] = []  # 无证据支撑

    r1 = node_quality_check(state)
    assert r1["quality_passed"] is False, "坏报告应质检不通过"
    print(f"✓ 质检发现问题: {r1['quality_report']['issues'][:2]}")

    # 返工路由：未超限 → rework
    state["quality_passed"] = False
    state["rework_count"] = 1
    route = route_after_quality(state)
    assert route == "rework"
    print("✓ 返工1次后路由 → rework")

    # 达到上限 → finalize（降级交付）
    state["rework_count"] = 2
    route = route_after_quality(state)
    assert route == "finalize"
    print("✓ 返工达上限 → finalize（降级交付，不无限循环）")


def test_clarification():
    """需求澄清：模糊提问触发澄清"""
    from agents.router import router_ingest

    r = router_ingest("咖啡市场怎么样")
    assert r["need_clarification"] is True
    assert len(r["questions"]) >= 1
    print(f"✓ 需求澄清触发: {len(r['questions'])}个问题")


if __name__ == "__main__":
    print("=== 测试1: 完整流程+冲突仲裁 ===")
    test_full_flow_with_conflict()
    print()
    print("=== 测试2: 质检返工 ===")
    test_quality_rework()
    print()
    print("=== 测试3: 需求澄清 ===")
    test_clarification()
    print()
    print("\n🎉 全部端到端测试通过")
