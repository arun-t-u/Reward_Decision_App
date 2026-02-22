import json
from typing import Optional
from app.db.cache import CacheClient
from app.api.models import RewardResponse
from app.core.config import get_settings

settings = get_settings()


class IdempotencyService:
    """
    Idempotency service to prevent duplicate reward calculations.
    """
    def __init__(self, cache: CacheClient) -> None:
        self.cache = cache

    def _get_key(self, txn_id: str, user_id: str, merchant_id: str) -> str:
        """
        Get the cache key for the idempotency service.
        """
        return f"idem:{txn_id}:{user_id}:{merchant_id}"

    async def get_stored_response(
        self, txn_id: str, user_id: str, merchant_id: str
    ) -> Optional[RewardResponse]:
        """
        Get the stored response for the given transaction.
        """
        key = self._get_key(txn_id, user_id, merchant_id)
        stored = await self.cache.get(key)
        if stored:
            data = json.loads(stored)
            return RewardResponse(**data)
        return None

    async def store_response(
        self,
        txn_id: str,
        user_id: str,
        merchant_id: str,
        response: RewardResponse,
    ) -> None:
        """
        Store the response for the given transaction.
        TTL is read from the live policy so it picks up hot-reload changes.
        """
        key = self._get_key(txn_id, user_id, merchant_id)

        policy = await settings.get_policy()
        ttl = int(policy.get("idempotency_ttl", 86400))

        try:
            json_str = response.model_dump_json()
        except AttributeError:
            json_str = response.json()

        await self.cache.set(key, json_str, ttl=ttl)
