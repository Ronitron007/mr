"""Live viewer: http://127.0.0.1:<port>/ re-reads mr's records on every poll (every 4 s in the page)."""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import load, render


def make_handler(home: str):
    cache = {"t": 0.0, "data": None}

    def data():
        if time.time() - cache["t"] > 2 or cache["data"] is None:
            cache["data"] = render.payload(load.build_traces(home), live=True)
            cache["t"] = time.time()
        return cache["data"]

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, render.page(data()).encode(), "text/html; charset=utf-8")
            elif path == "/data.json":
                self._send(200, json.dumps(data(), default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return H


def serve(home: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    srv = ThreadingHTTPServer((host, port), make_handler(home))
    print(f"mr-trace live at http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
