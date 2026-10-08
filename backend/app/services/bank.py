from dataclasses import dataclass, field

# A stand-in for a card provider. A real client would make an HTTP call with a
# timeout (e.g. 10 seconds) and raise TimeoutError when the bank doesn't answer.
# We send OUR payment id with every request, so we can void a payment even if
# its reply never reached us and we never learned the bank's id.


@dataclass
class MockBank:
    replies: list[str]          # one per authorize call: "approve", "decline", or "timeout"
    on_authorize: object = None  # optional hook, lets tests simulate time passing mid-call
    authorized: list[int] = field(default_factory=list)
    captured: list[str] = field(default_factory=list)
    voided: list[int] = field(default_factory=list)

    def authorize(self, payment_id: int, amount_cents: int) -> str | None:
        """Hold the money. Returns the bank's id if approved, None if declined."""
        self.authorized.append(payment_id)
        if self.on_authorize:
            self.on_authorize()
        reply = self.replies.pop(0)
        if reply == "timeout":
            raise TimeoutError
        return f"bank-{payment_id}" if reply == "approve" else None

    def capture(self, bank_id: str) -> None:
        """Take the held money."""
        self.captured.append(bank_id)

    def void(self, payment_id: int) -> None:
        """Release the held money. Safe to call even if nothing was held."""
        self.voided.append(payment_id)
