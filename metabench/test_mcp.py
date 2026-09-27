"""Minimal stdio MCP bridge used by native agents to request queued public tests."""
import argparse
import json
import sys
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

OPENER = build_opener(ProxyHandler({}))  # Trial capabilities only travel over loopback.


def request(url, token, body=None):
    if body is not None:
        body = {**body, "request_id": body.get("request_id", uuid.uuid4().hex)}
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    for attempt in range(6):
        try:
            with OPENER.open(req, timeout=120 if body is not None else 30) as response:
                return json.load(response)
        except HTTPError:
            raise  # Authorization and validation failures are not transient.
        except (URLError, OSError, HTTPException):
            if attempt == 5:
                raise
            # POST retries retain their identity and original immutable snapshot,
            # including when the server accepted a job but its reply was lost.
            time.sleep(.1 * 2 ** attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    token = Path(args.token_file).read_text().strip()
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        try:
            method = message["method"]
            if method == "initialize":
                result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "metabench-tests", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": "run_tests", "description":
                    "Run public build/test commands on this trial's persistent VM worktree. Current local changes are snapshotted automatically. Jobs wait for the VM lock; queued time is not execution time. The working directory is /repo. Use this tool for compilation and tests instead of running them locally.",
                    "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
                    "inputSchema": {"type": "object", "properties": {
                        "command": {"type": "string"}, "timeout": {"type": "number", "exclusiveMinimum": 0}},
                        "required": ["command"], "additionalProperties": False}}]}
            elif method == "tools/call":
                params = message["params"]
                if params["name"] != "run_tests":
                    raise ValueError("unknown tool")
                submitted = request(args.url + "/test", token, params.get("arguments", {}))
                while True:
                    state = request(args.url + "/jobs/" + submitted["job_id"], token)
                    if state["status"] in ("completed", "infrastructure_error"):
                        break
                    time.sleep(.5)
                result = {"content": [{"type": "text", "text": json.dumps(state)}],
                          "isError": state["status"] == "infrastructure_error"}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32601, "message": "method not found"}}), flush=True)
                continue
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": message["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": str(error)}]}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
