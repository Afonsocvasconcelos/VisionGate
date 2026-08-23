const $ = id => document.getElementById(id);

const NODE_TYPES = [
  ["Triggers", "trigger.manual", "Manual start"],
  ["Triggers", "trigger.schedule", "Schedule activator"],
  ["Triggers", "trigger.camera.authorized_presence", "Authorized target presence"],
  ["Triggers", "trigger.camera.class_presence", "Object class presence"],
  ["Triggers", "trigger.camera.connection", "Camera connection"],
  ["Triggers", "trigger.ewelink.property_changed", "Device property changed"],
  ["Triggers", "trigger.ewelink.connection", "Device connection"],
  ["Conditions", "condition.compare", "Compare a value"],
  ["Actions", "action.ewelink.switch", "Set device channel"],
  ["Actions", "action.ewelink.button", "Pulse device channel"],
  ["Actions", "action.ewelink.light", "Control light"],
  ["Actions", "action.ewelink.cover", "Control cover"],
  ["Actions", "action.ewelink.number", "Set numeric property"],
  ["Actions", "action.ewelink.enum", "Set option property"],
  ["Actions", "action.ewelink.refresh", "Refresh device"],
  ["Actions", "action.camera.enable", "Enable camera"],
  ["Actions", "action.camera.disable", "Disable camera"],
  ["Actions", "action.log", "Write to log"],
];

const TYPE_LABELS = Object.fromEntries(NODE_TYPES.map(([, kind, label]) => [kind, label]));
const TRIGGERS = new Set(NODE_TYPES.filter(([group]) => group === "Triggers").map(([, kind]) => kind));
const ACTIONS = new Set(NODE_TYPES.filter(([group]) => group === "Actions").map(([, kind]) => kind));
const CAMERA_KINDS = new Set(NODE_TYPES.filter(([, kind]) => kind.includes(".camera.")).map(([, kind]) => kind));
const DEVICE_KINDS = new Set(NODE_TYPES.filter(([, kind]) => kind.includes(".ewelink.")).map(([, kind]) => kind));
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let csrfToken = "";
let resources = {cameras: [], ewelink: [], identities: []};
let automations = [];
let current = null;
let graph = null;
let selectedNodeId = null;
let selectedEdgeId = null;
let connectingFrom = null;
let invalidNodes = new Set();
let invalidEdges = new Set();
let undoStack = [];
let dirty = false;
let idCounter = 0;
let view = {x: 24, y: 24, scale: 1};
let toastTimer;

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.add("show");
  toastTimer = setTimeout(() => $("toast").classList.remove("show"), 3200);
}

