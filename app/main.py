from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.routes import router as api_router
from app.core.config import get_settings
from app.db.cache import RedisCacheClient, MemoryCacheClient
from app.services.persona import PersonaService
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: create singletons once at startup,
    clean up on shutdown.
    """
    # Build cache (singleton)
    if settings.REDIS_HOST:
        cache = RedisCacheClient(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            max_connections=200,
        )
    else:
        cache = MemoryCacheClient()

    persona_service = PersonaService()
    decision_engine = await DecisionEngine.create(cache, persona_service)
    idempotency_service = IdempotencyService(cache)

    # Attach singletons to app.state so routes can access them
    app.state.cache = cache
    app.state.persona_service = persona_service
    app.state.decision_engine = decision_engine
    app.state.idempotency_service = idempotency_service

    yield  # Application is running

    # Shutdown cleanup
    await cache.close()


app = FastAPI(title="Reward Decision App", lifespan=lifespan)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        loop="asyncio",  # switch to "uvloop" on Linux for extra perf
        reload=True,
        access_log=True,
    )


# Recommended startup for high-throughput testing:
#
#   Windows (asyncio loop):
#     uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log
#
#   Linux / WSL2 (uvloop available):
#     pip install uvloop
#     uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --loop uvloop --no-access-log
