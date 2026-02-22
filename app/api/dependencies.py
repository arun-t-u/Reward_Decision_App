from fastapi import Request, Depends
from app.db.cache import CacheClient
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService


def get_cache(request: Request) -> CacheClient:
    return request.app.state.cache


def get_decision_engine(request: Request) -> DecisionEngine:
    return request.app.state.decision_engine


def get_idempotency_service(request: Request) -> IdempotencyService:
    return request.app.state.idempotency_service
