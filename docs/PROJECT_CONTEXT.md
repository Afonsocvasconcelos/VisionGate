# VisionGate repository context

Last updated: 2026-08-23

This is the maintainer handoff for VisionGate. It records the requirements, architecture, data, safety rules, and Windows workflows. Never add real camera URLs, passwords, device keys, tokens, public hostnames, embeddings, or personal images to this file.

## Product in one minute

VisionGate is a Windows-first smart access and automation server. It:

- Reads any number of independent RTSP camera streams.
- Detects and tracks `person`, `car`, `motorcycle`, and `bicycle` with YOLO11 and ByteTrack.
- Matches visual appearance against multiple reviewed samples per authorized identity.
- Imports every visible eWeLink device and exposes only capability-validated controls.
- Runs editable, persistent automation DAGs from camera, eWeLink, schedule, or manual triggers.
- Keeps the daily dashboard limited to camera, selected automation, identities, and essential status controls.
- Serves direct HTTP on port `83` for PC, LAN, and router-forwarded access.

The known door hardware is a SONOFF 4CH Pro R2, but no device has a special role. An automation chooses the device, channel, and command for every action.

## Requirements that must stay true

- Every camera detects, tracks, reconnects, and reports status independently.
- Camera URLs, camera credentials, recognition settings, eWeLink devices/channels, timing, branding, automations, graph layouts, and each automation's selected dashboard modules/order persist.
- Login credentials are file-only. The website must never offer password changes. Passwords may be any non-empty value within the configurator limit.
- The UI must work at `320px`, with touch, keyboard, visible focus, and concise language.
- Stats, routine polling, and detector diagnostics belong in the console or APIs, not the daily dashboard.
- CPU-only and integrated-GPU computers are supported; an NVIDIA GPU is optional.
- Direct HTTP port `83` is an explicit user requirement. Do not add HTTPS, Caddy, proxy headers, port `8000`, or other public ports unless the user changes it.
- Never infer physical door position from a momentary relay command. A separate position sensor is required.
- Retain the door's physical obstruction protection and independent safety timeout.

## Runtime map

```text
RTSP cameras ─> independent capture + YOLO/ByteTrack + ReID workers ─> event bus
eWeLink REST/WebSocket ──────────────────────────────────────────────> event bus
schedule clock + manual run ─────────────────────────────────────────> event bus
event bus ─> validated automation DAGs ─> camera / eWeLink device actions
                         │
                         └─> sanitized run history

Browser <─HTTP:83─> FastAPI <─> VisionManager <─> SQLite
```

The process is intentionally one Uvicorn worker. Sessions and the event bus are in memory; multiple web workers would require shared session and event storage.

### Main modules

| Path | Responsibility |
|---|---|
| `app.py` | FastAPI routes, middleware, camera workers, device orchestration |
| `core.py` | SQLite schema/migration, cameras, identities/samples, matching, LAN protocol |
| `automation.py` | Graph contract, validation, schedules, execution, waits, branches, run history |
| `enrollment.py` | Temporary 4 FPS recording, review frames, sample commit, cleanup |
| `ewelink_cloud.py` | Account import, optional developer QR flow, cloud REST/WebSocket helpers |
| `ewelink_devices.py` | Persistent inventory, capabilities, live updates, LAN/cloud actions |
| `auth.py` | File-only scrypt login, throttling, CSRF, and server-side sessions |
| `static/` | Shared responsive design, dashboard, and desktop/phone automation editors |

## Vision and recognition

1. OpenCV reads a credential-composed RTSP URL and reconnects after failure.
2. YOLO tracks only the four supported classes using `bytetrack.yaml` and persistent IDs.
3. MobileNet V3 Small features are combined with spatial color/edge descriptors.
4. Per-track embeddings are smoothed over time.
5. Matching compares only samples with the same object class.
6. Each identity receives its best sample score. It must pass the similarity threshold, beat the second identity by the lookalike margin, repeat for the required confirmations, and pass cooldown.
7. A confirmed transition emits an authorized-camera event. It no longer calls the door directly.

Automatic performance mode uses CUDA when available. On CPU it selects the nano detector, caps inference size, skips alternate frames, and limits PyTorch threads. Low-power mode forces the lighter path.

Visual ReID is appearance matching, not biometric proof. Clothing, lighting, viewpoint, occlusion, similar vehicles, and appearance changes can cause false accepts or misses.

## Identity enrollment

The dashboard uses record → review → select → save:

