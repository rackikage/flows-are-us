"""composio_pipe.py — the single Composio clean pipe for the social hub.

Every platform call (Instagram, Facebook) goes through Composio's MCP
endpoint via COMPOSIO_MULTI_EXECUTE_TOOL. No hand-rolled Graph API calls.

Credentials are read from the local Claude MCP config (~/.claude.json,
mcpServers.composio) or the COMPOSIO_MCP_URL / COMPOSIO_MCP_KEY env vars.
"""

import json
import os
import threading

import requests


class PipeError(RuntimeError):
    pass


def _load_mcp_config():
    url = os.environ.get("COMPOSIO_MCP_URL")
    key = os.environ.get("COMPOSIO_MCP_KEY")
    if url and key:
        return url, key
    cfg_path = os.path.expanduser("~/.claude.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        server = (cfg.get("mcpServers") or {}).get("composio") or {}
        url = server.get("url")
        key = (server.get("headers") or {}).get("x-consumer-api-key")
        if url and key:
            return url, key
    except (OSError, json.JSONDecodeError):
        pass
    raise PipeError(
        "Composio MCP config not found. Set COMPOSIO_MCP_URL and "
        "COMPOSIO_MCP_KEY, or configure the 'composio' MCP server in ~/.claude.json"
    )


class ComposioPipe:
    """Minimal MCP (streamable HTTP) client for Composio tool execution."""

    def __init__(self):
        self.url, self.key = _load_mcp_config()
        self._lock = threading.Lock()
        self._session = None
        self._sid = None

    # ── MCP plumbing ──────────────────────────────────────────────────────
    def _parse(self, resp):
        ctype = resp.headers.get("content-type", "")
        text = resp.content.decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            msg = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return msg
        return json.loads(text) if text.strip() else None

    def _connect(self):
        s = requests.Session()
        s.headers.update({
            "x-consumer-api-key": self.key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        r = s.post(self.url, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "flows-are-us-social-hub", "version": "1.0"},
            },
        }, timeout=60)
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        if sid:
            s.headers["mcp-session-id"] = sid
        s.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               timeout=30)
        self._session, self._sid = s, sid

    def _tools_call(self, arguments, timeout):
        with self._lock:
            if self._session is None:
                self._connect()
            session = self._session
        r = session.post(self.url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": arguments},
        }, timeout=timeout)
        if r.status_code in (400, 404):
            # session likely expired — reconnect once
            with self._lock:
                self._connect()
                session = self._session
            r = session.post(self.url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": arguments},
            }, timeout=timeout)
        r.raise_for_status()
        msg = self._parse(r)
        if not msg or "result" not in msg:
            raise PipeError(f"Bad MCP response: {json.dumps(msg)[:300]}")
        try:
            payload = json.loads(msg["result"]["content"][0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise PipeError(f"Unparseable tool payload: {e}")
        return payload

    # ── Public API ────────────────────────────────────────────────────────
    def execute_batch(self, tools, thought="social hub batch", timeout=420):
        """Run up to 50 independent Composio tools in one round trip.

        tools: [{"tool_slug": ..., "arguments": {...}, "account": ...}, ...]
        Returns a list (same order) of {"successful": bool, "data": ..., "error": ...}.
        """
        payload = self._tools_call({
            "tools": tools,
            "sync_response_to_workbench": False,
            "thought": thought,
        }, timeout)
        results = (payload.get("data") or {}).get("results") or []
        out = []
        for item in results:
            resp = item.get("response") or item
            out.append({
                "successful": bool(resp.get("successful")),
                "data": resp.get("data"),
                "error": resp.get("error"),
            })
        # pad if the server returned fewer results than requested
        while len(out) < len(tools):
            out.append({"successful": False, "data": None,
                        "error": "no result returned"})
        return out

    def execute(self, tool_slug, arguments, account=None, timeout=420):
        """Run a single Composio tool; returns its data dict or raises PipeError."""
        tool = {"tool_slug": tool_slug, "arguments": arguments}
        if account:
            tool["account"] = account
        res = self.execute_batch([tool], thought=f"social hub: {tool_slug}",
                                 timeout=timeout)[0]
        if not res["successful"]:
            raise PipeError(str(res["error"] or f"{tool_slug} failed"))
        return res["data"] or {}
