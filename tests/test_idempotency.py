"""
Unit tests for IdempotencyService.

Covers:
- Cache miss (no stored result) returns None
- Cache hit returns the exact stored RewardResponse
- Second call with same key returns cached result (not recomputed)
- Different key combinations do not collide
- TTL is applied to stored entries
"""
import json
import pytest
from unittest.mock import patch, AsyncMock

from app.api.models import RewardResponse, RewardType
from tests.conftest import BASE_POLICY, make_request

import uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_response(reward_type: RewardType = RewardType.XP) -> RewardResponse:
    return RewardResponse(
        decision_id=uuid.uuid4(),
        policy_version="v1.0",
        reward_type=reward_type,
        reward_value=0 if reward_type == RewardType.XP else 20,
        xp=50,
        reason_codes=["XP_EARNED"],
        meta={"persona": "NEW", "base_xp": 50, "multiplier": 1.5, "capped": False},
    )


# ===========================================================================
# IdempotencyService
# ===========================================================================

class TestIdempotencyService:
    # -----------------------------------------------------------------------
    # get_stored_response
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, idempotency_service):
        """Unknown key must return None."""
        result = await idempotency_service.get_stored_response("txn_miss", "u1", "m1")
        assert result is None

    @pytest.mark.asyncio
    async def test_hit_returns_stored_response(self, idempotency_service):
        """Storing then fetching must return an equal object."""
        original = _sample_response(RewardType.XP)
        await idempotency_service.store_response("txn_001", "user_a", "merch_a", original)

        retrieved = await idempotency_service.get_stored_response("txn_001", "user_a", "merch_a")
        assert retrieved is not None
        assert str(retrieved.decision_id) == str(original.decision_id)
        assert retrieved.reward_type == original.reward_type
        assert retrieved.xp == original.xp

    @pytest.mark.asyncio
    async def test_different_txn_ids_do_not_collide(self, idempotency_service):
        """Two different txn_ids must produce independent cache entries."""
        resp_a = _sample_response(RewardType.XP)
        resp_b = _sample_response(RewardType.CHECKOUT)

        await idempotency_service.store_response("txn_A", "u1", "m1", resp_a)
        await idempotency_service.store_response("txn_B", "u1", "m1", resp_b)

        hit_a = await idempotency_service.get_stored_response("txn_A", "u1", "m1")
        hit_b = await idempotency_service.get_stored_response("txn_B", "u1", "m1")

        assert hit_a.reward_type == RewardType.XP
        assert hit_b.reward_type == RewardType.CHECKOUT

    @pytest.mark.asyncio
    async def test_same_txn_different_users_do_not_collide(self, idempotency_service):
        """Same txn_id but different user/merchant must be independent."""
        resp_u1 = _sample_response(RewardType.XP)
        resp_u2 = _sample_response(RewardType.GOLD)

        await idempotency_service.store_response("txn_same", "user_1", "m1", resp_u1)
        await idempotency_service.store_response("txn_same", "user_2", "m1", resp_u2)

        hit_u1 = await idempotency_service.get_stored_response("txn_same", "user_1", "m1")
        hit_u2 = await idempotency_service.get_stored_response("txn_same", "user_2", "m1")

        assert hit_u1.reward_type == RewardType.XP
        assert hit_u2.reward_type == RewardType.GOLD

    # -----------------------------------------------------------------------
    # Full round-trip via calculate_reward
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_endpoint_returns_cached_on_retry(self, engine, idempotency_service):
        """
        A duplicate request (same txn_id) should get the cached response —
        simulating what the route handler does.
        """
        req = make_request(txn_id="txn_idem_retry", user_id="user_z", amount=1000.0)

        # First call — compute and store
        first_resp = await engine.calculate_reward(req)
        await idempotency_service.store_response(
            req.txn_id, req.user_id, req.merchant_id, first_resp
        )

        # Second call — should hit cache
        cached = await idempotency_service.get_stored_response(
            req.txn_id, req.user_id, req.merchant_id
        )
        assert cached is not None
        assert str(cached.decision_id) == str(first_resp.decision_id)

    @pytest.mark.asyncio
    async def test_monetary_reward_survives_serialization(self, idempotency_service):
        """CHECKOUT response must round-trip through JSON without data loss."""
        original = _sample_response(RewardType.CHECKOUT)
        await idempotency_service.store_response("txn_serial", "u1", "m1", original)

        retrieved = await idempotency_service.get_stored_response("txn_serial", "u1", "m1")
        assert retrieved.reward_type == RewardType.CHECKOUT
        assert retrieved.reward_value == original.reward_value
        assert retrieved.reason_codes == original.reason_codes
        assert retrieved.meta == original.meta

    @pytest.mark.asyncio
    async def test_store_calls_cache_set_with_ttl(self, fake_cache, idempotency_service):
        """store_response must pass a TTL so entries expire."""
        original = _sample_response()
        set_calls = []

        original_set = fake_cache.set

        async def spy_set(key, value, ttl=None):
            set_calls.append({"key": key, "ttl": ttl})
            return await original_set(key, value, ttl=ttl)

        fake_cache.set = spy_set

        await idempotency_service.store_response("txn_ttl", "u1", "m1", original)

        assert len(set_calls) == 1
        stored_ttl = set_calls[0]["ttl"]
        assert stored_ttl is not None and stored_ttl > 0, (
            f"Expected a positive TTL, got {stored_ttl}"
        )
