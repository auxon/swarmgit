"""Settlement sender interface for SwarmGit.

Richard said YES to real sats in the demo. Settlement goes through
AgentPay's POST /agent/send primitive (live BSV via the treasury).

- DryRunSender: records payout intent, moves nothing (test/CI use).
- AgentPaySender: the live implementation — POSTs to AgentPay, returns
  the real txid. The agent key comes from the environment (Cloudflare
  secret AGENTPAY_AGENT_KEY), never from code.
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
    """Records payout intent; moves nothing. Used in tests/CI."""

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


class AgentPaySender(Sender):
    """Live settlement via AgentPay POST /agent/send.

    POST https://entangleit.com/api/agentpay/agent/send
    body: {"dest": dest, "sats": sats, "memo": memo,
           "idempotency_key": idempotency_key}
    auth: Bearer <agp_ agent key> (from env, never in code)
    returns: {"ok": True, "settlement": "live", "txid": <txid>}
    """

    SEND_URL = "https://entangleit.com/api/agentpay/agent/send"

    def __init__(self, agent_key=None, send_url=None, http_post=None):
        import os
        self.agent_key = agent_key or os.environ.get("AGENTPAY_AGENT_KEY", "")
        self.send_url = send_url or self.SEND_URL
        self._http_post = http_post  # injectable for tests

    async def send(self, dest, sats, memo, idempotency_key=""):
        if not isinstance(sats, int) or isinstance(sats, bool) or sats <= 0:
            raise SendError("refused: sats must be a positive int")
        if not self.agent_key:
            raise SendError("refused: AGENTPAY_AGENT_KEY not configured")
        body = {"dest": dest, "sats": sats, "memo": memo or "",
                "idempotency_key": idempotency_key or ""}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.agent_key}"}
        if self._http_post is not None:
            res = await self._http_post(self.send_url, body, headers)
        else:
            import json
            import urllib.request
            req = urllib.request.Request(
                self.send_url, data=json.dumps(body).encode(),
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read().decode())
            except Exception as e:
                raise SendError(f"agentpay send failed: {e}")
        if not isinstance(res, dict) or not res.get("ok"):
            raise SendError(f"agentpay send refused: {res}")
        txid = res.get("txid")
        if not txid:
            raise SendError(f"agentpay send returned no txid: {res}")
        return {"ok": True, "settlement": res.get("settlement", "live"),
                "txid": txid}
