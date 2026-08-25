# VisionGate

VisionGate is a Windows-first FastAPI application that reads multiple RTSP cameras, tracks people, cars, motorcycles, and bicycles with YOLO11 + ByteTrack, compares multi-sample MobileNet appearance descriptors, and runs visual automations for eWeLink devices.

Each enabled camera has its own independent detector and tracker. Cameras share authorized identities and can control any imported device through editable automations. Each automation keeps its own canvas layout, and the dashboard remembers which layout is selected. Enrollment recordings are temporary and are deleted after save, cancel, expiry, or restart.

SQLite stores cameras, settings, credentials, identity samples, imported eWeLink devices, automations, and recent run/event history in `data/whitelist.db`. A match must agree on object class, exceed the configured similarity threshold, remain clearly better than lookalike profiles, repeat across observations, and pass the opening cooldown.

## Install

Clone or download the repository, then double-click **Install VisionGate.bat**. It installs Python 3.11 when needed, creates an isolated environment, installs every dependency and model, creates a desktop shortcut, and starts VisionGate. On the first launch it asks you to choose the dashboard username and password. Existing settings and data are preserved if the installer is run again.

The installer automatically uses NVIDIA CUDA when a working NVIDIA GPU is present. PCs with Intel/AMD integrated graphics or no dedicated GPU receive the official CPU-only PyTorch build. **Automatic** performance mode uses YOLO11 Nano, a smaller input, and frame skipping on CPU-only PCs; choose **Full quality** only when the hardware can keep up. To force CPU mode even on an NVIDIA PC, run this from Command Prompt before installing:

```bat
set VISIONGATE_BACKEND=CPU
"Install VisionGate.bat"
```

