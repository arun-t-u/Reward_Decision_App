import hashlib
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from app.core.config import get_settings
from app.api.models import RewardRequest, RewardResponse, Persona, RewardType
from app.services.persona import PersonaService
from app.db.cache import CacheClient


settings = get_settings()


class DecisionEngine:
    """
    Deterministic reward decision engine.
    """

    def __init__(self, cache: CacheClient, persona_service: PersonaService, policy: dict) -> None:
        self.cache = cache
        self.persona_service = persona_service
        self._snapshot_policy(policy)

    @classmethod
    async def create(cls, cache: CacheClient, persona_service: PersonaService) -> "DecisionEngine":
        """Async factory — policy is loaded synchronously (already cached by lifespan)."""
        policy = settings.get_policy()
        return cls(cache, persona_service, policy)

    def _snapshot_policy(self, policy: dict) -> None:
        """
        Cache policy sub-dicts as instance attributes for fast access.
        """
        self._policy_dict = policy
        self.policy = policy
        self._feature_flags: dict = policy.get("feature_flags", {})
        self._reason_map: dict = policy.get("reason_codes", {})
        self._xp_config: dict = policy.get("xp", {})
        self._persona_multipliers: dict = policy.get("persona_multipliers", {})
        self._cac_limits: dict = policy.get("cac_limits", {})
        self._reward_types: dict = policy.get("reward_types", {})
        self._reward_values: dict = policy.get("reward_values", {})
        self._policy_version: str = policy.get("policy_version", "v1")
        self._idempotency_ttl: int = int(policy.get("idempotency_ttl", 86400))

        # Pre-compute reward type weights once
        total_weight = sum(self._reward_types.values()) or 1
        self._norm_weights: dict = {k: v / total_weight for k, v in self._reward_types.items()}

    async def _refresh_policy(self) -> None:
        """
        Re-snapshot policy when a hot-reload is detected (sync get_policy).
        """
        policy = settings.get_policy()
        self._snapshot_policy(policy)

    @staticmethod
    def _get_cac_key(user_id: str, date: datetime) -> str:
        date_str = date.strftime("%Y-%m-%d")
        return f"cac:{user_id}:{date_str}"

    @staticmethod
    def _get_last_reward_key(user_id: str) -> str:
        return f"last_reward:{user_id}"

    async def update_cac(self, user_id: str, date: datetime, amount: int) -> None:
        """
        Increment daily CAC usage using a single pipeline round-trip.
        """
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        else:
            date = date.astimezone(timezone.utc)

        cac_key = self._get_cac_key(user_id, date)

        # Calculate seconds until end of day
        next_midnight = datetime.combine(
            date.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        expire_seconds = max(1, int((next_midnight - date).total_seconds()))

        await self.cache.pipeline_incr_expire(cac_key, amount, expire_seconds)

    async def update_last_reward_ts(self, user_id: str, timestamp: datetime) -> None:
        """
        Update last reward timestamp.
        """
        last_reward_key = self._get_last_reward_key(user_id)
        await self.cache.set(last_reward_key, timestamp.isoformat(), ttl=86400)

    async def _check_cooldown(self, user_id: str) -> bool:
        """
        Returns True if the user is in a cooldown window.
        """
        cooldown_minutes = self._feature_flags.get("cooldown_minutes", 0)
        if cooldown_minutes <= 0:
            return False

        key = self._get_last_reward_key(user_id)
        last_reward_str = await self.cache.get(key)
        if not last_reward_str:
            return False

        try:
            last_reward = datetime.fromisoformat(last_reward_str)
            return datetime.utcnow() < (last_reward + timedelta(minutes=cooldown_minutes))
        except (ValueError, TypeError):
            return False

    async def _get_remaining_cac(self, user_id: str, persona: str, date: datetime) -> int:
        """
        Get remaining CAC budget for user today.
        """
        daily_cap = self._cac_limits.get(persona, 500)
        cac_key = self._get_cac_key(user_id, date)
        current_usage_str = await self.cache.get(cac_key)
        current_usage = int(current_usage_str) if current_usage_str else 0
        return max(0, daily_cap - current_usage)

    def _select_reward_type(
        self,
        txn_id: str,
        user_id: str,
        cac_remaining: int,
        reason_codes: List[str],
    ) -> RewardType:
        """
        Deterministic reward selection. No I/O.
        """
        # Feature flag: force XP
        if self._feature_flags.get("force_xp_mode"):
            reason_codes.append(self._reason_map.get("prefer_xp", "PREFER_XP_MODE"))
            return RewardType.XP

        # Daily CAC cap check
        if self._feature_flags.get("enable_cac_cap") and cac_remaining <= 0:
            reason_codes.append(self._reason_map.get("cac_cap_exceeded", "CAC_CAP_EXCEEDED"))
            return RewardType.XP

        if not self._norm_weights:
            return RewardType.XP

        # SHA-256 deterministic bucket
        hash_input = f"{txn_id}:{user_id}"
        hash_value = int(hashlib.sha256(hash_input.encode()).hexdigest(), 16)
        normalized_value = hash_value / (2 ** 256)

        cumulative = 0.0
        for reward_type, weight in self._norm_weights.items():
            cumulative += weight
            if normalized_value <= cumulative:
                return RewardType(reward_type)

        return RewardType.XP

    def _calculate_xp(
        self, amount: float, persona: str, reason_codes: List[str]
    ) -> Tuple[int, dict]:
        """
        Calculate XP reward. No I/O.
        """
        xp_per_rupee = self._xp_config.get("xp_per_rupee", 0)
        max_xp = self._xp_config.get("max_xp_per_txn", 0)
        multiplier = self._persona_multipliers.get(persona, 1.0)

        base_xp = int(max(amount, 0) * xp_per_rupee)
        xp_with_multiplier = int(base_xp * multiplier)
        final_xp = min(xp_with_multiplier, max_xp)

        reason_codes.append(self._reason_map.get("xp_earned", "XP_EARNED"))
        if multiplier > 1.0:
            reason_codes.append(self._reason_map.get("persona_bonus", "PERSONA_BONUS"))
        if final_xp == max_xp and xp_with_multiplier > max_xp:
            reason_codes.append(self._reason_map.get("max_xp_capped", "MAX_XP_CAPPED"))

        meta = {"base_xp": base_xp, "multiplier": multiplier, "capped": final_xp < xp_with_multiplier}
        return final_xp, meta

    def _calculate_monetary_reward(self, reward_type: RewardType, amount: float) -> int:
        """
        Calculate monetary reward value. No I/O.
        """
        if reward_type == RewardType.XP:
            return 0

        reward_config = self._reward_values.get(reward_type.value)
        if not reward_config:
            return 0

        calculated_value = int(amount * reward_config.get("percent_of_amount", 0))
        return max(reward_config.get("min", 0), min(calculated_value, reward_config.get("max", 0)))

    def _create_cooldown_response(self, request: RewardRequest, persona: str) -> RewardResponse:
        """
        Create a cooldown response.
        """
        reason_codes: List[str] = [self._reason_map.get("cooldown_active", "COOLDOWN_ACTIVE")]
        xp, xp_meta = self._calculate_xp(request.amount, persona, reason_codes)
        return RewardResponse(
            decision_id=uuid.uuid4(),
            policy_version=self._policy_version,
            reward_type=RewardType.XP,
            reward_value=0,
            xp=xp,
            reason_codes=reason_codes,
            meta={"persona": persona, "in_cooldown": True, **xp_meta},
        )

    async def calculate_reward(self, request: RewardRequest) -> RewardResponse:
        """
        Calculate reward for a transaction.
        """
        current_policy = settings.get_policy()          # ← sync, zero-overhead
        if current_policy is not self._policy_dict:
            self._snapshot_policy(current_policy)

        persona = self.persona_service.get_persona(request.user_id)

        # Batch the two async lookups into one gather call
        cooldown_enabled = self._feature_flags.get("cooldown_enabled")
        in_cooldown, cac_remaining = await asyncio.gather(
            self._check_cooldown(request.user_id) if cooldown_enabled else _noop_false(),
            self._get_remaining_cac(request.user_id, persona, request.ts),
        )

        if cooldown_enabled and in_cooldown:
            return self._create_cooldown_response(request, persona)

        reason_codes: List[str] = []

        reward_type = self._select_reward_type(
            request.txn_id, request.user_id, cac_remaining, reason_codes
        )

        xp, xp_meta = self._calculate_xp(request.amount, persona, reason_codes)

        reward_value = 0
        if reward_type != RewardType.XP:
            reward_value = self._calculate_monetary_reward(reward_type, request.amount)

            if self._feature_flags.get("enable_cac_cap") and reward_value > cac_remaining:
                reward_type = RewardType.XP
                reward_value = 0
                reason_codes.append(self._reason_map.get("cac_cap_exceeded", "CAC_CAP_EXCEEDED"))
            else:
                if reward_type == RewardType.CHECKOUT:
                    reason_codes.append(self._reason_map.get("checkout_reward", "CHECKOUT_REWARD"))
                elif reward_type == RewardType.GOLD:
                    reason_codes.append(self._reason_map.get("gold_reward", "GOLD_REWARD"))

        meta = {
            "persona": persona,
            "cac_remaining": cac_remaining - reward_value,
            **xp_meta,
        }

        return RewardResponse(
            decision_id=uuid.uuid4(),
            policy_version=self._policy_version,
            reward_type=reward_type,
            reward_value=reward_value,
            xp=xp,
            reason_codes=reason_codes,
            meta=meta,
        )


async def _noop_false() -> bool:
    return False