async function api(url, options = {}) {
  const request = {...options, headers: new Headers(options.headers || {})};
  if (request.body && !request.headers.has("Content-Type")) request.headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes((request.method || "GET").toUpperCase())) {
    request.headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(url, request);
  if (response.status === 401) {
    location.replace("/login");
    throw new Error("Your session expired.");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = Array.isArray(body.detail) ? body.detail.map(item => item.msg).join("; ") : body.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function newId(prefix) { return `${prefix}-${Date.now().toString(36)}-${(++idCounter).toString(36)}`; }
function nodeById(id) { return graph?.nodes.find(node => node.id === id); }
function edgeById(id) { return graph?.edges.find(edge => edge.id === id); }
function localZone() { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
function firstCamera() { return resources.cameras[0]?.id ?? "*"; }
function firstDevice() { return resources.ewelink[0]?.id ?? ""; }
function firstIdentity() { return resources.identities[0]?.id ?? 0; }

function defaultConfig(kind) {
  if (kind === "trigger.schedule") return {mode: "time", time: "03:00", weekdays: [0, 1, 2, 3, 4, 5, 6], timezone: localZone()};
  if (CAMERA_KINDS.has(kind)) {
    const config = {camera_id: firstCamera()};
    if (kind === "trigger.camera.class_presence") Object.assign(config, {label: "person", present: true});
    if (kind === "trigger.camera.authorized_presence") config.present = true;
    if (kind === "trigger.camera.connection") config.online = true;
    return config;
  }
  if (DEVICE_KINDS.has(kind)) {
    const config = {device_id: firstDevice()};
    if (kind === "trigger.ewelink.property_changed") config.property = "channel_1";
    if (kind === "trigger.ewelink.connection") config.online = true;
    if (kind === "action.ewelink.switch") Object.assign(config, {channel: 1, state: "on"});
    if (kind === "action.ewelink.button") Object.assign(config, {channel: 1, pulse_seconds: 1});
    if (kind === "action.ewelink.light") Object.assign(config, {mode: "brightness", brightness: 100});
    if (kind === "action.ewelink.cover") config.action = "open";
    if (kind === "action.ewelink.number") Object.assign(config, {property: "value", value: 0});
    if (kind === "action.ewelink.enum") Object.assign(config, {property: "mode", value: ""});
    return config;
  }
  if (kind === "condition.compare") return {field: "event.authorized", operator: "equals", value: true, value_type: "boolean"};
  if (kind === "action.log") return {message: "Automation ran"};
  return {};
}

function starterGraph(name = "New automation") {
  return {
    schema_version: 1,
    name,
    enabled: false,
    revision: 1,
    max_concurrent_runs: 4,
    nodes: [
      {id: newId("manual"), kind: "trigger.manual", config: {}, position: {x: 80, y: 120}},
      {id: newId("log"), kind: "action.log", config: {message: "Automation ran"}, position: {x: 380, y: 120}},
    ],
    edges: [],
  };
}

function finishStarter(document) {
  document.edges.push({id: newId("edge"), from: document.nodes[0].id, to: document.nodes[1].id, from_port: "right", to_port: "left", outcome: "success", steps: []});
  return document;
}

function setDirty(value = true) {
  dirty = value;
  $("saveState").textContent = value ? "Unsaved" : current ? `Revision ${current.revision}` : "New";
}

function remember() {
  if (!graph) return;
  const snapshot = JSON.stringify(graph);
  if (undoStack.at(-1) !== snapshot) undoStack.push(snapshot);
  if (undoStack.length > 50) undoStack.shift();
}

function change(mutator) {
  remember();
  mutator();
  setDirty();
  renderGraph();
}

function undo() {
  const snapshot = undoStack.pop();
  if (!snapshot) return;
  graph = JSON.parse(snapshot);
  selectedNodeId = selectedEdgeId = null;
  setDirty();
  syncHeader();
  renderGraph();
}

function populatePalette() {
  for (const id of ["mobileNodeKind"]) {
    const select = $(id);
    select.replaceChildren();
    for (const groupName of [...new Set(NODE_TYPES.map(([group]) => group))]) {
      const group = document.createElement("optgroup");
      group.label = groupName;
      for (const [, kind, label] of NODE_TYPES.filter(([name]) => name === groupName)) {
        const option = document.createElement("option");
        option.value = kind;
        option.textContent = label;
        group.append(option);
      }
      select.append(group);
    }
  }
}

function syncHeader() {
  if (!graph) return;
  $("automationName").value = graph.name || "";
  $("automationConcurrency").value = graph.max_concurrent_runs ?? 4;
  $("automationEnabled").checked = Boolean(graph.enabled);
}

function applyView() {
  $("graphWorld").style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  $("resetView").textContent = `${Math.round(view.scale * 100)}%`;
}

function nodeClass(kind) {
  if (TRIGGERS.has(kind)) return "trigger";
  if (kind === "condition.compare") return "condition";
  return "action";
}

function nodeSummary(node) {
  const c = node.config || {};
  if (node.kind === "trigger.schedule") {
    return c.mode === "interval" ? `Every ${c.value || 1} ${c.unit || "minutes"}` : `${c.time || "03:00"} · ${(c.weekdays || []).length === 7 ? "daily" : "selected days"}`;
  }
  if (CAMERA_KINDS.has(node.kind)) {
    const camera = resources.cameras.find(item => item.id === c.camera_id);
    return `${camera?.name || (c.camera_id === "*" ? "Any camera" : "Choose camera")}${c.label ? ` · ${c.label}` : ""}`;
  }
  if (DEVICE_KINDS.has(node.kind)) {
    const device = resources.ewelink.find(item => item.id === c.device_id);
    return device?.name || "Choose device";
  }
  if (node.kind === "condition.compare") return `${c.field || "Choose field"} ${c.operator || ""} ${String(c.value ?? "")}`;
  if (node.kind === "action.log") return c.message || "Log message";
  return "";
}

function endpointEdges(nodeId, port) {
  return graph.edges.filter(edge => (edge.from === nodeId && edge.from_port === port) || (edge.to === nodeId && edge.to_port === port));
}

function beginConnect(nodeId, port) {
  const occupied = endpointEdges(nodeId, port);
  if (occupied.length) {
    connectingFrom = null;
    $("connectHint").hidden = true;
    change(() => { graph.edges = graph.edges.filter(edge => !occupied.includes(edge)); });
    return;
  }
  connectingFrom = {nodeId, port};
  $("connectHint").hidden = false;
  renderGraph();
}

function usePort(nodeId, port) {
  const occupied = endpointEdges(nodeId, port);
  if (occupied.length) return beginConnect(nodeId, port);
  if (!connectingFrom) return beginConnect(nodeId, port);
  if (connectingFrom.nodeId === nodeId) {
    connectingFrom = null;
    $("connectHint").hidden = true;
    return renderGraph();
  }
  const exists = graph.edges.some(edge => edge.from === connectingFrom.nodeId && edge.to === nodeId);
  if (exists) {
    toast("These nodes are already connected.");
    return;
  }
  const start = connectingFrom;
  const source = nodeById(start.nodeId);
  const outcome = source?.kind === "condition.compare"
    ? (graph.edges.some(edge => edge.from === source.id && edge.outcome === "true") ? "false" : "true")
    : "success";
  connectingFrom = null;
  $("connectHint").hidden = true;
  change(() => graph.edges.push({id: newId("edge"), from: start.nodeId, to: nodeId, from_port: start.port, to_port: port, outcome, steps: []}));
}

function createPort(node, port) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `node-port port-${port}${endpointEdges(node.id, port).length ? " occupied" : ""}`;
  button.setAttribute("aria-label", `${endpointEdges(node.id, port).length ? "Remove" : "Connect"} ${port} point of ${TYPE_LABELS[node.kind]}`);
  button.onclick = event => { event.stopPropagation(); usePort(node.id, port); };
  return button;
}

function createNodeElement(node) {
  const card = document.createElement("article");
  card.className = `graph-node ${nodeClass(node.kind)}`;
  if (selectedNodeId === node.id) card.classList.add("selected");
  if (connectingFrom?.nodeId === node.id) card.classList.add("connecting");
  if (invalidNodes.has(node.id)) card.classList.add("invalid");
  card.dataset.nodeId = node.id;
  card.style.left = `${node.position.x}px`;
  card.style.top = `${node.position.y}px`;
  card.tabIndex = 0;

  const ports = (node.kind === "condition.compare" ? ["top", "right", "bottom", "left"] : ["left", "right"]).map(port => createPort(node, port));

  const title = document.createElement("div");
  title.className = "node-title";
  const type = document.createElement("span");
  type.textContent = nodeClass(node.kind);
  const name = document.createElement("strong");
  name.textContent = TYPE_LABELS[node.kind] || node.kind;
  title.append(type, name);
  const summary = document.createElement("p");
  summary.textContent = nodeSummary(node) || "No settings";
  card.append(...ports, title, summary);

  card.onclick = () => selectNode(node.id);
  card.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(node.id); }
  };
  card.addEventListener("pointerdown", event => {
    if (event.target.closest("button, input, select, textarea, a")) return;
    startNodeDrag(event, node);
  });
  return card;
}