The hardware-specific packages follow PyTorch's official [CPU/CUDA installation indexes](https://pytorch.org/get-started/locally/). If Python is absent, the installer uses Microsoft's documented [Windows Package Manager](https://learn.microsoft.com/en-us/windows/package-manager/winget/install).

To check an existing installation without starting the server:

```bat
"Launch VisionGate.bat" --check
```

## Update

Double-click **Update VisionGate.bat**. It first saves `.env` and `data/` under `backups/pre-update-*`, then downloads a safe fast-forward update for Git checkouts and upgrades compatible dependencies. Login, cameras, identities, devices, automations, branding, and settings are preserved. Standalone ZIP copies receive dependency repair but need a fresh ZIP for application-code updates.

## Connect the SONOFF 4CH Pro R2

No Home Assistant, developer account, or MQTT broker is required. VisionGate prefers encrypted direct-LAN commands and falls back to eWeLink cloud control when the relay has no reachable LAN address.

1. Pair the 4CH Pro R2 in the ordinary eWeLink app and install any offered firmware update.
2. On the VisionGate PC, open `http://127.0.0.1:83`.
3. Select **Settings > Devices > Import device from eWeLink**.
4. Enter the ordinary eWeLink account and password, then choose **Sign in and find devices**. The password is used once and is never saved.
5. VisionGate saves every device returned by the account. A later sign-in refreshes that inventory and marks removed devices unavailable.
6. Use the device card's **Pulse** control on channels `1` and `2` while the door can be observed safely.
7. In **Automations**, drag a Trigger and Action to the canvas. Set the trigger to authorized presence `true`, then set the action to the 4CH Pro R2, channel `1`, and a short pulse.
8. For closing, connect authorized presence `false` through a **Wait** block and an authorized-count condition to a channel `2` pulse. Physical obstruction protection and an independent timeout remain required.

VisionGate refreshes the persistent eWeLink inventory once per minute. The 4CH Pro R2's momentary relays cannot sense physical door position; add a contact sensor if physical open/closed state is required.

The account importer uses the open-source [SonoffLAN](https://github.com/AlexxIT/SonoffLAN) compatibility identity. Official developer QR login and manual device-key entry remain available as fallbacks.

For credential safety, eWeLink importer sign-in is enabled only at `http://127.0.0.1:83` on the VisionGate PC. Keep the door's physical obstruction sensors and independent safe timeout enabled; camera-based auto-close is not a substitute for either.

## Run

Double-click **Launch VisionGate.bat**. It checks the installation, requests a one-time Windows private-network firewall rule, starts the server, and opens the dashboard.

The console prints the current `http://192.168.x.x:83` address for phones and other local devices.

Choose an automation on the responsive dashboard to show only the cameras, eWeLink devices, and manual control used by that automation. Choose **Customize** to remove, restore, drag, or move those controls; the order is saved separately for every automation. Every activator is a start node: its matching event follows the same execution path as Manual. A manual activator adds **Test safely** (no hardware changes) and a confirmed **Run now**. Disabling an automation pauses automatic triggers; explicit manual runs remain available.

The separate **Automations** page provides a desktop node canvas and an equivalent phone card editor. Authorized identities remain available beside the selected automation; diagnostics and routine history stay out of the everyday screen.

Add a **Schedule activator** to run an automation at a local time every day or on selected weekdays, or repeat it every chosen number of minutes, hours, or days. Each schedule saves its time zone and shows the next run.

The equivalent manual command is:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 83 --no-proxy-headers
```

On first run, the existing `info.md` stream is imported. Afterwards, use **Add camera** or **Settings** to edit stream URLs, camera credentials, recognition parameters, devices, app name, logo, and color palette. Use **Test connection** in the camera editor to validate an RTSP address before saving it.

The dashboard login cannot be changed from the website. Double-click **Configure Login.bat** on the VisionGate PC, choose a new username/password, then restart VisionGate. Only the username and a salted scrypt password hash are kept in `.env`; the password itself is never stored. Sessions are server-side, expire after 30 idle minutes or 8 total hours, and use HttpOnly/SameSite cookies, CSRF validation, login throttling, and restrictive browser security headers.

VisionGate uses direct HTTP on port 83 as requested. HTTP cannot protect credentials, camera video, sessions, or door commands from interception; use a unique password and keep the application updated.

## Access VisionGate over the internet

You need the router's public IPv4 address or a DDNS name. If the internet provider uses CGNAT and the router has no public IP, ordinary port forwarding cannot work.

1. Double-click **Configure Online Access.bat** and enter the public IPv4 address or DDNS name.
2. Approve the Windows Firewall request.
3. Reserve this PC's local IP address in the router so it does not change.
4. In the router, forward external TCP port `83` to this PC's TCP port `83`.
5. Remove old VisionGate forwards for ports `80`, `443`, and `8000`; do not enable DMZ mode.
6. Restart VisionGate and test the displayed `http://address:83` URL from a phone with Wi-Fi disabled.

Rerun **Configure Online Access.bat** if the public address changes. Keep **Update VisionGate.bat** current because this endpoint controls a physical door.

## Enroll and calibrate

1. Select a camera and wait for it to come online.
2. Select **Record samples**, move through useful angles, then stop the recording.
3. Review the timeline and select clear detected boxes.
4. Add them to an existing identity or enter a new name, then save.
5. Open an identity to review or remove individual samples. Its final sample cannot be removed.
6. Raise **Match threshold** if unknown targets match; raise **Lookalike margin** if similar identities are confused.

Older single-sample profiles migrate automatically and remain usable. Add varied samples when appearance or viewing angle changes. Clothing and general appearance are not identity-grade biometrics, so use another access factor wherever a false acceptance would create a serious safety or security risk.

## Checks

```powershell
.\Launch VisionGate.bat --check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile app.py auth.py automation.py core.py enrollment.py ewelink_cloud.py ewelink_devices.py
node --check static\dashboard.js
node --check static\automations.js
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\browser_ui_check.ps1
```

`.env.example` contains login/session options and optional first-run defaults. Device settings belong in the app; login changes stay file-only through **Configure Login.bat**. Tracking follows the [Ultralytics tracking API](https://docs.ultralytics.com/modes/track/).

For repository safety, `.env`, `info.md`, `data/`, model weights, virtual environments, databases, and editor caches are ignored. Never commit real RTSP credentials, eWeLink device keys, access tokens, whitelist embeddings, or event history. See [SECURITY.md](SECURITY.md) before making a repository public.
