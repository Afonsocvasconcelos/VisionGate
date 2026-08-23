const $ = id => document.getElementById(id);
let system = {cameras: [], profiles: [], door: {}, threshold: 0};
let config = {cameras: [], settings: {}};
let selectedCameraId = Number(localStorage.getItem("cameraId")) || null;
let loadedFeedId = null;
let enrolling = false;
let enrollmentSession = null;
let enrollmentReview = null;
let enrollmentSelection = new Map();
let enrollmentPoll = null;
let ewelinkImportSession = null;
let ewelinkImportedDevices = [];
let ewelinkDevices = [];
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
    label.textContent = `${profile.label} · ${profile.sample_count || 1} sample${(profile.sample_count || 1) === 1 ? "" : "s"}`;
    const manage = document.createElement("button");
    manage.className = "btn small";
    manage.type = "button";
    manage.textContent = "Samples";
    manage.onclick = () => openProfileSamples(profile);
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
    row.append(avatar, info, manage, remove);
    list.append(row);
  }
}

function renderSystem() {
  syncCameraSelector();
  const camera = selectedCamera();
  const online = camera?.camera === "connected" && camera?.vision.startsWith("running");
  const doorReady = Boolean(system.door.configured);
  const doorBusy = Boolean(system.door.busy);
  const state = doorBusy ? "changing" : doorReady ? system.door.state || "unknown" : "unconfigured";
  $("doorStateBox").dataset.state = state;
  $("doorState").textContent = {
    open: "Open",
    closed: "Closed",
    changing: "Changing…",
    unavailable: "Unavailable",
    unconfigured: "Not configured",
    unknown: "Unknown"
  }[state] || "Unknown";
  const check = $("doorCheck");
  check.hidden = !doorReady;
  const lastCommand = system.door.last_command ? ` · last command: ${system.door.last_command}` : "";
  const stateDevice = system.door.state_source === "binary_sensor:door" ? "Door sensor" : "Relay";
  check.textContent = system.door.state_check_error ? `${stateDevice} unavailable` : system.door.last_state_check ? `${stateDevice} checked${lastCommand}` : `Checking ${stateDevice.toLowerCase()}${lastCommand}`;
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
  $("ewelinkDeviceKey").value = "";
  $("ewelinkDeviceKey").placeholder = s.ewelink_device_key_configured ? "Saved · leave blank to keep" : "Required for LAN control";
  $("ewelinkOpenChannel").value = s.ewelink_open_channel ?? 1;
  $("ewelinkCloseChannel").value = s.ewelink_close_channel ?? 2;
  $("pulseSeconds").value = s.pulse_seconds;
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
  $("cameraPassword").value = "";
  $("cameraPassword").placeholder = camera?.password_configured ? "Saved · leave blank to keep" : "";
  $("cameraEnabled").checked = camera?.enabled ?? true;
  $("cameraTestStatus").textContent = "";
  $("deleteCamera").hidden = !camera;
  $("cameraDialog").showModal();
}

function cameraPayload(includeId = false) {
  const body = {name: $("cameraName").value.trim(), stream_url: $("cameraUrl").value.trim(), username: $("cameraUsername").value, password: $("cameraPassword").value, enabled: $("cameraEnabled").checked};
  if (includeId && $("cameraId").value) body.camera_id = Number($("cameraId").value);
  return body;
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
  try {
    await loadConfig();
    await Promise.all([loadEwelinkSetup(), loadEwelinkDevices()]);
    $("settingsDialog").showModal();
  }
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
    const result = await api("/api/cameras/test", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cameraPayload(true))});
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
    pulse_seconds: Number($("pulseSeconds").value)
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
  if (!setup.local_login_allowed) $("ewelinkImportStatus").textContent = "For account safety, open http://127.0.0.1:83 on this PC to import an eWeLink device.";
}

function deviceActionButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn small";
  button.textContent = label;
  button.onclick = action;
  return button;
}

