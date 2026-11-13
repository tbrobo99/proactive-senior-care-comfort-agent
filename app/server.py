"""HTTP server (Python standard library, no dependencies)."""
from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db

WEB_DIR = Path(__file__).resolve().parent / "web"

# mimetypes on Windows does not know .webmanifest, and a manifest served as
# application/octet-stream is silently ignored -- the app then just never
# becomes installable. Spell the important ones out.
EXTRA_TYPES = {
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".js": "application/javascript",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LGHomeAgent/1.1"
    agent_ref = None  # injected by serve()

    # ------------------------------------------------ helpers

    def log_message(self, fmt, *args):
        pass  # keep the console clean

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        if not re.fullmatch(r"[A-Za-z0-9_\-./]+", rel) or ".." in rel:
            self._send(403, b"forbidden", "text/plain")
            return
        path = WEB_DIR / rel
        if not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = (EXTRA_TYPES.get(path.suffix.lower())
                 or mimetypes.guess_type(str(path))[0]
                 or "application/octet-stream")
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    # ------------------------------------------------ routes

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/snapshot":
            return self._json(self.agent_ref.snapshot())
        if path == "/api/mode":
            return self._json({"mode": self.agent_ref.snapshot()["mode"]})
        if path == "/api/messages":
            return self._json({"messages": db.recent_messages(60)})
        if path == "/api/logs":
            return self._json({"logs": db.recent_logs(60)})
        if path == "/api/audit":
            q = parse_qs(urlparse(self.path).query)
            return self._json({"audit": db.recent_audit(
                100, approval_id=(q.get("approval_id") or [None])[0])})
        if path == "/api/approvals":
            # Phase 2: Tier-2 requests waiting on the user. Absent engine ->
            # empty list, never a 500.
            engine = getattr(self.agent_ref, "proactive", None)
            if engine is None:
                return self._json({"approvals": []})
            return self._json({"approvals": [a.to_dict() for a in engine.approvals.pending()]})
        if path == "/api/proactive":
            engine = getattr(self.agent_ref, "proactive", None)
            return self._json(engine.state() if engine else {"running": False})
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/chat":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "text must not be empty"}, 400)
            result = self.agent_ref.handle(text)
            return self._json({**result, "snapshot": self.agent_ref.snapshot()})

        if path == "/api/mode":
            # Switched live, not on restart: the point of the two modes is that
            # the same sentence can be asked of both, one after the other.
            mode = str(body.get("mode") or "").lower()
            if mode not in ("cascade", "agent"):
                return self._json({"error": "mode must be cascade or agent"}, 400)
            self.agent_ref.cfg.setdefault("agent", {})["mode"] = mode
            return self._json({"ok": True, "mode": mode,
                               "snapshot": self.agent_ref.snapshot()})

        if path == "/api/control":
            device_id = body.get("device_id")
            actions = body.get("actions") or []
            if not device_id or not actions:
                return self._json({"error": "device_id and actions are required"}, 400)
            res = self.agent_ref.adapter.apply(device_id, actions)
            db.log("control.manual", f"{device_id} {actions} -> {res.get('ok')}")
            return self._json({**res, "snapshot": self.agent_ref.snapshot()})

        if path == "/api/jobs/clear":
            n = db.clear_jobs()
            return self._json({"ok": True, "deleted": n,
                               "snapshot": self.agent_ref.snapshot()})

        if path == "/api/messages/clear":
            db.clear_messages()
            return self._json({"ok": True})

        m = re.fullmatch(r"/api/approvals/([A-Za-z0-9_]+)/(confirm|decline)", path)
        if m:
            # The only door into Tier-2 execution. No approval id, no action —
            # the queue itself refuses anything not pending.
            engine = getattr(self.agent_ref, "proactive", None)
            if engine is None:
                return self._json({"error": "proactive engine not running"}, 400)
            fn = engine.confirm if m.group(2) == "confirm" else engine.decline
            # Which surface the answer came from goes into the audit trail.
            channel = str(body.get("channel") or "api")[:16]
            return self._json(fn(m.group(1), channel=channel))

        return self._json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_]+)", path)
        if m:
            ok = db.delete_job(m.group(1))
            return self._json({"ok": ok, "snapshot": self.agent_ref.snapshot()})
        return self._json({"error": "not found"}, 404)


def serve(agent, host: str = "127.0.0.1", port: int = 8848) -> ThreadingHTTPServer:
    Handler.agent_ref = agent
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
