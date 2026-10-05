"""Production outbound HTTP for the SwarmGit Worker.

Forked from the TestSwarm scaffold. Drives the runtime's fetch via
Pyodide FFI. Zero dependencies.

The `body` kwarg carries request bodies (the merge gate's MCP JSON-RPC
probing POSTs through this seam — merge_gate passes `body=` and
degrades to diff-only mode if the seam can't carry it).

Returns:
  {"ok": True, "status": int, "body": str (<=4000 chars), "latency_ms": int}
  {"ok": False, "error": str, "latency_ms": int}
"""
import asyncio
import time

from js import fetch, Object
from pyodide.ffi import to_js


async def http_request(method, url, headers, timeout=10, body=None):
    t0 = time.time()

    def ms():
        return int((time.time() - t0) * 1000)

    try:
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8")
        init = {"method": method,
                "headers": dict(headers),
                "redirect": "follow"}
        if body is not None and method.upper() not in ("GET", "HEAD"):
            init["body"] = body
        init_js = to_js(init, dict_converter=Object.fromEntries)
        # wait_for gives us timeout semantics: fetch itself has no
        # timeout knob we can rely on from Python.
        resp = await asyncio.wait_for(fetch(url, init_js), timeout)
        status = int(resp.status)
        text = await asyncio.wait_for(resp.text(), timeout)
        return {"ok": True, "status": status,
                "body": str(text)[:4000], "latency_ms": ms()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "latency_ms": ms()}
