const $ = id => document.getElementById(id);
let system = {cameras: [], profiles: [], door: {}, threshold: 0};
let config = {cameras: [], settings: {}};
let selectedCameraId = Number(localStorage.getItem("cameraId")) || null;
let loadedFeedId = null;
let enrolling = false;
let ewelinkImportSession = null;
let ewelinkImportedDevices = [];
let toastTimer;
let csrfToken = "";
let brandName = "VisionGate";

function applyBrand(brand) {
  brandName = brand.name;
  document.documentElement.dataset.palette = brand.palette;
  document.querySelectorAll("[data-brand-name]").forEach(item => item.textContent = brand.name);
  document.querySelectorAll("[data-brand-logo]").forEach(item => item.src = brand.logo);
  document.title = brand.name;
}

async function loadBrand() { applyBrand(await api("/api/brand")); }

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.add("show");
  toastTimer = setTimeout(() => $("toast").classList.remove("show"), 3400);
}

async function api(url, options = {}) {
  const request = {...options, headers: new Headers(options.headers || {})};
  if (!["GET", "HEAD", "OPTIONS"].includes((request.method || "GET").toUpperCase())) {
    request.headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(url, request);
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Your session expired. Sign in again.");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

async function loadSession() {
  const session = await api("/api/auth/session");
  csrfToken = session.csrf_token;
}

function selectedCamera() {
  return system.cameras.find(camera => camera.id === selectedCameraId);
}

function syncCameraSelector() {
  const ids = system.cameras.map(camera => camera.id);
  if (!ids.includes(selectedCameraId)) selectedCameraId = ids[0] ?? null;
  const select = $("cameraSelect");
  select.replaceChildren();
  for (const camera of system.cameras) {
    const option = document.createElement("option");
    option.value = camera.id;
    option.textContent = camera.name;
    select.append(option);
  }
  select.value = String(selectedCameraId ?? "");
  select.disabled = !ids.length;
  if (selectedCameraId !== loadedFeedId) {
    loadedFeedId = selectedCameraId;
    $("feed").src = selectedCameraId ? `/video/${selectedCameraId}?v=${Date.now()}` : "";
    localStorage.setItem("cameraId", selectedCameraId ?? "");
  }
  $("editCamera").disabled = !selectedCameraId;
  $("enroll").disabled = !selectedCameraId;
}

function renderProfiles(profiles) {
  const list = $("profiles");
  list.replaceChildren();
  $("profileCount").textContent = profiles.length;
  if (!profiles.length) {
    list.innerHTML = '<div class="empty">No authorized targets</div>';
    return;
  }
  for (const profile of profiles) {
    const row = document.createElement("div");
    row.className = "profile";
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = profile.label.slice(0, 3);
    const info = document.createElement("div");
    info.className = "profile-info";
    const name = document.createElement("strong");
    name.textContent = profile.name;
    const label = document.createElement("small");
    label.textContent = profile.label;
    const remove = document.createElement("button");
    remove.className = "remove";
    remove.type = "button";
    remove.title = `Remove ${profile.name}`;
    remove.setAttribute("aria-label", `Remove ${profile.name}`);
    remove.textContent = "×";
    remove.onclick = async () => {
      if (!confirm(`Remove ${profile.name} from the whitelist?`)) return;
      try { await api(`/api/whitelist/${profile.id}`, {method: "DELETE"}); await refreshAll(); }
      catch (error) { toast(error.message); }
    };
    info.append(name, label);
    row.append(avatar, info, remove);
    list.append(row);
  }
}

function renderSystem() {
  syncCameraSelector();
  const camera = selectedCamera();
  const online = camera?.camera === "connected" && camera?.vision.startsWith("running");
  const doorReady = Boolean(system.door.configured);
  const doorBusy = Boolean(system.door.busy);
  const state = doorBusy ? "busy" : doorReady ? system.door.state || "unknown" : "unconfigured";
  $("doorStateBox").dataset.state = state;
  $("doorState").textContent = {
    open: "Open",
    closed: "Closed",
    busy: "Changing...",
    unconfigured: "Not configured",
    unknown: "Unknown"
  }[state] || "Unknown";
  const timing = $("doorTiming");
  timing.hidden = !system.door.auto_close_armed;
  timing.textContent = system.door.auto_close_armed ? `Closing in ${Math.ceil(system.door.auto_close_remaining)}s` : "";
  const check = $("doorCheck");
  check.hidden = !doorReady;
  check.textContent = system.door.state_check_error ? "Relay check unavailable" : system.door.last_state_check ? "Relay checked" : "Checking relay";
  check.title = system.door.state_check_error || (system.door.last_state_check ? new Date(system.door.last_state_check * 1000).toLocaleString() : "");
  for (const id of ["doorOpenCard", "doorCloseCard"]) $(id).disabled = !doorReady || doorBusy;
  $("dot").className = `dot${online ? " ok" : camera?.vision === "failed" ? " bad" : ""}`;
  $("connection").textContent = online ? camera.name : camera ? "Camera offline" : "No camera";
  renderProfiles(system.profiles);
}

function renderCameraList() {
  const list = $("cameraList");
  list.replaceChildren();
  if (!config.cameras.length) {
    list.innerHTML = '<div class="empty">No cameras configured</div>';
    return;
  }
  for (const camera of config.cameras) {
    const row = document.createElement("div");
    row.className = "camera-row";
    const info = document.createElement("div");
    info.className = "row-info";
    const name = document.createElement("strong");
    name.textContent = camera.name;
    const url = document.createElement("small");
    url.textContent = `${camera.enabled ? "Enabled" : "Disabled"} · ${camera.stream_url}`;
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn small";
    edit.textContent = "Edit";
    edit.onclick = () => openCameraDialog(camera);
    info.append(name, url);
    row.append(info, edit);
    list.append(row);
  }
}

function fillSettings() {
  const s = config.settings;
  $("appName").value = s.app_name ?? "VisionGate";
  $("brandPalette").value = s.brand_palette ?? "teal";
  $("performanceMode").value = s.performance_mode ?? "auto";
  $("yoloModel").value = s.yolo_model;
  $("yoloImgsz").value = s.yolo_imgsz;
  $("detectionConfidence").value = s.detection_confidence;
  $("jpegQuality").value = s.jpeg_quality;
  $("matchThreshold").value = s.match_threshold;
  $("matchMargin").value = s.match_margin;
  $("matchConfirmations").value = s.match_confirmations;
  $("embedEvery").value = s.embed_every;
  $("cooldown").value = s.open_cooldown_seconds;
  $("ewelinkModel").value = s.ewelink_model ?? "SONOFF 4CH Pro R2";
  $("ewelinkHost").value = s.ewelink_host ?? "";
  $("ewelinkPort").value = s.ewelink_port ?? 8081;
  $("ewelinkDeviceId").value = s.ewelink_device_id ?? "";
  $("ewelinkDeviceKey").value = s.ewelink_device_key ?? "";
  $("ewelinkOpenChannel").value = s.ewelink_open_channel ?? 1;
  $("ewelinkCloseChannel").value = s.ewelink_close_channel ?? 2;
  $("pulseSeconds").value = s.pulse_seconds;
  $("autoCloseSeconds").value = s.auto_close_seconds ?? 5;
  renderCameraList();
}

async function loadConfig() { config = await api("/api/config"); fillSettings(); }
async function refresh() {
  try { system = await api("/api/status"); renderSystem(); }
  catch (_) { $("dot").className = "dot bad"; $("connection").textContent = "Server unavailable"; }
}
async function refreshAll() { await Promise.all([refresh(), loadConfig()]); }

function openCameraDialog(camera = null) {
  $("cameraDialogTitle").textContent = camera ? "Edit camera" : "Add camera";
  $("cameraId").value = camera?.id ?? "";
  $("cameraName").value = camera?.name ?? "";
  $("cameraUrl").value = camera?.stream_url ?? "";
  $("cameraUsername").value = camera?.username ?? "";
  $("cameraPassword").value = camera?.password ?? "";
  $("cameraEnabled").checked = camera?.enabled ?? true;
  $("cameraTestStatus").textContent = "";
  $("deleteCamera").hidden = !camera;
  $("cameraDialog").showModal();
}

function cameraPayload() {
  return {name: $("cameraName").value.trim(), stream_url: $("cameraUrl").value.trim(), username: $("cameraUsername").value, password: $("cameraPassword").value, enabled: $("cameraEnabled").checked};
}

document.querySelectorAll("[data-close]").forEach(button => {
  button.onclick = () => $(button.dataset.close).close();
});
document.querySelectorAll(".tab").forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(item => { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", item === tab); });
    document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === tab.dataset.tab));
  };
});

