"""bsv-walletd client. The phrase never leaves the daemon.

JSON-RPC over SWARMSGIT_WALLETD_URL (the daemon HTTPS endpoint, or a
tunnel to it). Methods used: getBalance (escrow address) and send
(sign + broadcast the release).
"""
import json


class WalletDError(Exception):
    pass


def _parse(res):
    if not isinstance(res, dict) or res.get("ok") is False:
        raise WalletDError("walletd unreachable: %s" % (res or ""))
    status = int(res.get("status") or 0)
    if status and status >= 400:
        raise WalletDError("walletd HTTP %s" % status)
    try:
        body = json.loads(res.get("body") or "{}")
    except Exception as e:
        raise WalletDError("walletd returned non-json") from e
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise WalletDError("walletd refused: %s" % msg)
    return body.get("result") if "result" in body else body


async def call(http, url, method, params=None):
    res = await http(
        "POST", url, {"Content-Type": "application/json"},
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": params or {}}))
    return _parse(res)


async def escrow_address(http, url):
    """The daemon spend address (selfAddress, path m/0/0)."""
    bal = await call(http, url, "getBalance")
    addr = (bal or {}).get("address") or ""
    if not addr:
        raise WalletDError("walletd returned no spend address — is it unlocked?")
    return addr


async def send(http, url, dest, sats, label):
    """Daemon signs with the seed and broadcasts. Returns {txid, fee}."""
    out = await call(http, url, "send",
                     {"to": dest, "sats": int(sats), "label": label[:80]})
    txid = (out or {}).get("txid") or ""
    if len(txid) != 64:
        raise WalletDError("walletd send did not return a txid")
    return {"txid": txid, "fee": (out or {}).get("fee")}
