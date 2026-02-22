import json
from datetime import timedelta
from typing import Optional
from app.db.cache import CacheClient
from app.api.models import RewardResponse
from app.core.config import get_settings

settings = get_settings()

class IdempotencyService:
    def __init__(self, cache: CacheClient):
        self.cache = cache
        self.policy = self._get_policy()

    def _get_policy(self):
        return settings.get_policy()

    def _get_key(self, txn_id: str, user_id: str, merchant_id: str) -> str:
        return f"idem:{txn_id}:{user_id}:{merchant_id}"

    async def get_stored_response(self, txn_id: str, user_id: str, merchant_id: str) -> Optional[RewardResponse]:
        key = self._get_key(txn_id, user_id, merchant_id)
        stored = await self.cache.get(key)
        if stored:
            data = json.loads(stored)
            return RewardResponse(**data)
        return None

    async def store_response(self, txn_id: str, user_id: str, merchant_id: str, response: RewardResponse):
        key = self._get_key(txn_id, user_id, merchant_id)
       
        try:
             json_str = response.model_dump_json()
        except AttributeError:
             json_str = response.json()
             
        await self.cache.set(key, json_str, ttl=int(self.policy.get("idempotency_ttl", "86400")))