function startNodeDrag(event, node) {
  if (event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  selectNode(node.id);
  remember();
  const start = {x: event.clientX, y: event.clientY, left: node.position.x, top: node.position.y};
  let moved = false;
  const move = pointer => {
    moved = true;
    node.position.x = Math.max(0, Math.round(start.left + (pointer.clientX - start.x) / view.scale));
    node.position.y = Math.max(0, Math.round(start.top + (pointer.clientY - start.y) / view.scale));
    const element = document.querySelector(`[data-node-id="${CSS.escape(node.id)}"]`);
    if (element) {
      element.style.left = `${node.position.x}px`;
      element.style.top = `${node.position.y}px`;
    }
    drawConnections();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (moved) { setDirty(); renderMobileGraph(); }
    else undoStack.pop();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up, {once: true});
}

function edgeText(edge) {
  const parts = [edge.outcome || "success"];
  for (const step of edge.steps || []) {
    parts.push(step.type === "wait" ? `wait ${step.seconds}s` : `set ${step.name}`);
  }
  return parts.join(" · ");
}

function drawConnections() {
  const svg = $("graphConnections");
  svg.replaceChildren();
  if (!graph) return;
  for (const edge of graph.edges) {
    const source = nodeById(edge.from);
    const target = nodeById(edge.to);
    if (!source || !target) continue;
    const point = (node, port) => ({
      top: [node.position.x + 110, node.position.y],
      right: [node.position.x + 220, node.position.y + 55],
      bottom: [node.position.x + 110, node.position.y + 110],
      left: [node.position.x, node.position.y + 55],
    }[port]);
    const tangent = {top: [0, -1], right: [1, 0], bottom: [0, 1], left: [-1, 0]};
    const [x1, y1] = point(source, edge.from_port || "right");
    const [x2, y2] = point(target, edge.to_port || "left");
    const bend = Math.max(65, Math.hypot(x2 - x1, y2 - y1) * .35);
    const [sx, sy] = tangent[edge.from_port || "right"], [tx, ty] = tangent[edge.to_port || "left"];
    const pathData = `M ${x1} ${y1} C ${x1 + sx * bend} ${y1 + sy * bend}, ${x2 + tx * bend} ${y2 + ty * bend}, ${x2} ${y2}`;
    const hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
    hit.setAttribute("d", pathData);
    hit.setAttribute("class", "graph-edge-hit");
    hit.onclick = event => { event.stopPropagation(); selectEdge(edge.id); };
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    path.setAttribute("class", `graph-edge${selectedEdgeId === edge.id ? " selected" : ""}${invalidEdges.has(edge.id) ? " invalid" : ""}`);
    path.setAttribute("tabindex", "0");
    path.onclick = event => { event.stopPropagation(); selectEdge(edge.id); };
    path.onkeydown = event => { if (event.key === "Enter") selectEdge(edge.id); };
    const text = edgeText(edge);
    const centerX = (x1 + x2) / 2, centerY = (y1 + y2) / 2 - 8;
    const chip = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    const width = Math.max(58, text.length * 6.5 + 18);
    chip.setAttribute("class", `edge-chip${selectedEdgeId === edge.id ? " selected" : ""}${invalidEdges.has(edge.id) ? " invalid" : ""}`);
    chip.setAttribute("tabindex", "0");
    background.setAttribute("class", "edge-chip-bg");
    background.setAttribute("x", String(centerX - width / 2));
    background.setAttribute("y", String(centerY - 16));
    background.setAttribute("width", String(width));
    background.setAttribute("height", "24");
    background.setAttribute("rx", "7");
    label.setAttribute("x", String(centerX));
    label.setAttribute("y", String(centerY));
    label.setAttribute("class", "edge-label");
    label.textContent = text;
    chip.onclick = event => { event.stopPropagation(); selectEdge(edge.id); };
    chip.onkeydown = event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectEdge(edge.id); } };
    chip.append(background, label);
    svg.append(hit, path, chip);
  }
}

function renderGraph() {
  if (!graph) return;
  const layer = $("graphNodes");
  layer.replaceChildren(...graph.nodes.map(createNodeElement));
  drawConnections();
  applyView();
  renderInspector();
  renderMobileGraph();
  $("undoAutomation").disabled = !undoStack.length;
}

function selectNode(id) {
  selectedNodeId = id;
  selectedEdgeId = null;
  renderGraph();
}

function selectEdge(id) {
  selectedEdgeId = id;
  selectedNodeId = null;
  renderGraph();
}

function deleteNode(id) {
  change(() => {
    graph.nodes = graph.nodes.filter(node => node.id !== id);
    graph.edges = graph.edges.filter(edge => edge.from !== id && edge.to !== id);
    selectedNodeId = null;
  });
}

function deleteEdge(id) {
  change(() => {
    graph.edges = graph.edges.filter(edge => edge.id !== id);
    selectedEdgeId = null;
  });
}

function addNode(kind, afterId = null, position = null) {
  const source = afterId ? nodeById(afterId) : null;
  const count = graph.nodes.length;
  const node = {
    id: newId(kind.split(".").at(-1)),
    kind,
    config: defaultConfig(kind),
    position: position || (source ? {x: source.position.x + 300, y: source.position.y + 130} : {x: 90 + (count % 4) * 270, y: 90 + Math.floor(count / 4) * 150}),
  };
  change(() => {
    graph.nodes.push(node);
    if (source) {
      graph.edges.push({id: newId("edge"), from: source.id, to: node.id, from_port: "right", to_port: "left", outcome: source.kind === "condition.compare" ? "true" : "success", steps: []});
    }
    selectedNodeId = node.id;
    selectedEdgeId = null;
  });
}

function field(labelText, control, wide = false) {
  const label = document.createElement("label");
  if (wide) label.className = "wide";
  const span = document.createElement("span");
  span.textContent = labelText;
  label.append(span, control);
  return label;
}

function input(value, options = {}) {
  const element = document.createElement("input");
  element.type = options.type || "text";
  element.value = value ?? "";
  if (options.id) element.id = options.id;
  for (const key of ["min", "max", "step", "maxlength", "placeholder"]) if (options[key] !== undefined) element.setAttribute(key, options[key]);
  element.onchange = () => options.change?.(element.value, element);
  return element;
}

