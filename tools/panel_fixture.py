"""UI-only test fixture. NOT device firmware; never use for attendance records.

Run: python tools/panel_fixture.py
Then open http://127.0.0.1:8765 . All data is disposable and in memory.
"""
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "fixture-csrf-token"
STATE = {
    "csrf": TOKEN,
    "now": int(datetime(2026, 10, 23, 12, 34, 56, tzinfo=timezone.utc).timestamp()),
    "clock_ok": True,
    "storage_ok": True,
    "rfid_ok": True,
    "oled_ok": True,
    "sd_enabled": False,
    "sd_ok": False,
    "last_uid": "DEADBEEF",
    "message": "TEST VERISI: fiziksel cihaza bagli degil",
    "sequence": 2,
    "employees": [
        {"uid": "11223344", "name": "Ayşe Yılmaz", "inside": False,
         "entry": 0, "total_seconds": 28800, "live_seconds": 0},
        {"uid": "AABBCCDD", "name": '<img src=x onerror="window.xss=1">',
         "inside": False, "entry": 0, "total_seconds": 3661, "live_seconds": 0},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(200, (ROOT / "data/index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self.send(200, json.dumps(STATE, ensure_ascii=False))
        elif self.path == "/api/events.csv":
            self.send(200, "sequence,epoch,uid,event,seconds\n1,1792756800,11223344,I,0\n",
                      "text/csv; charset=utf-8")
        else:
            self.send(404, b"{}")

    def do_POST(self):
        if self.headers.get("X-CSRF-Token") != TOKEN:
            self.send(403, json.dumps({"ok": False, "message": "CSRF reddedildi"}))
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1024:
            self.send(413, b"{}")
            return
        body = json.loads(self.rfile.read(size))
        if self.path == "/api/employee":
            employee = next((e for e in STATE["employees"] if e["uid"] == body["uid"]), None)
            if employee is None:
                employee = {"uid": body["uid"], "inside": False, "entry": 0,
                            "total_seconds": 0, "live_seconds": 0}
                STATE["employees"].append(employee)
            employee["name"] = body["name"]
        elif self.path == "/api/time":
            STATE["now"] = body["epoch"]
        else:
            self.send(404, b"{}")
            return
        self.send(200, json.dumps({"ok": True, "message": "Test sunucusunda kaydedildi"}))


if __name__ == "__main__":
    print("UI TEST ONLY: http://127.0.0.1:8765 (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
