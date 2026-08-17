# VisionGate

VisionGate is a local FastAPI application that reads multiple RTSP cameras, tracks people, cars, motorcycles, and bicycles with YOLO11 + ByteTrack, compares spatial MobileNet appearance descriptors with a click-to-enroll whitelist, and controls one eWeLink door relay over LAN or cloud.

Each enabled camera has its own independent detector and tracker. Cameras share the whitelist and door controller, so a match from either camera can open the same door. After an automatic open, either camera seeing an authorized target resets the configurable auto-close timer; when the last authorized target has been gone for that delay, VisionGate activates the close channel. Manual opens do not start this timer. Camera frames are not recorded.

SQLite stores camera settings and credentials, recognition settings, whitelist embeddings, and the latest 1,000 activity events in `data/whitelist.db`. A match must agree on object class, exceed the configured similarity threshold, remain clearly better than lookalike profiles, repeat across observations, and pass the opening cooldown.

## Install

Clone or download the repository, then double-click **Install VisionGate.bat**. It installs Python 3.11 when needed, creates an isolated environment, installs every dependency and model, creates a desktop shortcut, and starts VisionGate. On the first launch it asks you to choose the dashboard username and password. Existing settings and data are preserved if the installer is run again.

The installer automatically uses NVIDIA CUDA when a working NVIDIA GPU is present. PCs with Intel/AMD integrated graphics or no dedicated GPU receive the official CPU-only PyTorch build and run without GPU acceleration. On a CPU-only PC, choose **YOLO11 Nano** in Recognition settings if more speed is needed. To force CPU mode even on an NVIDIA PC, run this from Command Prompt before installing:

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

Double-click **Update VisionGate.bat**. It upgrades compatible dependencies, repairs the selected CPU/CUDA runtime, and never removes `data`, `.env`, camera settings, events, or whitelist profiles. For a Git checkout, it installs Git through Windows Package Manager when needed and downloads application updates with a safe fast-forward pull; standalone copies still receive dependency updates.

## Connect the SONOFF 4CH Pro R2

No Home Assistant, developer account, or MQTT broker is required. VisionGate prefers encrypted direct-LAN commands and falls back to eWeLink cloud control when the relay has no reachable LAN address.

1. Pair the 4CH Pro R2 in the ordinary eWeLink app and install any offered firmware update.
2. On the VisionGate PC, open `http://127.0.0.1:8000`.
3. Select **Settings > Door & eWeLink > Import device from eWeLink**.
4. Enter the ordinary eWeLink account and password, then choose **Sign in and find devices**. The password is used once and is never saved.
5. Choose the 4CH Pro R2 and select **Use selected device**. Keep open channel `1`, close channel `2`, and a short pulse unless this installation differs.
6. If discovery finds a LAN IP, reserve it in the router. Leaving the IP blank uses cloud control.
7. Use **Open door** and **Close door** while the door can be observed safely.

Set **Auto-close delay** in the same Door settings panel. The default is 5 seconds after the last authorized person or vehicle disappears; set it to `0` to disable automatic closing.

The dashboard's **Last known** door state is the latest successful command saved by VisionGate and survives restarts. The 4CH Pro R2's momentary open/close relays cannot sense physical door position; use a contact sensor if the app must detect movement made outside VisionGate.

The account importer uses the open-source [SonoffLAN](https://github.com/AlexxIT/SonoffLAN) compatibility identity. Official developer QR login and manual device-key entry remain available as fallbacks.

For credential safety, eWeLink importer sign-in is enabled only at `http://127.0.0.1:8000` on the VisionGate PC. Keep the door's physical obstruction sensors and independent safe timeout enabled; camera-based auto-close is not a substitute for either.

## Run

Double-click **Launch VisionGate.bat**. It checks the installation, requests a one-time Windows private-network firewall rule, starts the server, and opens the dashboard.

The console prints the current `http://192.168.x.x:8000` address for phones and other local devices.

The responsive interface contains the live camera, door controls, authorized identities, and settings. Diagnostics and event history stay out of the everyday screen. The applied usability and accessibility decisions are documented in [docs/UX_RESEARCH.md](docs/UX_RESEARCH.md).

The equivalent manual command is:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

On first run, the existing `info.md` stream is imported. Afterwards, use **Add camera** or **Settings** to edit stream URLs, camera credentials, recognition parameters, and door details. Use **Test connection** in the camera editor to validate an RTSP address before saving it.

The dashboard login cannot be changed from the website. Double-click **Configure Login.bat** on the VisionGate PC, choose a new username/password, then restart VisionGate. Only the username and a salted scrypt password hash are kept in `.env`; the password itself is never stored. Sessions are server-side, expire after 30 idle minutes or 8 total hours, and use HttpOnly/SameSite cookies, CSRF validation, login throttling, and restrictive browser security headers.

Keep VisionGate on a trusted private LAN and never expose port 8000 directly to the internet. For untrusted networks, put it behind HTTPS and set `VISIONGATE_SECURE_COOKIES=1` in `.env`; plain HTTP cannot protect credentials from a device already able to intercept that network.

## Enroll and calibrate

1. Select a camera and wait for its camera and recognition states to become online.
2. Select **Enroll identity**, then click inside a clear, fully visible tracked box and give it a unique name.
3. Walk or drive past the camera and watch the similarity shown on green matches.
4. Raise **Match threshold** if unknown targets match. Lower it carefully if genuine targets miss.
5. Raise **Lookalike margin** when two similar whitelist entries are confused.

Older profiles remain usable as legacy descriptors. Remove and then re-enroll profiles marked as legacy to gain spatial appearance matching. Clothing and general appearance are not identity-grade biometrics, so use a second access factor wherever a false acceptance would create a serious safety or security risk.

## Checks

```powershell
.\Launch VisionGate.bat --check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile app.py auth.py core.py ewelink_cloud.py
```

`.env.example` contains login/session options and optional first-run defaults. Device settings belong in the app; login changes stay file-only through **Configure Login.bat**. Tracking follows the [Ultralytics tracking API](https://docs.ultralytics.com/modes/track/).

For repository safety, `.env`, `info.md`, `data/`, model weights, virtual environments, databases, and editor caches are ignored. Never commit real RTSP credentials, eWeLink device keys, access tokens, whitelist embeddings, or event history. See [SECURITY.md](SECURITY.md) before making a repository public.
