import asyncio
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from app.api.models import RewardRequest, RewardResponse
from app.services.decision_engine import DecisionEngine
from app.services.idempotency import IdempotencyService
from app.api.dependencies import get_decision_engine, get_idempotency_service

router = APIRouter()


@router.post("/reward/decide", response_model=RewardResponse)
async def decide_reward(
    request: RewardRequest,
    background_tasks: BackgroundTasks,
    decision_engine: DecisionEngine = Depends(get_decision_engine),
    idempotency_service: IdempotencyService = Depends(get_idempotency_service),
) -> RewardResponse:
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
        raise HTTPException(status_code=500, detail=str(e))

    # Prepare background updates
    background_tasks.add_task(
        _run_background_updates,
        decision_engine=decision_engine,
        idempotency_service=idempotency_service,
        request=request,
        response=response,
    )

    return response


async def _run_background_updates(
    decision_engine: DecisionEngine,
    idempotency_service: IdempotencyService,
    request: RewardRequest,
    response: RewardResponse,
) -> None:
    """
    Run all post-response Redis writes concurrently.
    Runs *after* the HTTP response has already been sent to the client.
    """
    update_tasks = [
        # # Store Result (Idempotency)
        idempotency_service.store_response(
            request.txn_id, request.user_id, request.merchant_id, response
        ),
        # # Update last reward timestamp
        decision_engine.update_last_reward_ts(request.user_id, request.ts),
    ]

    # Update CAC tracking
    if response.reward_value > 0:
        update_tasks.append(
            decision_engine.update_cac(request.user_id, request.ts, response.reward_value)
        )

    await asyncio.gather(*update_tasks, return_exceptions=True)
