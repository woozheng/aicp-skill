#!/usr/bin/env python3
"""mcp_stdio_bridge.py - expose an HTTP MCP endpoint over stdio.

  python3 mcp_stdio_bridge.py http://127.0.0.1:9100/mcp

For clients that cannot speak Streamable HTTP (Claude Desktop, and any
stdio-only host). It forwards newline-delimited JSON-RPC from stdin to the
AICP /mcp endpoint, re-posts the session id and returns the responses on
stdout. Only stdlib; no file writes.
"""

import json
import sys
import urllib.error
import urllib.request


def _headers(session_id):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _parse(body):
    text = (body or "").strip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    # SSE fallback: take the last non-empty data: line
    found = None
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                found = chunk
    return json.loads(found) if found else None


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: mcp_stdio_bridge.py <mcp-http-url>\n")
        return 2
    url = argv[1]
    session_id = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            sys.stderr.write("mcp_stdio_bridge: dropping non-JSON line\n")
            continue

        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(message).encode("utf-8"),
                headers=_headers(session_id),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as resp:
                for key, value in resp.headers.items():
                    if key.lower() == "mcp-session-id":
                        session_id = value
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if not body:
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32603, "message": "HTTP %s" % exc.code},
                    }
                )
        except Exception as exc:  # network down, endpoint moved, ...
            if "id" not in message:
                continue  # notification: nothing to answer
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {
                        "code": -32603,
                        "message": "%s: %s" % (type(exc).__name__, exc),
                    },
                }
            )

        parsed = _parse(body)
        if parsed is None:
            continue
        batch = parsed if isinstance(parsed, list) else [parsed]
        for item in batch:
            sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
