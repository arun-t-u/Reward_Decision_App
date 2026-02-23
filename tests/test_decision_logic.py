"""
Unit tests for reward decision logic.

Covers:
- XP calculation (base, persona multiplier, cap)
- Deterministic reward type selection (SHA-256 bucket)
- Monetary reward calculation (CHECKOUT, GOLD, min/max clamp)
- force_xp_mode feature flag
- Cooldown guard (active / inactive)
- Full calculate_reward integration path
"""
import hashlib
import copy
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

from app.api.models import RewardType, TransactionType
from tests.conftest import BASE_POLICY, make_request


# ===========================================================================
# _calculate_xp
# ===========================================================================

class TestCalculateXP:
    def test_base_xp_for_new_user(self, engine):
        """NEW persona gets 1.5× multiplier."""
        xp, meta = engine._calculate_xp(amount=1000.0, persona="NEW", reason_codes=[])
        # base = 1000 * 0.1 = 100, × 1.5 = 150, capped at 100
        assert xp == 100
        assert meta["base_xp"] == 100
        assert meta["multiplier"] == 1.5
        assert meta["capped"] is True

    def test_returning_user_no_multiplier(self, engine):
        """RETURNING persona gets 1.0× — no bonus."""
        reason_codes = []
        xp, meta = engine._calculate_xp(amount=200.0, persona="RETURNING", reason_codes=reason_codes)
        # base = 200 * 0.1 = 20, × 1.0 = 20
        assert xp == 20
        assert meta["multiplier"] == 1.0
        assert "PERSONA_BONUS" not in reason_codes

    def test_power_user_multiplier(self, engine):
        """POWER persona gets 2.0× multiplier."""
        reason_codes = []
        xp, meta = engine._calculate_xp(amount=200.0, persona="POWER", reason_codes=reason_codes)
        # base = 20, × 2.0 = 40
        assert xp == 40
        assert meta["multiplier"] == 2.0
        assert "PERSONA_BONUS" in reason_codes

    def test_xp_capped_at_max(self, engine):
        """XP never exceeds max_xp_per_txn."""
        xp, meta = engine._calculate_xp(amount=50_000.0, persona="POWER", reason_codes=[])
        assert xp == 100           # max cap
        assert meta["capped"] is True

    def test_xp_not_capped_below_max(self, engine):
        """Small amount should NOT hit the cap."""
        xp, meta = engine._calculate_xp(amount=10.0, persona="RETURNING", reason_codes=[])
        # base = 10 * 0.1 = 1
        assert xp == 1
        assert meta["capped"] is False

    def test_xp_reason_code_always_added(self, engine):
        """XP_EARNED reason code is always appended."""
        reason_codes = []
        engine._calculate_xp(100.0, "RETURNING", reason_codes)
        assert "XP_EARNED" in reason_codes

    def test_max_xp_capped_reason_code(self, engine):
        """MAX_XP_CAPPED is added when the computed XP exceeds the cap."""
        reason_codes = []
        engine._calculate_xp(50_000.0, "POWER", reason_codes)
        assert "MAX_XP_CAPPED" in reason_codes

    def test_zero_amount(self, engine):
        """Zero-amount transaction yields 0 XP without crashing."""
        xp, _ = engine._calculate_xp(amount=0.0, persona="NEW", reason_codes=[])
        assert xp == 0


# ===========================================================================
# _select_reward_type  (deterministic)
# ===========================================================================

class TestSelectRewardType:
    def test_deterministic_for_same_input(self, engine):
        """Same txn_id + user_id must always return the same reward type."""
        result_a = engine._select_reward_type("txn_abc", "user_1", 999, [])
        result_b = engine._select_reward_type("txn_abc", "user_1", 999, [])
        assert result_a == result_b

    def test_different_txn_ids_can_differ(self, engine):
        """Different txn_ids should (statistically) yield different reward types."""
        results = {
            engine._select_reward_type(f"txn_{i}", "user_x", 999, [])
            for i in range(50)
        }
        # With 50 draws across 3 types all types should appear
        assert len(results) > 1

    def test_force_xp_mode_flag(self, engine):
        """force_xp_mode=True must always return XP regardless of hash."""
        policy = copy.deepcopy(BASE_POLICY)
        policy["feature_flags"]["force_xp_mode"] = True
        with patch("app.services.decision_engine.settings.get_policy", return_value=policy):
            engine._snapshot_policy(policy)

        for i in range(10):
            reason_codes = []
            result = engine._select_reward_type(f"txn_{i}", "user_x", 999, reason_codes)
            assert result == RewardType.XP
            assert "PREFER_XP_MODE" in reason_codes

    def test_cac_zero_forces_xp(self, engine):
        """cac_remaining=0 with enable_cac_cap must yield XP."""
        reason_codes = []
        result = engine._select_reward_type("txn_001", "user_1", cac_remaining=0, reason_codes=reason_codes)
        assert result == RewardType.XP
        assert "CAC_CAP_EXCEEDED" in reason_codes

    def test_cac_positive_allows_monetary(self, engine):
        """With enough CAC budget, non-XP types may be returned."""
        # Run many txn_ids — at least one should be CHECKOUT or GOLD with open budget
        results = {
            engine._select_reward_type(f"txn_{i}", "user_y", cac_remaining=9999, reason_codes=[])
            for i in range(60)
        }
        assert RewardType.CHECKOUT in results or RewardType.GOLD in results


