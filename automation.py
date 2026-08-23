from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


log = logging.getLogger("visiongate.automation")


TRIGGER_KINDS = {
    "trigger.camera.authorized_presence",
    "trigger.camera.class_presence",
    "trigger.camera.connection",
    "trigger.ewelink.property_changed",
    "trigger.ewelink.connection",
    "trigger.manual",
    "trigger.schedule",
}
ACTION_KINDS = {
    "action.ewelink.switch",
    "action.ewelink.button",
    "action.ewelink.light",
    "action.ewelink.cover",
    "action.ewelink.number",
    "action.ewelink.enum",
    "action.ewelink.refresh",
    "action.camera.enable",
    "action.camera.disable",
    "action.log",
}
CONDITION_KINDS = {"condition.compare"}
NODE_KINDS = TRIGGER_KINDS | ACTION_KINDS | CONDITION_KINDS
CAMERA_TRIGGERS = {kind for kind in TRIGGER_KINDS if kind.startswith("trigger.camera.")}
EWELINK_TRIGGERS = {kind for kind in TRIGGER_KINDS if kind.startswith("trigger.ewelink.")}
CAMERA_ACTIONS = {kind for kind in ACTION_KINDS if kind.startswith("action.camera.")}
EWELINK_ACTIONS = {kind for kind in ACTION_KINDS if kind.startswith("action.ewelink.")}
OBJECT_LABELS = {"person", "car", "motorcycle", "bicycle"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:password|passwd|token|secret|api[_-]?key|device[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
SENSITIVE_TEXT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_ -]?key|device[_ -]?key)\s*[:=]\s*[^\s,;]+"
)
GRAPH_FIELDS = {
    "schema_version", "name", "enabled", "revision", "max_concurrent_runs", "nodes", "edges"
}


def _config_fields(kind: str, config: dict) -> set[str]:
    if kind == "trigger.manual":
        return set()
    if kind == "trigger.schedule":
        return {"mode", "time", "weekdays", "timezone"} if config.get("mode") == "time" else {"mode", "value", "unit"}
    if kind in CAMERA_TRIGGERS | CAMERA_ACTIONS:
        if kind == "trigger.camera.class_presence":
            return {"camera_id", "label", "present"}
        if kind == "trigger.camera.authorized_presence":
            return {"camera_id", "present"}
        if kind == "trigger.camera.connection":
            return {"camera_id", "online"}
        return {"camera_id"}
    if kind in EWELINK_TRIGGERS:
        return {"device_id", "property"} if kind == "trigger.ewelink.property_changed" else {"device_id", "online"}
    if kind == "condition.compare":
        fields = {"field", "operator", "value", "value_type"}
        if config.get("field") in {"state.camera_online", "state.authorized_count"}:
            fields.add("camera_id")
        if config.get("field") in {"state.ewelink_property", "state.ewelink_online"}:
            fields.update(("device_id", "property"))
            if config.get("field") == "state.ewelink_online":
                fields.discard("property")
        return fields
    return {
        "action.ewelink.switch": {"device_id", "channel", "state"},
        "action.ewelink.button": {"device_id", "channel", "pulse_seconds"},
        "action.ewelink.light": (
            {"device_id", "mode", "color"}
            if config.get("mode") == "color"
            else {"device_id", "mode"}
            if config.get("mode") in {"on", "off"}
            else {"device_id", "mode", "brightness"}
        ),
        "action.ewelink.cover": (
            {"device_id", "action", "position"}
            if config.get("action") == "position"
            else {"device_id", "action"}
        ),
        "action.ewelink.number": {"device_id", "property", "value"},
        "action.ewelink.enum": {"device_id", "property", "value"},
        "action.ewelink.refresh": {"device_id"},
        "action.log": {"message"},
    }.get(kind, set())


def sanitize_automation_value(value, depth: int = 0):
    """Remove credential-shaped fields before persisting automation history."""
    if depth > 10:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): sanitize_automation_value(item, depth + 1)
            for key, item in value.items()
            if not SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_automation_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}=[redacted]", value)[:2000]
    if value is None or type(value) in {bool, int, float}:
        return value
    return sanitize_automation_value(str(value), depth + 1)


def _contains_sensitive_graph_value(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_sensitive_graph_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_graph_value(item) for item in value)
    return isinstance(value, str) and bool(
        SENSITIVE_TEXT.search(value) or SENSITIVE_KEY.fullmatch(value)
    )


class GraphValidationError(ValueError):
    def __init__(self, issues: list[str] | str):
        self.issues = [issues] if isinstance(issues, str) else issues
        super().__init__("; ".join(self.issues))