async function runEwelinkDeviceAction(device, action, arguments_, label) {
  if (!confirm(`${label} on ${device.name}?`)) return;
  try {
    const result = await api(`/api/ewelink/devices/${encodeURIComponent(device.id)}/actions/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirm: true, arguments: arguments_})
    });
    ewelinkDevices = ewelinkDevices.map(item => item.id === device.id ? result : item);
    renderEwelinkDevices();
    toast(`${device.name}: ${label} completed`);
  } catch (error) {
    toast(error.message);
  }
}

function addChannelControls(container, device, capability) {
  const switches = device.state?.switches || [];
  for (const channel of capability.channels || []) {
    const row = document.createElement("div");
    row.className = "capability-row";
    const state = switches.find(item => item.outlet === channel - 1)?.switch || "unknown";
    const label = document.createElement("span");
    label.textContent = `Channel ${channel} · ${state}`;
    const actions = document.createElement("div");
    actions.append(
      deviceActionButton("On", () => runEwelinkDeviceAction(device, "switch", {channel, state: "on"}, `turn channel ${channel} on`)),
      deviceActionButton("Off", () => runEwelinkDeviceAction(device, "switch", {channel, state: "off"}, `turn channel ${channel} off`)),
      deviceActionButton("Pulse", () => runEwelinkDeviceAction(device, "button", {channel, pulse_seconds: Number($("pulseSeconds").value) || 1}, `pulse channel ${channel}`))
    );
    row.append(label, actions);
    container.append(row);
  }
}

function addValueControl(container, device, capability) {
  const row = document.createElement("div");
  row.className = "capability-row";
  const label = document.createElement("label");
  label.textContent = capability.id;
  if (["number_sensor", "binary_sensor"].includes(capability.type)) {
    const value = document.createElement("strong");
    value.textContent = String(device.state?.[capability.id] ?? "Unknown");
    row.append(label, value);
  } else if (capability.type === "number") {
    const input = document.createElement("input");
    input.type = "number";
    input.min = capability.minimum;
    input.max = capability.maximum;
    input.step = "any";
    input.value = device.state?.[capability.id] ?? capability.minimum;
    row.append(label, input, deviceActionButton("Set", () => runEwelinkDeviceAction(device, "number", {property: capability.id, value: Number(input.value)}, `set ${capability.id}`)));
  } else if (capability.type === "enum") {
    const select = document.createElement("select");
    for (const value of capability.options || []) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
    select.value = device.state?.[capability.id] ?? "";
    row.append(label, select, deviceActionButton("Set", () => runEwelinkDeviceAction(device, "enum", {property: capability.id, value: select.value}, `set ${capability.id}`)));
  }
  container.append(row);
}

function addCapabilityControls(container, device, capability) {
  if (capability.type === "channels") return addChannelControls(container, device, capability);
  if (["number_sensor", "binary_sensor", "number", "enum"].includes(capability.type)) return addValueControl(container, device, capability);
  const row = document.createElement("div");
  row.className = "capability-row";
  const label = document.createElement("span");
  label.textContent = capability.type === "switch"
    ? `Switch · ${device.state?.switch || "unknown"}`
    : capability.type === "light"
      ? `Light${capability.switch_key ? ` · ${device.state?.[capability.switch_key] || "unknown"}` : ""}`
      : `Cover${capability.position == null ? "" : ` · ${capability.position}%`}`;
  const actions = document.createElement("div");
  if (capability.type === "switch") {
    actions.append(
      deviceActionButton("On", () => runEwelinkDeviceAction(device, "switch", {state: "on"}, "turn on")),
      deviceActionButton("Off", () => runEwelinkDeviceAction(device, "switch", {state: "off"}, "turn off")),
      deviceActionButton("Pulse", () => runEwelinkDeviceAction(device, "button", {pulse_seconds: Number($("pulseSeconds").value) || 1}, "pulse"))
    );
  } else if (capability.type === "light") {
    if (capability.switch_key) {
      actions.append(
        deviceActionButton("On", () => runEwelinkDeviceAction(device, "light", {mode: "on"}, "turn on")),
        deviceActionButton("Off", () => runEwelinkDeviceAction(device, "light", {mode: "off"}, "turn off"))
      );
    }
    if (capability.brightness_key) {
      const brightness = document.createElement("input");
      brightness.type = "range";
      brightness.min = 0;
      brightness.max = 100;
      brightness.value = device.state?.[capability.brightness_key] ?? 100;
      brightness.setAttribute("aria-label", "Brightness");
      actions.append(brightness, deviceActionButton("Set brightness", () => runEwelinkDeviceAction(device, "light", {mode: "brightness", brightness: Number(brightness.value)}, "set brightness")));
    }
    if (capability.rgb_keys) {
      const color = document.createElement("input");
      color.type = "color";
      color.setAttribute("aria-label", "Light color");
      color.value = `#${capability.rgb_keys.map(key => Math.min(255, Math.max(0, Number(device.state?.[key]) || 0)).toString(16).padStart(2, "0")).join("")}`;
      actions.append(color, deviceActionButton("Set color", () => runEwelinkDeviceAction(device, "light", {mode: "color", color: color.value}, "set color")));
    }
  } else if (capability.type === "cover") {
    for (const movement of capability.actions || []) actions.append(deviceActionButton(movement[0].toUpperCase() + movement.slice(1), () => runEwelinkDeviceAction(device, "cover", {action: movement}, movement)));
    if (capability.position_command_key) {
      const position = document.createElement("input");
      position.type = "range";
      position.min = 0;
      position.max = 100;
      position.value = capability.position ?? 50;
      position.setAttribute("aria-label", "Cover position");
      actions.append(position, deviceActionButton("Set position", () => runEwelinkDeviceAction(device, "cover", {action: "position", position: Number(position.value)}, "set position")));
    }
  } else {
    return;
  }
  row.append(label, actions);
  container.append(row);
}