# ===========================================================================
# _calculate_monetary_reward
# ===========================================================================

class TestCalculateMonetaryReward:
    def test_xp_type_returns_zero(self, engine):
        assert engine._calculate_monetary_reward(RewardType.XP, 5000.0) == 0

    def test_checkout_min_floor(self, engine):
        """Tiny amount should still return the min floor."""
        # 1 * 0.005 = 0.005 → 0, but floor is 5
        result = engine._calculate_monetary_reward(RewardType.CHECKOUT, 1.0)
        assert result == 5

    def test_checkout_max_ceiling(self, engine):
        """Very large amount should be capped at max."""
        # 100_000 * 0.005 = 500, capped at 50
        result = engine._calculate_monetary_reward(RewardType.CHECKOUT, 100_000.0)
        assert result == 50

    def test_checkout_mid_range(self, engine):
        """Mid-range amount within min/max bounds."""
        # 2000 * 0.005 = 10 → clamp(5, 10, 50) = 10
        result = engine._calculate_monetary_reward(RewardType.CHECKOUT, 2000.0)
        assert result == 10

    def test_gold_min_floor(self, engine):
        # 1 * 0.003 ~ 0, floor = 15
        result = engine._calculate_monetary_reward(RewardType.GOLD, 1.0)
        assert result == 15

    def test_gold_max_ceiling(self, engine):
        # 100_000 * 0.003 = 300, capped at 40
        result = engine._calculate_monetary_reward(RewardType.GOLD, 100_000.0)
        assert result == 40


# ===========================================================================
# calculate_reward  (full async integration)
# ===========================================================================

class TestCalculateReward:
    @pytest.mark.asyncio
    async def test_response_structure(self, engine):
        """Response must include all required fields with valid types."""
        req = make_request(txn_id="txn_struct_001", amount=500.0)
        resp = await engine.calculate_reward(req)

        assert resp.decision_id is not None
        assert resp.policy_version == "v1.0"
        assert resp.reward_type in RewardType
        assert resp.xp >= 0
        assert resp.reward_value >= 0
        assert isinstance(resp.reason_codes, list)
        assert len(resp.reason_codes) > 0

    @pytest.mark.asyncio
    async def test_xp_always_awarded(self, engine):
        """XP must be > 0 for any positive transaction amount."""
        req = make_request(txn_id="txn_xp_001", amount=100.0)
        resp = await engine.calculate_reward(req)
        assert resp.xp > 0

    @pytest.mark.asyncio
    async def test_deterministic_across_calls(self, engine):
        """Same txn_id + user_id must return the same reward type every time."""
        req = make_request(txn_id="txn_det_999", user_id="user_fixed")
        resp1 = await engine.calculate_reward(req)
        resp2 = await engine.calculate_reward(req)
        assert resp1.reward_type == resp2.reward_type

    @pytest.mark.asyncio
    async def test_cooldown_active_returns_xp_only(self, engine, fake_cache):
        """A user who already received a reward within the cooldown window gets XP only."""
        # Plant a last-reward timestamp 5 minutes ago (cooldown = 10 min)
        key = f"last_reward:user_cool"
        recent_ts = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        await fake_cache.set(key, recent_ts)

        req = make_request(user_id="user_cool", txn_id="txn_cool_001", amount=2000.0)
        resp = await engine.calculate_reward(req)

        assert resp.reward_type == RewardType.XP
        assert resp.reward_value == 0
        assert "COOLDOWN_ACTIVE" in resp.reason_codes
        assert resp.meta.get("in_cooldown") is True

    @pytest.mark.asyncio
    async def test_no_cooldown_after_window_expires(self, engine, fake_cache):
        """A user whose cooldown window has passed should NOT be blocked."""
        key = f"last_reward:user_expired_cool"
        old_ts = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        await fake_cache.set(key, old_ts)

        req = make_request(user_id="user_expired_cool", txn_id="txn_expiry_001", amount=500.0)
        resp = await engine.calculate_reward(req)

        assert "COOLDOWN_ACTIVE" not in resp.reason_codes

    @pytest.mark.asyncio
    async def test_force_xp_mode_returns_xp(self, engine):
        """force_xp_mode=True: every request returns XP reward type, value=0."""
        policy = copy.deepcopy(BASE_POLICY)
        policy["feature_flags"]["force_xp_mode"] = True

        # Patch must stay active for the ENTIRE calculate_reward call because
        # the hot-reload guard inside it also awaits get_policy().
        with patch(
            "app.services.decision_engine.settings.get_policy",
            return_value=policy,
        ):
            engine._snapshot_policy(policy)
            req = make_request(txn_id="txn_force_xp", amount=5000.0)
            resp = await engine.calculate_reward(req)

        assert resp.reward_type == RewardType.XP
        assert resp.reward_value == 0

