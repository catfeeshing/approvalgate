import json
import mimetypes
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audit
import mock_actions
from wrapper import APPROVE, decide

REVIEW_OUTCOMES = {"HOLD_CONFIRMED", "REJECTED", "REVISED"}

UI_DIR = Path(__file__).parent / "ui"
PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"

PERSONAS = {"customer_support", "paralegal", "financial_analyst"}


def samples(persona):
    """Practice inputs (text and context only) so a UI can offer 'simulate from a test input'."""
    path = PERSONAS_DIR / persona / "practice_set.json"
    items = json.loads(path.read_text())["items"]
    return [{"id": i["id"], "text": i["text"], "context": i["context"]} for i in items]


def ui_file(path):
    """Static files from wrapper/ui/, so a team can use plain HTML, a built SPA, or anything a browser runs."""
    name = "persona.html" if path in ("/", "") else path.lstrip("/")
    target = (UI_DIR / name).resolve()
    if UI_DIR.resolve() not in target.parents or not target.is_file():
        return None
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, body):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route == "/health":
            return self.send_json(200, {"status": "ok"})
        if route == "/api/samples":
            persona = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("persona")
            if persona not in PERSONAS:
                return self.send_json(400, {"error": "Unknown persona"})
            return self.send_json(200, {"samples": samples(persona)})
        if route == "/api/audit":
            return self.send_json(200, audit.recent())
        served = ui_file(route)
        if served:
            raw, content_type = served
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_json(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if self.path == "/api/review":
            return self.post_review()
        if self.path != "/decide":
            return self.send_json(404, {"error": "Unknown endpoint"})
        self.connection.settimeout(10)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 1_000_000:
                raise ValueError("Body must be 1 to 1000000 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            text, persona, context = payload.get("text"), payload.get("persona"), payload.get("context", {})
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("text must be a non-empty string of at most 20000 characters")
            if not isinstance(persona, str) or persona not in PERSONAS:
                raise ValueError("Unknown persona")
            if not isinstance(context, dict):
                raise ValueError("context must be an object")
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Invalid request: supply text, persona, and an optional context object"})
        try:
            result = decide(text, context, persona)
            execute = result.decision == APPROVE
            if execute:
                mock_actions.run_for_persona(persona, text)
            self.send_json(200, {**asdict(result), "action_executed": execute})
        except Exception:
            self.send_json(500, {"error": "Wrapper failed; inspect the decision implementation"})

    def post_review(self):
        """Records a reviewer's outcome for a held input. It never runs the action; that stays in /decide."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 100_000:
                raise ValueError("Body must be 1 to 100000 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            text, persona = payload.get("text"), payload.get("persona")
            outcome, note = payload.get("outcome"), payload.get("note")
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("text must be a non-empty string of at most 20000 characters")
            if persona not in PERSONAS:
                raise ValueError("Unknown persona")
            if outcome not in REVIEW_OUTCOMES:
                raise ValueError("outcome must be HOLD_CONFIRMED, REJECTED, or REVISED")
            if not isinstance(note, str) or not note.strip() or len(note) > 1000:
                raise ValueError("note must be a non-empty string of at most 1000 characters")
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Invalid review: supply text, persona, outcome, and a note"})
        audit.record_review(persona, text, outcome, note.strip())
        self.send_json(200, {"recorded": True})


def main():
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Approval Gate wrapper on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
