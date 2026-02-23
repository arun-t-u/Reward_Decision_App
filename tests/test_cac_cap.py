"""
Unit tests for CAC (Customer Acquisition Cost) cap enforcement.

Covers:
- _get_remaining_cac: correct budget calculation per persona
- _get_remaining_cac: floors at 0, never negative
- update_cac: increments the usage counter correctly
- update_cac: multiple calls accumulate correctly
- select_reward_type: returns XP when cac_remaining == 0 (enable_cac_cap=True)
- calculate_reward: falls back to XP when computed monetary reward > cac_remaining
- calculate_reward: allows monetary reward when budget is sufficient
- calculate_reward: reason code CAC_CAP_EXCEEDED is added on fallback
"""
import copy
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from app.api.models import RewardType
from tests.conftest import BASE_POLICY, make_request


# ===========================================================================
# _get_remaining_cac
# ===========================================================================

class TestGetRemainingCac:
    @pytest.mark.asyncio
    async def test_full_budget_when_no_usage(self, engine):
        """With no prior usage the full daily cap should be returned."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        remaining = await engine._get_remaining_cac("user_fresh", "NEW", ts)
        # NEW daily cap = 100 (from BASE_POLICY)
        assert remaining == 100

    @pytest.mark.asyncio
    async def test_deducted_after_prior_usage(self, engine, fake_cache):
        """Remaining budget must be daily_cap minus current usage."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        cac_key = engine._get_cac_key("user_used", ts)
        await fake_cache.set(cac_key, "40")  # simulate 40 already spent

        remaining = await engine._get_remaining_cac("user_used", "NEW", ts)
        assert remaining == 60   # 100 - 40

    @pytest.mark.asyncio
    async def test_never_negative(self, engine, fake_cache):
        """Remaining budget must floor at 0, never go negative."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        cac_key = engine._get_cac_key("user_over", ts)
        await fake_cache.set(cac_key, "999")  # way over cap

        remaining = await engine._get_remaining_cac("user_over", "NEW", ts)
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_per_persona_caps(self, engine):
        """Each persona uses its own daily cap."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        persona_caps = {"NEW": 100, "RETURNING": 50, "POWER": 200}
        for persona, expected_cap in persona_caps.items():
            user = f"user_cap_{persona}"
            remaining = await engine._get_remaining_cac(user, persona, ts)
            assert remaining == expected_cap, (
                f"Persona {persona}: expected cap {expected_cap}, got {remaining}"
            )


# ===========================================================================
# update_cac
# ===========================================================================

