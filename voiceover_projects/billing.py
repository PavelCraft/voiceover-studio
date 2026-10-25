"""Payment-provider boundary; Django stores entitlements, adapters handle checkout."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CheckoutRequest:
    account_id: str
    plan: str
    success_url: str
    cancel_url: str


class BillingProvider(Protocol):
    def create_checkout(self, request: CheckoutRequest) -> str: ...
    def verify_webhook(self, payload: bytes, signature: str) -> dict: ...
