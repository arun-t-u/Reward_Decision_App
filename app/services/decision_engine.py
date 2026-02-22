import random
import hashlib
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Tuple

from app.core.config import get_settings
from app.api.models import RewardRequest, RewardResponse, Persona, RewardType
from app.services.persona import PersonaService
from app.db.cache import CacheClient



settings = get_settings()

class DecisionEngine:
    def __init__(self, cache: CacheClient, persona_service: PersonaService):
        self.cache = cache
        self.persona_service = persona_service
        self.policy = self._get_policy()

    def _get_policy(self):
        return settings.get_policy()
    
    def _get_cac_key(self, user_id: str, date: datetime) -> str:
        """
        Generate CAC tracking key for a user on a specific date.
        
        Args:
            user_id: User ID
            date: Date to track (uses UTC date)
            
        Returns:
            Cache key for CAC tracking
        """
        date_str = date.strftime("%Y-%m-%d")
        return f"cac:{user_id}:{date_str}"

    def _get_last_reward_key(self, user_id: str) -> str:
        return f"last_reward:{user_id}"


    async def _update_cac(self, user_id: str, date: datetime, amount: int) -> None:
        """
        Increment daily CAC usage for a user-persona.
        Key auto-expires at end of day.
        
        Args:
            user_id: User ID
            date: Current date
            amount: Amount to add to CAC usage
        
        """
        cac_key = self._get_cac_key(user_id, date)

        # Increment first
        await self.cache.incr(cac_key, amount)

        # Ensure key expires at end of transaction day (UTC)
        ttl = await self.cache.ttl(cac_key)

        if ttl == -1:
            next_midnight = datetime.combine(
                date.date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc
            )

            expire_seconds = int((next_midnight - date).total_seconds())

            if expire_seconds > 0:
                await self.cache.expire(cac_key, expire_seconds)

    async def _check_cooldown(self, user_id: str) -> bool:
        """
        Returns True if user is currently in cooldown period
        """
        cooldown_minutes = self.policy.get("feature_flags", {}).get("cooldown_minutes", 0)
        if cooldown_minutes <= 0:
            return False
            
        key = self._get_last_reward_key(user_id)
        last_reward_str = await self.cache.get(key)

        if not last_reward_str:
            return False
        
        try:
            last_reward = datetime.fromisoformat(last_reward_str)
            cooldown_until = last_reward + timedelta(minutes=cooldown_minutes)
            now = datetime.utcnow()

            is_in_cooldown = now < cooldown_until
            return is_in_cooldown
            
        except (ValueError, TypeError) as e:
            return False

    async def _update_last_reward_ts(self, user_id: str, timestamp: datetime) -> None:
        """Update last reward timestamp."""
        last_reward_key = self._get_last_reward_key(user_id)
        await self.cache.set(last_reward_key, timestamp.isoformat(), ttl=86400)

    async def calculate_reward(self, request: RewardRequest) -> RewardResponse:
        persona = self.persona_service.get_persona(request.user_id)

        # Check Cooldown
        if self.policy.get("feature_flags", {}).get("cooldown_enabled"):
            if await self._check_cooldown(request.user_id):
                return self._create_cooldown_response(request, persona)

        # Get remaining CAC budget
        cac_remaining = await self._get_remaining_cac(request.user_id, persona, request.ts)

        # Calculate rewards
        reason_codes: List[str] = []

        # Select reward type
        reward_type = self._select_reward_type(
            request.txn_id,
            request.user_id,
            request.amount,
            cac_remaining,
            reason_codes
        )

        # Always calculate XP
        xp, xp_meta = self._calculate_xp(
            request.amount,
            persona,
            reason_codes
        )

        # Calculate monetary reward if applicable
        reward_value = 0
        if reward_type != RewardType.XP:
            reward_value = self._calculate_monetary_reward(
                reward_type,
                request.amount
            )
            
            # Check if we have enough CAC budget
            if self.policy.get("feature_flags", {}).get("enable_cac_cap") and reward_value > cac_remaining:
                # logger.info(
                #     f"CAC cap exceeded: needed={reward_value}, "
                #     f"remaining={cac_remaining}, falling back to XP"
                # )
                reward_type = RewardType.XP
                reward_value = 0
                reason_codes.append(self.policy.get("reason_codes", {}).get("cac_cap_exceeded", "CAC_CAP_EXCEEDED"))
            else:
                # Add appropriate reason code
                if reward_type == RewardType.CHECKOUT:
                    reason_codes.append(self.policy.get("reason_codes", {}).get("checkout_reward", "CHECKOUT_REWARD"))
                elif reward_type == RewardType.GOLD:
                    reason_codes.append(self.policy.get("reason_codes", {}).get("gold_reward", "GOLD_REWARD"))

        # Build metadata
        meta = {
            "persona": persona,
            "cac_remaining": cac_remaining - reward_value,
            **xp_meta
        }

        # Create response
        response = RewardResponse(
            decision_id=uuid.uuid4(),
            policy_version=self.policy.get("policy_version"),
            reward_type=reward_type,
            reward_value=reward_value,
            xp=xp,
            reason_codes=reason_codes,
            meta=meta
        )

        return response
    
    def _create_cooldown_response(self, request: RewardRequest, persona: Persona) -> RewardResponse:
        """
        Create response for user in cooldown period.
        Returns XP-only reward with cooldown reason code.
        """
        reason_config = self.policy.get("reason_codes", {})
        reason_codes: List[str] = [
            reason_config.get("cooldown_active", "COOLDOWN_ACTIVE")
        ]
        
        # Calculate XP
        xp, xp_meta = self._calculate_xp(
            request.amount,
            persona,
            reason_codes
        )

        return RewardResponse(
            decision_id=uuid.uuid4(),
            policy_version=self.policy.get("policy_version", "1.0"),
            reward_type=RewardType.XP,
            reward_value=0,
            xp=xp,
            reason_codes=reason_codes,
            meta={
                "persona": persona,
                "in_cooldown": True,
                **xp_meta
            }
        )
    
    def _calculate_xp(
        self,
        amount: float,
        persona: Persona,
        reason_codes: List[str]
    ) -> Tuple[int, dict]:
        """
        Calculate XP reward based on transaction amount and persona.
        
        Args:
            amount: Transaction amount in rupees
            persona: User persona
            reason_codes: List to append reason codes to
            
        Returns:
            Tuple of (xp_amount, metadata)
        """
        # Get XP configuration
        xp_config = self.policy.get("xp", {})
        xp_per_rupee = xp_config.get("xp_per_rupee", 0)
        max_xp = xp_config.get("max_xp_per_txn", 0)

        amount = max(amount, 0)

        # Get persona multiplier
        multiplier = self.policy.get("persona_multipliers", {}).get(persona, 1.0)

        # Calculate base XP
        base_xp = int(amount * xp_per_rupee)
        
        # Apply persona multiplier
        xp_with_multiplier = int(base_xp * multiplier)
        
        # Cap at maximum
        final_xp = min(xp_with_multiplier, max_xp)
        
        # Add reason codes
        reason_config = self.policy.get("reason_codes", {})
        reason_codes.append(reason_config.get("xp_earned", "XP_EARNED"))

        if multiplier > 1.0:
            reason_codes.append(reason_config.get("persona_bonus", "PERSONA_BONUS"))

        if final_xp == max_xp and xp_with_multiplier > max_xp:
            reason_codes.append(reason_config.get("max_xp_capped", "MAX_XP_CAPPED"))
        
        # Metadata
        meta = {
            "base_xp": base_xp,
            "multiplier": multiplier,
            "capped": final_xp < xp_with_multiplier
        }
        
        return final_xp, meta
    
    async def _get_remaining_cac(
        self,
        user_id: str,
        persona: Persona,
        date: datetime
    ) -> int:
        """
        Get remaining CAC budget for user today.
        
        Args:
            user_id: User ID
            persona: User persona
            date: Current date
            
        Returns:
            Remaining CAC budget in rupees
        """
        # Get daily CAC cap for persona; default 500
        daily_cap = self.policy.get("cac_limits", {}).get(persona, 500)
        
        # Get current CAC usage
        cac_key = self._get_cac_key(user_id, date)
        current_usage_str = await self.cache.get(cac_key)
        current_usage = int(current_usage_str) if current_usage_str else 0
        
        remaining = daily_cap - current_usage

        return max(0, remaining)
    

    def _select_reward_type(
        self,
        txn_id: str,
        user_id: str,
        amount: float,
        cac_remaining: int,
        reason_codes: List[str]
    ) -> RewardType:
        """
        Deterministic reward selection based on transaction + user context.

        Decision priority:
        1. Feature flag: force_xp_mode → always XP
        2. CAC daily cap exceeded → always XP
        3. Eligibility checks (global monthly budget + user monthly usage)
        4. Deterministic bucket selection via SHA-256
        5. Fallback → XP

        Returns:
            (RewardType, reward_value, reason_codes)
        """
        feature_flags: dict = self.policy.get("feature_flags", {})
        reason_map: dict = self.policy.get("reason_codes", {})

        # Force-XP feature flag
        if feature_flags.get("force_xp_mode"):
            reason_codes.append(reason_map.get("prefer_xp", "PREFER_XP_MODE"))
            # logger.debug("force_xp_mode active → XP", extra={"user_id": user_id, "txn_id": txn_id})
            return RewardType.XP
        
        # Daily CAC cap check
        if feature_flags.get("enable_cac_cap") and cac_remaining <= 0:
            reason_codes.append(reason_map.get("cac_cap_exceeded", "CAC_CAP_EXCEEDED"))
            # logger.debug("CAC cap exceeded, forcing XP reward")
            return RewardType.XP
        
        # todo: Add global monthly budgets
        # todo: Eligibility can add if more than specific amount or specific merchant the chance of getting gold is high

        reward_types = self.policy.get("reward_types", {})
        if not reward_types:
            raise ValueError("No reward types configured")
        
        # Normalize weights
        total_weight = sum(reward_types.values())
        if total_weight <= 0:
            raise ValueError("Invalid reward weights")
        
        normalized_weights = {
            k: v / total_weight
            for k, v in reward_types.items()
        }

        # Use hash of txn_id + user_id for deterministic selection
        hash_input = f"{txn_id}:{user_id}"
        hash_value = int(hashlib.sha256(hash_input.encode()).hexdigest(), 16)

        # Use full entropy
        normalized_value = hash_value / 2**256

        cumulative = 0.0

        for reward_type in normalized_weights.keys():
            cumulative += normalized_weights[reward_type]
            print(f"{reward_type = }  {normalized_value =}  {cumulative =}")
            if normalized_value <= cumulative:
                return RewardType(reward_type)
        
        # Fallback to XP
        return RewardType.XP


    def _calculate_monetary_reward(
        self,
        reward_type: RewardType,
        amount: float
    ) -> int:
        """
        Calculate monetary reward value.
        
        Args:
            reward_type: Type of monetary reward (CHECKOUT or GOLD)
            amount: Transaction amount
            
        Returns:
            Reward value in rupees
        """
        if reward_type == RewardType.XP:
            return 0
                
        reward_config = self.policy.get("reward_values", {}).get(reward_type.value)
        if not reward_config:
            # logger.warning(f"No configuration for reward type: {reward_type}")
            return 0
        
        # Calculate based on percentage of transaction
        calculated_value = int(amount * reward_config.get("percent_of_amount"))
        
        # Clamp between min and max
        reward_value = max(
            reward_config.get("min"),
            min(calculated_value, reward_config.get('max'))
        )
        
        # logger.debug(
        #     f"Monetary reward: type={reward_type.value}, amount={amount}, "
        #     f"calculated={calculated_value}, final={reward_value}"
        # )
        
        return reward_value
    
    
