"""Invoice Tester: a small stand-alone page for trying the invoice extraction API.

Serves index.html and forwards requests to the Vision API (default http://127.0.0.1:8000).
Extraction takes minutes on a CPU, so each upload runs as a background job that the page
polls; the full result (every field's status, the validation checks, warnings) is kept in
memory until the server stops. The API key is passed through per request and never stored.

Run from the project folder (uses the API's virtual environment for httpx):

    vision-api\\.venv\\Scripts\\python.exe invoice-tester\\server.py            # http://localhost:3002
    vision-api\\.venv\\Scripts\\python.exe invoice-tester\\server.py --port 3003 --api http://127.0.0.1:8000
"""

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import httpx

HERE = Path(__file__).resolve().parent
MAX_UPLOAD = 25 * 1024 * 1024
EXTRACT_TIMEOUT = 1800.0  # seconds; a multi-page invoice on a laptop CPU can take many minutes

JOBS: dict[str, dict[str, object]] = {}
LOCK = threading.Lock()
API_URL = "http://127.0.0.1:8000"


def follow_progress(job_id: str, key: str, progress_id: str, stop: threading.Event) -> None:
    """Copy the pipeline's live comments (GET /v1/invoices/progress/{id}) into the job."""
    count = 0
    while not stop.wait(1.2):
        try:
            response = httpx.get(f"{API_URL}/v1/invoices/progress/{progress_id}", params={"after": count},
                                 headers={"Authorization": f"Bearer {key}"}, timeout=10)
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue  # 404 until the API has opened the channel (it reads the upload first)
        found = response.json()
        with LOCK:
            JOBS[job_id].setdefault("events", []).extend(found["events"])
        count = found["count"]


def run_job(job_id: str, key: str, filename: str, content_type: str, body: bytes) -> None:
    progress_id = f"tester-{job_id}"
    stop = threading.Event()
    follower = threading.Thread(target=follow_progress, args=(job_id, key, progress_id, stop), daemon=True)
    follower.start()
    try:
        response = httpx.post(
            f"{API_URL}/v1/invoices/extract",
            headers={"Authorization": f"Bearer {key}", "X-Progress-Id": progress_id},
            files={"file": (filename, body, content_type)},
            timeout=EXTRACT_TIMEOUT,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"message": response.text[:500] or f"HTTP {response.status_code}"}}
        update = {"state": "done" if response.status_code == 200 else "error", "http_status": response.status_code, "result": payload}
    except httpx.HTTPError as exc:
        update = {"state": "error", "http_status": None, "result": {"error": {"message": f"Could not reach the API at {API_URL}: {exc}"}}}
    stop.set()
    follower.join(timeout=5)
    try:  # the last comments (validation, result) arrive just before the response
        response = httpx.get(f"{API_URL}/v1/invoices/progress/{progress_id}",
                             params={"after": len(JOBS[job_id].get("events", []))},
                             headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if response.status_code == 200:
            with LOCK:
                JOBS[job_id].setdefault("events", []).extend(response.json()["events"])
    except httpx.HTTPError:
        pass
    with LOCK:
        JOBS[job_id].update(update, finished=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceTester/1.0"

    def log_message(self, format: str, *args: object) -> None:  # quieter console: one line per request
        print(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/form_fields.json":
            body = (HERE / "form_fields.json").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            try:
                ok = httpx.get(f"{API_URL}/health", timeout=5).status_code == 200
            except httpx.HTTPError:
                ok = False
            self._json(200, {"api": API_URL, "reachable": ok})
        elif path.startswith("/api/jobs/") and path.endswith("/file"):
            with LOCK:
                job = JOBS.get(path.removeprefix("/api/jobs/").removesuffix("/file"))
                upload = (job or {}).get("upload")
            if not upload:
                self._json(404, {"error": {"message": "Unknown job"}})
                return
            body, content_type = upload
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/jobs/"):
            with LOCK:
                job = JOBS.get(path.removeprefix("/api/jobs/"))
                snapshot = {**{k: v for k, v in job.items() if k != "upload"}, "events": list(job.get("events", []))} if job else None
            if snapshot is None:
                self._json(404, {"error": {"message": "Unknown job (the tester was restarted?)"}})
            else:
                self._json(200, snapshot)
        else:
            self._json(404, {"error": {"message": "Not found"}})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/api/extract":
            self._json(404, {"error": {"message": "Not found"}})
            return
        key = self.headers.get("X-Api-Key", "").strip()
        length = int(self.headers.get("Content-Length") or 0)
        if not key:
            self._json(400, {"error": {"message": "Enter an API key."}})
            return
        if length <= 0 or length > MAX_UPLOAD:
            self._json(400, {"error": {"message": f"Choose a file (at most {MAX_UPLOAD // 1024 // 1024} MB)."}})
            return
        body = self.rfile.read(length)
        filename = unquote(self.headers.get("X-Filename", "invoice")) or "invoice"
        content_type = self.headers.get("Content-Type", "application/octet-stream")
        job_id = uuid.uuid4().hex
        with LOCK:
            JOBS[job_id] = {"id": job_id, "state": "running", "filename": filename, "started": time.time(), "events": [],
                            "content_type": content_type, "upload": (body, content_type)}
        threading.Thread(target=run_job, args=(job_id, key, filename, content_type, body), daemon=True).start()
        self._json(202, {"id": job_id})


def main() -> None:
    global API_URL
    parser = argparse.ArgumentParser(description="Invoice Tester")
    parser.add_argument("--port", type=int, default=3002)
    parser.add_argument("--api", default=API_URL, help="Vision API base URL (default: %(default)s)")
    args = parser.parse_args()
    API_URL = args.api.rstrip("/")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Invoice Tester on http://localhost:{args.port}  (API: {API_URL})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
