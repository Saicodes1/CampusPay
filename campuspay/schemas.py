from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Money = Annotated[StrictInt, Field(ge=1, le=1_000_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Amount(StrictModel):
    amount: Money


class Transfer(Amount):
    recipient: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")]


class PaymentEvent(StrictModel):
    eventId: UUID
    type: Literal["FUNDED", "TRANSFERRED", "CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED", "SETTLED"]
    aggregateId: UUID
    merchantId: Literal["cafe", "books"] | None
    amount: Money
    currency: Literal["USD"]


class OperationResult(BaseModel):
    id: UUID
    status: str
    amount: int
    currency: Literal["USD"]
    qrPayload: str | None = None
    qrImage: str | None = None