- Capture is temporary JPEG at 4 FPS, maximum 120 seconds and 960px width.
- Detection boxes include normalized coordinates, track ID, class, confidence, and a server-side embedding.
- The browser receives thumbnails and metadata, never raw embeddings.
- Samples can create a new identity or append to one with the same class.
- Near-duplicates are skipped; each identity supports up to 64 samples.
- Individual samples can be removed except the final sample.
- Temporary data is removed on commit, cancel, 60-minute inactivity, shutdown, or startup.
- There is no enrollment-recording download or permanent footage archive.

## Automations

Automations are versioned, acyclic JSON graphs shared by the runtime and both editors.

The dashboard is derived from the selected graph. It offers modules only for cameras, eWeLink devices, and a manual trigger actually used by that automation. Users can remove, restore, drag, or move modules, and SQLite stores a separate order for every automation. Switching the selector replaces the module set. Manual modules provide a hardware-free dry run and a confirmed live run. Disabling an automation pauses its automatic triggers but does not block an explicit authenticated manual run.

### Triggers

- Authorized identity presence, configured as present or absent.
- Supported object-class presence, configured as present or absent.
- Camera connection, configured as online or offline.
- eWeLink property changes or connection state, configured as online or offline.
- Manual run.
- **Schedule activator:** a chosen local time every day or selected weekdays, or every chosen number of minutes, hours, or days.

Schedule examples include every day at `03:00` and every `3` minutes. Schedules store an IANA time zone and the next UTC occurrence. Missed occurrences are skipped after downtime. DST fallback fires once; a missing spring-forward time runs at the next valid minute.

### Conditions, steps, and actions

- Conditions use fixed typed fields/operators; there is no script or raw expression input.
- Rules can select a specific saved identity, individual eWeLink channels such as `channel_1`, device online state, or other known scalar device properties.
- Edges can wait `0–86,400` seconds and set run-local scalar variables.
- Actions control an explicitly selected eWeLink device/channel/capability, camera enable state, or the application log.
- Failure and false outcomes can use separate edges.
- Dry runs never move hardware.
- A confirmed live manual run is required before hardware actions execute.

Each automation allows 1–16 concurrent runs, default 4. Excess runs are recorded as dropped. Commands to the same physical device are serialized. Active runs become canceled after restart; waits never resume stale hardware commands. The latest 1,000 run summaries are retained with sensitive values removed.

### Example door access automation

The editable default graph has two paths:

1. Authorized presence `true` on any camera → pulse the selected device's open channel.
2. Authorized presence `false` → wait → count authorized targets across every camera → pulse the selected close channel only if the count is still zero.

A manual open does not activate this close flow. An authorized target returning on either camera prevents the pending close.

## eWeLink behavior

- Ordinary eWeLink account import uses the open-source SonoffLAN-compatible identity; no developer account, Home Assistant, or MQTT is required.
- Account passwords are used once and never saved. Account/developer authorization can start only from `http://127.0.0.1:83`.
- Every returned device is upserted. Missing devices become unavailable instead of being deleted.
- A live cloud connection receives state changes; REST inventory reconciles every 60 seconds; reconnect uses bounded exponential backoff.
- Supported commands prefer encrypted LAN control and fall back to cloud.
- Capabilities are typed: channels/switches, momentary buttons, lights, covers, bounded numbers, enums, sensors, and online state. Light on/off, brightness, and reported RGB fields use validated controls. Cover commands and positions follow the reported DualR3, Zigbee, KingArt, or T5 protocol family; raw commands are never accepted.
- Unknown properties are read-only diagnostics. Secret-shaped properties are neither returned nor emitted as events.
- Every manual/test action is server-validated, explicitly confirmed, serialized per device, and immediately reports success or failure.

On the 4CH Pro R2, idle relay state does not prove physical door position. Manual device controls and automation actions validate the selected capability and channel before sending a command.

## Persistence and migration

SQLite remains the only database. `data/whitelist.db` contains:

- `profiles` and `profile_samples`
- `cameras` and `settings`
- `ewelink_devices`
- `automations` and `automation_runs`
- `events`

Schema migration is additive and transactional. Before the original automation/schema migration, SQLite creates and integrity-checks `data/whitelist.db.pre-automation.bak`. Current startup migration converts paired Boolean triggers, legacy device actions, typed conditions, edge endpoints, and old graph names while preserving layouts, waits, and enabled state.

Runtime files intentionally excluded from Git:

| Location | Contents |
|---|---|
| `.env` | Login hash, session options, public/trusted hosts, first-run defaults |
| `data/` | SQLite, optional logo, temporary enrollment directory |
| `backups/` | Timestamped pre-update copies of `.env` and `data/` |
| `info.md` | Legacy first-camera bootstrap, possibly containing credentials |
| `.venv/`, `*.pt` | Rebuildable Python runtime and models |