async function useAsPrimaryDoor(device) {
  const channels = device.capabilities.find(capability => capability.type === "channels")?.channels || [];
  if (channels.length < 2) return toast("Primary Door requires separate open and close channels.");
  let openChannel = Number($("ewelinkOpenChannel").value);
  let closeChannel = Number($("ewelinkCloseChannel").value);
  if (!channels.includes(openChannel)) openChannel = channels[0];
  if (!channels.includes(closeChannel) || closeChannel === openChannel) closeChannel = channels.find(channel => channel !== openChannel);
  if (!confirm(`Use ${device.name} as the Primary Door on channels ${openChannel}/${closeChannel}?`)) return;
  try {
    const result = await api(`/api/ewelink/devices/${encodeURIComponent(device.id)}/primary-door`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({host: device.host || "", port: device.port || 8081, open_channel: openChannel, close_channel: closeChannel, pulse_seconds: Number($("pulseSeconds").value) || 1})
    });
    config.settings = {...config.settings, ...result.settings};
    fillSettings();
    renderEwelinkDevices();
    toast(`${device.name} is now the Primary Door`);
  } catch (error) { toast(error.message); }
}

function renderEwelinkDevices() {
  const list = $("ewelinkDeviceList");
  const query = $("ewelinkDeviceSearch").value.trim().toLowerCase();
  const filtered = ewelinkDevices.filter(device => `${device.name} ${device.model}`.toLowerCase().includes(query));
  list.replaceChildren();
  $("ewelinkConnection").textContent = ewelinkDevices.length ? `${ewelinkDevices.length} device${ewelinkDevices.length === 1 ? "" : "s"} synced` : "No eWeLink account devices imported";
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = query ? "No matching devices" : "Sign in below to import devices";
    list.append(empty);
    return;
  }
  for (const device of filtered) {
    const card = document.createElement("article");
    card.className = "device-card";
    const head = document.createElement("div");
    head.className = "device-card-head";
    const identity = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = device.name;
    const paths = [device.connections?.lan ? "LAN" : "", device.connections?.cloud ? "cloud" : ""].filter(Boolean).join(" + ") || "no control path";
    const model = document.createElement("small"); model.textContent = `${device.model || "Unknown model"} · ${device.online === true ? "online" : device.online === false ? "offline" : "state unknown"} · ${paths}`;
    identity.append(name, model);
    const primary = deviceActionButton(config.settings.ewelink_device_id === device.id ? "Primary Door" : "Use as Primary Door", () => useAsPrimaryDoor(device));
    primary.disabled = config.settings.ewelink_device_id === device.id;
    head.append(identity, primary);
    const controls = document.createElement("div");
    controls.className = "capability-list";
    const capabilities = device.capabilities || [];
    const ownedSwitches = new Set(capabilities.filter(item => ["light", "cover"].includes(item.type)).map(item => item.switch_key || item.action_key).filter(Boolean));
    for (const capability of capabilities) {
      if (capability.type === "switch" && ownedSwitches.has(capability.id)) continue;
      addCapabilityControls(controls, device, capability);
    }
    if (!controls.children.length) {
      const note = document.createElement("p"); note.className = "field-help"; note.textContent = "No safe controls are known for this model."; controls.append(note);
    }
    const footer = document.createElement("div");
    footer.className = "device-card-footer";
    const updated = document.createElement("small");
    updated.textContent = device.last_sync ? `Updated ${new Date(device.last_sync).toLocaleString()}` : "Not synced";
    footer.append(updated, deviceActionButton("Check state", () => runEwelinkDeviceAction(device, "refresh", {}, "check state")));
    card.append(head, controls, footer);
    if (Object.keys(device.diagnostics || {}).length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = "Read-only diagnostics";
      const output = document.createElement("pre"); output.textContent = JSON.stringify(device.diagnostics, null, 2);
      details.append(summary, output); card.append(details);
    }
    list.append(card);
  }
}