function select(value, choices, change, id = "") {
  const element = document.createElement("select");
  if (id) element.id = id;
  for (const [optionValue, label] of choices) {
    const option = document.createElement("option");
    option.value = String(optionValue);
    option.textContent = label;
    element.append(option);
  }
  if (![...element.options].some(option => option.value === String(value))) {
    const option = document.createElement("option");
    option.value = String(value ?? "");
    option.textContent = String(value || "Choose");
    element.prepend(option);
  }
  element.value = String(value ?? "");
  element.onchange = () => change(element.value, element);
  return element;
}

function mutateConfig(node, key, value) {
  change(() => { node.config[key] = value; });
}

function cameraSelect(node, allowAny = false) {
  const choices = resources.cameras.map(camera => [camera.id, camera.name]);
  if (allowAny) choices.unshift(["*", "Any camera"]);
  return field("Camera", select(node.config.camera_id, choices, value => mutateConfig(node, "camera_id", value === "*" ? "*" : Number(value))));
}

function deviceSelect(node) {
  return field("Device", select(node.config.device_id, resources.ewelink.map(device => [device.id, `${device.name} · ${device.online === true ? "online" : device.online === false ? "offline" : "unknown"}`]), value => mutateConfig(node, "device_id", value)), true);
}

function propertyChoices(node) {
  const device = resources.ewelink.find(item => item.id === node.config.device_id);
  return Object.entries(device?.state || {})
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .map(([name]) => [name, name.replaceAll("_", " ")]);
}

function scheduleFields(node, body) {
  const config = node.config;
  body.append(field(
    "Schedule type",
    select(config.mode, [["time", "Time of day"], ["interval", "Repeat interval"]], value => change(() => {
      node.config = value === "time"
        ? {mode: "time", time: "03:00", weekdays: [0, 1, 2, 3, 4, 5, 6], timezone: localZone()}
        : {mode: "interval", value: 3, unit: "minutes"};
    })),
    true
  ));
  if (config.mode === "interval") {
    body.append(
      field("Every", input(config.value, {type: "number", min: 1, step: 1, change: value => mutateConfig(node, "value", Number(value))})),
      field("Unit", select(config.unit, [["minutes", "Minutes"], ["hours", "Hours"], ["days", "Days"]], value => mutateConfig(node, "unit", value)))
    );
  } else {
    body.append(field("Time", input(config.time, {id: "scheduleTime", type: "time", change: value => mutateConfig(node, "time", value)})));
    let zones = [localZone(), "UTC", "Europe/Lisbon", "Europe/London", "America/New_York", "Asia/Tokyo"];
    if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf("timeZone");
    zones = [...new Set([config.timezone, ...zones].filter(Boolean))];
    body.append(field("Time zone", select(config.timezone, zones.map(zone => [zone, zone]), value => mutateConfig(node, "timezone", value), "scheduleTimezone"), true));
    const weekdayGroup = document.createElement("fieldset");
    weekdayGroup.className = "weekday-field wide";
    const legend = document.createElement("legend");
    legend.textContent = "Days";
    weekdayGroup.append(legend);
    WEEKDAYS.forEach((day, index) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = (config.weekdays || []).includes(index);
      box.onchange = () => change(() => {
        const days = new Set(node.config.weekdays || []);
        box.checked ? days.add(index) : days.delete(index);
        node.config.weekdays = [...days].sort();
      });
      label.append(box, document.createTextNode(day));
      weekdayGroup.append(label);
    });
    body.append(weekdayGroup);
  }
  const preview = document.createElement("p");
  preview.className = "schedule-preview wide";
  preview.textContent = current?.next_run_at ? `Next run: ${new Date(current.next_run_at).toLocaleString()}` : "Next run appears after this automation is saved and enabled.";
  body.append(preview);
}

function conditionFields(node, body) {
  const config = node.config;
  const fields = [
    ["event.authorized", "Event target is authorized"], ["event.profile_id", "Event identity"], ["event.label", "Event object class"], ["event.camera_id", "Event camera ID"],
    ["state.camera_online", "Camera is online"], ["state.authorized_count", "Authorized targets present"],
    ["state.ewelink_property", "eWeLink property"], ["state.ewelink_online", "eWeLink device is online"], ["variable.value", "Run variable"],
  ];
  body.append(field(
    "Value to compare",
    select(config.field, fields, value => change(() => {
      const initial = ["event.authorized", "state.camera_online", "state.ewelink_online"].includes(value) ? true : ["event.camera_id", "event.profile_id", "state.authorized_count"].includes(value) ? 0 : "";
      node.config = {field: value, operator: "equals", value: initial, value_type: typeof initial === "boolean" ? "boolean" : typeof initial === "number" ? "number" : "string"};
      if (value === "event.profile_id") node.config.value = firstIdentity();
      if (["state.camera_online", "state.authorized_count"].includes(value)) node.config.camera_id = firstCamera();
      if (["state.ewelink_property", "state.ewelink_online"].includes(value)) node.config.device_id = firstDevice();
      if (value === "state.ewelink_property") node.config.property = "channel_1";
    })),
    true
  ));
  if (config.field?.startsWith("variable.")) {
    body.append(field("Variable name", input(config.field.slice(9), {change: value => change(() => { node.config.field = `variable.${value}`; })}), true));
  }
  if (["state.camera_online", "state.authorized_count"].includes(config.field)) body.append(cameraSelect(node, true));
  if (config.field === "state.ewelink_online") body.append(deviceSelect(node));
  if (config.field === "state.ewelink_property") {
    body.append(deviceSelect(node), field("Property", select(config.property, propertyChoices(node), value => mutateConfig(node, "property", value))));
  }
  const valueType = config.value_type || (typeof config.value === "boolean" ? "boolean" : typeof config.value === "number" ? "number" : "string");
  const booleanValue = valueType === "boolean";
  const numberValue = valueType === "number";
  const operators = numberValue
    ? [["equals", "Equals"], ["not_equals", "Does not equal"], ["greater", "Greater than"], ["greater_or_equal", "At least"], ["less", "Less than"], ["less_or_equal", "At most"]]
    : [["equals", "Equals"], ["not_equals", "Does not equal"]];
  body.append(field("Operator", select(config.operator, operators, value => mutateConfig(node, "operator", value))));
  const typeControl = select(valueType, [["boolean", "True / false"], ["string", "Text"], ["number", "Number"]], type => change(() => {
    node.config.value_type = type;
    node.config.value = type === "boolean" ? true : type === "number" ? 0 : "";
    node.config.operator = "equals";
  }));
  typeControl.disabled = !config.field?.startsWith("variable.") && config.field !== "state.ewelink_property";
  body.append(field("Value type", typeControl));
  if (config.field === "event.profile_id") {
    body.append(field("Identity", select(config.value, resources.identities.map(identity => [identity.id, `${identity.name} · ${identity.label}`]), value => mutateConfig(node, "value", Number(value))), true));
  } else if (booleanValue) {
    body.append(field("Value", select(String(config.value), [["true", "True"], ["false", "False"]], value => mutateConfig(node, "value", value === "true"))));
  } else {
    body.append(field("Value", input(config.value, {type: numberValue ? "number" : "text", step: numberValue ? "any" : undefined, change: value => mutateConfig(node, "value", numberValue ? Number(value) : value)})));
  }
}