def _schedule_issues(config: dict) -> list[str]:
    issues: list[str] = []
    mode = config.get("mode")
    if mode == "time":
        value = config.get("time")
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            issues.append("schedule time must use HH:MM")
        weekdays = config.get("weekdays")
        if (
            not isinstance(weekdays, list)
            or not weekdays
            or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)
            or len(set(weekdays)) != len(weekdays)
        ):
            issues.append("schedule weekdays must contain unique days from 0 to 6")
        zone = config.get("timezone", "UTC")
        try:
            ZoneInfo(zone) if isinstance(zone, str) else (_ for _ in ()).throw(ValueError())
        except (ZoneInfoNotFoundError, ValueError):
            issues.append("schedule time zone is invalid")
    elif mode == "interval":
        value, unit = config.get("value"), config.get("unit")
        if type(value) is not int or value < 1:
            issues.append("schedule interval must be a positive whole number")
        if unit not in {"minutes", "hours", "days"}:
            issues.append("schedule interval unit must be minutes, hours, or days")
        if type(value) is int and value >= 1 and unit in {"minutes", "hours", "days"}:
            seconds = value * {"minutes": 60, "hours": 3600, "days": 86400}[unit]
            if not 60 <= seconds <= 365 * 86400:
                issues.append("schedule interval must be between 1 minute and 365 days")
    else:
        issues.append("schedule mode must be time or interval")
    return issues


def _valid_local_time(naive: datetime, zone: ZoneInfo) -> datetime:
    for offset in range(181):
        candidate_naive = naive + timedelta(minutes=offset)
        candidate = candidate_naive.replace(tzinfo=zone, fold=0)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == candidate_naive:
            return candidate
    raise GraphValidationError("schedule time could not be resolved in its time zone")


