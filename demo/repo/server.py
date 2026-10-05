#!/usr/bin/env python3
"""SwarmGit demo repo: a minimal MCP server with NO rate limiting.

The SwarmGit contest demo task (see ../TASK.md) is to add rate-limit
headers to every tool response. This is the starting point — the
"before" that two agents will independently fix in their forks.

Tools: add_note, list_notes, delete_note (in-memory storage).
Transport: Streamable HTTP (POST /mcp, JSON-RPC 2.0). No dependencies.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

NOTES: dict = {}
NEXT_ID = [1]

TOOLS = [
    {"name": "add_note",
     "description": "Store a note and return its id.",
     "inputSchema": {"type": "object",
                     "required": ["text"],
                     "properties": {"text": {"type": "string"}}}},
    {"name": "list_notes",
     "description": "List all stored notes.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "delete_note",
     "description": "Delete a note by id. Returns deleted: true/false.",
     "inputSchema": {"type": "object",
                     "required": ["id"],
                     "properties": {"id": {"type": "integer"}}}},
]


def call_tool(name, args):
    if name == "add_note":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            return {"error": "text is required"}
        nid = NEXT_ID[0]
        NEXT_ID[0] += 1
        NOTES[nid] = text
        return {"id": nid}
    if name == "list_notes":
        return {"notes": [{"id": k, "text": v}
                          for k, v in sorted(NOTES.items())]}
    if name == "delete_note":
        nid = args.get("id")
        if nid in NOTES:
            del NOTES[nid]
            return {"deleted": True}
        return {"deleted": False}
    return {"error": f"unknown tool: {name}"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        msg_id = body.get("id")
        method = body.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "swarmgit-demo-notes",
                                     "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = body.get("params") or {}
            result = {"content": [{"type": "text",
                                   "text": json.dumps(call_tool(
                                       params.get("name"),
                                       params.get("arguments") or {}))}]}
        else:
            result = {"error": f"unknown method: {method}"}
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id,
                              "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # NOTE: no X-RateLimit-* headers — adding them is the demo task.
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("swarmgit-demo-notes on http://127.0.0.1:8765/mcp")
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