function channelChoices(node) {
  const device = resources.ewelink.find(item => item.id === node.config.device_id);
  const channels = device?.capabilities?.find(capability => capability.type === "channels")?.count || 4;
  return Array.from({length: channels}, (_, index) => [index + 1, `Channel ${index + 1}`]);
}

function renderNodeInspector(node, inspector) {
  const head = document.createElement("div");
  head.className = "inspector-head";
  const heading = document.createElement("div");
  const kicker = document.createElement("span");
  kicker.className = "panel-kicker";
  kicker.textContent = nodeClass(node.kind);
  const title = document.createElement("h2");
  title.textContent = TYPE_LABELS[node.kind] || node.kind;
  heading.append(kicker, title);
  const remove = document.createElement("button");
  remove.className = "btn small danger";
  remove.type = "button";
  remove.textContent = "Delete";
  remove.onclick = () => deleteNode(node.id);
  head.append(heading, remove);
  const body = document.createElement("div");
  body.className = "inspector-form";
  const config = node.config;
  const group = nodeClass(node.kind) === "trigger" ? "Triggers" : nodeClass(node.kind) === "condition" ? "Conditions" : "Actions";
  body.append(field("Behavior", select(node.kind, NODE_TYPES.filter(([name]) => name === group).map(([, kind, label]) => [kind, label]), kind => change(() => {
    node.kind = kind;
    node.config = defaultConfig(kind);
  })), true));

  if (node.kind === "trigger.schedule") scheduleFields(node, body);
  else if (node.kind === "condition.compare") conditionFields(node, body);
  else {
    if (CAMERA_KINDS.has(node.kind)) body.append(cameraSelect(node, TRIGGERS.has(node.kind)));
    if (node.kind === "trigger.camera.class_presence") body.append(field("Object class", select(config.label, ["person", "car", "motorcycle", "bicycle"].map(value => [value, value[0].toUpperCase() + value.slice(1)]), value => mutateConfig(node, "label", value))));
    if (["trigger.camera.authorized_presence", "trigger.camera.class_presence"].includes(node.kind)) body.append(field("When target is", select(String(config.present), [["true", "Present"], ["false", "Absent"]], value => mutateConfig(node, "present", value === "true"))));
    if (["trigger.camera.connection", "trigger.ewelink.connection"].includes(node.kind)) body.append(field("When device is", select(String(config.online), [["true", "Online"], ["false", "Offline"]], value => mutateConfig(node, "online", value === "true"))));
    if (DEVICE_KINDS.has(node.kind)) body.append(deviceSelect(node));
    if (node.kind === "trigger.ewelink.property_changed") body.append(field("Property", select(config.property, propertyChoices(node), value => mutateConfig(node, "property", value)), true));
    if (["action.ewelink.switch", "action.ewelink.button"].includes(node.kind)) body.append(field("Channel", select(config.channel, channelChoices(node), value => mutateConfig(node, "channel", Number(value)))));
    if (node.kind === "action.ewelink.switch") body.append(field("State", select(config.state, [["on", "On"], ["off", "Off"]], value => mutateConfig(node, "state", value))));
    if (node.kind === "action.ewelink.button") body.append(field("Pulse seconds", input(config.pulse_seconds, {type: "number", min: .1, max: 30, step: .1, change: value => mutateConfig(node, "pulse_seconds", Number(value))})));
    if (node.kind === "action.ewelink.light") {
      const mode = config.mode || "brightness";
      body.append(field("Control", select(mode, [["brightness", "Brightness"], ["color", "Color"], ["on", "On"], ["off", "Off"]], value => change(() => {
        node.config = {device_id: config.device_id, ...(
          value === "brightness" ? {mode: value, brightness: 100} : value === "color" ? {mode: value, color: "#ffffff"} : {mode: value}
        )};
      }))));
      if (mode === "brightness") body.append(field("Brightness", input(config.brightness, {type: "number", min: 0, max: 100, step: 1, change: value => mutateConfig(node, "brightness", Number(value))})));
      if (mode === "color") body.append(field("Color", input(config.color || "#ffffff", {type: "color", change: value => mutateConfig(node, "color", value)})));
    }
    if (node.kind === "action.ewelink.cover") {
      body.append(field("Movement", select(config.action, [["open", "Open"], ["close", "Close"], ["stop", "Stop"], ["position", "Set position"]], value => change(() => {
        node.config = {device_id: config.device_id, action: value, ...(value === "position" ? {position: 50} : {})};
      }))));
      if (config.action === "position") body.append(field("Position", input(config.position, {type: "number", min: 0, max: 100, step: 1, change: value => mutateConfig(node, "position", Number(value))})));
    }
    if (["action.ewelink.number", "action.ewelink.enum"].includes(node.kind)) body.append(field("Property", input(config.property, {change: value => mutateConfig(node, "property", value)})));
    if (node.kind === "action.ewelink.number") body.append(field("Value", input(config.value, {type: "number", step: "any", change: value => mutateConfig(node, "value", Number(value))})));
    if (node.kind === "action.ewelink.enum") body.append(field("Value", input(config.value, {change: value => mutateConfig(node, "value", value)})));
    if (node.kind === "action.log") body.append(field("Message", input(config.message, {maxlength: 500, change: value => mutateConfig(node, "message", value)}), true));
  }
  if (!body.children.length) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "This node has no additional settings.";
    body.append(note);
  }
  inspector.append(head, body);
}

