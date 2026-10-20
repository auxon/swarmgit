"""Settlement sender interface for SwarmGit.

DryRunSender is the default: it records payout intent and moves
nothing. Live sats move only when ALL of these hold:

  * SWARMSGIT_LIVE_SETTLEMENT is truthy (wrangler var)
  * secret AGENTPAY_KEY (agp_…) is set
  * AgentPaySender.send returns a txid

The live primitive is agentpay's actual endpoint, inspected 2026-10-20:

  POST https://entangleit.com/api/agentpay/agent/send
  Authorization: Bearer agp_…
  body: {dest, sats, memo, idempotency_key}
  200:  {ok: true, settlement: "live", txid, dest, sats}
  retry with the same (wallet, idempotency_key) returns the original txid

WalletDSender remains an alternative when SWARMSGIT_WALLETD_URL is set
and live agentpay is not. The outbound walletd puller
(tools/walletd_release.py) is the other alternative: a pending
release_requested line, not an in-worker send.

HARD: never mark a payout live without a txid. A failing or unset live
sender leaves the release pending.
"""
import json


class SendError(Exception):
    pass


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


class Sender:
    """Settlement primitive interface."""

    settlement_mode = "dry_run"

    async def send(self, dest, sats, memo, idempotency_key=""):
        """Move sats to dest. Returns a receipt dict with at least:
        {"ok": bool, "settlement": "live"|"dry_run", "txid": str|None}."""
        raise NotImplementedError


class DryRunSender(Sender):
    """Records payout intent; moves nothing. The default Sender."""

    settlement_mode = "dry_run"

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


class WalletDSender(Sender):
    """Ask bsv-walletd to sign and broadcast the release.

    The seed phrase stays in the daemon. This sender only sends
    JSON-RPC `send` to SWARMSGIT_WALLETD_URL. No WIF crosses the worker.
    """

    settlement_mode = "live"

    def __init__(self, http, url):
        self.http = http
        self.url = url.rstrip("/")
        self.intents = []

    async def send(self, dest, sats, memo, idempotency_key=""):
        import walletd
        if not isinstance(sats, int) or isinstance(sats, bool) or sats <= 0:
            raise SendError("refused: sats must be a positive int")
        out = await walletd.send(self.http, self.url, dest, sats,
                                 memo or "swarmgit release")
        txid = out.get("txid")
        if not txid:
            raise SendError("walletd send returned no txid")
        self.intents.append({"dest": dest, "sats": sats, "txid": txid,
                             "idempotency_key": idempotency_key or "",
                             "settlement": "live"})
        return {"ok": True, "settlement": "live", "txid": txid,
                "fee": out.get("fee"), "note": "signed by bsv-walletd"}


class AgentPaySender(Sender):
    """Live settlement via agentpay POST /agent/send.

    Auth is a vaulted agp_ agent key (AGENTPAY_KEY), never in code.
    dest is a BSV P2PKH address (agentpay currently rejects apw_ wallet
    ids — SwarmGit still accepts them as a payout destination so we can
    pass them through the day agentpay grows that primitive).
    """

    settlement_mode = "live"
    DEFAULT_URL = "https://entangleit.com/api/agentpay/agent/send"

    def __init__(self, http, agent_key, send_url=None):
        self.http = http
        self.agent_key = (agent_key or "").strip()
        self.send_url = _agentpay_send_url(send_url)
        self.intents = []

    async def send(self, dest, sats, memo, idempotency_key=""):
        if not isinstance(sats, int) or isinstance(sats, bool) or sats <= 0:
            raise SendError("refused: sats must be a positive int")
        dest = (dest or "").strip()
        if not dest:
            raise SendError("refused: dest is required")
        if not self.agent_key:
            raise SendError("refused: AGENTPAY_KEY is not configured")
        body = {"dest": dest, "sats": sats, "memo": (memo or "")[:200],
                "idempotency_key": (idempotency_key or "")[:120]}
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.agent_key}
        try:
            res = await self.http(
                "POST", self.send_url, headers, 30,
                body=json.dumps(body))
        except Exception as e:
            raise SendError(f"agentpay send failed: {type(e).__name__}")
        parsed = _parse_agentpay(res)
        txid = parsed.get("txid")
        if not txid:
            raise SendError("agentpay send returned no txid")
        receipt = {"ok": True, "settlement": "live", "txid": txid,
                   "dest": parsed.get("dest") or dest,
                   "sats": parsed.get("sats") or sats}
        self.intents.append({**receipt,
                             "idempotency_key": idempotency_key or ""})
        return receipt


def _agentpay_send_url(send_url):
    url = (send_url or AgentPaySender.DEFAULT_URL).strip().rstrip("/")
    if not url:
        return AgentPaySender.DEFAULT_URL
    if url.endswith("/agent/send"):
        return url
    if url.endswith("/api/agentpay"):
        return url + "/agent/send"
    if url.endswith("/agentpay"):
        return url + "/agent/send"
    return url


def _parse_agentpay(res):
    if not isinstance(res, dict):
        raise SendError("agentpay send returned a non-object")
    if res.get("ok") is False and "status" not in res:
        raise SendError("agentpay unreachable: %s" % (res.get("error") or res))
    status = int(res.get("status") or 0)
    raw = res.get("body")
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    if status and status >= 400:
        err = data.get("error") or data.get("message") or raw or status
        raise SendError("agentpay HTTP %s: %s" % (status, err))
    if data.get("ok") is False:
        raise SendError("agentpay send refused: %s"
                        % (data.get("error") or data))
    txid = data.get("txid") or res.get("txid")
    if not txid:
        raise SendError("agentpay send returned no txid")
    return {"txid": str(txid), "dest": data.get("dest"),
            "sats": data.get("sats"),
            "settlement": data.get("settlement") or "live"}


def choose_sender(env, http):
    """Pick the wired Sender. Live agentpay only with flag AND key."""
    try:
        live = _truthy(getattr(env, "SWARMSGIT_LIVE_SETTLEMENT", ""))
    except Exception:
        live = False
    key = ""
    try:
        key = (getattr(env, "AGENTPAY_KEY", None)
               or getattr(env, "AGENTPAY_AGENT_KEY", None)
               or "")
        key = str(key).strip()
    except Exception:
        key = ""
    if live and key:
        try:
            base = (getattr(env, "AGENTPAY_URL", "") or "").strip()
        except Exception:
            base = ""
        return AgentPaySender(http, key, base or None)
    try:
        wurl = (getattr(env, "SWARMSGIT_WALLETD_URL", "") or "").strip()
    except Exception:
        wurl = ""
    if wurl:
        return WalletDSender(http, wurl)
    return DryRunSender()
