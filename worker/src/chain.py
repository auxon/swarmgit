"""BSV chain watch and release broadcast for SwarmGit escrow.

Watch: a funding txid is accepted only if WhatsOnChain shows an output
to the escrow address covering the bounty.

Release: the escrow WIF (wrangler secret SWARMSGIT_ESCROW_WIF, never
committed) signs a P2PKH payment to the agent's address and the raw
tx is broadcast. No WIF means the release is refused, not silently
dry-run, once a task was funded on-chain.
"""
import hashlib
import json

WOC = "https://api.whatsonchain.com/v1/bsv/main"
SIGHASH_ALL_FORKID = 0x41
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ChainError(Exception):
    pass


def _b58encode(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + (out or "")


def _b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") or b"\x00"
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def _hash256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _hash160(b):
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


def _varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    return b"\xfe" + n.to_bytes(4, "little")


def address_to_script(addr):
    raw = _b58decode(addr)
    if len(raw) != 25 or raw[0] != 0 or _hash256(raw[:-4])[:4] != raw[-4:]:
        raise ChainError("refused: escrow or release address is not P2PKH")
    h = raw[1:-4]
    return b"\x76\xa9\x14" + h + b"\x88\xac"


def wif_to_priv(wif):
    raw = _b58decode(wif.strip())
    if _hash256(raw[:-4])[:4] != raw[-4:]:
        raise ChainError("refused: escrow WIF checksum mismatch")
    body = raw[:-4]
    if body[0] != 0x80:
        raise ChainError("refused: escrow WIF is not a mainnet key")
    compressed = len(body) == 34 and body[-1] == 1
    return body[1:33], compressed


def _body(res):
    if not isinstance(res, dict):
        raise ChainError("chain watch failed: empty response")
    if res.get("ok") is False:
        raise ChainError("chain watch failed: %s" % res.get("error"))
    status = int(res.get("status") or 0)
    if status and status != 200:
        raise ChainError("chain watch failed: HTTP %s" % status)
    raw = res.get("body") or ""
    try:
        return json.loads(raw)
    except Exception as e:
        raise ChainError("chain watch failed: not json") from e


async def confirm_funding(http, txid, escrow_address, bounty_sats):
    """Refuse unless the tx pays escrow_address at least bounty_sats."""
    if not escrow_address:
        raise ChainError("refused: SWARMSGIT_PAY_ADDRESS is not set")
    url = "%s/tx/hash/%s" % (WOC, txid)
    payload = _body(await http("GET", url, {"Accept": "application/json"}))
    paid = 0
    for vout in payload.get("vout") or []:
        spk = vout.get("scriptPubKey") or {}
        addrs = spk.get("addresses") or []
        if escrow_address not in addrs:
            continue
        try:
            paid += int(round(float(vout.get("value") or 0) * 1e8))
        except (TypeError, ValueError):
            continue
    if paid < int(bounty_sats):
        raise ChainError(
            "refused: funding tx pays %s sats to escrow, bounty is %s"
            % (paid, bounty_sats))
    conf = int(payload.get("confirmations") or 0)
    return {"ok": True, "paid_sats": paid, "confirmations": conf,
            "funding_txid": txid, "escrow_address": escrow_address}


async def unspent(http, address):
    url = "%s/address/%s/unspent" % (WOC, address)
    payload = _body(await http("GET", url, {"Accept": "application/json"}))
    if isinstance(payload, dict):
        payload = payload.get("result") or payload.get("unspent") or []
    return list(payload or [])


async def broadcast(http, txhex):
    url = "%s/tx/raw" % WOC
    res = await http("POST", url, {"Content-Type": "application/json"},
                     body=json.dumps({"txhex": txhex}))
    if not isinstance(res, dict) or res.get("ok") is False:
        raise ChainError("broadcast failed: %s" % (res or ""))
    status = int(res.get("status") or 0)
    body = (res.get("body") or "").strip().strip('"')
    if status and status != 200:
        raise ChainError("broadcast failed: HTTP %s %s" % (status, body[:180]))
    if len(body) != 64 or any(c not in "0123456789abcdef" for c in body.lower()):
        raise ChainError("broadcast failed: %s" % body[:180])
    return body.lower()


def _pubkey(priv, compressed):
    # secp256k1, pure python. One signature per release is enough.
    p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
    gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

    def inv(a):
        return pow(a, p - 2, p)

    def add(p1, p2):
        if p1 is None:
            return p2
        if p2 is None:
            return p1
        x1, y1 = p1
        x2, y2 = p2
        if x1 == x2 and (y1 + y2) % p == 0:
            return None
        if p1 == p2:
            lam = (3 * x1 * x1) * inv(2 * y1) % p
        else:
            lam = (y2 - y1) * inv(x2 - x1) % p
        x3 = (lam * lam - x1 - x2) % p
        y3 = (lam * (x1 - x3) - y1) % p
        return x3, y3

    k = int.from_bytes(priv, "big")
    point = None
    base = (gx, gy)
    while k:
        if k & 1:
            point = add(point, base)
        base = add(base, base)
        k >>= 1
    x, y = point
    if compressed:
        return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _sign(priv, digest):
    p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
    gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

    def inv(a, mod):
        return pow(a, mod - 2, mod)

    def add(p1, p2):
        if p1 is None:
            return p2
        if p2 is None:
            return p1
        x1, y1 = p1
        x2, y2 = p2
        if x1 == x2 and (y1 + y2) % p == 0:
            return None
        if p1 == p2:
            lam = (3 * x1 * x1) * inv(2 * y1, p) % p
        else:
            lam = (y2 - y1) * inv((x2 - x1) % p, p) % p
        x3 = (lam * lam - x1 - x2) % p
        return x3, (lam * (x1 - x3) - y1) % p

    z = int.from_bytes(digest, "big")
    secret = int.from_bytes(priv, "big")
    k = int.from_bytes(hashlib.sha256(priv + digest).digest(), "big") % n or 1
    point = None
    base = (gx, gy)
    kk = k
    while kk:
        if kk & 1:
            point = add(point, base)
        base = add(base, base)
        kk >>= 1
    r = point[0] % n
    s = (inv(k, n) * (z + r * secret)) % n
    if s > n // 2:
        s = n - s
    def der(x):
        xb = x.to_bytes(32, "big").lstrip(b"\x00") or b"\x00"
        if xb[0] & 0x80:
            xb = b"\x00" + xb
        return bytes([2, len(xb)]) + xb
    body = der(r) + der(s)
    return bytes([0x30, len(body)]) + body


def build_release(wif, escrow_address, utxos, dest, sats, fee=200):
    """P2PKH release. Returns (txhex, txid). Change back to escrow."""
    priv, compressed = wif_to_priv(wif)
    pub = _pubkey(priv, compressed)
    script = address_to_script(escrow_address)
    if _hash160(pub) != script[3:23]:
        raise ChainError("refused: WIF does not match SWARMSGIT_PAY_ADDRESS")
    need = int(sats) + fee
    chosen = []
    total = 0
    for u in sorted(utxos, key=lambda x: int(x.get("value") or 0)):
        chosen.append(u)
        total += int(u.get("value") or 0)
        if total >= need:
            break
    if total < need:
        raise ChainError("refused: escrow has %s sats, release needs %s" % (total, need))
    change = total - need
    outs = [(address_to_script(dest), int(sats))]
    if change > 546:
        outs.append((script, change))
    version = (1).to_bytes(4, "little")
    locktime = (0).to_bytes(4, "little")
    seq = (0xffffffff).to_bytes(4, "little")
    prevouts = b""
    sequences = b""
    ins = []
    for u in chosen:
        txid = bytes.fromhex(u["tx_hash"])[::-1]
        vout = int(u.get("tx_pos") or 0).to_bytes(4, "little")
        ins.append((txid, vout, int(u["value"])))
        prevouts += txid + vout
        sequences += seq
    raw_outs = b""
    for sc, val in outs:
        raw_outs += val.to_bytes(8, "little") + _varint(len(sc)) + sc
    hash_prev = _hash256(prevouts)
    hash_seq = _hash256(sequences)
    hash_outs = _hash256(raw_outs)
    sigs = []
    for txid, vout, amount in ins:
        pre = (version + hash_prev + hash_seq + txid + vout
               + _varint(len(script)) + script
               + amount.to_bytes(8, "little") + seq + hash_outs + locktime
               + SIGHASH_ALL_FORKID.to_bytes(4, "little"))
        sig = _sign(priv, _hash256(pre)) + bytes([SIGHASH_ALL_FORKID])
        script_sig = (_varint(len(sig)) + sig + _varint(len(pub)) + pub)
        sigs.append(script_sig)
    raw = version + _varint(len(ins))
    for (txid, vout, _), script_sig in zip(ins, sigs):
        raw += txid + vout + _varint(len(script_sig)) + script_sig + seq
    raw += _varint(len(outs)) + raw_outs + locktime
    return raw.hex(), _hash256(raw)[::-1].hex()
