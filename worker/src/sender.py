"""Settlement sender interface for SwarmGit.

DryRunSender is the default: it records payout intent and moves
nothing. Real sats move only when ALL of these hold:

  * SWARMSGIT_LIVE_SETTLEMENT is truthy (wrangler var)
  * secret AGENTPAY_KEY or AGENTPAY_AGENT_KEY (agp_…) is set
  * every spend cap env var is configured (see spendctl.CAP_VARS)
  * AgentPaySender.send returns a txid (on-chain) or receipt (internal)
  * the operator pause is off (checked at settle, not here)

The live primitive is agentpay's actual endpoint, inspected 2026-10-20
and updated for auxon/agentpay#7 (apw_ dests):

  POST https://entangleit.com/api/agentpay/agent/send
  Authorization: Bearer agp_…
  body: {dest, sats, memo, idempotency_key}
  dest: BSV P2PKH (1…/3…) or public wallet id (apw_… from GET /agent/me).
        Never the 64-hex secret wallet token.
  200 P2PKH:    {ok, settlement: "live", txid: <bsv txid>, dest, sats}
  200 apw_…:    {ok, settlement: "internal", txid: "snd_…",
                 receiptId: "snd_…", dest, sats}
  retry with the same (wallet, idempotency_key) returns the original
  txid / receipt

WalletDSender remains an alternative when SWARMSGIT_WALLETD_URL is set
and live agentpay is not. The outbound walletd puller
(tools/walletd_release.py) is the other alternative: a pending
release_requested line, not an in-worker send.

HARD: never mark a payout paid without a txid or receipt. A failing
or unset paying sender leaves the release pending. Honor agentpay's
settlement field; never hardcode "live".
"""
import json

import gitlib


class SendError(Exception):
    pass


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


class Sender:
    """Settlement primitive interface."""

    settlement_mode = "dry_run"

    async def send(self, dest, sats, memo, idempotency_key=""):
        """Move sats to dest. Returns a receipt dict with at least:
        {"ok": bool, "settlement": "live"|"internal"|"dry_run",
         "txid": str|None, "receipt_id": str|None}."""
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
                  "settlement": "dry_run", "txid": None,
                  "receipt_id": None}
        self.intents.append(intent)
        return {"ok": True, "settlement": "dry_run", "txid": None,
                "receipt_id": None, "note": "dry-run: no funds moved"}


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
        if not gitlib.is_bsv_txid(txid):
            raise SendError("walletd send returned no txid")
        self.intents.append({"dest": dest, "sats": sats, "txid": txid,
                             "idempotency_key": idempotency_key or "",
                             "settlement": "live"})
        return {"ok": True, "settlement": "live", "txid": txid,
                "fee": out.get("fee"), "note": "signed by bsv-walletd"}


class AgentPaySender(Sender):
    """Settlement via agentpay POST /agent/send.

    Auth is a vaulted agp_ agent key (AGENTPAY_KEY), never in code.
    dest is a BSV P2PKH address (settlement=live, txid is on-chain) or
    a public agentpay wallet id (settlement=internal, receipt is snd_…).
    Secret 64-hex wallet tokens are refused here so they never leave
    the worker as a pay dest.
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
        if gitlib.is_agentpay_wallet_token(dest):
            raise SendError(
                "refused: dest looks like a wallet token; pass the "
                "public wallet id (apw_…), never the 64-hex secret token")
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
        receipt = _receipt_from_parsed(_parse_agentpay(res), dest, sats)
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
    txid = data.get("txid")
    receipt_id = data.get("receiptId") or data.get("receipt_id")
    return {
        "txid": str(txid) if txid else None,
        "receipt_id": str(receipt_id) if receipt_id else None,
        "dest": data.get("dest"),
        "sats": data.get("sats"),
        "settlement": data.get("settlement"),
    }


def _receipt_from_parsed(parsed, dest, sats):
    """Honor agentpay's settlement field. Never default-allow / hardcode live."""
    settlement = (parsed.get("settlement") or "").strip()
    txid = parsed.get("txid")
    receipt_id = parsed.get("receipt_id")
    dest = parsed.get("dest") or dest
    sats = parsed.get("sats") or sats
    if not settlement:
        rid = receipt_id or txid
        if rid and gitlib.is_agentpay_receipt_id(rid):
            settlement = "internal"
        elif gitlib.is_bsv_txid(txid) and gitlib.is_bsv_address(dest):
            settlement = "live"
        else:
            raise SendError("agentpay send returned no settlement")
    if settlement == "internal":
        rid = receipt_id or txid
        if not rid:
            raise SendError("agentpay send returned no receipt")
        return {"ok": True, "settlement": "internal",
                "txid": str(rid), "receipt_id": str(rid),
                "dest": dest, "sats": sats}
    if settlement == "live":
        if not txid:
            raise SendError("agentpay send returned no txid")
        if not gitlib.is_bsv_txid(txid):
            raise SendError("agentpay live send returned a non-BSV txid")
        return {"ok": True, "settlement": "live",
                "txid": str(txid).lower(), "dest": dest, "sats": sats}
    raise SendError("agentpay send returned unknown settlement %r"
                    % settlement)


def choose_sender(env, http):
    """Pick the wired Sender. Live agentpay only when fully armed.

    Flag + key is not enough: every spend cap must be configured too
    (spendctl.live_armed). Flipping SWARMSGIT_LIVE_SETTLEMENT=1 alone
    stays DryRun even if AGENTPAY_AGENT_KEY is already in prod.
    """
    import spendctl
    if spendctl.live_armed(env):
        key = spendctl.agentpay_key(env)
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
