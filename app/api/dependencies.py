from fastapi import Depends
from app.core.config import get_settings
from app.db.cache import RedisCacheClient, MemoryCacheClient, CacheClient

settings = get_settings()

# Global cache instance (initialized on startup in main.py ideally, but lazy here for simplicity)
_cache_client: CacheClient = None

def get_cache() -> CacheClient:
    global _cache_client
    if _cache_client is None:
        if settings.REDIS_HOST:
             _cache_client = RedisCacheClient(
                 host=settings.REDIS_HOST,
                 port=settings.REDIS_PORT,
                 db=settings.REDIS_DB
             )
        else:
             _cache_client = MemoryCacheClient()
    print(_cache_client)
    return _cache_client
