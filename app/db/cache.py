import abc
import time
import json
from typing import Optional, Any
import redis.asyncio as redis
import logging

logger = logging.getLogger(__name__)

class CacheClient(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        pass

    @abc.abstractmethod
    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        pass

    @abc.abstractmethod
    async def incr(self, key: str, amount: int = 1) -> int:
        pass
        
    @abc.abstractmethod
    async def ttl(self, key: str) -> int:
        pass

    @abc.abstractmethod
    async def expire(self, key: str, ttl: int) -> bool:
        pass


class RedisCacheClient(CacheClient):
    def __init__(self, host: str, port: int, db: int):
        self.client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        # Real connection check usually happens on first usage or startup event.
        logger.info(f"Initialized Redis Client at {host}:{port}")

    async def get(self, key: str) -> Optional[Any]:
        try:
            return await self.client.get(key)
        except redis.RedisError:
            return None

    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        try:
            return await self.client.set(key, value, ex=ttl)
        except redis.RedisError:
            return False

    async def incr(self, key: str, amount: int = 1) -> int:
        try:
            return await self.client.incrby(key, amount)
        except redis.RedisError:
            return 0
            
    async def ttl(self, key: str) -> int:
        try:
            return await self.client.ttl(key)
        except redis.RedisError:
            return -1

    async def expire(self, key: str, ttl: int) -> bool:
        try:
            return await self.client.expire(key, ttl)
        except redis.RedisError:
            return False


class MemoryCacheClient(CacheClient):
    def __init__(self):
        self.store = {}
        self.expiries = {}
        logger.info("Using In-Memory Cache")

    def _is_expired(self, key: str) -> bool:
        if key in self.expiries:
            if time.time() > self.expiries[key]:
                del self.store[key]
                del self.expiries[key]
                return True
        return False

    async def get(self, key: str) -> Optional[Any]:
        if self._is_expired(key):
            return None
        return self.store.get(key)

    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        self.store[key] = str(value)
        if ttl:
            self.expiries[key] = time.time() + ttl
        elif key in self.expiries:
            del self.expiries[key]
        return True

    async def incr(self, key: str, amount: int = 1) -> int:
        if self._is_expired(key):
             self.store[key] = "0"
        
        current_val = int(self.store.get(key, "0"))
        new_val = current_val + amount
        self.store[key] = str(new_val)
        return new_val
        
    async def ttl(self, key: str) -> int:
        if self._is_expired(key) or key not in self.store:
            return -2
        if key not in self.expiries:
            return -1
        return int(self.expiries[key] - time.time())

    async def expire(self, key: str, ttl: int) -> bool:
        if key not in self.store or self._is_expired(key):
            return False
        self.expiries[key] = time.time() + ttl
        return True