function sourceOutcomes(edge) {
  const source = nodeById(edge.from);
  if (source?.kind === "condition.compare") return [["true", "True"], ["false", "False"]];
  if (ACTIONS.has(source?.kind)) return [["success", "Success"], ["failure", "Failure"]];
  return [["success", "Continue"]];
}

function scalarEditor(step, index, edge) {
  const row = document.createElement("div");
  row.className = "edge-step";
  const heading = document.createElement("strong");
  heading.textContent = step.type === "wait" ? "Wait" : "Set variable";
  const controls = document.createElement("div");
  controls.className = "step-controls";
  const up = document.createElement("button"), down = document.createElement("button"), remove = document.createElement("button");
  for (const button of [up, down, remove]) { button.type = "button"; button.className = "mini-button"; }
  up.textContent = "↑"; up.title = "Move up"; up.disabled = index === 0;
  down.textContent = "↓"; down.title = "Move down"; down.disabled = index === edge.steps.length - 1;
  remove.textContent = "×"; remove.title = "Remove step";
  up.onclick = () => change(() => { [edge.steps[index - 1], edge.steps[index]] = [edge.steps[index], edge.steps[index - 1]]; });
  down.onclick = () => change(() => { [edge.steps[index + 1], edge.steps[index]] = [edge.steps[index], edge.steps[index + 1]]; });
  remove.onclick = () => change(() => edge.steps.splice(index, 1));
  controls.append(up, down, remove);
  row.append(heading, controls);
  if (step.type === "wait") {
    row.append(field("Seconds", input(step.seconds, {type: "number", min: 0, max: 86400, step: .1, change: value => change(() => { step.seconds = Number(value); })}), true));
  } else {
    row.append(field("Name", input(step.name, {maxlength: 64, change: value => change(() => { step.name = value; })})), scalarValueField(step));
  }
  return row;
}

function scalarValueField(step) {
  const currentType = step.value === null ? "null" : typeof step.value;
  const wrap = document.createElement("div");
  wrap.className = "scalar-value wide";
  const typeControl = select(currentType, [["string", "Text"], ["number", "Number"], ["boolean", "True / false"], ["null", "Empty"]], type => change(() => {
    step.value = type === "string" ? "" : type === "number" ? 0 : type === "boolean" ? true : null;
  }));
  wrap.append(field("Value type", typeControl));
  if (currentType === "boolean") wrap.append(field("Value", select(String(step.value), [["true", "True"], ["false", "False"]], value => change(() => { step.value = value === "true"; }))));
  else if (currentType !== "null") wrap.append(field("Value", input(step.value, {type: currentType === "number" ? "number" : "text", step: "any", change: value => change(() => { step.value = currentType === "number" ? Number(value) : value; })})));
  return wrap;
}

function renderEdgeInspector(edge, inspector) {
  const head = document.createElement("div");
  head.className = "inspector-head";
  const heading = document.createElement("div");
  const kicker = document.createElement("span");
  kicker.className = "panel-kicker";
  kicker.textContent = "Connection";
  const title = document.createElement("h2");
  title.textContent = `${TYPE_LABELS[nodeById(edge.from)?.kind] || edge.from} → ${TYPE_LABELS[nodeById(edge.to)?.kind] || edge.to}`;
  heading.append(kicker, title);
  const remove = document.createElement("button");
  remove.className = "btn small danger";
  remove.type = "button";
  remove.textContent = "Delete";
  remove.onclick = () => deleteEdge(edge.id);
  head.append(heading, remove);
  const body = document.createElement("div");
  body.className = "inspector-form edge-inspector";
  body.append(field("Follow when", select(edge.outcome, sourceOutcomes(edge), value => change(() => { edge.outcome = value; })), true));
  const stepHead = document.createElement("div");
  stepHead.className = "edge-step-head wide";
  const label = document.createElement("strong"); label.textContent = "Transition steps";
  const actions = document.createElement("div");
  const wait = document.createElement("button"), variable = document.createElement("button");
  for (const button of [wait, variable]) { button.type = "button"; button.className = "btn small"; }
  wait.textContent = "+ Wait"; variable.textContent = "+ Variable";
  wait.onclick = () => change(() => edge.steps.push({type: "wait", seconds: 10}));
  variable.onclick = () => change(() => edge.steps.push({type: "set_variable", name: "value", value: true}));
  actions.append(wait, variable); stepHead.append(label, actions); body.append(stepHead);
  edge.steps.forEach((step, index) => body.append(scalarEditor(step, index, edge)));
  if (!edge.steps.length) {
    const note = document.createElement("p"); note.className = "muted wide"; note.textContent = "No wait or variable steps."; body.append(note);
  }
  inspector.append(head, body);
}

function renderInspector() {
  const inspector = $("nodeInspector");
  inspector.replaceChildren();
  const node = nodeById(selectedNodeId);
  const edge = edgeById(selectedEdgeId);
  if (node) renderNodeInspector(node, inspector);
  else if (edge) renderEdgeInspector(edge, inspector);
  else {
    const empty = document.createElement("div");
    empty.className = "inspector-empty";
    const title = document.createElement("h2"); title.textContent = "Settings";
    const text = document.createElement("p"); text.textContent = "Select a node or connection.";
    empty.append(title, text); inspector.append(empty);
  }
}

function orderedNodes() {
  const incoming = Object.fromEntries(graph.nodes.map(node => [node.id, []]));
  for (const edge of graph.edges) if (incoming[edge.to]) incoming[edge.to].push(edge);
  const depth = Object.fromEntries(graph.nodes.map(node => [node.id, TRIGGERS.has(node.kind) ? 0 : 99]));
  for (let pass = 0; pass < graph.nodes.length; pass++) {
    for (const edge of graph.edges) if (depth[edge.from] < 99) depth[edge.to] = Math.min(depth[edge.to], depth[edge.from] + 1);
  }
  return [...graph.nodes].sort((a, b) => depth[a.id] - depth[b.id] || a.position.y - b.position.y || a.position.x - b.position.x).map(node => ({node, depth: Math.min(depth[node.id], 4), incoming: incoming[node.id]}));
}

