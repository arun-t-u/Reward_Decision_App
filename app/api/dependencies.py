from fastapi import Request, Depends
from app.db.cache import CacheClient
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService


def get_cache(request: Request) -> CacheClient:
    """
    Get cache client from request state.
    """
    return request.app.state.cache


def get_decision_engine(request: Request) -> DecisionEngine:
    """
    Get decision engine from request state.
    """
    return request.app.state.decision_engine


def get_idempotency_service(request: Request) -> IdempotencyService:
    """
    Get idempotency service from request state.
    """
    return request.app.state.idempotency_service