Camera passwords, eWeLink keys, and cloud authorization are currently protected by filesystem access, not encrypted at rest. Protect the PC and backups.

## Web and authentication

Supported addresses:

- PC: `http://127.0.0.1:83`
- LAN: `http://PC-LAN-IP:83`
- Public: `http://PUBLIC-IP-OR-DDNS:83`

HTTP does not encrypt passwords, sessions, video, or door commands. This is an explicitly accepted deployment risk, not a security guarantee.

Controls that must remain:

- Salted scrypt password hash; no plaintext dashboard password storage.
- Generic login errors and client/account rate limiting.
- Server-side sessions with idle and absolute expiry.
- HttpOnly, SameSite=Strict cookie; Secure only when explicitly deployed behind HTTPS.
- Same-origin and per-session CSRF checks on state changes.
- Trusted-host checks, restrictive browser headers, no-store responses, bounded validation.
- Write-only camera passwords and device keys in web APIs.
- No credentials in HTML, browser storage, automation graphs, logs, or run history.

`Configure Online Access.bat` validates a public IPv4/DDNS name, trusts it, opens Windows Firewall TCP 83, and prints the router-forwarding steps. CGNAT prevents ordinary inbound forwarding.

## Windows workflows

- `Install VisionGate.bat`: finds or installs Python 3.11 with Winget, creates `.venv`, selects official CPU/CUDA PyTorch wheels, installs dependencies/models, compiles entry points, creates a shortcut, and launches.
- `Launch VisionGate.bat`: repairs missing dependencies, creates file-only login if absent, avoids duplicate port-83 servers, configures private firewall access, and opens the correct address.
- `Configure Login.bat`: changes username/password locally with masked input; restart required.
- `Configure Online Access.bat`: configures direct HTTP port 83 and the public trusted host.
- `Update VisionGate.bat`: creates a timestamped runtime backup, performs a fast-forward Git update when applicable, repairs dependencies/backend, and preserves all runtime data.

`VISIONGATE_BACKEND=CPU` or `CUDA` overrides automatic backend selection. A clean computer needs Windows Package Manager or a manual Python 3.11 installation.

## Repository checks

```powershell
.\Launch VisionGate.bat --check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile app.py auth.py automation.py core.py enrollment.py ewelink_cloud.py ewelink_devices.py
node --check static\dashboard.js
node --check static\automations.js
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\browser_ui_check.ps1
```

GitHub Actions runs the Python dependency, test, compilation, and installer-plan gates on Windows with the official CPU PyTorch wheels. Some tests deliberately produce relay, HTTP, or timeout warnings while proving failure handling.

Latest local release evidence on 2026-08-23: all `160` tests passed; dependency, launcher, compilation, and JavaScript checks passed; direct HTTP port `83` started successfully; real YOLO + MobileNet inference passed with CUDA disabled and on an NVIDIA GTX 1060; authenticated Edge runs verified automation switching, module removal/restoration/reordering persistence, a confirmed manual run, camera deletion, and zero overflow with `44px` controls at `320px`; the desktop and phone automation editors also passed their canvas/card checks.

## Known constraints

- Physical 4CH Pro R2 channel behavior must be verified while the real door can be observed safely.
- A contact sensor exposed to eWeLink as a `door` binary sensor is needed for authoritative open/closed position; the 4CH Pro R2 relays alone cannot provide it.
- Physical channel 1 open/channel 2 close actuation still requires an explicitly confirmed test while the real door is safe to move.
- Installer/updater validation on a separate clean Windows PC without Python is still required before publishing a release.
- eWeLink compatibility can change outside this repository.
- Direct public HTTP can be intercepted.
- Sessions and active execution are in memory and are lost/canceled on restart.
- Multiple high-resolution streams can exceed CPU capacity.
- VisionGate does not provide permanent footage recording, plate OCR, face recognition, notifications, MQTT, webhooks, arbitrary HTTP, plugins, or user code nodes.

## Change checklist

1. Keep the daily dashboard minimal and the phone layout usable.
2. Preserve per-camera isolation and global authorized-presence safety.
3. Keep graph/action validation, authentication, CSRF, trusted hosts, relay shutoff, and physical safeguards.
4. Keep credentials write-only and sanitize logs/run history.
5. Preserve `.env`, `data/`, and custom branding during install/update.
6. Never commit real runtime data or secrets; inspect full Git history before publication.
7. Add a regression check, run all gates, and update this document when behavior changes.