async function loadEwelinkDevices(refresh = false) {
  ewelinkDevices = await api("/api/ewelink/devices" + (refresh ? "/refresh" : ""), refresh ? {method: "POST"} : {});
  renderEwelinkDevices();
}

$("ewelinkDeviceSearch").oninput = renderEwelinkDevices;
$("refreshEwelinkDevices").onclick = async () => {
  $("refreshEwelinkDevices").disabled = true;
  try { await loadEwelinkDevices(true); toast("eWeLink devices refreshed"); }
  catch (error) { toast(error.message); }
  finally { $("refreshEwelinkDevices").disabled = false; }
};

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
    await loadEwelinkDevices();
    $("ewelinkImportResult").classList.remove("show");
    $("ewelinkImportStatus").textContent = `${result.name} imported. ${brandName} now uses ${result.mode === "lan" ? "LAN with cloud fallback" : "cloud control"} on channels ${body.open_channel}/${body.close_channel}.`;
    toast("eWeLink door configured");
  } catch (error) { $("ewelinkImportStatus").textContent = error.message; toast(error.message); }
};

function resetEnrollment() {
  clearInterval(enrollmentPoll);
  enrollmentPoll = null;
  enrollmentSession = null;
  enrollmentReview = null;
  enrollmentSelection.clear();
  enrolling = false;
  $("videoWrap").classList.remove("enrolling");
  $("enroll").textContent = "Record samples";
  $("enroll").disabled = !selectedCameraId;
}

function syncEnrollmentTargets() {
  const target = $("enrollmentTarget");
  const previous = target.value;
  const label = enrollmentSelection.values().next().value?.label;
  target.replaceChildren();
  const fresh = document.createElement("option");
  fresh.value = "new";
  fresh.textContent = "New identity";
  target.append(fresh);
  for (const profile of system.profiles.filter(profile => !label || profile.label === label)) {
    const option = document.createElement("option");
    option.value = String(profile.id);
    option.textContent = `${profile.name} · ${profile.label}`;
    target.append(option);
  }
  target.value = [...target.options].some(option => option.value === previous) ? previous : "new";
  $("enrollmentNameLabel").hidden = target.value !== "new";
}

