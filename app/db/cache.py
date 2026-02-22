import abc
import time
import json
from typing import Optional, Any
import redis.asyncio as redis
from redis.asyncio import ConnectionPool
import logging

logger = logging.getLogger(__name__)


class CacheClient(abc.ABC):
    """
    Abstract base class for cache client.
    """
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

    @abc.abstractmethod
    async def pipeline_incr_expire(self, key: str, amount: int, expire_seconds: int) -> int:
        """
        Atomically increment key and set expiry in one pipeline round-trip.
        """
        pass

    async def close(self) -> None:
        """
        Optional cleanup hook.
        """
        pass


class RedisCacheClient(CacheClient):
    """
    Redis cache client.
    """
    def __init__(self, host: str, port: int, db: int, max_connections: int = 200):
        self._pool = ConnectionPool(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
            max_connections=max_connections,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        self.client = redis.Redis(connection_pool=self._pool)
        logger.info(f"Initialized Redis Client at {host}:{port} (pool={max_connections})")

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

    async def pipeline_incr_expire(self, key: str, amount: int, expire_seconds: int) -> int:
        """
        INCR + EXPIRE in a single pipeline round-trip.
        Uses NX on EXPIRE so we only set expiry when first created.
        Returns the new counter value.
        """
        try:
            async with self.client.pipeline(transaction=False) as pipe:
                pipe.incrby(key, amount)
                pipe.expire(key, expire_seconds, nx=True)  # NX = only set if no expiry yet
                results = await pipe.execute()
            return results[0]
        except redis.RedisError:
            return 0

    async def close(self) -> None:
        await self._pool.disconnect()
        logger.info("Redis connection pool disconnected")


class MemoryCacheClient(CacheClient):
    """
    In-memory cache client.
    """
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

    async def pipeline_incr_expire(self, key: str, amount: int, expire_seconds: int) -> int:
        """
        In-memory equivalent: incr then set expiry if not already set.
        """
        if self._is_expired(key):
            self.store[key] = "0"
        current_val = int(self.store.get(key, "0"))
        new_val = current_val + amount
        self.store[key] = str(new_val)
        # Only set expiry if not already scheduled (NX semantics)
        if key not in self.expiries:
            self.expiries[key] = time.time() + expire_seconds
        return new_val