function renderMobileGraph() {
  const list = $("mobileNodeList");
  list.replaceChildren();
  if (!graph) return;
  for (const {node, depth, incoming} of orderedNodes()) {
    const card = document.createElement("article");
    card.className = `mobile-node ${nodeClass(node.kind)}${selectedNodeId === node.id ? " selected" : ""}`;
    card.style.setProperty("--depth", depth);
    for (const edge of incoming) {
      const relation = document.createElement("button");
      relation.type = "button";
      relation.className = `mobile-edge${selectedEdgeId === edge.id ? " selected" : ""}`;
      relation.textContent = `↳ ${edgeText(edge)}`;
      relation.onclick = () => selectEdge(edge.id);
      card.append(relation);
    }
    const title = document.createElement("strong"); title.textContent = TYPE_LABELS[node.kind] || node.kind;
    const summary = document.createElement("p"); summary.textContent = nodeSummary(node) || "No settings";
    const actions = document.createElement("div"); actions.className = "mobile-node-actions";
    const edit = document.createElement("button"), add = document.createElement("button"), up = document.createElement("button"), down = document.createElement("button"), remove = document.createElement("button");
    for (const button of [edit, add, up, down, remove]) { button.type = "button"; button.className = "mini-button"; }
    edit.textContent = "Edit"; add.textContent = "+ Next"; up.textContent = "↑"; down.textContent = "↓"; remove.textContent = "×";
    edit.onclick = () => selectNode(node.id);
    add.onclick = () => addNode($("mobileNodeKind").value, node.id);
    up.onclick = () => change(() => { node.position.y = Math.max(0, node.position.y - 120); });
    down.onclick = () => change(() => { node.position.y += 120; });
    remove.onclick = () => deleteNode(node.id);
    actions.append(edit, add, up, down, remove);
    card.append(title, summary, actions); list.append(card);
  }
}

function renderAutomationList() {
  const list = $("automationList");
  list.replaceChildren();
  if (!automations.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No saved automations"; list.append(empty); return;
  }
  for (const item of automations) {
    const row = document.createElement("div");
    row.className = `automation-row${current?.id === item.id ? " active" : ""}`;
    const choose = document.createElement("button"); choose.type = "button"; choose.className = "automation-choice";
    const name = document.createElement("strong"); name.textContent = item.name;
    const state = document.createElement("small"); state.textContent = `${item.enabled ? "Enabled" : "Disabled"}${item.next_run_at ? ` · ${new Date(item.next_run_at).toLocaleString()}` : ""}`;
    choose.append(name, state); choose.onclick = () => loadAutomation(item.id);
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "mini-button"; remove.textContent = "×"; remove.title = `Delete ${item.name}`;
    remove.onclick = async () => {
      if (!confirm(`Delete “${item.name}”?`)) return;
      try {
        await api(`/api/automations/${item.id}`, {method: "DELETE"});
        automations = automations.filter(candidate => candidate.id !== item.id);
        if (current?.id === item.id) newAutomation(); else renderAutomationList();
        toast("Automation deleted.");
      } catch (error) { toast(error.message); }
    };
    row.append(choose, remove); list.append(row);
  }
}

function newAutomation() {
  current = null;
  graph = finishStarter(starterGraph());
  selectedNodeId = graph.nodes[0].id;
  selectedEdgeId = connectingFrom = null;
  undoStack = [];
  invalidNodes.clear(); invalidEdges.clear();
  view = {x: 24, y: 24, scale: 1};
  setDirty(false); syncHeader(); renderAutomationList(); renderGraph();
}

async function loadAutomation(id) {
  if (dirty && !confirm("Discard your unsaved changes?")) return;
  try {
    current = await api(`/api/automations/${id}`);
    graph = clone(current.graph);
    graph.name = current.name;
    graph.enabled = current.enabled;
    selectedNodeId = graph.nodes[0]?.id || null;
    selectedEdgeId = connectingFrom = null;
    undoStack = [];
    invalidNodes.clear(); invalidEdges.clear();
    view = {x: 24, y: 24, scale: 1};
    setDirty(false); syncHeader(); renderAutomationList(); renderGraph();
  } catch (error) { toast(error.message); }
}

function documentFromForm() {
  graph.name = $("automationName").value.trim();
  graph.enabled = $("automationEnabled").checked;
  graph.max_concurrent_runs = Number($("automationConcurrency").value);
  return graph;
}

function showValidation(message, ok = false) {
  const status = $("validationStatus");
  status.textContent = message;
  status.className = `validation-status ${ok ? "ok" : "bad"}`;
}

function markErrors(message) {
  invalidNodes.clear(); invalidEdges.clear();
  for (const match of message.matchAll(/node ([A-Za-z0-9_-]+)/g)) invalidNodes.add(match[1]);
  for (const match of message.matchAll(/edge ([A-Za-z0-9_-]+)/g)) invalidEdges.add(match[1]);
  renderGraph();
}

async function validateCurrent() {
  try {
    documentFromForm();
    const result = await api("/api/automations/validate", {method: "POST", body: JSON.stringify({graph})});
    graph = result.graph;
    invalidNodes.clear(); invalidEdges.clear();
    showValidation("No problems found.", true); renderGraph();
    return true;
  } catch (error) {
    showValidation(error.message); markErrors(error.message); return false;
  }
}

async function saveCurrent() {
  if (!await validateCurrent()) return false;
  const payload = {name: graph.name, enabled: graph.enabled, graph};
  try {
    current = await api(current ? `/api/automations/${current.id}` : "/api/automations", {method: current ? "PUT" : "POST", body: JSON.stringify(payload)});
    graph = clone(current.graph);
    automations = await api("/api/automations");
    undoStack = [];
    setDirty(false); syncHeader(); renderAutomationList(); renderGraph();
    showValidation("Saved.", true);
    return true;
  } catch (error) { showValidation(error.message); markErrors(error.message); return false; }
}