function renderSelectedSamples() {
  const list = $("selectedSamples");
  list.replaceChildren();
  if (!enrollmentSelection.size) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No samples selected";
    list.append(empty);
  }
  for (const detection of enrollmentSelection.values()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "selected-sample";
    button.title = "Remove sample";
    const image = document.createElement("img");
    image.src = detection.thumbnail_url;
    image.alt = `${detection.label} track ${detection.track_id}`;
    const remove = document.createElement("span");
    remove.textContent = "×";
    button.append(image, remove);
    button.onclick = () => {
      enrollmentSelection.delete(detection.id);
      renderEnrollmentFrame();
      renderSelectedSamples();
    };
    list.append(button);
  }
  syncEnrollmentTargets();
  $("commitEnrollment").disabled = !enrollmentSelection.size;
}

function toggleEnrollmentSample(detection) {
  if (!detection.selectable) return toast("This detection does not have a visual descriptor yet.");
  if (!enrollmentSelection.has(detection.id) && enrollmentSelection.size >= 64) return toast("An identity can contain at most 64 selected samples.");
  const selectedLabel = enrollmentSelection.values().next().value?.label;
  if (selectedLabel && selectedLabel !== detection.label) return toast("Choose samples of one person or vehicle at a time.");
  enrollmentSelection.has(detection.id) ? enrollmentSelection.delete(detection.id) : enrollmentSelection.set(detection.id, detection);
  renderEnrollmentFrame();
  renderSelectedSamples();
}

function renderEnrollmentFrame() {
  const frames = enrollmentReview?.frames || [];
  const index = Math.min(Number($("enrollmentTimeline").value) || 0, Math.max(0, frames.length - 1));
  const frame = frames[index];
  $("enrollmentBoxes").replaceChildren();
  $("enrollmentFrameCount").textContent = `Frame ${frames.length ? index + 1 : 0} of ${frames.length}`;
  if (!frame) {
    $("enrollmentFrame").removeAttribute("src");
    return;
  }
  $("enrollmentFrame").src = frame.url;
  for (const detection of frame.detections) {
    const [x1, y1, x2, y2] = detection.box;
    const box = document.createElement("button");
    box.type = "button";
    box.className = `review-box${enrollmentSelection.has(detection.id) ? " selected" : ""}`;
    box.disabled = !detection.selectable;
    box.style.left = `${x1 * 100}%`;
    box.style.top = `${y1 * 100}%`;
    box.style.width = `${(x2 - x1) * 100}%`;
    box.style.height = `${(y2 - y1) * 100}%`;
    box.textContent = `${detection.label} #${detection.track_id}`;
    box.setAttribute("aria-label", `${enrollmentSelection.has(detection.id) ? "Remove" : "Select"} ${detection.label} track ${detection.track_id}`);
    box.onclick = () => toggleEnrollmentSample(detection);
    $("enrollmentBoxes").append(box);
  }
}

function openEnrollmentReview(review) {
  clearInterval(enrollmentPoll);
  enrollmentPoll = null;
  enrollmentReview = review;
  enrolling = false;
  $("videoWrap").classList.remove("enrolling");
  $("enroll").textContent = "Record samples";
  $("enrollmentTimeline").max = Math.max(0, review.frames.length - 1);
  $("enrollmentTimeline").value = 0;
  $("enrollmentName").value = "";
  $("enrollmentReviewStatus").textContent = review.frames.length ? "" : "No processed frames were recorded. Try again when the camera is online.";
  enrollmentSelection.clear();
  renderEnrollmentFrame();
  renderSelectedSamples();
  $("enrollmentDialog").showModal();
}

async function pollEnrollment() {
  if (!enrollmentSession) return;
  try {
    const review = await api(`/api/enrollments/${enrollmentSession.id}`);
    if (review.status === "review") openEnrollmentReview(review);
  } catch (error) {
    resetEnrollment();
    toast(error.message);
  }
}