$("cameraSelect").onchange = () => { selectedCameraId = Number($("cameraSelect").value); loadedFeedId = null; renderSystem(); };
$("addCamera").onclick = () => openCameraDialog();
$("settingsAddCamera").onclick = () => openCameraDialog();
$("editCamera").onclick = () => openCameraDialog(config.cameras.find(camera => camera.id === selectedCameraId));
$("openSettings").onclick = async () => {
  try { await Promise.all([loadConfig(), loadEwelinkSetup()]); $("settingsDialog").showModal(); }
  catch (error) { toast(error.message); }
};
$("logout").onclick = async () => {
  $("logout").disabled = true;
  try { await api("/api/auth/logout", {method: "POST"}); }
  finally { window.location.replace("/login"); }
};

$("cameraForm").onsubmit = async event => {
  event.preventDefault();
  const id = $("cameraId").value;
  const body = cameraPayload();
  try {
    const camera = await api(id ? `/api/cameras/${id}` : "/api/cameras", {method: id ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    selectedCameraId = camera.id;
    loadedFeedId = null;
    $("cameraDialog").close();
    toast(`${camera.name} saved`);
    await refreshAll();
  } catch (error) { toast(error.message); }
};

$("testCameraConnection").onclick = async () => {
  const button = $("testCameraConnection");
  button.disabled = true;
  $("cameraTestStatus").textContent = "Connecting to the RTSP stream…";
  try {
    const result = await api("/api/cameras/test", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cameraPayload())});
    $("cameraTestStatus").textContent = `Connected · ${result.width} × ${result.height}`;
  } catch (error) {
    $("cameraTestStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

$("deleteCamera").onclick = async () => {
  const id = Number($("cameraId").value);
  const camera = config.cameras.find(item => item.id === id);
  if (!camera || !confirm(`Delete ${camera.name}?`)) return;
  try {
    await api(`/api/cameras/${id}`, {method: "DELETE"});
    $("cameraDialog").close();
    selectedCameraId = null;
    loadedFeedId = null;
    toast("Camera deleted");
    await refreshAll();
  } catch (error) { toast(error.message); }
};

$("settingsForm").onsubmit = async event => {
  event.preventDefault();
  const body = {
    app_name: $("appName").value.trim(),
    brand_palette: $("brandPalette").value,
    performance_mode: $("performanceMode").value,
    yolo_model: $("yoloModel").value,
    yolo_imgsz: Number($("yoloImgsz").value),
    detection_confidence: Number($("detectionConfidence").value),
    jpeg_quality: Number($("jpegQuality").value),
    match_threshold: Number($("matchThreshold").value),
    match_margin: Number($("matchMargin").value),
    match_confirmations: Number($("matchConfirmations").value),
    embed_every: Number($("embedEvery").value),
    open_cooldown_seconds: Number($("cooldown").value),
    ewelink_model: $("ewelinkModel").value.trim(),
    ewelink_host: $("ewelinkHost").value.trim(),
    ewelink_port: Number($("ewelinkPort").value),
    ewelink_device_id: $("ewelinkDeviceId").value.trim(),
    ewelink_device_key: $("ewelinkDeviceKey").value.trim(),
    ewelink_open_channel: Number($("ewelinkOpenChannel").value),
    ewelink_close_channel: Number($("ewelinkCloseChannel").value),
    pulse_seconds: Number($("pulseSeconds").value),
    auto_close_seconds: Number($("autoCloseSeconds").value)
  };
  try {
    await api("/api/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const logo = $("appLogo").files[0];
    if (logo) {
      const image = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("Could not read the logo"));
        reader.readAsDataURL(logo);
      });
      await api("/api/branding/logo", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({image})});
      $("appLogo").value = "";
    }
    await loadBrand();
    $("settingsDialog").close();
    toast("Settings saved; affected cameras are restarting");
    await refreshAll();
  } catch (error) { toast(error.message); }
};

