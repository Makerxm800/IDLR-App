"""Private JSON-line media worker used by the Tauri desktop application."""

import json
import sys

# Keep the real stdout for the JSON-line protocol; everything the app prints
# while running must not corrupt it, so normal output is redirected to stderr
# (which the desktop shell records in IDLR's log).
rpc_stdout = sys.stdout
sys.stdout = sys.stderr


def reply(payload):
    """Write one response line. A dead pipe means the app closed, so stop."""
    try:
        rpc_stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        rpc_stdout.flush()
    except (OSError, ValueError):
        raise SystemExit(0)


def _startup_failure(exc):
    """Answer requests with the real reason instead of dying silently.

    When `import app` fails — a missing or quarantined dependency, a broken
    bundled FFmpeg — the old behaviour was a traceback nobody could see (stderr
    was sent to NUL) and a UI stuck on 'Starting…' forever.
    """
    message = (
        "IDLR's download helper could not start: "
        f"{type(exc).__name__}: {exc} "
        "(If a security product quarantined IDLR's files, restore them and reinstall.)"
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            request_id = json.loads(line).get("id")
        except ValueError:
            request_id = None
        reply({"id": request_id, "status": 503, "body": {"error": message, "code": "worker_broken"}})


try:
    import app as idlr
except BaseException as exc:  # noqa: BLE001 - any import failure must be reported, not swallowed
    print(f"IDLR worker failed to start: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    _startup_failure(exc)
    raise SystemExit(1)


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
    reply(result)