async function startOrStopEnrollment() {
  if (!selectedCameraId) return;
  $("enroll").disabled = true;
  try {
    if (enrollmentSession) {
      const review = await api(`/api/enrollments/${enrollmentSession.id}/stop`, {method: "POST"});
      openEnrollmentReview(review);
      return;
    }
    enrollmentSession = await api(`/api/cameras/${selectedCameraId}/enrollment/start`, {method: "POST"});
    enrolling = true;
    $("videoWrap").classList.add("enrolling");
    $("enroll").textContent = "Stop and review";
    enrollmentPoll = setInterval(pollEnrollment, 1000);
  } catch (error) {
    resetEnrollment();
    toast(error.message);
  } finally {
    $("enroll").disabled = !selectedCameraId;
  }
}

async function cancelEnrollment() {
  const id = enrollmentSession?.id;
  resetEnrollment();
  if ($("enrollmentDialog").open) $("enrollmentDialog").close();
  if (!id) return;
  try { await api(`/api/enrollments/${id}`, {method: "DELETE"}); }
  catch (error) { if (!error.message.includes("not found")) toast(error.message); }
}

async function openProfileSamples(profile) {
  try {
    let samples = await api(`/api/profiles/${profile.id}/samples`);
    const render = () => {
      const list = $("profileSamples");
      list.replaceChildren();
      for (const sample of samples) {
        const card = document.createElement("article");
        card.className = "sample-card";
        if (sample.thumbnail_url) {
          const image = document.createElement("img");
          image.src = sample.thumbnail_url;
          image.alt = `${profile.name} visual sample`;
          card.append(image);
        } else {
          const placeholder = document.createElement("div");
          placeholder.className = "sample-placeholder";
          placeholder.textContent = profile.label;
          card.append(placeholder);
        }
        const date = document.createElement("small");
        date.textContent = new Date(sample.created_at).toLocaleDateString();
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn small danger";
        remove.textContent = "Remove";
        remove.disabled = samples.length <= 1;
        remove.onclick = async () => {
          if (!confirm("Remove this visual sample?")) return;
          try {
            await api(`/api/profiles/${profile.id}/samples/${sample.id}`, {method: "DELETE"});
            samples = samples.filter(item => item.id !== sample.id);
            render();
            await refresh();
          } catch (error) { toast(error.message); }
        };
        card.append(date, remove);
        list.append(card);
      }
    };
    $("samplesTitle").textContent = `${profile.name} · ${samples.length} sample${samples.length === 1 ? "" : "s"}`;
    render();
    $("samplesDialog").showModal();
  } catch (error) { toast(error.message); }
}

$("enroll").onclick = startOrStopEnrollment;
$("enrollmentTimeline").oninput = renderEnrollmentFrame;
$("enrollmentTarget").onchange = () => { $("enrollmentNameLabel").hidden = $("enrollmentTarget").value !== "new"; };
$("cancelEnrollment").onclick = cancelEnrollment;
$("cancelEnrollmentTop").onclick = cancelEnrollment;
$("enrollmentDialog").addEventListener("cancel", event => { event.preventDefault(); cancelEnrollment(); });
$("enrollmentForm").onsubmit = async event => {
  event.preventDefault();
  if (!enrollmentSession || !enrollmentSelection.size) return;
  const target = $("enrollmentTarget").value;
  const body = {
    sample_ids: [...enrollmentSelection.keys()],
    profile_id: target === "new" ? null : Number(target),
    name: target === "new" ? $("enrollmentName").value.trim() : ""
  };
  if (target === "new" && !body.name) return toast("Enter a name for the new identity.");
  $("commitEnrollment").disabled = true;
  try {
    const result = await api(`/api/enrollments/${enrollmentSession.id}/commit`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    $("enrollmentDialog").close();
    resetEnrollment();
    toast(`${result.profile.name}: ${result.added} sample${result.added === 1 ? "" : "s"} saved${result.skipped_duplicates ? `, ${result.skipped_duplicates} duplicate skipped` : ""}`);
    await refreshAll();
  } catch (error) {
    $("enrollmentReviewStatus").textContent = error.message;
    $("commitEnrollment").disabled = false;
  }
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
  api("/api/door/refresh", {method: "POST"}).then(door => { system.door = door; renderSystem(); }).catch(() => {});
  setInterval(refresh, 2000);
}
start().catch(error => toast(error.message));
