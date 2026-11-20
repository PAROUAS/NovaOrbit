# NOVA Orbit

**Find your signal. Keep your space.**

NOVA Orbit combines the NOVA desktop browser, local/live search, crawler, and temporary mailbox in one Windows desktop application. The browser is the product shell; Search opens by default and Mail is available from the browser toolbar and the Search home page.

## Start

Double-click **`start.bat`**. It starts the launcher hidden so no command window sits behind the app; startup errors are shown in a dialog and recorded in `data\logs\nova-one.log`. NOVA Orbit opens its own Electron profile chooser, where you can select a saved profile or create one with a four-digit PIN. Creating a profile immediately unlocks it; the chooser stays visible with a transition animation while the profile's service and browser start, then fades away once the browser is ready. Switching profiles animates the current browser out and returns to the chooser. To install the branded Windows Desktop shortcut, run `.\create_shortcut.ps1` from PowerShell in this folder; it creates **NOVA Orbit.lnk** with a custom orbital icon and points it at this workspace's launcher.

Requirements:

- Windows
- Python 3.10 or newer
- Python packages in `requirements.txt` (`python -m pip install -r requirements.txt`)

The Electron/Chromium runtime is included in `runtime/`; Node.js and npm are not required to run the app.

## Integrated routes

- `http://127.0.0.1:5001/` — NOVA Orbit Search and browser home
- `http://127.0.0.1:5001/mail/` — NOVA Orbit Mail
- `http://127.0.0.1:5001/api/...` — search, analytics, and crawler APIs
- `http://127.0.0.1:5001/mail/api/...` — mail APIs (namespaced to avoid collisions)
- `http://127.0.0.1:5001/mail/healthz` — local mail service readiness check

The browser remains a normal Chromium web browser. Tabs, address/search, back/forward, reload, bookmarks, browsing history, downloads, find-in-page, zoom, and ordinary external web navigation remain available. The Mail toolbar button opens Mail in its own browser tab.

## Mail privacy and productivity

Local Mail API routes require a random bearer token stored for each NOVA profile; Electron adds it only to requests sent to this app's `/mail` routes. Mailbox access is separated by profile token. Pre-existing mailboxes are assigned to the first profile during legacy-data migration. Mail.tm account passwords and access tokens are protected with Windows DPAPI in each profile's local database; the migration is applied at startup. Keep the Windows profile available to access those credentials.

Mail adds cached-message search across all inboxes owned by the profile, safe bounded retry for idempotent provider reads, and encrypted backup/restore. Backups use Windows DPAPI and can only be restored by the same Windows profile on the same machine/user context; they are not portable archives. Cached-message search operates only on messages already refreshed into the local cache. A command palette in the browser chrome opens with **Ctrl+Shift+P**.

The combined service adds a restrictive content-security policy and common browser hardening headers. Mail provider availability, SMTP email alerts, and live web search still depend on their external services and network configuration. App-data DPAPI is a Windows user-profile protection layer, not a substitute for disk encryption or Windows account security.

## User profiles

NOVA Orbit stores its profile list at `%APPDATA%\nova-browser\profiles.json` and keeps each user's browser profile, bookmarks/history, search database, mail database, attachments, and Mail access token under `%APPDATA%\nova-browser\profiles\<profile-id>\`. The profile list stores a salted scrypt verifier, not the PIN itself. The chooser and PIN keypad are rendered inside the NOVA Electron application window. On first launch, existing NOVA/Mail databases in this workspace's `data\` folder are copied into the first created profile and legacy local mailbox records are assigned to it; the source databases are not modified. Additional profiles start with separate databases and tokens. Profile names must be unique on this Windows account.

The four-digit PIN is a profile-selection convenience lock, not strong authentication or full-disk encryption. Someone with access to the same Windows account can potentially inspect or alter local app files; use separate Windows accounts for meaningful operating-system-level user separation. Mail provider credentials remain DPAPI-protected for the Windows account, and encrypted Mail backups remain tied to that account.

## Data and privacy

Application files and bundled runtime are self-contained in this folder. Active search/mail databases, attachments, and Electron browser state are stored separately for each profile under `%APPDATA%\nova-browser\profiles\`. The workspace `data\` directory holds the legacy data copied into the first profile and development/test defaults. These stores are separate from the original projects' databases; downloaded files remain in the Windows Downloads folder.

Search suggestions and live search use the selected external providers; mailbox creation and message retrieval contact the configured Mail.tm provider. Mail SMTP notifications are disabled unless configured through environment variables. Do not put credentials in source files. Startup diagnostics and mail logs are written to the active profile's app-data directory. The local service binds only to `127.0.0.1` on port `5001` by default. Set `NOVA_ONE_PORT` before launching to use another port; the launcher passes the same value to the service and Electron browser.

## Development and checks

Run the NOVA backend, provider, DPAPI, and integration tests with:

```powershell
python -m unittest discover -s tests -v
node --test browser\python-discovery.test.js browser\permission-policy.test.js browser\navigation-policy.test.js browser\mail-auth.test.js browser\profile-store.test.js browser\integration-wiring.test.js tests\category-selection.test.js tests\admin-responsive.test.js
```

The Node suite can also be run as `npm test` from `browser\`. Provider retry tests mock all Mail.tm network traffic; Mail authentication, cross-inbox search scoping, encrypted credential storage, and backup routing are covered by the Python integration suite.

The upstream Mail test module imports a separate `global-chat` server; that unrelated subproject is not part of NOVA Orbit. The combined mail routes and profile isolation are covered by tests under `tests/`.

For a direct integration smoke test without opening Electron, run `python combined_server.py` and check the Search and Mail URLs above. Use `Ctrl+C` to stop it. The supported desktop entry point remains `start.bat`.
