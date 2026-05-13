"""Tests for the candidate universe builder."""

from src.tracking.universe import build_universe, CORE_WATCHLIST


class TestBuildUniverse:
    def test_current_positions_always_first(self):
        u = build_universe(current_positions=["XYZ", "FOO"], discovered=[], max_size=10)
        assert u[:2] == ["XYZ", "FOO"]

    def test_dedup(self):
        u = build_universe(current_positions=["NVDA"], discovered=["NVDA", "AMD"], max_size=10)
        # NVDA appears once
        assert u.count("NVDA") == 1

    def test_case_normalized(self):
        u = build_universe(current_positions=["nvda"], discovered=["NVDA"], max_size=10)
        assert u.count("NVDA") == 1
        assert "NVDA" in u

    def test_cap_keeps_current_positions(self):
        # 25 fake current positions + cap of 10 → all 25 survive (forced)
        positions = [f"T{i}" for i in range(25)]
        u = build_universe(current_positions=positions, discovered=[], max_size=10)
        # All 25 forced names should still appear
        for t in positions:
            assert t in u

    def test_core_watchlist_included(self):
        u = build_universe(current_positions=[], discovered=[], max_size=100)
        assert "SPY" in u
        assert "NVDA" in u
        assert "IBIT" in u   # crypto-via-ETF
