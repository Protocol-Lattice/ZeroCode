"""Local stdio MCP fixture. Tests launch it as a real, persistent subprocess."""

import json
import os
from pathlib import Path
import sys
import time


def send(message):
    data = json.dumps(message, ensure_ascii=False) + "\n"
    # Exercise fragmented UTF-8/JSON reads instead of relying on pipe framing.
    midpoint = len(data) // 2
    sys.stdout.write(data[:midpoint])
    sys.stdout.flush()
    sys.stdout.write(data[midpoint:])
    sys.stdout.flush()


def main():
    log = Path(os.environ["MCP_TEST_LOG"])
    marker = os.environ.get("MCP_TEST_STARTED")
    if marker:
        Path(marker).write_text(str(os.getpid()))
    with log.open("a") as handle:
        handle.write(json.dumps({"event": "started", "argv": sys.argv[1:], "cwd": os.getcwd(),
                                 "secret": os.environ.get("MCP_TEST_SECRET"), "pid": os.getpid()}) + "\n")
    calls = 0
    print("Fixture diagnostic on stderr, not a protocol error.", file=sys.stderr, flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        with log.open("a") as handle:
            handle.write(json.dumps(message) + "\n")
        method = message.get("method")
        if method is None or method.startswith("notifications/"):
            continue
        if method == "initialize":
            if os.environ.get("MCP_TEST_MODE") == "malformed":
                print("not valid JSON", flush=True)
                continue
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "Starting"}})
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "zero-test", "version": "1"}}
        elif method == "tools/list":
            if not message.get("params", {}).get("cursor"):
                send({"jsonrpc": "2.0", "id": "server-ping", "method": "ping"})
                result = {"tools": [{"name": "echo", "description": "Echo text using the persistent test session.",
                                     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}],
                          "nextCursor": "second-page"}
            else:
                result = {"tools": [{"name": "path.query", "description": "A second tool discovered through pagination.",
                                     "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            calls += 1
            arguments = message["params"]["arguments"]
            if arguments.get("text") == "wait":
                time.sleep(120)
            if arguments.get("text") == "rpc-error":
                send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "Bad arguments"}})
                continue
            result = {"content": [{"type": "text", "text": f"MCP ECHO {calls}: {arguments.get('text', 'query')} ✓"}],
                      "structuredContent": {"calls": calls, "pid": os.getpid()},
                      "isError": arguments.get("text") == "tool-error"}
        else:
            send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Unknown method"}})
            continue
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if method == "tools/list" and "nextCursor" not in result:
            send({"jsonrpc": "2.0", "id": "ready-ping", "method": "ping"})


if __name__ == "__main__":
    main()
