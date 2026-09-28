"""Offline interactive reports and a loopback-only live result viewer."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .charts import chart_data


def dashboard_data(rows, metadata=None):
    data = chart_data(rows)
    data["tasks"] = sorted({r["task_id"] for r in rows})
    data["task_charts"] = {
        task: chart_data([r for r in rows if r["task_id"] == task])["charts"]
        for task in data["tasks"]
    }
    # Keep tool transcripts, patches, local paths and provider receipts out of the page.
    fields = ("config_id", "task_id", "step", "repeat", "status", "score_eligible",
              "scores", "usage", "observed_usage_lower_bound", "budget_mode",
              "wall_seconds", "active_seconds", "queue_wait_seconds")
    data["rows"] = [{key: r[key] for key in fields if key in r} for r in rows]
    data["metadata"] = metadata or {}
    serialized = json.dumps(data, sort_keys=True, allow_nan=False)
    data["version"] = hashlib.sha256(serialized.encode()).hexdigest()
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    return data


def dashboard_html(data, *, live=False):
    template = files("metabench").joinpath("dashboard.html").read_text(encoding="utf-8")
    # JSON in a script element must not allow a model/task name to close the element.
    payload = json.dumps({"data": data, "live": live}, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return template.replace("__METABENCH_DATA__", payload)


def write_dashboard(rows, out, metadata=None):
    data = dashboard_data(rows, metadata)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=out.parent,
                                     prefix=".metabench-dashboard-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(dashboard_html(data))
    try:
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
    return data


def results_loader(paths, metadata_path=None):
    def load():
        rows = [json.loads(line) for path in paths for line in Path(path).read_text().splitlines() if line.strip()]
        metadata = json.loads(Path(metadata_path).read_text()) if metadata_path else None
        return dashboard_data(rows, metadata)
    return load


def dashboard_server(load, port=8765):
    """Serve only the report and validated data, never a filesystem directory."""
    load()  # Validate before listening.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path not in ("/", "/index.html", "/data.json"):
                self.send_error(404)
                return
            try:
                data = load()
                if path == "/data.json":
                    body = json.dumps(data, allow_nan=False).encode()
                    content_type = "application/json; charset=utf-8"
                else:
                    body = dashboard_html(data, live=True).encode()
                    content_type = "text/html; charset=utf-8"
                status = 200
            except (ValueError, KeyError, OSError, TypeError):
                # The browser keeps its last complete snapshot on a partial write.
                body = b'{"error":"Results are being updated or are invalid; retaining the last snapshot."}'
                content_type, status = "application/json; charset=utf-8", 503
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_dashboard(paths, *, port=8765, metadata_path=None):
    with dashboard_server(results_loader(paths, metadata_path), port) as server:
        print(f"Live dashboard: http://127.0.0.1:{server.server_port}/ (refreshes every 5 seconds)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