function ewelinkDeveloperCredentials() {
  const app_id = $("ewelinkAppId").value.trim();
  const app_secret = $("ewelinkAppSecret").value.trim();
  if (!app_id || !app_secret) throw new Error("Enter the eWeLink Developer App ID and App Secret first");
  return {app_id, app_secret};
}

async function loadEwelinkSetup() {
  const setup = await api("/api/ewelink/oauth/setup");
  $("ewelinkCallback").value = setup.callback_url;
  $("ewelinkQrLogin").disabled = !setup.local_login_allowed;
  $("ewelinkPasswordLogin").disabled = !setup.local_login_allowed;
  if (!setup.local_login_allowed) $("ewelinkImportStatus").textContent = "For account safety, open http://127.0.0.1:8000 on this PC to import an eWeLink device.";
}

function showImportedDevices(sessionId, devices) {
  ewelinkImportSession = sessionId;
  ewelinkImportedDevices = devices;
  const select = $("ewelinkImportDevice");
  select.replaceChildren();
  for (const device of devices) {
    const option = document.createElement("option");
    option.value = device.id;
    option.textContent = `${device.name} · ${device.model}${device.online ? " · online" : " · offline"}${device.host ? ` · ${device.host}` : ""}`;
    select.append(option);
  }
  $("ewelinkImportResult").classList.add("show");
  $("ewelinkImportStatus").textContent = `Found ${devices.length} compatible device${devices.length === 1 ? "" : "s"}. Select the door relay below.`;
  syncImportedAddress();
}