def next_schedule(config: dict, after: datetime) -> datetime:
    """Return the first due UTC instant strictly after an aware datetime."""
    issues = _schedule_issues(config)
    if issues:
        raise GraphValidationError(issues)
    if after.tzinfo is None:
        raise ValueError("Schedule calculation requires a timezone-aware datetime")
    after = after.astimezone(timezone.utc)
    if config["mode"] == "interval":
        delta = timedelta(**{config["unit"]: config["value"]})
        return after + delta

    zone = ZoneInfo(config.get("timezone", "UTC"))
    local_after = after.astimezone(zone)
    hour, minute = (int(part) for part in config["time"].split(":"))
    weekdays = set(config["weekdays"])
    for offset in range(8):
        date = local_after.date() + timedelta(days=offset)
        if date.weekday() not in weekdays:
            continue
        naive = datetime.combine(date, clock_time(hour, minute))
        candidate = _valid_local_time(naive, zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    raise GraphValidationError("schedule has no future occurrence")


def _reference_issue(
    issues: list[str], resources: dict | None, kind: str, value: object
) -> None:
    if resources is None:
        return
    key = {
        "camera": "camera_ids",
        "device": "device_ids",
        "identity": "profile_ids",
    }[kind]
    if value not in resources.get(key, set()):
        issues.append(f"{kind} {value} does not exist")


def _validate_condition(config: dict, issues: list[str], resources: dict | None) -> None:
    field = config.get("field")
    field_types = {
        "event.authorized": "boolean",
        "event.label": "text",
        "event.camera_id": "number",
        "event.profile_id": "number",
        "state.camera_online": "boolean",
        "state.authorized_count": "number",
        "state.ewelink_property": "dynamic",
        "state.ewelink_online": "boolean",
    }
    if isinstance(field, str) and field.startswith("variable.") and VARIABLE.fullmatch(field[9:]):
        value_type = "dynamic"
    else:
        value_type = field_types.get(field)
    if not value_type:
        issues.append("condition field is invalid")
        return
    value = config.get("value")
    declared_type = config.setdefault(
        "value_type",
        "boolean" if type(value) is bool else "number" if isinstance(value, (int, float)) else "string",
    )
    if declared_type not in {"boolean", "number", "string"}:
        issues.append("condition value type must be boolean, number, or string")
    if value_type == "boolean" and type(value) is not bool:
        issues.append("condition value must be true or false")
    elif value_type == "number" and (not isinstance(value, (int, float)) or type(value) is bool):
        issues.append("condition value must be a number")
    elif value_type == "text" and not isinstance(value, str):
        issues.append("condition value must be text")
    actual_type = (
        "boolean"
        if type(value) is bool
        else "number"
        if isinstance(value, (int, float))
        else "text"
        if isinstance(value, str)
        else "invalid"
    )
    declared_internal = {"boolean": "boolean", "number": "number", "string": "text"}.get(declared_type)
    if declared_internal and actual_type != declared_internal:
        issues.append(f"condition value must match its {declared_type} type")
    operators = {
        "boolean": {"equals", "not_equals"},
        "number": {"equals", "not_equals", "greater", "greater_or_equal", "less", "less_or_equal"},
        "text": {"equals", "not_equals"},
        "invalid": set(),
    }
    operator_type = declared_internal if value_type == "dynamic" else value_type
    if config.get("operator") not in operators.get(operator_type, set()):
        issues.append("condition operator is invalid for its value type")
    if field in {"state.camera_online", "state.authorized_count"}:
        camera_id = config.get("camera_id")
        if camera_id in {"event", "*"}:
            pass
        elif type(camera_id) is not int:
            issues.append("condition camera is required")
        else:
            _reference_issue(issues, resources, "camera", camera_id)
    if field == "event.profile_id" and type(value) is int:
        _reference_issue(issues, resources, "identity", value)
    if field == "state.ewelink_property":
        device_id, property_name = config.get("device_id"), config.get("property")
        if not isinstance(device_id, str) or not device_id:
            issues.append("condition eWeLink device is required")
        else:
            _reference_issue(issues, resources, "device", device_id)
        if not isinstance(property_name, str) or not VARIABLE.fullmatch(property_name):
            issues.append("condition eWeLink property is invalid")
    if field == "state.ewelink_online":
        device_id = config.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            issues.append("condition eWeLink device is required")
        else:
            _reference_issue(issues, resources, "device", device_id)


def _validate_node(node: dict, issues: list[str], resources: dict | None) -> None:
    kind, config = node.get("kind"), node.get("config")
    if kind not in NODE_KINDS:
        issues.append(f"node {node.get('id', '?')} has an unsupported kind")
        return
    if not isinstance(config, dict):
        issues.append(f"node {node.get('id', '?')} config must be an object")
        return
    if kind in CAMERA_TRIGGERS | CAMERA_ACTIONS:
        camera_id = config.get("camera_id")
        if camera_id != "*" and type(camera_id) is not int:
            issues.append("camera node requires a camera")
        elif camera_id != "*":
            _reference_issue(issues, resources, "camera", camera_id)
    if kind == "trigger.camera.class_presence":
        if config.get("label") not in OBJECT_LABELS:
            issues.append("camera class trigger has an invalid object class")
    if kind in {"trigger.camera.authorized_presence", "trigger.camera.class_presence"}:
        if type(config.get("present")) is not bool:
            issues.append("camera presence trigger must choose present or absent")
    if kind == "trigger.camera.connection" and type(config.get("online")) is not bool:
        issues.append("camera connection trigger must choose online or offline")
    if kind == "trigger.ewelink.connection" and type(config.get("online")) is not bool:
        issues.append("eWeLink connection trigger must choose online or offline")
    if kind in EWELINK_TRIGGERS | EWELINK_ACTIONS:
        device_id = config.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            issues.append("eWeLink node requires a device")
        else:
            _reference_issue(issues, resources, "device", device_id)
    if kind == "trigger.ewelink.property_changed":
        if not isinstance(config.get("property"), str) or not config["property"]:
            issues.append("eWeLink property trigger requires a property")
    if kind == "trigger.schedule":
        issues.extend(_schedule_issues(config))
    if kind == "condition.compare":
        _validate_condition(config, issues, resources)
    if kind == "action.log":
        message = config.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            issues.append("log action message must contain 1-500 characters")
    if kind == "action.ewelink.switch":
        if type(config.get("channel")) is not int or not 1 <= config["channel"] <= 32:
            issues.append("eWeLink switch channel must be between 1 and 32")
        if config.get("state") not in {"on", "off"}:
            issues.append("eWeLink switch state must be on or off")
    if kind == "action.ewelink.button":
        if type(config.get("channel")) is not int or not 1 <= config["channel"] <= 32:
            issues.append("eWeLink button channel must be between 1 and 32")
        pulse = config.get("pulse_seconds", 1)
        if not isinstance(pulse, (int, float)) or type(pulse) is bool or not 0.1 <= pulse <= 30:
            issues.append("eWeLink button pulse must be between 0.1 and 30 seconds")
    if kind == "action.ewelink.light":
        mode = config.get("mode", "brightness")
        if mode == "brightness":
            brightness = config.get("brightness")
            if type(brightness) is not int or not 0 <= brightness <= 100:
                issues.append("eWeLink light brightness must be between 0 and 100")
        elif mode == "color":
            if not isinstance(config.get("color"), str) or not re.fullmatch(
                r"#[0-9A-Fa-f]{6}", config["color"]
            ):
                issues.append("eWeLink light color must use #RRGGBB format")
        elif mode not in {"on", "off"}:
            issues.append("eWeLink light mode must be on, off, brightness, or color")
    if kind == "action.ewelink.cover":
        movement = config.get("action")
        if movement == "position":
            position = config.get("position")
            if type(position) is not int or not 0 <= position <= 100:
                issues.append("eWeLink cover position must be between 0 and 100")
        elif movement not in {"open", "close", "stop"}:
            issues.append("eWeLink cover action must be open, close, stop, or position")
    if kind in {"action.ewelink.number", "action.ewelink.enum"}:
        if not isinstance(config.get("property"), str) or not VARIABLE.fullmatch(config["property"]):
            issues.append("eWeLink setting property is invalid")
    if kind == "action.ewelink.number" and (
        not isinstance(config.get("value"), (int, float)) or type(config.get("value")) is bool
    ):
        issues.append("eWeLink numeric setting requires a number")
    if kind == "action.ewelink.enum" and not isinstance(config.get("value"), str):
        issues.append("eWeLink enum setting requires text")
    if kind in EWELINK_ACTIONS and resources is not None:
        capabilities = resources.get("device_capabilities", {}).get(config.get("device_id"))
        if capabilities is not None:
            from ewelink_cloud import typed_device_action

            try:
                typed_device_action(
                    capabilities,
                    kind.rsplit(".", 1)[1],
                    {key: value for key, value in config.items() if key != "device_id"},
                )
            except ValueError as error:
                issues.append(f"eWeLink action is not supported: {error}")


def validate_graph(graph: dict, resources: dict | None = None) -> dict:
    """Validate and return a JSON-safe AutomationGraph v1 document."""
    try:
        graph = json.loads(json.dumps(graph))
    except (TypeError, ValueError) as error:
        raise GraphValidationError("automation graph must contain JSON values") from error
    issues: list[str] = []
    if not isinstance(graph, dict):
        raise GraphValidationError("automation graph must be an object")
    if _contains_sensitive_graph_value(graph):
        issues.append("automation cannot contain sensitive credential fields or values")
    if extra := set(graph) - GRAPH_FIELDS:
        issues.append(f"automation contains unsupported field(s): {', '.join(sorted(extra))}")
    if graph.get("schema_version") != 1:
        issues.append("automation schema_version must be 1")
    if not isinstance(graph.get("name"), str) or not graph["name"].strip() or len(graph["name"]) > 100:
        issues.append("automation name must contain 1-100 characters")
    concurrency = graph.get("max_concurrent_runs")
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        issues.append("automation concurrency must be between 1 and 16")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        issues.append("automation must contain 1-256 nodes")
        nodes = []
    if not isinstance(edges, list) or len(edges) > 512:
        issues.append("automation must contain at most 512 edges")
        edges = []

    node_map: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            issues.append("every node must be an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not IDENTIFIER.fullmatch(node_id):
            issues.append("every node needs a unique safe ID")
            continue
        if node_id in node_map:
            issues.append(f"node ID {node_id} is duplicated")
            continue
        node.setdefault("config", {})
        node.setdefault("position", {"x": 0, "y": 0})
        if extra := set(node) - {"id", "kind", "config", "position"}:
            issues.append(f"node {node_id} contains unsupported field(s): {', '.join(sorted(extra))}")
        position = node["position"]
        if (
            not isinstance(position, dict)
            or not all(isinstance(position.get(axis), (int, float)) for axis in ("x", "y"))
        ):
            issues.append(f"node {node_id} position is invalid")
        node_map[node_id] = node
        local_issues: list[str] = []
        if isinstance(node["config"], dict):
            if extra := set(node["config"]) - _config_fields(node.get("kind"), node["config"]):
                local_issues.append(f"config contains unsupported field(s): {', '.join(sorted(extra))}")
        _validate_node(node, local_issues, resources)
        issues.extend(f"node {node_id}: {issue}" for issue in local_issues)

    adjacency = {node_id: [] for node_id in node_map}
    undirected = {node_id: set() for node_id in node_map}
    indegree = {node_id: 0 for node_id in node_map}
    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            issues.append("every edge must be an object")
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not IDENTIFIER.fullmatch(edge_id) or edge_id in edge_ids:
            issues.append("every edge needs a unique safe ID")
            continue
        edge_ids.add(edge_id)
        if extra := set(edge) - {"id", "from", "to", "from_port", "to_port", "outcome", "steps"}:
            issues.append(f"edge {edge_id} contains unsupported field(s): {', '.join(sorted(extra))}")
        source, target = edge.get("from"), edge.get("to")
        if source not in node_map or target not in node_map:
            issues.append(f"edge {edge_id} references a missing node")
            continue
        if source == target:
            issues.append(f"edge {edge_id} creates a cycle")
        adjacency[source].append(target)
        undirected[source].add(target)
        undirected[target].add(source)
        indegree[target] += 1
        source_kind = node_map[source].get("kind")
        for field, fallback in (("from_port", "right"), ("to_port", "left")):
            edge.setdefault(field, fallback)
            if edge[field] not in {"top", "right", "bottom", "left"}:
                issues.append(f"edge {edge_id} {field} is invalid")
        expected = (
            {"true", "false"}
            if source_kind in CONDITION_KINDS
            else {"success", "failure"}
            if source_kind in ACTION_KINDS
            else {"success"}
        )
        if edge.get("outcome", "success") not in expected:
            issues.append(f"edge {edge_id} outcome is invalid for {source_kind}")
        edge.setdefault("outcome", "success")
        steps = edge.setdefault("steps", [])
        if not isinstance(steps, list) or len(steps) > 32:
            issues.append(f"edge {edge_id} must contain at most 32 steps")
            continue
        for step in steps:
            if not isinstance(step, dict):
                issues.append(f"edge {edge_id} contains an invalid step")
            elif step.get("type") == "wait":
                if extra := set(step) - {"type", "seconds"}:
                    issues.append(f"edge {edge_id} wait contains unsupported field(s): {', '.join(sorted(extra))}")
                seconds = step.get("seconds")
                if (
                    not isinstance(seconds, (int, float))
                    or type(seconds) is bool
                    or not 0 <= seconds <= 86400
                ):
                    issues.append(f"edge {edge_id} wait must be between 0 and 86400 seconds")
            elif step.get("type") == "set_variable":
                if extra := set(step) - {"type", "name", "value"}:
                    issues.append(f"edge {edge_id} variable contains unsupported field(s): {', '.join(sorted(extra))}")
                if not isinstance(step.get("name"), str) or not VARIABLE.fullmatch(step["name"]):
                    issues.append(f"edge {edge_id} variable name is invalid")
                if not isinstance(step.get("value"), (str, int, float, bool, type(None))):
                    issues.append(f"edge {edge_id} variable must be a scalar value")
            else:
                issues.append(f"edge {edge_id} contains an unsupported step")

    trigger_ids = {
        node_id for node_id, node in node_map.items() if node.get("kind") in TRIGGER_KINDS
    }
    for trigger_id in trigger_ids:
        if indegree[trigger_id]:
            issues.append(f"trigger {trigger_id} cannot have an incoming edge")
    remaining = dict(indegree)
    queue = [node_id for node_id, degree in remaining.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for target in adjacency[current]:
            remaining[target] -= 1
            if remaining[target] == 0:
                queue.append(target)
    if visited != len(node_map):
        issues.append("automation graph contains a cycle")

    reachable: set[str] = set()
    pending = list(trigger_ids)
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(adjacency[current])
    for node_id in sorted(set(node_map) - reachable):
        issues.append(f"node {node_id} is unreachable from a trigger")

    unseen = set(node_map)
    while unseen:
        start = next(iter(unseen))
        component, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(undirected[current])
        unseen -= component
        component_triggers = component & trigger_ids
        if len(component_triggers) != 1:
            issues.append("each automation component must contain exactly one trigger")

    if issues:
        raise GraphValidationError(issues)
    return graph


def default_device_graph(
    device_id: str,
    auto_close_seconds: float,
    open_channel: int = 1,
    close_channel: int = 2,
    pulse_seconds: float = 1,
) -> dict:
    nodes = [
        {
            "id": "authorized-arrived",
            "kind": "trigger.camera.authorized_presence",
            "config": {"camera_id": "*", "present": True},
            "position": {"x": 40, "y": 60},
        },
        {
            "id": "open-door",
            "kind": "action.ewelink.button",
            "config": {"device_id": device_id, "channel": open_channel, "pulse_seconds": pulse_seconds},
            "position": {"x": 360, "y": 60},
        },
    ]
    edges = [
        {
            "id": "authorized-open",
            "from": "authorized-arrived",
            "to": "open-door",
            "from_port": "right",
            "to_port": "left",
            "outcome": "success",
            "steps": [],
        }
    ]
    if auto_close_seconds > 0:
        nodes.extend(
            [
                {
                    "id": "authorized-left",
                    "kind": "trigger.camera.authorized_presence",
                    "config": {"camera_id": "*", "present": False},
                    "position": {"x": 40, "y": 260},
                },
                {
                    "id": "still-away",
                    "kind": "condition.compare",
                    "config": {
                        "field": "state.authorized_count",
                        "operator": "equals",
                        "value": 0,
                        "value_type": "number",
                        "camera_id": "*",
                    },
                    "position": {"x": 360, "y": 260},
                },
                {
                    "id": "close-door",
                    "kind": "action.ewelink.button",
                    "config": {"device_id": device_id, "channel": close_channel, "pulse_seconds": pulse_seconds},
                    "position": {"x": 620, "y": 260},
                },
            ]
        )
        edges.extend(
            [
                {
                    "id": "left-wait-check",
                    "from": "authorized-left",
                    "to": "still-away",
                    "from_port": "right",
                    "to_port": "left",
                    "outcome": "success",
                    "steps": [{"type": "wait", "seconds": auto_close_seconds}],
                },
                {
                    "id": "still-away-close",
                    "from": "still-away",
                    "to": "close-door",
                    "from_port": "right",
                    "to_port": "left",
                    "outcome": "true",
                    "steps": [],
                },
            ]
        )
    return {
        "schema_version": 1,
        "name": "Door access",
        "enabled": True,
        "revision": 1,
        "max_concurrent_runs": 4,
        "nodes": nodes,
        "edges": edges,
    }


def upgrade_automation_graph(graph: dict, settings: dict) -> tuple[dict, bool]:
    """Convert legacy single-door graphs to general device automations."""
    upgraded = json.loads(json.dumps(graph))
    changed = False
    device_id = str(settings.get("ewelink_device_id") or "").strip()
    pulse = float(settings.get("pulse_seconds") or 1)
    trigger_kinds = {
        "trigger.camera.authorized_appeared": ("trigger.camera.authorized_presence", {"present": True}),
        "trigger.camera.authorized_disappeared": ("trigger.camera.authorized_presence", {"present": False}),
        "trigger.camera.no_authorized_present": ("trigger.camera.authorized_presence", {"present": False}),
        "trigger.camera.class_appeared": ("trigger.camera.class_presence", {"present": True}),
        "trigger.camera.class_disappeared": ("trigger.camera.class_presence", {"present": False}),
        "trigger.camera.online": ("trigger.camera.connection", {"online": True}),
        "trigger.camera.offline": ("trigger.camera.connection", {"online": False}),
        "trigger.ewelink.online": ("trigger.ewelink.connection", {"online": True}),
        "trigger.ewelink.offline": ("trigger.ewelink.connection", {"online": False}),
    }
    for node in upgraded.get("nodes", []):
        kind = node.get("kind")
        config = node.setdefault("config", {})
        if kind in trigger_kinds:
            node["kind"], extra = trigger_kinds[kind]
            config.update(extra)
            changed = True
        elif isinstance(kind, str) and kind.startswith("action.primary_door."):
            action = kind.rsplit(".", 1)[1]
            if device_id:
                if action == "query":
                    node["kind"], node["config"] = "action.ewelink.refresh", {"device_id": device_id}
                else:
                    channel = settings.get(f"ewelink_{action}_channel", 1 if action == "open" else 2)
                    node["kind"], node["config"] = "action.ewelink.button", {
                        "device_id": device_id,
                        "channel": int(channel),
                        "pulse_seconds": pulse,
                    }
            else:
                node["kind"], node["config"] = "action.log", {
                    "message": "Connect a device and choose its channel for this action"
                }
            changed = True
        if node.get("kind") == "condition.compare":
            if config.get("field") == "state.door":
                if device_id:
                    config.update(field="state.ewelink_property", device_id=device_id, property="door")
                else:
                    config["field"] = "variable.device_state"
                changed = True
            value = config.get("value")
            value_type = "boolean" if type(value) is bool else "number" if isinstance(value, (int, float)) else "string"
            if "value_type" not in config:
                config["value_type"] = value_type
                changed = True
    for edge in upgraded.get("edges", []):
        if "from_port" not in edge:
            edge["from_port"] = "right"
            changed = True
        if "to_port" not in edge:
            edge["to_port"] = "left"
            changed = True
    if upgraded.get("name") == "Default smart door":
        upgraded["name"] = "Door access"
        changed = True
    return upgraded, changed


class _RunCanceled(RuntimeError):
    pass


class AutomationEngine:
    def __init__(
        self,
        database,
        action_handler,
        state_provider=None,
        *,
        now=None,
        tick_seconds: float = 1.0,
    ):
        self.database = database
        self.action_handler = action_handler
        self.state_provider = state_provider or (lambda _field, _config, _context: None)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tick_seconds = max(0.05, tick_seconds)
        self.stop_event = threading.Event()
        self._guard = threading.RLock()
        self._active: dict[int, int] = {}
        self._threads: set[threading.Thread] = set()
        self._device_locks: dict[str, threading.Lock] = {}
        self._due: dict[tuple[int, str], datetime] = {}
        self._scheduler: threading.Thread | None = None
        self.database.cancel_active_automation_runs()

    @property
    def active_runs(self) -> int:
        with self._guard:
            return sum(self._active.values())

    def _resource_key(self, kind: str, config: dict) -> str | None:
        if kind.startswith("action.ewelink."):
            return f"ewelink:{config.get('device_id', '')}"
        if kind.startswith("action.camera."):
            return f"camera:{config.get('camera_id', '')}"
        return None

    @staticmethod
    def _trigger_matches(node: dict, event_kind: str, payload: dict) -> bool:
        if node["kind"] != event_kind:
            return False
        config = node["config"]
        if event_kind.startswith("trigger.camera."):
            if config.get("camera_id") not in {"*", payload.get("camera_id")}:
                return False
            if event_kind == "trigger.camera.class_presence" and config.get("label") != payload.get("label"):
                return False
            if event_kind in {"trigger.camera.authorized_presence", "trigger.camera.class_presence"} and config.get("present") != payload.get("present"):
                return False
            if event_kind == "trigger.camera.connection" and config.get("online") != payload.get("online"):
                return False
        if event_kind.startswith("trigger.ewelink."):
            if config.get("device_id") != payload.get("device_id"):
                return False
            if (
                event_kind == "trigger.ewelink.property_changed"
                and config.get("property") != payload.get("property")
            ):
                return False
            if event_kind == "trigger.ewelink.connection" and config.get("online") != payload.get("online"):
                return False
        if event_kind == "trigger.schedule" and payload.get("trigger_node_id"):
            return node["id"] == payload["trigger_node_id"]
        return True

    def emit(self, event_kind: str, payload: dict | None = None, *, wait: bool = False):
        if event_kind not in TRIGGER_KINDS:
            raise ValueError("Unknown automation event")
        payload = sanitize_automation_value(dict(payload or {}))
        runs = []
        for automation in self.database.automations(enabled=True):
            starts = [
                node["id"]
                for node in automation.graph["nodes"]
                if self._trigger_matches(node, event_kind, payload)
            ]
            if starts:
                runs.append(
                    self._start_run(
                        automation,
                        {"kind": event_kind, **payload},
                        starts,
                        dry_run=False,
                        wait=wait,
                    )
                )
        return runs

    def run_automation(
        self, automation_id: int, *, dry_run: bool = False, wait: bool = False
    ):
        automation = self.database.automation(automation_id)
        if not automation:
            raise KeyError("Automation not found")
        starts = [
            node["id"]
            for node in automation.graph["nodes"]
            if node["kind"] == "trigger.manual"
        ]
        if dry_run and not starts:
            starts = [
                node["id"]
                for node in automation.graph["nodes"]
                if node["kind"] in TRIGGER_KINDS
            ]
        if not starts:
            raise ValueError("Automation has no manual trigger")
        return self._start_run(
            automation,
            {"kind": "trigger.manual"},
            starts,
            dry_run=dry_run,
            wait=wait,
        )

    def _start_run(self, automation, trigger: dict, starts: list[str], *, dry_run: bool, wait: bool):
        maximum = automation.graph["max_concurrent_runs"]
        with self._guard:
            active = self._active.get(automation.id, 0)
            if active >= maximum:
                return self.database.start_automation_run(
                    automation.id, automation.revision, trigger, status="dropped"
                )
            self._active[automation.id] = active + 1
        try:
            run = self.database.start_automation_run(
                automation.id, automation.revision, trigger
            )
        except Exception:
            with self._guard:
                self._active[automation.id] -= 1
            raise
        if wait:
            self._execute_run(run.id, automation, trigger, starts, dry_run)
            return self.database.automation_run(run.id)
        thread = threading.Thread(
            target=self._execute_run,
            args=(run.id, automation, trigger, starts, dry_run),
            daemon=True,
            name=f"automation-{automation.id}-{run.id}",
        )
        with self._guard:
            self._threads.add(thread)
        thread.start()
        return run

    def _condition_value(self, config: dict, context: dict):
        field = config["field"]
        if field.startswith("variable."):
            return context["variables"].get(field[9:])
        if field.startswith("event."):
            return context["event"].get(field[6:])
        state_config = dict(config)
        if state_config.get("camera_id") == "event":
            state_config["camera_id"] = context["event"].get("camera_id")
        return self.state_provider(field, state_config, context)

    def _evaluate(self, config: dict, context: dict) -> bool:
        left, right, operator = self._condition_value(config, context), config["value"], config["operator"]
        return {
            "equals": lambda: left == right,
            "not_equals": lambda: left != right,
            "greater": lambda: left > right,
            "greater_or_equal": lambda: left >= right,
            "less": lambda: left < right,
            "less_or_equal": lambda: left <= right,
        }[operator]()

    def _perform_node(self, node: dict, context: dict, dry_run: bool) -> tuple[str, dict]:
        kind, config = node["kind"], node["config"]
        if kind in TRIGGER_KINDS:
            return "success", {}
        if kind in CONDITION_KINDS:
            return ("true" if self._evaluate(config, context) else "false"), {}
        if dry_run:
            return "success", {"simulated": True}
        resource = self._resource_key(kind, config)
        if resource:
            with self._guard:
                lock = self._device_locks.setdefault(resource, threading.Lock())
            with lock:
                return "success", self.action_handler(kind, config, context) or {}
        return "success", self.action_handler(kind, config, context) or {}

    def _edge_context(self, edge: dict, context: dict) -> dict:
        branch = {"event": context["event"], "variables": dict(context["variables"])}
        for step in edge["steps"]:
            if self.stop_event.is_set():
                raise _RunCanceled()
            if step["type"] == "wait":
                if self.stop_event.wait(step["seconds"]):
                    raise _RunCanceled()
            else:
                branch["variables"][step["name"]] = step.get("value")
        return branch

    def _execute_run(
        self, run_id: int, automation, trigger: dict, starts: list[str], dry_run: bool
    ) -> None:
        graph = automation.graph
        nodes = {node["id"]: node for node in graph["nodes"]}
        outgoing: dict[str, list[dict]] = {node_id: [] for node_id in nodes}
        for edge in graph["edges"]:
            outgoing[edge["from"]].append(edge)
        results: list[dict] = []
        result_guard = threading.Lock()
        unhandled: list[str] = []

        def visit(node_id: str, context: dict) -> None:
            if self.stop_event.is_set():
                raise _RunCanceled()
            node = nodes[node_id]
            started = time.monotonic()
            try:
                outcome, detail = self._perform_node(node, context, dry_run)
                detail = sanitize_automation_value(detail)
                error = None
            except Exception as exception:
                outcome, detail = "failure", {}
                error = sanitize_automation_value(str(exception))[:500]
                log.error(
                    "Automation %s run %d node %s failed: %s",
                    automation.name,
                    run_id,
                    node_id,
                    error,
                )
            with result_guard:
                results.append(
                    {
                        "id": node_id,
                        "kind": node["kind"],
                        "outcome": outcome,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        **({"detail": detail} if detail else {}),
                        **({"error": error} if error else {}),
                    }
                )
            edges = [edge for edge in outgoing[node_id] if edge["outcome"] == outcome]
            if outcome == "failure" and not edges:
                with result_guard:
                    unhandled.append(error or f"{node_id} failed")
                return

            def follow(edge: dict) -> None:
                visit(edge["to"], self._edge_context(edge, context))

            if len(edges) > 1:
                with ThreadPoolExecutor(max_workers=min(16, len(edges))) as pool:
                    futures = [pool.submit(follow, edge) for edge in edges]
                    for future in futures:
                        future.result()
            elif edges:
                follow(edges[0])

        status = "completed"
        try:
            base = {"event": trigger, "variables": {}}
            if len(starts) > 1:
                with ThreadPoolExecutor(max_workers=min(16, len(starts))) as pool:
                    futures = [
                        pool.submit(visit, start, {"event": trigger, "variables": {}})
                        for start in starts
                    ]
                    for future in futures:
                        future.result()
            else:
                visit(starts[0], base)
            if unhandled:
                status = "failed"
        except _RunCanceled:
            status = "canceled"
        except Exception as error:
            status = "failed"
            unhandled.append(sanitize_automation_value(str(error))[:500])
        result = sanitize_automation_value({
            "dry_run": dry_run,
            "nodes": results,
            **({"errors": unhandled} if unhandled else {}),
        })
        try:
            self.database.finish_automation_run(run_id, status, result)
        finally:
            with self._guard:
                self._active[automation.id] -= 1
                if self._active[automation.id] <= 0:
                    self._active.pop(automation.id, None)
                self._threads.discard(threading.current_thread())

    def initialize_schedules(self, now: datetime | None = None) -> None:
        now = (now or self.now()).astimezone(timezone.utc)
        due: dict[tuple[int, str], datetime] = {}
        for automation in self.database.automations(enabled=True):
            schedule_nodes = [
                node for node in automation.graph["nodes"]
                if node["kind"] == "trigger.schedule"
            ]
            saved_due = None
            if len(schedule_nodes) == 1 and automation.next_run_at:
                try:
                    parsed_due = datetime.fromisoformat(automation.next_run_at)
                    if parsed_due.tzinfo is not None:
                        saved_due = parsed_due.astimezone(timezone.utc)
                except (ValueError, TypeError):
                    pass
            times = []
            for node in schedule_nodes:
                config = node["config"]
                if saved_due and config["mode"] == "interval":
                    delta = timedelta(**{config["unit"]: config["value"]})
                    skipped = max(0, int((now - saved_due) // delta) + 1)
                    instant = saved_due + skipped * delta
                else:
                    instant = next_schedule(config, now)
                due[(automation.id, node["id"])] = instant
                times.append(instant)
            self.database.set_automation_next_run(
                automation.id, min(times) if times else None
            )
        with self._guard:
            self._due = due

    def tick(self, now: datetime | None = None):
        now = (now or self.now()).astimezone(timezone.utc)
        with self._guard:
            due = [item for item, instant in self._due.items() if instant <= now]
        runs = []
        affected: set[int] = set()
        for automation_id, node_id in due:
            automation = self.database.automation(automation_id)
            if not automation or not automation.enabled:
                with self._guard:
                    self._due.pop((automation_id, node_id), None)
                continue
            node = next(item for item in automation.graph["nodes"] if item["id"] == node_id)
            runs.append(
                self._start_run(
                    automation,
                    {
                        "kind": "trigger.schedule",
                        "automation_id": automation_id,
                        "trigger_node_id": node_id,
                    },
                    [node_id],
                    dry_run=False,
                    wait=False,
                )
            )
            with self._guard:
                self._due[(automation_id, node_id)] = next_schedule(node["config"], now)
            affected.add(automation_id)
        with self._guard:
            for automation_id in affected:
                times = [
                    instant
                    for (current_id, _node_id), instant in self._due.items()
                    if current_id == automation_id
                ]
                self.database.set_automation_next_run(
                    automation_id, min(times) if times else None
                )
        return runs

    def start(self) -> None:
        if self._scheduler and self._scheduler.is_alive():
            return
        self.stop_event.clear()
        self.initialize_schedules()
        self._scheduler = threading.Thread(
            target=self._schedule_loop, daemon=True, name="automation-scheduler"
        )
        self._scheduler.start()

    def _schedule_loop(self) -> None:
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(self.tick_seconds)

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._guard:
                threads = list(self._threads)
            if not threads and not self.active_runs:
                return True
            for thread in threads:
                thread.join(timeout=min(0.05, max(0, deadline - time.monotonic())))
        return self.active_runs == 0

    def stop(self) -> None:
        self.stop_event.set()
        scheduler = self._scheduler
        if scheduler and scheduler is not threading.current_thread():
            scheduler.join(timeout=2)
        self._scheduler = None
        self.wait_for_idle(2)
