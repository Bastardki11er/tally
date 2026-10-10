"""Request and response models. These also drive the OpenAPI docs at /docs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")

# Money is always an integer number of minor units (cents for USD), never a float.
Amount = Annotated[int, Field(gt=0, le=10**15, description="Minor units, e.g. 1050 = $10.50")]
AccountId = Annotated[int, Field(gt=0)]
Description = Annotated[str, Field(max_length=500)]
Direction = Literal["debit", "credit"]


class AccountIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    type: Literal["asset", "liability", "equity", "revenue", "expense"]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["USD"])]
    allow_negative: bool = Field(False, description="Whether the balance may go below zero")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountOut(BaseModel):
    id: int
    name: str
    type: str
    normal_side: Direction
    currency: str
    allow_negative: bool
    balance: int
    held: int
    available: int
    metadata: dict[str, Any]
    created_at: datetime


class EntryIn(BaseModel):
    account_id: AccountId
    direction: Direction
    amount: Amount


class EntryOut(BaseModel):
    id: int
    account_id: int
    direction: Direction
    amount: int
    currency: str
    balance_after: int
    posted_at: datetime


class TransactionIn(BaseModel):
    entries: Annotated[list[EntryIn], Field(min_length=2, max_length=100)]
    description: Description = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransferIn(BaseModel):
    debit_account_id: AccountId
    credit_account_id: AccountId
    amount: Amount
    description: Description = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TransactionOut(BaseModel):
    id: UUID
    description: str
    metadata: dict[str, Any]
    reverses_id: UUID | None
    posted_at: datetime
    entries: list[EntryOut]
    reversed_by: UUID | None = None


class ReversalIn(BaseModel):
    description: Description = ""


class HoldIn(BaseModel):
    account_id: AccountId
    amount: Amount
    expires_in_seconds: Annotated[int, Field(ge=1, le=30 * 24 * 3600)] = 7 * 24 * 3600
    description: Description = ""


class CaptureIn(BaseModel):
    destination_account_id: AccountId
    amount: Amount | None = Field(None, description="Defaults to the full hold; the rest is released")
    description: Description = ""


class HoldOut(BaseModel):
    id: UUID
    account_id: int
    amount: int
    status: Literal["active", "captured", "voided", "expired"]
    description: str
    expires_at: datetime
    captured_transaction_id: UUID | None
    created_at: datetime
    updated_at: datetime


class CaptureOut(BaseModel):
    hold: HoldOut
    transaction: TransactionOut


class StatementLine(BaseModel):
    id: int
    transaction_id: UUID
    direction: Direction
    amount: int
    currency: str
    balance_after: int
    posted_at: datetime
    description: str


class BalanceOut(BaseModel):
    account_id: int
    as_of: datetime
    balance: int


class Page(BaseModel, Generic[T]):
    data: list[T]
    next_cursor: int | None = Field(None, description="Pass back as the cursor to get the next page")