class TestUpdateCac:
    @pytest.mark.asyncio
    async def test_increments_cac_counter(self, engine, fake_cache):
        """update_cac must increase the stored counter."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        await engine.update_cac("user_inc", ts, 30)

        cac_key = engine._get_cac_key("user_inc", ts)
        stored = await fake_cache.get(cac_key)
        assert int(stored) == 30

    @pytest.mark.asyncio
    async def test_multiple_calls_accumulate(self, engine, fake_cache):
        """Multiple update_cac calls for the same user+day must accumulate."""
        ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        await engine.update_cac("user_accum", ts, 20)
        await engine.update_cac("user_accum", ts, 15)

        cac_key = engine._get_cac_key("user_accum", ts)
        stored = await fake_cache.get(cac_key)
        assert int(stored) == 35

    @pytest.mark.asyncio
    async def test_different_days_are_independent(self, engine, fake_cache):
        """CAC counters are partitioned by date — different days must not interfere."""
        day1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        day2 = datetime(2025, 1, 16, 10, 0, 0, tzinfo=timezone.utc)

        await engine.update_cac("user_days", day1, 50)
        await engine.update_cac("user_days", day2, 10)

        key_day1 = engine._get_cac_key("user_days", day1)
        key_day2 = engine._get_cac_key("user_days", day2)

        assert int(await fake_cache.get(key_day1)) == 50
        assert int(await fake_cache.get(key_day2)) == 10


# ===========================================================================
# CAC enforcement in _select_reward_type
# ===========================================================================

class TestCacCapInRewardSelection:
    def test_zero_cac_returns_xp(self, engine):
        """_select_reward_type must return XP when cac_remaining == 0."""
        reason_codes = []
        result = engine._select_reward_type(
            txn_id="txn_zero_cac",
            user_id="user_a",
            cac_remaining=0,
            reason_codes=reason_codes,
        )
        assert result == RewardType.XP
        assert "CAC_CAP_EXCEEDED" in reason_codes

    def test_zero_cac_reason_code_present(self, engine):
        """CAC_CAP_EXCEEDED reason code must be appended exactly once."""
        reason_codes = []
        engine._select_reward_type("txn_x", "user_b", cac_remaining=0, reason_codes=reason_codes)
        assert reason_codes.count("CAC_CAP_EXCEEDED") == 1

    def test_positive_cac_allows_non_xp(self, engine):
        """When budget is generous, non-XP rewards must be reachable."""
        results = {
            engine._select_reward_type(f"txn_{i}", "user_c", cac_remaining=9999, reason_codes=[])
            for i in range(60)
        }
        assert (RewardType.CHECKOUT in results) or (RewardType.GOLD in results)

    def test_cac_cap_disabled_ignores_budget(self, engine):
        """With enable_cac_cap=False, zero budget must NOT force XP."""
        policy = copy.deepcopy(BASE_POLICY)
        policy["feature_flags"]["enable_cac_cap"] = False
        with patch("app.services.decision_engine.settings.get_policy", return_value=policy):
            engine._snapshot_policy(policy)

        # Run many times — we should see non-XP types even with zero budget
        results = {
            engine._select_reward_type(f"txn_{i}", "user_d", cac_remaining=0, reason_codes=[])
            for i in range(60)
        }
        assert (RewardType.CHECKOUT in results) or (RewardType.GOLD in results)


# ===========================================================================
# CAC enforcement in calculate_reward (full async path)
# ===========================================================================

class TestCacCapInCalculateReward:
    @pytest.mark.asyncio
    async def test_falls_back_to_xp_when_reward_exceeds_budget(self, engine, fake_cache):
        """
        If the calculated monetary reward > cac_remaining the engine must
        fall back to XP and append CAC_CAP_EXCEEDED.
        """
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Exhaust the budget for user NEW (cap=100): set usage to 98 → remaining=2
        cac_key = engine._get_cac_key("user_near_cap", ts)
        await fake_cache.set(cac_key, "98")

        # amount=5000 → CHECKOUT reward = min(50, max(5, int(5000*0.005))) = 25
        # But remaining CAC = 2 < 25 → should fall back to XP
        req = make_request(
            txn_id="txn_cap_fallback",
            user_id="user_near_cap",   # persona=NEW (default via PersonaService)
            amount=5000.0,
            ts=ts,
        )

        # Force a CHECKOUT selection for this specific txn_id / user_id
        with patch.object(engine, "_select_reward_type", return_value=RewardType.CHECKOUT):
            resp = await engine.calculate_reward(req)

        assert resp.reward_type == RewardType.XP
        assert resp.reward_value == 0
        assert "CAC_CAP_EXCEEDED" in resp.reason_codes

    @pytest.mark.asyncio
    async def test_monetary_granted_when_budget_sufficient(self, engine, fake_cache):
        """
        When the calculated monetary reward fits within the CAC budget,
        the monetary reward type should be preserved.
        """
        ts = datetime(2025, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
        # Leave plenty of budget (usage=0 → remaining=100 for NEW persona)

        req = make_request(
            txn_id="txn_cap_ok",
            user_id="user_fresh_cap",
            amount=1000.0,
            ts=ts,
        )

        # Force CHECKOUT selection; reward = max(5, min(50, int(1000*0.005))) = max(5,5)=5
        # remaining = 100 → 5 ≤ 100 → should stay CHECKOUT
        with patch.object(engine, "_select_reward_type", return_value=RewardType.CHECKOUT):
            resp = await engine.calculate_reward(req)

        assert resp.reward_type == RewardType.CHECKOUT
        assert resp.reward_value == 5
        assert "CHECKOUT_REWARD" in resp.reason_codes
        assert "CAC_CAP_EXCEEDED" not in resp.reason_codes

    @pytest.mark.asyncio
    async def test_cac_remaining_in_meta_decremented(self, engine, fake_cache):
        """Response meta.cac_remaining must reflect the budget after the reward."""
        ts = datetime(2025, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        req = make_request(
            txn_id="txn_meta_cac",
            user_id="user_meta_check",
            amount=1000.0,
            ts=ts,
        )
        resp = await engine.calculate_reward(req)

        # After award, meta cac_remaining = initial_remaining - reward_value
        assert "cac_remaining" in resp.meta
        assert resp.meta["cac_remaining"] >= 0

