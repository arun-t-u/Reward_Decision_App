from pydantic import BaseModel, UUID4, Field, field_validator
from typing import List, Optional, Any, Dict
from datetime import datetime
from enum import Enum


class TransactionType(str, Enum):
    """
    Valid transaction types.
    """
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    PAYMENT = "PAYMENT"
    TRANSFER = "TRANSFER"
    UPI = "UPI"


class RewardType(str, Enum):
    """Valid reward types."""
    XP = "XP"
    CHECKOUT = "CHECKOUT"
    GOLD = "GOLD"


class Persona(str, Enum):
    """User persona types."""
    NEW = "NEW"
    RETURNING = "RETURNING"
    POWER = "POWER"


class RewardRequest(BaseModel):
    """
    Request model for reward decision endpoint.
    """
    txn_id: str = Field(..., description="Unique transaction identifier")
    user_id: str = Field(..., description="User identifier")
    merchant_id: str = Field(..., description="Merchant identifier")
    amount: float = Field(..., gt=0, description="Transaction amount in rupees")
    txn_type: TransactionType = Field(..., description="Type of transaction")
    ts: datetime = Field(..., description="Transaction timestamp")

    @field_validator('txn_id', 'user_id', 'merchant_id')
    @classmethod
    def validate_ids(cls, v: str) -> str:
        """Validate that IDs are non-empty strings."""
        if not v or not v.strip():
            raise ValueError("ID cannot be empty")
        return v.strip()
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "txn_id": "txn_123456",
                    "user_id": "user_789",
                    "merchant_id": "merchant_abc",
                    "amount": 1500.50,
                    "txn_type": "PURCHASE",
                    "ts": "2024-02-16T10:30:00Z"
                }
            ]
        }
    }


class RewardResponse(BaseModel):
    """
    Response model for reward decision endpoint.
    """
    decision_id: UUID4 = Field(..., description="Unique decision identifier (deterministic)")
    policy_version: str = Field("v1", description="Policy version used for decision")
    reward_type: RewardType = Field(..., description="Type of reward granted")
    reward_value: int = Field(..., ge=0, description="Monetary value of reward (0 for XP-only)")
    xp: int = Field(..., ge=0, description="Experience points awarded")
    reason_codes: List[str] = Field(..., description="List of reason codes explaining the decision")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "decision_id": "550e8400-e29b-41d4-a716-446655440000",
                    "policy_version": "v1",
                    "reward_type": "XP",
                    "reward_value": 0,
                    "xp": 3000,
                    "reason_codes": ["XP_EARNED", "PERSONA_BONUS"],
                    "meta": {
                        "persona": "POWER",
                        "base_xp": 1500,
                        "multiplier": 2.0,
                        "cac_remaining": 5000
                    }
                }
            ]
        }
    }
