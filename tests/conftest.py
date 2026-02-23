"""
Shared pytest fixtures for all Reward Decision App test modules.

get_policy() is now sync — all mocks use plain MagicMock (no AsyncMock needed).
"""
import pytest
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from app.db.cache import CacheClient
from app.services.persona import PersonaService
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService
from app.api.models import RewardRequest, TransactionType


# ---------------------------------------------------------------------------
# Baseline policy — mirrors policy.json so tests are self-contained
# ---------------------------------------------------------------------------

BASE_POLICY: Dict[str, Any] = {
    "policy_version": "v1.0",
    "idempotency_ttl": "86400",
    "reward_types": {
        "XP": 0.5,
        "CHECKOUT": 0.3,
        "GOLD": 0.2,
    },
    "xp": {
        "xp_per_rupee": 0.1,
        "max_xp_per_txn": 100,
    },
    "persona_multipliers": {
        "NEW": 1.5,
        "RETURNING": 1.0,
        "POWER": 2.0,
    },
    "cac_limits": {
        "NEW": 100,
        "RETURNING": 50,
        "POWER": 200,
    },
    "reward_values": {
        "CHECKOUT": {"min": 5, "max": 50, "percent_of_amount": 0.005},
        "GOLD":     {"min": 15, "max": 40, "percent_of_amount": 0.003},
    },
    "feature_flags": {
        "force_xp_mode":    False,
        "cooldown_enabled": True,
        "cooldown_minutes": 10,
        "enable_cac_cap":   True,
    },
    "reason_codes": {
        "xp_earned":        "XP_EARNED",
        "persona_bonus":    "PERSONA_BONUS",
        "cac_cap_exceeded": "CAC_CAP_EXCEEDED",
        "cooldown_active":  "COOLDOWN_ACTIVE",
        "checkout_reward":  "CHECKOUT_REWARD",
        "gold_reward":      "GOLD_REWARD",
        "prefer_xp":        "PREFER_XP_MODE",
        "max_xp_capped":    "MAX_XP_CAPPED",
    },
}


# ---------------------------------------------------------------------------
# Fake async cache — no Redis needed
# ---------------------------------------------------------------------------

class FakeCacheClient(CacheClient):
    """Pure in-memory cache implementing the full CacheClient interface."""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        self._store[key] = str(value)
        return True

    async def incr(self, key: str, amount: int = 1) -> int:
        current = int(self._store.get(key, "0"))
        new_val = current + amount
        self._store[key] = str(new_val)
        return new_val

    async def ttl(self, key: str) -> int:
        return -1 if key in self._store else -2

    async def expire(self, key: str, ttl: int) -> bool:
        return key in self._store

    async def pipeline_incr_expire(self, key: str, amount: int, expire_seconds: int) -> int:
        return await self.incr(key, amount)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cache() -> FakeCacheClient:
    return FakeCacheClient()


@pytest.fixture
def persona_service() -> PersonaService:
    return PersonaService()


@pytest.fixture
def engine(fake_cache: FakeCacheClient, persona_service: PersonaService) -> DecisionEngine:
    """
    DecisionEngine wired with the baseline policy (no disk reads).
    get_policy() is now sync, so we patch it with a plain return value.
    """
    with patch(
        "app.services.decision_engine.settings.get_policy",
        return_value=BASE_POLICY,
    ):
        eng = DecisionEngine(
            cache=fake_cache,
            persona_service=persona_service,
            policy=BASE_POLICY,
        )
    return eng


@pytest.fixture
def idempotency_service(fake_cache: FakeCacheClient) -> IdempotencyService:
    """IdempotencyService with sync get_policy mocked."""
    with patch(
        "app.services.idempotency.settings.get_policy",
        return_value=BASE_POLICY,
    ):
        svc = IdempotencyService(cache=fake_cache)
    return svc


def make_request(
    txn_id: str = "txn_001",
    user_id: str = "user_99",        # unknown → persona defaults to NEW
    merchant_id: str = "m_01",
    amount: float = 1000.0,
    txn_type: TransactionType = TransactionType.UPI,
    ts: datetime = None,
) -> RewardRequest:
    """Helper to build a RewardRequest with sensible defaults."""
    return RewardRequest(
        txn_id=txn_id,
        user_id=user_id,
        merchant_id=merchant_id,
        amount=amount,
        txn_type=txn_type,
        ts=ts or datetime.now(timezone.utc),
    )
