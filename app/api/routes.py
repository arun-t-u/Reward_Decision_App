import asyncio
from fastapi import APIRouter, Depends, HTTPException
from app.api.models import RewardRequest, RewardResponse
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService
from app.services.persona import PersonaService
from app.api.dependencies import get_cache
from app.db.cache import CacheClient

router = APIRouter()


@router.post("/reward/decide", response_model=RewardResponse)
async def decide_reward(
    request: RewardRequest,
    cache: CacheClient = Depends(get_cache)
) -> RewardResponse:
    persona_service = PersonaService()
    idempotency_service = IdempotencyService(cache)
    decision_engine = DecisionEngine(cache, persona_service)

    # Check Idempotency 
    cached_response = await idempotency_service.get_stored_response(
        request.txn_id, request.user_id, request.merchant_id
    )

    if cached_response:
        return cached_response
    
    # Calculate Reward
    try:
        response = await decision_engine.calculate_reward(request)
    except Exception as e:
        # Log error and raise 500
        raise HTTPException(status_code=500, detail=str(e))
    
    # Prepare background updates
    update_tasks = []

    # Update CAC tracking
    if response.reward_value > 0:
        update_tasks.append(decision_engine._update_cac(request.user_id, request.ts, response.reward_value))
        # await self._update_cac(request.user_id, request.ts, reward_value)

    # Update last reward timestamp
    update_tasks.append(decision_engine._update_last_reward_ts(request.user_id, request.ts))

    # Store Result (Idempotency)
    await idempotency_service.store_response(
        request.txn_id, request.user_id, request.merchant_id, response
    )

    # Execute updates concurrently
    if update_tasks:
        await asyncio.gather(*update_tasks)
    
    return response
