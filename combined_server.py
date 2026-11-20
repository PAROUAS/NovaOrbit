from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TEMPEST_DIR = ROOT / "tempest"

os.environ.setdefault("NOVA_DB", str(DATA_DIR / "nova.sqlite3"))
os.environ.setdefault("TEMPMAIL_DATABASE", str(DATA_DIR / "temp_mail.sqlite3"))
os.environ.setdefault("ATTACHMENT_DIR", str(DATA_DIR / "attachments"))
os.environ.setdefault("TEMPMAIL_LOG_FILE", str(DATA_DIR / "logs" / "mail.log"))
os.environ.setdefault("NOVA_ONE_PORT", "5001")

if not TEMPEST_DIR.is_dir():
    raise FileNotFoundError(f"Integrated mail sources are missing: {TEMPEST_DIR}")
sys.path.insert(0, str(TEMPEST_DIR))

import server as nova
from app import app as mail_app, initialize_db as initialize_mail_db


nova.mail_app = mail_app
PORT = int(os.environ["NOVA_ONE_PORT"])


def create_server(port: int | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", PORT if port is None else port), nova.Handler)


def shutdown_when_input_closes(httpd: ThreadingHTTPServer) -> None:
    sys.stdin.readline()
    httpd.shutdown()


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    nova.init_db()
    initialize_mail_db()
    httpd = create_server()
    threading.Thread(target=shutdown_when_input_closes, args=(httpd,), daemon=True).start()
    print(f"NOVA Orbit service listening at http://127.0.0.1:{PORT}")
    print(f"Search and browser home: http://127.0.0.1:{PORT}/")
    print(f"Mail: http://127.0.0.1:{PORT}/mail/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