async function ensureSaved() {
  if (!current || dirty) return saveCurrent();
  return true;
}

async function run(dry) {
  if (!await ensureSaved()) return;
  if (!dry && !confirm(`Run “${current.name}” now? This may move connected hardware.`)) return;
  try {
    showValidation(dry ? "Running safely without hardware…" : "Running…", true);
    const result = await api(`/api/automations/${current.id}/${dry ? "dry-run" : "run"}`, {method: "POST", body: dry ? undefined : JSON.stringify({confirm: true})});
    showValidation(`${dry ? "Dry run" : "Run"}: ${result.status}.`, result.status === "completed");
  } catch (error) { showValidation(error.message); }
}

async function showHistory() {
  if (!current) return toast("Save this automation first.");
  try {
    const runs = await api(`/api/automations/${current.id}/runs?limit=100`);
    const list = $("runHistory"); list.replaceChildren();
    if (!runs.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No runs yet"; list.append(empty); }
    for (const run of runs) {
      const row = document.createElement("article"); row.className = "run-row";
      const head = document.createElement("div"); const state = document.createElement("strong"); const date = document.createElement("time");
      state.textContent = run.status; date.textContent = new Date(run.started_at).toLocaleString(); head.append(state, date);
      const trigger = document.createElement("p"); trigger.textContent = `Trigger: ${run.trigger?.kind || "manual"} · revision ${run.revision}`;
      row.append(head, trigger); list.append(row);
    }
    $("historyDialog").showModal();
  } catch (error) { toast(error.message); }
}

function setupCanvas() {
  const canvas = $("graphCanvas");
  document.querySelectorAll(".node-template").forEach(template => {
    template.ondragstart = event => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("text/plain", template.dataset.nodeKind);
    };
    template.onclick = () => addNode(template.dataset.nodeKind);
  });
  canvas.addEventListener("dragover", event => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
  canvas.addEventListener("drop", event => {
    event.preventDefault();
    const kind = event.dataTransfer.getData("text/plain");
    if (!NODE_TYPES.some(([, candidate]) => candidate === kind)) return;
    const bounds = canvas.getBoundingClientRect();
    addNode(kind, null, {
      x: Math.max(0, Math.round((event.clientX - bounds.left - view.x) / view.scale - 110)),
      y: Math.max(0, Math.round((event.clientY - bounds.top - view.y) / view.scale - 55)),
    });
  });
  canvas.addEventListener("pointerdown", event => {
    if (event.button !== 0 || ![canvas, $("graphWorld"), $("graphConnections"), $("graphNodes")].includes(event.target)) return;
    selectedNodeId = selectedEdgeId = null; renderGraph();
    const start = {x: event.clientX, y: event.clientY, left: view.x, top: view.y};
    const move = pointer => { view.x = start.left + pointer.clientX - start.x; view.y = start.top + pointer.clientY - start.y; applyView(); };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up, {once: true});
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    view.scale = Math.min(1.8, Math.max(.45, view.scale * (event.deltaY > 0 ? .9 : 1.1)));
    applyView();
  }, {passive: false});
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); undo(); return; }
    if (["INPUT", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (event.key === "Delete" || event.key === "Backspace") {
      if (selectedNodeId) deleteNode(selectedNodeId); else if (selectedEdgeId) deleteEdge(selectedEdgeId);
    }
    if (event.key === "Escape") { connectingFrom = null; $("connectHint").hidden = true; renderGraph(); }
    if (selectedNodeId && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      const delta = event.shiftKey ? 20 : 5, node = nodeById(selectedNodeId);
      change(() => {
        if (event.key === "ArrowLeft") node.position.x = Math.max(0, node.position.x - delta);
        if (event.key === "ArrowRight") node.position.x += delta;
        if (event.key === "ArrowUp") node.position.y = Math.max(0, node.position.y - delta);
        if (event.key === "ArrowDown") node.position.y += delta;
      });
    }
  });
}

async function initialize() {
  try {
    const session = await api("/api/auth/session"); csrfToken = session.csrf_token;
    const [brand, deviceData, saved] = await Promise.all([api("/api/brand"), api("/api/devices"), api("/api/automations")]);
    document.documentElement.dataset.palette = brand.palette;
    document.querySelectorAll("[data-brand-name]").forEach(item => item.textContent = brand.name);
    document.querySelectorAll("[data-brand-logo]").forEach(item => item.src = brand.logo);
    document.title = `Automations · ${brand.name}`;
    resources = deviceData;
    automations = saved;
    renderAutomationList();
    const requested = Number(new URLSearchParams(location.search).get("id"));
    if (automations.length) await loadAutomation(automations.some(item => item.id === requested) ? requested : automations[0].id); else newAutomation();
  } catch (error) { showValidation(error.message); }
}

populatePalette();
setupCanvas();
$("newAutomation").onclick = () => { if (!dirty || confirm("Discard your unsaved changes?")) newAutomation(); };
$("mobileAddNode").onclick = () => addNode($("mobileNodeKind").value);
$("undoAutomation").onclick = undo;
$("validateAutomation").onclick = validateCurrent;
$("saveAutomation").onclick = saveCurrent;
$("dryRunAutomation").onclick = () => run(true);
$("runAutomation").onclick = () => run(false);
$("showRunHistory").onclick = showHistory;
$("zoomIn").onclick = () => { view.scale = Math.min(1.8, view.scale + .1); applyView(); };
$("zoomOut").onclick = () => { view.scale = Math.max(.45, view.scale - .1); applyView(); };
$("resetView").onclick = () => { view = {x: 24, y: 24, scale: 1}; applyView(); };
$("automationName").onchange = event => change(() => { graph.name = event.target.value.trim(); });
$("automationConcurrency").onchange = event => change(() => { graph.max_concurrent_runs = Number(event.target.value); });
$("automationEnabled").onchange = event => change(() => { graph.enabled = event.target.checked; });
$("logout").onclick = async () => { try { await api("/api/auth/logout", {method: "POST"}); } finally { location.replace("/login"); } };
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });

initialize();
