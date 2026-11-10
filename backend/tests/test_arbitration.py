"""可解释证据仲裁模型测试 —— 评审命门1的技术钩子

覆盖：
1. 可信度合成分计算正确
2. 冲突仲裁：分差大 → 采信高分者
3. 冲突仲裁：L1官方来源优先
4. 冲突仲裁：分差接近且无L1 → 并列展示（不替用户下结论）
5. 边界：空来源
"""

import pytest

from core.models import (
    Evidence,
    Source,
    SourceLevel,
    arbitrate_conflict,
    evidence_credibility_score,
)


def _make_source(
    level: SourceLevel,
    title: str = "来源",
    timeliness: float = 0.5,
    independence: float = 0.5,
    corroboration: int = 0,
) -> Source:
    return Source(
        title=title,
        level=level,
        timeliness_score=timeliness,
        independence_score=independence,
        corroboration_count=corroboration,
    )


class TestCredibilityScore:
    def test_official_outranks_social(self):
        """L1官方 > L4用户（信源等级权重最大）"""
        official = _make_source(SourceLevel.L1_OFFICIAL)
        user = _make_source(SourceLevel.L4_USER)
        assert evidence_credibility_score(official) > evidence_credibility_score(user)

    def test_fresh_and_corroborated_high(self):
        """新 + 多佐证 → 高分"""
        s = _make_source(
            SourceLevel.L3_INDUSTRY, timeliness=0.9, independence=0.8, corroboration=3
        )
        assert evidence_credibility_score(s) > 0.8

    def test_score_bounded(self):
        """分数必须在 [0,1]"""
        for level in SourceLevel:
            s = _make_source(level)
            score = evidence_credibility_score(s)
            assert 0.0 <= score <= 1.0


class TestArbitration:
    def test_big_gap_adopts_winner(self):
        """分差≥0.15 → 采信高分者"""
        a = Evidence(claim="A", sources=[_make_source(SourceLevel.L1_OFFICIAL)])
        b = Evidence(claim="B", sources=[_make_source(SourceLevel.L4_USER)])
        r = arbitrate_conflict(a, b)
        assert r["decision"] == "adopt_winner"
        assert r["winner"] == "A"

    def test_official_precedence(self):
        """无L1但有一方含L1 → 采信L1方"""
        a = Evidence(claim="A", sources=[_make_source(SourceLevel.L3_INDUSTRY)])
        b = Evidence(claim="B", sources=[_make_source(SourceLevel.L3_INDUSTRY)])
        # 人为让A含L1
        a.sources[0].level = SourceLevel.L1_OFFICIAL
        r = arbitrate_conflict(a, b)
        assert r["decision"] == "adopt_winner"
        assert r["winner"] == "A"

    def test_close_gap_shows_both(self):
        """分差<0.15 且无L1 → 并列展示，不替用户下结论"""
        a = Evidence(claim="A", sources=[_make_source(SourceLevel.L3_INDUSTRY)])
        b = Evidence(claim="B", sources=[_make_source(SourceLevel.L3_INDUSTRY)])
        r = arbitrate_conflict(a, b)
        assert r["decision"] == "show_both"
        assert r["winner"] == ""

    def test_empty_sources_safe(self):
        """空来源不崩溃"""
        a = Evidence(claim="A")
        b = Evidence(claim="B")
        r = arbitrate_conflict(a, b)
        assert r["decision"] in ("adopt_winner", "show_both")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
