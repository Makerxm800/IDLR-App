"""Private JSON-line media worker used by the Tauri desktop application."""

import json
import sys

rpc_stdout = sys.stdout
sys.stdout = sys.stderr

import app as idlr


def handle_message(message):
    request_id = message.get("id")
    method = message.get("method")
    path = message.get("path")
    body = message.get("body")
    if not isinstance(request_id, str) or not isinstance(method, str) or not isinstance(path, str):
        raise ValueError("Worker requests need an id, method, and path.")
    if not path.startswith("/api/"):
        raise ValueError("The media worker accepts only API routes.")
    if path.split("?", 1)[0] not in {
        "/api/download",
        "/api/preview",
        "/api/thumbnail",
        "/api/info",
        "/api/update-ytdlp",
    } and not path.startswith("/api/progress-state/"):
        raise ValueError("This route is not available in the media worker.")

    with idlr.app.test_client() as client:
        response = client.open(path, method=method, json=body)
    payload = response.get_json(silent=True)
    if payload is None:
        raise RuntimeError(f"Media worker returned non-JSON data for {path}.")
    return {"id": request_id, "status": response.status_code, "body": payload}


for line in sys.stdin:
    request = None
    try:
        request = json.loads(line)
        result = handle_message(request)
    except Exception as exc:
        result = {
            "id": request.get("id") if isinstance(request, dict) else None,
            "status": 500,
            "body": {"error": str(exc)},
        }
    rpc_stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    rpc_stdout.flush()
