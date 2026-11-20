#!/usr/bin/env python3
"""Launch the NOVA Orbit combined local service and Electron desktop app."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from profile_manager import load_selected_profile

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("NOVA_ONE_PORT", "5001"))
ORIGIN = f"http://127.0.0.1:{PORT}"
HEALTH_URLS = (f"{ORIGIN}/api/health", f"{ORIGIN}/mail/healthz")
BACKEND = ROOT / "combined_server.py"
ELECTRON = ROOT / "runtime" / "electron.exe"
LOG_FILE = ROOT / "data" / "logs" / "nova-one.log"
PROFILE_SWITCH_EXIT_CODE = 23


class ProfilePickerSession:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        close_file: Path,
        browser_ready_file: Path,
        temporary: tempfile.TemporaryDirectory[str],
    ) -> None:
        self.process = process
        self.close_file = close_file
        self.browser_ready_file = browser_ready_file
        self.temporary = temporary

    def close(self) -> None:
        if self.process.poll() is None:
            self.close_file.write_text("close\n", encoding="ascii")
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.temporary.cleanup()


def ensure_mail_access_token(token_file: Path) -> str:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with token_file.open("x", encoding="ascii") as token_stream:
            token = secrets.token_urlsafe(32)
            token_stream.write(token)
            token_stream.flush()
            os.fsync(token_stream.fileno())
            return token
    except FileExistsError:
        token = token_file.read_text(encoding="ascii").strip()
        if len(token) < 40 or any(char.isspace() for char in token):
            raise RuntimeError(f"Local mail access token is invalid: {token_file}")
        return token


def migrate_legacy_data(profile_dir: Path, access_token: str) -> None:
    data_root = ROOT / "data"
    marker = profile_dir.parent / ".legacy-data-migrated"
    if marker.exists():
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    for source_name, target_name in (
        ("nova.sqlite3", "nova.sqlite3"),
        ("temp_mail.sqlite3", "temp_mail.sqlite3"),
    ):
        source = data_root / source_name
        target = profile_dir / target_name
        if source.is_file() and not target.exists():
            source_db = sqlite3.connect(source)
            try:
                target_db = sqlite3.connect(target)
                try:
                    source_db.backup(target_db)
                finally:
                    target_db.close()
            finally:
                source_db.close()

    mail_db = profile_dir / "temp_mail.sqlite3"
    if mail_db.is_file():
        owner_id = "local:" + hashlib.sha256(
            access_token.encode("utf-8")
        ).hexdigest()
        with closing(sqlite3.connect(mail_db)) as connection, connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            previous_owner = None
            if "local_mail_owner_claim" in tables:
                claim = connection.execute(
                    "SELECT owner_id FROM local_mail_owner_claim WHERE id = 1"
                ).fetchone()
                previous_owner = claim[0] if claim else None
            legacy_owners = {"local"}
            if previous_owner:
                legacy_owners.add(previous_owner)
            if "mailboxes" in tables:
                legacy_owners.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT owner_id FROM mailboxes WHERE owner_id LIKE 'local:%'"
                    )
                )
            owner_tables = (
                "mailboxes",
                "shared_mailbox_access",
                "mailbox_creation_events",
                "email_notification_settings",
                "email_notification_deliveries",
            )
            for table in owner_tables:
                if table not in tables:
                    continue
                columns = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "owner_id" not in columns:
                    continue
                for previous in legacy_owners:
                    connection.execute(
                        f"UPDATE {table} SET owner_id = ? WHERE owner_id = ?",
                        (owner_id, previous),
                    )
            if "local_mail_owner_claim" in tables:
                connection.execute(
                    "UPDATE local_mail_owner_claim SET owner_id = ? WHERE id = 1",
                    (owner_id,),
                )
            connection.commit()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="ascii")


def profile_environment(identity: dict[str, str]) -> dict[str, str]:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise RuntimeError("Windows APPDATA is required to save profile data.")
    profile_dir = Path(app_data) / "nova-browser" / "profiles" / identity["id"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    electron_profile_dir = profile_dir / "electron"
    electron_profile_dir.mkdir(parents=True, exist_ok=True)
    token = ensure_mail_access_token(profile_dir / "mail-access-token")
    migrate_legacy_data(profile_dir, token)
    environment = os.environ.copy()
    environment.update(
        {
            "NOVA_ONE_PORT": str(PORT),
            "NOVA_PROFILE_ID": identity["id"],
            "NOVA_PROFILE_NAME": identity["name"],
            "NOVA_ONE_PROFILE_DIR": str(electron_profile_dir),
            "NOVA_MAIL_TOKEN_FILE": str(profile_dir / "mail-access-token"),
            "NOVA_MAIL_ACCESS_TOKEN": token,
            "NOVA_DB": str(profile_dir / "nova.sqlite3"),
            "TEMPMAIL_DATABASE": str(profile_dir / "temp_mail.sqlite3"),
            "ATTACHMENT_DIR": str(profile_dir / "attachments"),
            "TEMPMAIL_LOG_FILE": str(profile_dir / "logs" / "mail.log"),
        }
    )
    for picker_variable in (
        "NOVA_PROFILE_PICKER",
        "NOVA_PROFILE_REGISTRY",
        "NOVA_PROFILE_SELECTION_FILE",
        "NOVA_PROFILE_PICKER_CLOSE_FILE",
        "NOVA_PROFILE_BROWSER_READY_FILE",
    ):
        environment.pop(picker_variable, None)
    return environment


def select_profile(registry: Path) -> tuple[dict[str, str], ProfilePickerSession] | None:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise RuntimeError("Windows APPDATA is required to select a NOVA Orbit profile.")
    picker_data = Path(app_data) / "nova-browser" / "profile-picker"
    picker_data.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="nova-orbit-profile-")
    directory = Path(temporary.name)
    selection_file = directory / "selected-profile.json"
    close_file = directory / "close-picker"
    browser_ready_file = directory / "browser-ready"
    environment = os.environ.copy()
    environment.update(
        {
            "NOVA_PROFILE_PICKER": "1",
            "NOVA_PROFILE_REGISTRY": str(registry),
            "NOVA_PROFILE_SELECTION_FILE": str(selection_file),
            "NOVA_PROFILE_PICKER_CLOSE_FILE": str(close_file),
            "NOVA_PROFILE_BROWSER_READY_FILE": str(browser_ready_file),
            "NOVA_ONE_PROFILE_DIR": str(picker_data),
        }
    )
    try:
        process = subprocess.Popen(
            [str(ELECTRON), str(ROOT / "browser")],
            cwd=ROOT,
            env=environment,
        )
    except OSError:
        temporary.cleanup()
        raise
    picker = ProfilePickerSession(process, close_file, browser_ready_file, temporary)
    while True:
        if selection_file.is_file():
            try:
                identity = load_selected_profile(registry, selection_file)
            except RuntimeError:
                picker.close()
                raise
            return identity, picker
        return_code = process.poll()
        if return_code is not None:
            picker.close()
            if return_code == 0:
                return None
            raise RuntimeError(f"NOVA Orbit profile window exited unexpectedly ({return_code}).")
        time.sleep(0.1)


def endpoint_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(32_000))
            return isinstance(payload, dict) and payload.get("ok") is True
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def service_ready() -> bool:
    return all(endpoint_ready(url) for url in HEALTH_URLS)


def wait_for_service(
    process: subprocess.Popen[str],
    timeout: float = 45,
    log_file: Path = LOG_FILE,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"NOVA Orbit service exited early. See {log_file}")
        if service_ready():
            return
        time.sleep(0.25)
    raise TimeoutError(f"NOVA Orbit service did not become ready. See {log_file}")


def wait_for_browser_window(
    browser: subprocess.Popen[bytes],
    service: subprocess.Popen[str],
    ready_file: Path,
    timeout: float = 45,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if browser.poll() is not None:
            raise RuntimeError("NOVA Orbit browser closed before its window was ready.")
        if service.poll() is not None:
            raise RuntimeError("NOVA Orbit local service stopped before the browser opened.")
        if ready_file.is_file():
            return
        time.sleep(0.1)
    raise TimeoutError("NOVA Orbit browser window did not become ready.")


def stop_service(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if process.stdin is not None:
            process.stdin.write("shutdown\n")
            process.stdin.flush()
            process.stdin.close()
        process.wait(timeout=8)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def report_startup_error(error: Exception) -> None:
    message = f"NOVA Orbit could not start: {error}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, f"{message}\n\nDetails: {LOG_FILE}", "NOVA Orbit", 0x10)


def main() -> int:
    if not BACKEND.is_file():
        raise FileNotFoundError(f"NOVA Orbit service is missing: {BACKEND}")
    if not ELECTRON.is_file():
        raise FileNotFoundError(f"Bundled Electron runtime is missing: {ELECTRON}")
    profile_root = os.environ.get("APPDATA")
    if not profile_root:
        raise RuntimeError("Windows APPDATA is required to select a NOVA Orbit profile.")
    registry = Path(profile_root) / "nova-browser" / "profiles.json"
    while True:
        selection = select_profile(registry)
        if selection is None:
            return 0
        identity, picker = selection
        environment = profile_environment(identity)
        log_file = Path(environment["NOVA_ONE_PROFILE_DIR"]) / "logs" / "nova-one.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if service_ready():
            picker.close()
            raise RuntimeError(f"A NOVA Orbit service is already listening at {ORIGIN}.")
        service: subprocess.Popen[str] | None = None
        browser: subprocess.Popen[bytes] | None = None
        try:
            with log_file.open("a", encoding="utf-8") as log:
                print(f"Starting NOVA Orbit for {identity['name']}...")
                service = subprocess.Popen(
                    [sys.executable, str(BACKEND)],
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            wait_for_service(service, log_file=log_file)
            environment["NOVA_PROFILE_BROWSER_READY_FILE"] = str(picker.browser_ready_file)
            print("NOVA Orbit is ready. Transitioning into the desktop browser...")
            browser = subprocess.Popen(
                [str(ELECTRON), str(ROOT / "browser")],
                cwd=ROOT,
                env=environment,
            )
            wait_for_browser_window(browser, service, picker.browser_ready_file)
            picker.close()
            while browser.poll() is None:
                if service.poll() is not None:
                    raise RuntimeError(
                        f"NOVA Orbit local service stopped unexpectedly. See {log_file}"
                    )
                time.sleep(0.5)
            if browser.returncode != PROFILE_SWITCH_EXIT_CODE:
                return browser.returncode or 0
        except KeyboardInterrupt:
            if browser is not None and browser.poll() is None:
                browser.terminate()
                browser.wait(timeout=5)
            return 0
        finally:
            if service is not None:
                stop_service(service)
            picker.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, TimeoutError) as error:
        report_startup_error(error)
        raise SystemExit(1)
