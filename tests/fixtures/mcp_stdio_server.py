"""Deterministic test server. No network, user configuration or real credentials."""

import json
import os
import sys
import threading
import time
from pathlib import Path

log = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"
lock = threading.Lock()


def respond(request):
    method, params = request["method"], request.get("params", {})
    if "id" not in request:
        return
    error = None
    if method == "initialize":
        if mode == "hang":
            return
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        names = ["echo", "fail"] if not params.get("cursor") else ["slow", "mixed"]
        result = {
            "tools": [
                {"name": n, "description": n, "inputSchema": schema, "annotations": {"readOnlyHint": True}}
                for n in names
            ]
        }
        if not params.get("cursor"):
            result["nextCursor"] = "next"
        if mode == "repeat":
            result["nextCursor"] = "next"
    elif method == "tools/call":
        name, args = params["name"], params["arguments"]
        with lock:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {"id": request["id"], "name": name, "args": args, "env": os.environ.get("MOSS_MCP_FIXTURE")}
                    )
                    + "\n"
                )
        if name == "slow":
            time.sleep(5)
        elif args["text"] == "delayed":
            time.sleep(0.15)
        result = {"content": [{"type": "text", "text": args["text"]}], "isError": name == "fail"}
        if args["text"] == "large-error":
            result = {"content": [{"type": "text", "text": "错误" * 30000}], "isError": True}
        if name == "mixed":
            result["structuredContent"] = {"received": args["text"]}
            result["content"].append({"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"})
            if args["text"].startswith("picture"):
                result["content"][-1]["data"] = PICTURE
                result["isError"] = args["text"] == "picture-error"
        if args["text"] == "protocol-error":
            error = {"code": -32602, "message": "fixture-private-error"}
        if args["text"] == "disconnect":
            os._exit(0)
    else:
        result = {}
    response = {"jsonrpc": "2.0", "id": request["id"]}
    response["error" if error else "result"] = error or result
    with lock:
        print(json.dumps(response), flush=True)


# A 3×2 red PNG.
PICTURE = "iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAEElEQVR4nGP8zwAFTDAGAwATKQED8NgHhAAAAABJRU5ErkJggg=="


# MCP stdio is UTF-8; Python otherwise uses the locale code page (GBK on Chinese Windows) for pipes.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
log.with_suffix(".pid").write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "tools/call":
        threading.Thread(target=respond, args=(request,), daemon=True).start()
    else:
        respond(request)
log.with_suffix(".closed").write_text("closed")