function syncImportedAddress() {
  const device = ewelinkImportedDevices.find(item => item.id === $("ewelinkImportDevice").value);
  if (device?.host) { $("ewelinkHost").value = device.host; $("ewelinkPort").value = device.port || 8081; }
}

$("ewelinkImportDevice").onchange = syncImportedAddress;
async function waitForEwelink(sessionId) {
  for (let attempt = 0; attempt < 300; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const status = await api(`/api/ewelink/import/${encodeURIComponent(sessionId)}`);
    if (status.status === "ready") { showImportedDevices(sessionId, status.devices); return; }
    if (status.status === "error") throw new Error(status.error);
  }
  throw new Error("eWeLink authorization timed out; start it again");
}

$("copyEwelinkCallback").onclick = async () => {
  const input = $("ewelinkCallback");
  try { await navigator.clipboard.writeText(input.value); toast("Callback URL copied"); }
  catch (_) { input.select(); document.execCommand("copy"); toast("Callback URL copied"); }
};
$("ewelinkQrLogin").onclick = async () => {
  let popup;
  try {
    const credentials = ewelinkDeveloperCredentials();
    popup = window.open("about:blank", "ewelinkOAuth", "width=520,height=720");
    if (!popup) throw new Error(`Allow pop-ups for ${brandName}, then try again`);
    popup.document.body.textContent = "Opening secure eWeLink authorization…";
    $("ewelinkImportStatus").textContent = "Starting eWeLink QR authorization…";
    const started = await api("/api/ewelink/oauth/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(credentials)});
    $("ewelinkAppSecret").value = "";
    popup.location.replace(started.authorization_url);
    await waitForEwelink(started.session_id);
  } catch (error) { popup?.close(); $("ewelinkImportStatus").textContent = error.message; toast(error.message); }
};
$("ewelinkPasswordLogin").onclick = async () => {
  try {
    const body = {account: $("ewelinkAccount").value.trim(), password: $("ewelinkAccountPassword").value, country_code: $("ewelinkCountryCode").value.trim(), region: $("ewelinkRegion").value};
    if (!body.account || !body.password) throw new Error("Enter the eWeLink account and password");
    $("ewelinkImportStatus").textContent = "Signing in and finding compatible devices…";
    const result = await api("/api/ewelink/import/password", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    showImportedDevices(result.session_id, result.devices);
  } catch (error) { $("ewelinkImportStatus").textContent = error.message; toast(error.message); }
  finally { $("ewelinkAccountPassword").value = ""; }
};
$("ewelinkApplyImport").onclick = async () => {
  const device = ewelinkImportedDevices.find(item => item.id === $("ewelinkImportDevice").value);
  const host = $("ewelinkHost").value.trim();
  if (!device) return toast("Select an eWeLink device");
  const body = {session_id: ewelinkImportSession, device_id: device.id, host, port: Number($("ewelinkPort").value) || 8081, open_channel: Number($("ewelinkOpenChannel").value), close_channel: Number($("ewelinkCloseChannel").value), pulse_seconds: Number($("pulseSeconds").value)};
  try {
    const result = await api("/api/ewelink/import/apply", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    await refreshAll();
    $("ewelinkImportResult").classList.remove("show");
    $("ewelinkImportStatus").textContent = `${result.name} imported. ${brandName} now uses ${host ? "LAN with cloud fallback" : "cloud control"} on channels ${body.open_channel}/${body.close_channel}.`;
    toast("eWeLink door configured");
  } catch (error) { $("ewelinkImportStatus").textContent = error.message; toast(error.message); }
};

$("enroll").onclick = () => {
  enrolling = !enrolling;
  $("videoWrap").classList.toggle("enrolling", enrolling);
  $("enroll").textContent = enrolling ? "Cancel enrollment" : "Enroll identity";
};
$("feed").onclick = event => {
  if (!enrolling || !selectedCameraId) return;
  const box = event.currentTarget.getBoundingClientRect();
  $("enrollmentX").value = (event.clientX - box.left) / box.width;
  $("enrollmentY").value = (event.clientY - box.top) / box.height;
  $("enrollmentName").value = "";
  $("enrollmentDialog").showModal();
  $("enrollmentName").focus();
};
$("enrollmentForm").onsubmit = async event => {
  event.preventDefault();
  try {
    const profile = await api(`/api/cameras/${selectedCameraId}/whitelist`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({x: Number($("enrollmentX").value), y: Number($("enrollmentY").value), name: $("enrollmentName").value.trim()})});
    $("enrollmentDialog").close();
    enrolling = false;
    $("videoWrap").classList.remove("enrolling");
    $("enroll").textContent = "Enroll identity";
    toast(`${profile.name} added as ${profile.label}`);
    await refreshAll();
  } catch (error) { toast(error.message); }
};

async function testDoor(action) {
  if (!confirm(`${action === "open" ? "Open" : "Close"} the door?`)) return;
  try {
    await api("/api/door/test", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({confirm: true, action})});
    await refreshAll();
    toast(`Door ${action} command completed`);
  } catch (error) { await refreshAll(); toast(error.message); }
}
$("doorOpenCard").onclick = () => testDoor("open");
$("doorCloseCard").onclick = () => testDoor("close");

async function start() {
  await Promise.all([loadSession(), loadBrand()]);
  await refreshAll();
  setInterval(refresh, 2000);
}
start().catch(error => toast(error.message));
