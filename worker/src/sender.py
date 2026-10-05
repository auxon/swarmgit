"""Settlement sender interface for SwarmGit.

Richard said YES to real sats in the demo — but AgentPay has no send
endpoint yet, so every settlement event records settlement="dry_run"
(same discipline as TestSwarm's settle_job and PreFlight billing).

The interface below is REAL-READY, not a stub: DryRunSender implements
the full contract (dest, sats, memo, idempotency) and returns a
dry-run receipt. Flipping to live means implementing Sender.send with
Richard's POST /agent/send primitive — the exact contract is documented
in README "Flipping settlement live". No other code changes.

HARD CONSTRAINT: no code path in this worker may move funds until a
live Sender exists. DryRunSender.send moves nothing by construction.
"""


class SendError(Exception):
    pass


class Sender:
    """Settlement primitive interface."""

    async def send(self, dest, sats, memo, idempotency_key=""):
        """Move sats to dest. Returns a receipt dict with at least:
        {"ok": bool, "settlement": "live"|"dry_run", "txid": str|None}."""
        raise NotImplementedError


class DryRunSender(Sender):
    """Records payout intent; moves nothing. The only Sender until
    Richard's send primitive exists."""

    def __init__(self):
        self.intents = []  # every send() call, in order (auditable)

    async def send(self, dest, sats, memo, idempotency_key=""):
        if not isinstance(sats, int) or isinstance(sats, bool) or sats < 0:
            raise SendError("refused: sats must be a non-negative int")
        intent = {"dest": dest, "sats": sats, "memo": memo,
                  "idempotency_key": idempotency_key or "",
                  "settlement": "dry_run", "txid": None}
        self.intents.append(intent)
        return {"ok": True, "settlement": "dry_run", "txid": None,
                "note": "dry-run: no funds moved"}
