import sqlite3
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core import Database, best_match
from automation import (
    AutomationEngine,
    GraphValidationError,
    default_device_graph,
    next_schedule,
    upgrade_automation_graph,
    validate_graph,
)
from ewelink_cloud import device_capabilities


class DatabaseMigrationTests(unittest.TestCase):
    def test_legacy_profiles_are_backed_up_and_seeded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visiongate.db"
            vector = np.array([0.25, 0.75], np.float32)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE profiles (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        label TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "INSERT INTO profiles VALUES (1, 'Alice', 'person', ?, '2026-01-01')",
                    (vector.tobytes(),),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            reopened = Database(path)

            self.assertTrue(path.with_name("visiongate.db.pre-automation.bak").exists())
            samples = reopened.profile_samples(1)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0].label, "person")
            np.testing.assert_allclose(samples[0].embedding, vector)
            backup = sqlite3.connect(path.with_name("visiongate.db.pre-automation.bak"))
            try:
                self.assertEqual(
                    backup.execute("SELECT name FROM profiles").fetchone()[0], "Alice"
                )
            finally:
                backup.close()
            self.assertEqual(len(database.profile_samples(1)), 1)

    def test_legacy_door_and_default_automation_migrate_in_the_schema_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visiongate.db"
            vector = np.array([0.25, 0.75], np.float32)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE profiles (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, label TEXT NOT NULL,
                        embedding BLOB NOT NULL, created_at TEXT NOT NULL
                    )"""
                )
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO profiles VALUES (1, 'Alice', 'person', ?, 'now')",
                    (vector.tobytes(),),
                )
                values = {
                    "auto_close_seconds": 12,
                    "ewelink_model": "SONOFF 4CH Pro R2",
                    "ewelink_host": "192.168.1.44",
                    "ewelink_port": 8081,
                    "ewelink_device_id": "1000aaaa11",
                    "ewelink_device_key": "device-key",
                    "ewelink_open_channel": 1,
                    "ewelink_close_channel": 2,
                }
                connection.executemany(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    [(key, json.dumps(value)) for key, value in values.items()],
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)

            device = database.ewelink_device("1000aaaa11")
            self.assertIsNotNone(device)
            self.assertEqual(device.device_key, "device-key")
            self.assertEqual(device.host, "192.168.1.44")
            automation = database.automations()[0]
            self.assertTrue(automation.enabled)
            wait = next(
                step
                for edge in automation.graph["edges"]
                for step in edge["steps"]
                if step["type"] == "wait"
            )
            self.assertEqual(wait["seconds"], 12)

    def test_legacy_install_without_a_device_does_not_create_an_invalid_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visiongate.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE profiles (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, label TEXT NOT NULL,
                        embedding BLOB NOT NULL, created_at TEXT NOT NULL
                    )"""
                )
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO settings VALUES ('auto_close_seconds', ?)",
                    (json.dumps(90000),),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)

            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertIn("profile_samples", tables)
            self.assertIn("ewelink_devices", tables)
            self.assertIn("automations", tables)
            self.assertEqual(database.automations(), [])

    def test_existing_default_automation_is_upgraded_to_global_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visiongate.db"
            database = Database(path)
            old_graph = default_device_graph("1000aaaa11", 5)
            condition = next(node for node in old_graph["nodes"] if node["id"] == "still-away")
            condition["config"]["camera_id"] = "event"
            automation = database.create_automation("Default smart door", old_graph, True)

            reopened = Database(path)
            upgraded = reopened.automation(automation.id)
            condition = next(
                node for node in upgraded.graph["nodes"] if node["id"] == "still-away"
            )

            self.assertEqual(condition["config"]["camera_id"], "*")
            self.assertGreater(upgraded.revision, automation.revision)

    def test_matching_uses_the_best_sample_per_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "samples.db")
            alice = database.add("Alice", "person", np.array([1.0, 0.0], np.float32))
            database.add_sample(
                alice.id, "person", np.array([0.0, 1.0], np.float32), b"thumbnail"
            )
            database.add("Bob", "person", np.array([0.99, 0.01], np.float32))

            match = best_match(
                database.matching_profiles(),
                "person",
                np.array([0.0, 1.0], np.float32),
                threshold=0.8,
                ambiguity_margin=0.1,
            )

            self.assertIsNotNone(match)
            self.assertEqual(match.profile.id, alice.id)
            self.assertAlmostEqual(match.similarity, 1.0)
            self.assertEqual(len(database.profile_samples(alice.id)), 2)

    def test_samples_persist_and_the_last_sample_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.db"
            database = Database(path)
            profile = database.add(
                "Blue car", "car", np.array([1.0, 0.0], np.float32)
            )
            second = database.add_sample(
                profile.id, "car", np.array([0.8, 0.2], np.float32), b"jpeg"
            )

            reopened = Database(path)
            self.assertEqual(reopened.profile_samples(profile.id)[1].thumbnail, b"jpeg")
            self.assertEqual(reopened.profile_sample_counts(), {profile.id: 2})
            self.assertTrue(reopened.delete_sample(profile.id, second.id))
            remaining = reopened.profile_samples(profile.id)
            self.assertEqual(len(remaining), 1)
            self.assertFalse(reopened.delete_sample(profile.id, remaining[0].id))

    def test_sample_class_and_limit_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "samples.db")
            profile = database.add("Alice", "person", np.array([1.0], np.float32))

            with self.assertRaisesRegex(ValueError, "same object class"):
                database.add_sample(
                    profile.id, "car", np.array([1.0], np.float32), None
                )
            for index in range(63):
                database.add_sample(
                    profile.id,
                    "person",
                    np.array([float(index + 2)], np.float32),
                    None,
                )
            with self.assertRaisesRegex(ValueError, "64 samples"):
                database.add_sample(
                    profile.id, "person", np.array([100.0], np.float32), None
                )


class EWeLinkInventoryPersistenceTests(unittest.TestCase):
    def test_device_inventory_upserts_and_marks_missing_devices_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.db"
            database = Database(path)
            database.sync_ewelink_devices(
                [
                    {
                        "id": "1000aaaa11",
                        "name": "Gate",
                        "model": "4CHPROR2",
                        "device_key": "gate-key",
                        "uiid": 126,
                        "host": "192.168.1.44",
                        "port": 8081,
                        "online": True,
                        "params": {
                            "switches": [
                                {"outlet": 0, "switch": "off"},
                                {"outlet": 1, "switch": "on"},
                            ]
                        },
                    },
                    {
                        "id": "1000bbbb22",
                        "name": "Lamp",
                        "model": "BASICR2",
                        "device_key": "lamp-key",
                        "uiid": 1,
                        "online": False,
                        "params": {"switch": "off"},
                    },
                ]
            )
            database.sync_ewelink_devices(
                [
                    {
                        "id": "1000aaaa11",
                        "name": "Front gate",
                        "model": "4CHPROR2",
                        "device_key": "gate-key",
                        "uiid": 126,
                        "online": True,
                        "params": {"switches": []},
                    }
                ]
            )

            reopened = Database(path)
            devices = {item.device_id: item for item in reopened.ewelink_devices()}
            self.assertEqual(devices["1000aaaa11"].name, "Front gate")
            self.assertTrue(devices["1000aaaa11"].available)
            self.assertFalse(devices["1000bbbb22"].available)
            self.assertEqual(devices["1000bbbb22"].device_key, "lamp-key")
            self.assertEqual(devices["1000aaaa11"].uiid, 126)

    def test_known_capabilities_are_typed_and_unknown_values_are_read_only(self):
        capabilities = device_capabilities(
            126,
            {
                "switches": [
                    {"outlet": 0, "switch": "off"},
                    {"outlet": 1, "switch": "on"},
                ],
                "temperature": 21.5,
                "door": "open",
                "startup": "stay",
                "mystery": {"danger": True},
            },
        )

        by_id = {item["id"]: item for item in capabilities}
        self.assertEqual(by_id["switches"]["channels"], [1, 2])
        self.assertEqual(by_id["temperature"]["type"], "number_sensor")
        self.assertEqual(by_id["door"]["type"], "binary_sensor")
        self.assertEqual(by_id["startup"]["options"], ["on", "off", "stay"])
        self.assertNotIn("mystery", by_id)


class AutomationGraphTests(unittest.TestCase):
    def test_generalized_boolean_triggers_typed_conditions_and_ports_are_saved(self):
        graph = self.graph(
            [
                {
                    "id": "presence",
                    "kind": "trigger.camera.authorized_presence",
                    "config": {"camera_id": 3, "present": False},
                },
                {
                    "id": "condition",
                    "kind": "condition.compare",
                    "config": {
                        "field": "variable.allowed",
                        "operator": "equals",
                        "value": True,
                        "value_type": "boolean",
                    },
                },
            ],
            [
                {
                    "id": "presence-condition",
                    "from": "presence",
                    "to": "condition",
                    "from_port": "bottom",
                    "to_port": "top",
                    "outcome": "success",
                    "steps": [{"type": "wait", "seconds": 3}],
                }
            ],
        )

        validated = validate_graph(graph, {"camera_ids": {3}})

        self.assertEqual(validated["edges"][0]["from_port"], "bottom")
        self.assertEqual(validated["edges"][0]["to_port"], "top")
        self.assertEqual(validated["nodes"][1]["config"]["value_type"], "boolean")

        graph["nodes"][1]["config"]["value_type"] = "string"
        with self.assertRaisesRegex(GraphValidationError, "match its string type"):
            validate_graph(graph, {"camera_ids": {3}})

    def test_legacy_door_graph_is_migrated_to_device_actions_and_boolean_presence(self):
        legacy = {
            "schema_version": 1,
            "name": "Default smart door",
            "enabled": True,
            "revision": 1,
            "max_concurrent_runs": 4,
            "nodes": [
                {"id": "presence", "kind": "trigger.camera.authorized_appeared", "config": {"camera_id": "*"}},
                {"id": "open", "kind": "action.primary_door.open", "config": {}},
            ],
            "edges": [{"id": "open", "from": "presence", "to": "open", "outcome": "success", "steps": []}],
        }

        upgraded, changed = upgrade_automation_graph(
            legacy,
            {
                "ewelink_device_id": "1000abcd12",
                "ewelink_open_channel": 1,
                "ewelink_close_channel": 2,
                "pulse_seconds": 0.5,
            },
        )

        self.assertTrue(changed)
        self.assertNotIn("primary_door", json.dumps(upgraded))
        kinds = {node["kind"] for node in upgraded["nodes"]}
        self.assertIn("trigger.camera.authorized_presence", kinds)
        self.assertIn("action.ewelink.button", kinds)
        self.assertTrue(all("from_port" in edge and "to_port" in edge for edge in upgraded["edges"]))

    def test_graph_rejects_unknown_fields_and_unreachable_nodes(self):
        def valid_graph():
            return self.graph(
                [
                    {"id": "start", "kind": "trigger.manual", "config": {}},
                    {"id": "log", "kind": "action.log", "config": {"message": "Hi"}},
                ],
                [{"id": "start-log", "from": "start", "to": "log", "outcome": "success", "steps": []}],
            )

        graph = valid_graph()
        graph["password"] = "must-not-be-stored"
        graph["nodes"][1]["config"]["device_key"] = "must-not-be-stored"
        with self.assertRaisesRegex(GraphValidationError, "unsupported field"):
            validate_graph(graph)

        unreachable = valid_graph()
        unreachable["nodes"].append(
            {
                "id": "orphan-root",
                "kind": "action.log",
                "config": {"message": "orphan"},
                "position": {"x": 0, "y": 300},
            }
        )
        unreachable["edges"].append(
            {
                "id": "orphan-merge",
                "from": "orphan-root",
                "to": unreachable["nodes"][1]["id"],
                "outcome": "success",
                "steps": [],
            }
        )
        with self.assertRaisesRegex(GraphValidationError, "unreachable"):
            validate_graph(unreachable)

    def test_graph_rejects_credential_shaped_fields_and_values(self):
        graph = self.graph(
            [
                {"id": "manual", "kind": "trigger.manual", "config": {}},
                {
                    "id": "log",
                    "kind": "action.log",
                    "config": {"message": "token=do-not-store"},
                },
            ],
            [
                {
                    "id": "edge",
                    "from": "manual",
                    "to": "log",
                    "outcome": "success",
                    "steps": [
                        {"type": "set_variable", "name": "api_key", "value": "value"}
                    ],
                }
            ],
        )

        with self.assertRaisesRegex(GraphValidationError, "sensitive"):
            validate_graph(graph)

    @staticmethod
    def graph(nodes, edges, **values):
        return {
            "schema_version": 1,
            "name": "Test automation",
            "enabled": False,
            "revision": 1,
            "max_concurrent_runs": 4,
            "nodes": nodes,
            "edges": edges,
            **values,
        }

    def test_valid_graph_accepts_typed_steps_conditions_and_resources(self):
        graph = self.graph(
            [
                {
                    "id": "manual",
                    "kind": "trigger.manual",
                    "config": {},
                    "position": {"x": 0, "y": 0},
                },
                {
                    "id": "condition",
                    "kind": "condition.compare",
                    "config": {
                        "field": "state.authorized_count",
                        "operator": "equals",
                        "value": 0,
                        "camera_id": 3,
                    },
                    "position": {"x": 200, "y": 0},
                },
                {
                    "id": "open",
                    "kind": "action.log",
                    "config": {"message": "Open"},
                    "position": {"x": 400, "y": 0},
                },
            ],
            [
                {
                    "id": "one",
                    "from": "manual",
                    "to": "condition",
                    "outcome": "success",
                    "steps": [
                        {"type": "wait", "seconds": 3},
                        {"type": "set_variable", "name": "checked", "value": True},
                    ],
                },
                {
                    "id": "two",
                    "from": "condition",
                    "to": "open",
                    "outcome": "true",
                    "steps": [],
                },
            ],
        )

        validated = validate_graph(graph, {"camera_ids": {3}, "device_ids": set()})

        self.assertEqual(validated["schema_version"], 1)
        self.assertEqual(validated["nodes"][1]["config"]["camera_id"], 3)

    def test_specific_identity_condition_requires_a_saved_identity(self):
        graph = self.graph(
            [
                {"id": "manual", "kind": "trigger.manual", "config": {}},
                {
                    "id": "identity",
                    "kind": "condition.compare",
                    "config": {
                        "field": "event.profile_id",
                        "operator": "equals",
                        "value": 7,
                    },
                },
            ],
            [
                {
                    "id": "edge",
                    "from": "manual",
                    "to": "identity",
                    "outcome": "success",
                    "steps": [],
                }
            ],
        )

        self.assertEqual(
            validate_graph(graph, {"profile_ids": {7}})["nodes"][1]["config"]["value"],
            7,
        )
        with self.assertRaisesRegex(GraphValidationError, "identity 7 does not exist"):
            validate_graph(graph, {"profile_ids": {8}})

    def test_device_online_condition_requires_a_saved_device(self):
        graph = self.graph(
            [
                {"id": "manual", "kind": "trigger.manual", "config": {}},
                {
                    "id": "online",
                    "kind": "condition.compare",
                    "config": {
                        "field": "state.ewelink_online",
                        "operator": "equals",
                        "value": True,
                        "device_id": "1000abcd12",
                    },
                },
            ],
            [{"id": "edge", "from": "manual", "to": "online", "outcome": "success"}],
        )

        self.assertEqual(
            validate_graph(graph, {"device_ids": {"1000abcd12"}})["nodes"][1]["config"]["value"],
            True,
        )
        with self.assertRaisesRegex(GraphValidationError, "device 1000abcd12"):
            validate_graph(graph, {"device_ids": set()})

    def test_cycles_and_unreachable_components_are_rejected(self):
        cyclic = self.graph(
            [
                {"id": "start", "kind": "trigger.manual", "config": {}},
                {"id": "log", "kind": "action.log", "config": {"message": "Hi"}},
            ],
            [
                {"id": "a", "from": "start", "to": "log", "outcome": "success"},
                {"id": "b", "from": "log", "to": "start", "outcome": "success"},
            ],
        )
        orphan = self.graph(
            [
                {"id": "start", "kind": "trigger.manual", "config": {}},
                {"id": "log", "kind": "action.log", "config": {"message": "Hi"}},
            ],
            [],
        )

        with self.assertRaisesRegex(GraphValidationError, "cycle"):
            validate_graph(cyclic)
        with self.assertRaisesRegex(GraphValidationError, "trigger"):
            validate_graph(orphan)

    def test_each_execution_component_requires_exactly_one_trigger(self):
        no_trigger = self.graph(
            [{"id": "log", "kind": "action.log", "config": {"message": "Hi"}}],
            [],
        )
        merged_triggers = self.graph(
            [
                {"id": "first", "kind": "trigger.manual", "config": {}},
                {"id": "second", "kind": "trigger.manual", "config": {}},
                {"id": "log", "kind": "action.log", "config": {"message": "Hi"}},
            ],
            [
                {"id": "one", "from": "first", "to": "log", "outcome": "success"},
                {"id": "two", "from": "second", "to": "log", "outcome": "success"},
            ],
        )

        for graph in (no_trigger, merged_triggers):
            with self.assertRaisesRegex(GraphValidationError, "exactly one trigger"):
                validate_graph(graph)

    def test_missing_resources_invalid_waits_and_bad_conditions_are_rejected(self):
        graph = self.graph(
            [
                {
                    "id": "camera",
                    "kind": "trigger.camera.authorized_presence",
                    "config": {"camera_id": 99, "present": True},
                },
                {
                    "id": "condition",
                    "kind": "condition.compare",
                    "config": {
                        "field": "state.authorized_count",
                        "operator": "contains",
                        "value": 0,
                        "camera_id": 99,
                    },
                },
            ],
            [
                {
                    "id": "edge",
                    "from": "camera",
                    "to": "condition",
                    "outcome": "success",
                    "steps": [{"type": "wait", "seconds": 90000}],
                }
            ],
        )

        with self.assertRaises(GraphValidationError) as raised:
            validate_graph(graph, {"camera_ids": {1}, "device_ids": set()})
        message = str(raised.exception)
        self.assertIn("camera 99", message)
        self.assertIn("wait", message)
        self.assertIn("operator", message)

    def test_schedule_supports_daily_weekdays_and_repeat_intervals(self):
        daily = {
            "mode": "time",
            "time": "03:00",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "timezone": "Europe/Lisbon",
        }
        interval = {"mode": "interval", "value": 3, "unit": "minutes"}

        self.assertEqual(
            next_schedule(daily, datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)),
            datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_schedule(
                interval, datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
            ),
            datetime(2026, 8, 22, 12, 3, tzinfo=timezone.utc),
        )
        monday_only = {**daily, "weekdays": [0]}
        self.assertEqual(
            next_schedule(
                monday_only, datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
            ),
            datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        )

    def test_schedule_rejects_invalid_time_zone_and_sub_minute_interval(self):
        for config in (
            {
                "mode": "time",
                "time": "25:00",
                "weekdays": [0],
                "timezone": "Nowhere/Invalid",
            },
            {"mode": "interval", "value": 0, "unit": "minutes"},
        ):
            graph = self.graph(
                [{"id": "schedule", "kind": "trigger.schedule", "config": config}],
                [],
            )
            with self.assertRaises(GraphValidationError):
                validate_graph(graph)

    def test_device_action_must_match_the_saved_device_capabilities(self):
        graph = self.graph(
            [
                {"id": "manual", "kind": "trigger.manual", "config": {}},
                {
                    "id": "unsupported",
                    "kind": "action.ewelink.switch",
                    "config": {
                        "device_id": "1000abcd12",
                        "channel": 4,
                        "state": "on",
                    },
                },
            ],
            [
                {
                    "id": "run",
                    "from": "manual",
                    "to": "unsupported",
                    "outcome": "success",
                }
            ],
        )
        resources = {
            "camera_ids": set(),
            "device_ids": {"1000abcd12"},
            "device_capabilities": {
                "1000abcd12": [
                    {
                        "id": "switches",
                        "type": "channels",
                        "channels": [1, 2],
                        "writable": True,
                    }
                ]
            },
        }

        with self.assertRaisesRegex(GraphValidationError, "not supported|supported"):
            validate_graph(graph, resources)

    def test_light_color_and_cover_position_actions_use_saved_capabilities(self):
        for kind, config, capabilities in (
            (
                "action.ewelink.light",
                {"device_id": "device", "mode": "color", "color": "#123456"},
                device_capabilities(
                    59,
                    {
                        "switch": "on",
                        "bright": 50,
                        "colorR": 1,
                        "colorG": 2,
                        "colorB": 3,
                    },
                ),
            ),
            (
                "action.ewelink.cover",
                {"device_id": "device", "action": "position", "position": 65},
                device_capabilities(None, {"motorTurn": 0, "currLocation": 20}),
            ),
        ):
            with self.subTest(kind=kind):
                graph = self.graph(
                    [
                        {"id": "manual", "kind": "trigger.manual", "config": {}},
                        {"id": "device-action", "kind": kind, "config": config},
                    ],
                    [
                        {
                            "id": "run",
                            "from": "manual",
                            "to": "device-action",
                            "outcome": "success",
                        }
                    ],
                )
                validate_graph(
                    graph,
                    {
                        "camera_ids": set(),
                        "device_ids": {"device"},
                        "device_capabilities": {"device": capabilities},
                    },
                )


class AutomationPersistenceAndRuntimeTests(unittest.TestCase):
    @staticmethod
    def manual_graph(action="action.log", concurrency=4):
        config = {"message": "Run"} if action == "action.log" else {
            "device_id": "device", "channel": 1, "pulse_seconds": 1
        }
        return {
            "schema_version": 1,
            "name": "Manual test",
            "enabled": True,
            "revision": 1,
            "max_concurrent_runs": concurrency,
            "nodes": [
                {"id": "start", "kind": "trigger.manual", "config": {}},
                {"id": "action", "kind": action, "config": config},
            ],
            "edges": [
                {
                    "id": "start-action",
                    "from": "start",
                    "to": "action",
                    "outcome": "success",
                    "steps": [],
                }
            ],
        }

    def test_automations_and_run_history_persist_with_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "automations.db"
            database = Database(path)
            created = database.create_automation(
                "Manual test", self.manual_graph(), enabled=False
            )
            updated_graph = {**created.graph, "max_concurrent_runs": 2}
            updated = database.update_automation(
                created.id, "Renamed", updated_graph, enabled=True
            )
            run = database.start_automation_run(
                updated.id, updated.revision, {"kind": "trigger.manual"}
            )
            database.finish_automation_run(
                run.id, "completed", {"nodes": [{"id": "action", "outcome": "success"}]}
            )

            reopened = Database(path)
            loaded = reopened.automation(created.id)
            history = reopened.automation_runs(created.id)

            self.assertEqual((loaded.name, loaded.revision, loaded.enabled), ("Renamed", 2, True))
            self.assertEqual(loaded.graph["max_concurrent_runs"], 2)
            self.assertEqual(history[0].status, "completed")
            self.assertEqual(history[0].result["nodes"][0]["id"], "action")

    def test_run_history_retains_only_the_latest_thousand_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retention.db"
            database = Database(path)
            automation = database.create_automation(
                "Retention", self.manual_graph(), enabled=True
            )
            now = datetime.now(timezone.utc).isoformat()
            connection = sqlite3.connect(path)
            try:
                connection.executemany(
                    """INSERT INTO automation_runs
                       (automation_id, revision, trigger, status, started_at, finished_at, result)
                       VALUES (?, ?, ?, 'dropped', ?, ?, '{}')""",
                    [
                        (
                            automation.id,
                            automation.revision,
                            json.dumps({"kind": "trigger.manual", "sequence": sequence}),
                            now,
                            now,
                        )
                        for sequence in range(1000)
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            database.start_automation_run(
                automation.id,
                automation.revision,
                {"kind": "trigger.manual", "sequence": 1000},
                status="dropped",
            )

            history = database.automation_runs(automation.id, limit=1000)

            self.assertEqual(len(history), 1000)
            self.assertEqual(history[0].trigger["sequence"], 1000)
            self.assertEqual(history[-1].trigger["sequence"], 1)

    def test_run_history_redacts_sensitive_event_result_and_error_values(self):
        graph = {
            "schema_version": 1,
            "name": "Redaction",
            "enabled": True,
            "revision": 1,
            "max_concurrent_runs": 1,
            "nodes": [
                {
                    "id": "camera",
                    "kind": "trigger.camera.connection",
                    "config": {"camera_id": 3, "online": True},
                },
                {
                    "id": "safe-result",
                    "kind": "action.log",
                    "config": {"message": "result"},
                },
                {
                    "id": "failed-result",
                    "kind": "action.log",
                    "config": {"message": "failure"},
                },
            ],
            "edges": [
                {"id": "one", "from": "camera", "to": "safe-result", "outcome": "success", "steps": []},
                {"id": "two", "from": "safe-result", "to": "failed-result", "outcome": "success", "steps": []},
            ],
        }

        def action(_kind, config, _context):
            if config["message"] == "failure":
                raise RuntimeError("device token=history-secret")
            return {"token": "history-secret", "safe": True}

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "redaction.db")
            automation = database.create_automation("Redaction", graph, enabled=True)
            engine = AutomationEngine(database, action)
            [run] = engine.emit(
                "trigger.camera.connection",
                {"camera_id": 3, "online": True, "password": "history-secret"},
                wait=True,
            )
            saved = database.automation_run(run.id)

        serialized = json.dumps({"trigger": saved.trigger, "result": saved.result})
        self.assertNotIn("history-secret", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn('"token"', serialized)
        self.assertIn('"safe": true', serialized)

    def test_restart_cancels_running_automation_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "restart.db")
            automation = database.create_automation(
                "Interrupted", self.manual_graph(), enabled=True
            )
            run = database.start_automation_run(
                automation.id, automation.revision, {"kind": "trigger.manual"}
            )

            engine = AutomationEngine(database, lambda *_args, **_kwargs: {})

            self.assertEqual(database.automation_runs()[0].id, run.id)
            self.assertEqual(database.automation_runs()[0].status, "canceled")
            self.assertEqual(engine.active_runs, 0)

    def test_runtime_executes_variables_conditions_and_failure_branches(self):
        graph = {
            "schema_version": 1,
            "name": "Conditional",
            "enabled": True,
            "revision": 1,
            "max_concurrent_runs": 4,
            "nodes": [
                {"id": "start", "kind": "trigger.manual", "config": {}},
                {
                    "id": "condition",
                    "kind": "condition.compare",
                    "config": {
                        "field": "variable.allowed",
                        "operator": "equals",
                        "value": True,
                    },
                },
                {"id": "open", "kind": "action.ewelink.button", "config": {"device_id": "device", "channel": 1, "pulse_seconds": 1}},
                {
                    "id": "recovered",
                    "kind": "action.log",
                    "config": {"message": "Open failed safely"},
                },
            ],
            "edges": [
                {
                    "id": "set",
                    "from": "start",
                    "to": "condition",
                    "outcome": "success",
                    "steps": [
                        {"type": "set_variable", "name": "allowed", "value": True}
                    ],
                },
                {
                    "id": "open",
                    "from": "condition",
                    "to": "open",
                    "outcome": "true",
                    "steps": [],
                },
                {
                    "id": "failure",
                    "from": "open",
                    "to": "recovered",
                    "outcome": "failure",
                    "steps": [],
                },
            ],
        }
        actions = []

        def handler(kind, config, context):
            actions.append(kind)
            if kind == "action.ewelink.button":
                raise RuntimeError("relay unavailable")
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "runtime.db")
            automation = database.create_automation("Conditional", graph, enabled=True)
            engine = AutomationEngine(database, handler)

            run = engine.run_automation(automation.id, wait=True)

            self.assertEqual(run.status, "completed")
            self.assertEqual(
                actions, ["action.ewelink.button", "action.log"]
            )
            outcomes = {item["id"]: item["outcome"] for item in run.result["nodes"]}
            self.assertEqual(outcomes["condition"], "true")
            self.assertEqual(outcomes["open"], "failure")

    def test_dry_run_never_calls_the_hardware_action_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "dry.db")
            automation = database.create_automation(
                "Dry", self.manual_graph(), enabled=True
            )
            actions = []
            engine = AutomationEngine(
                database, lambda kind, *_args: actions.append(kind) or {}
            )

            run = engine.run_automation(automation.id, dry_run=True, wait=True)

            self.assertEqual(run.status, "completed")
            self.assertEqual(actions, [])
            self.assertTrue(run.result["dry_run"])

    def test_concurrency_limit_drops_excess_runs(self):
        started, release = threading.Event(), threading.Event()

        def handler(*_args):
            started.set()
            release.wait(2)
            return {}

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "concurrency.db")
            automation = database.create_automation(
                "Limited", self.manual_graph(concurrency=1), enabled=True
            )
            engine = AutomationEngine(database, handler)

            first = engine.run_automation(automation.id, wait=False)
            self.assertTrue(started.wait(1))
            second = engine.run_automation(automation.id, wait=False)
            release.set()
            engine.wait_for_idle(2)

            self.assertEqual(database.automation_run(second.id).status, "dropped")
            self.assertEqual(database.automation_run(first.id).status, "completed")

    def test_parallel_branches_serialize_commands_for_the_same_device(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def handler(_kind, _config, _context):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return {}

        graph = self.manual_graph()
        graph["nodes"] = [
            graph["nodes"][0],
            {"id": "first", "kind": "action.ewelink.button", "config": {"device_id": "device", "channel": 1, "pulse_seconds": 1}},
            {"id": "second", "kind": "action.ewelink.button", "config": {"device_id": "device", "channel": 2, "pulse_seconds": 1}},
        ]
        graph["edges"] = [
            {"id": "first", "from": "start", "to": "first", "outcome": "success"},
            {"id": "second", "from": "start", "to": "second", "outcome": "success"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "locks.db")
            automation = database.create_automation("Locks", graph, enabled=True)
            engine = AutomationEngine(database, handler)

            run = engine.run_automation(automation.id, wait=True)

            self.assertEqual(run.status, "completed")
            self.assertEqual(maximum, 1)

    def test_parallel_branches_receive_independent_variable_contexts(self):
        observed = []
        graph = self.manual_graph(action="action.log")
        graph["nodes"] = [
            graph["nodes"][0],
            {"id": "first", "kind": "action.log", "config": {"message": "First"}},
            {"id": "second", "kind": "action.log", "config": {"message": "Second"}},
        ]
        graph["edges"] = [
            {
                "id": "first",
                "from": "start",
                "to": "first",
                "outcome": "success",
                "steps": [{"type": "set_variable", "name": "branch", "value": "first"}],
            },
            {
                "id": "second",
                "from": "start",
                "to": "second",
                "outcome": "success",
                "steps": [{"type": "set_variable", "name": "branch", "value": "second"}],
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "branch-context.db")
            automation = database.create_automation("Branches", graph, enabled=True)
            engine = AutomationEngine(
                database,
                lambda _kind, _config, context: observed.append(
                    context["variables"]["branch"]
                ) or {},
            )

            run = engine.run_automation(automation.id, wait=True)

        self.assertEqual(run.status, "completed")
        self.assertEqual(sorted(observed), ["first", "second"])

    def test_default_device_graph_rechecks_presence_after_the_wait(self):
        graph = validate_graph(default_device_graph("device", 7))
        close_edges = [edge for edge in graph["edges"] if edge["to"] == "close-door"]

        self.assertEqual(close_edges[0]["outcome"], "true")
        wait_edge = next(edge for edge in graph["edges"] if edge["to"] == "still-away")
        self.assertEqual(wait_edge["steps"], [{"type": "wait", "seconds": 7}])
        condition = next(node for node in graph["nodes"] if node["id"] == "still-away")
        self.assertEqual(condition["config"]["camera_id"], "*")

    def test_default_device_runtime_activates_and_a_returned_target_prevents_second_action(self):
        actions = []
        state = {"authorized_count": 0}

        def action(kind, _config, _context):
            actions.append(kind)
            return {}

        def state_provider(field, _config, _context):
            return state["authorized_count"] if field == "state.authorized_count" else None

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "default-runtime.db")
            database.create_automation("Door access", default_device_graph("device", 0.04), True)
            engine = AutomationEngine(database, action, state_provider)

            engine.emit("trigger.camera.authorized_presence", {"camera_id": 1, "present": True})
            self.assertTrue(engine.wait_for_idle(1))
            self.assertEqual(actions, ["action.ewelink.button"])

            engine.emit("trigger.camera.authorized_presence", {"camera_id": 1, "present": False})
            time.sleep(0.01)
            state["authorized_count"] = 1
            self.assertTrue(engine.wait_for_idle(1))
            self.assertEqual(actions, ["action.ewelink.button"])

            state["authorized_count"] = 0
            engine.emit("trigger.camera.authorized_presence", {"camera_id": 2, "present": False})
            self.assertTrue(engine.wait_for_idle(1))
            self.assertEqual(
                actions,
                ["action.ewelink.button", "action.ewelink.button"],
            )

    def test_daily_schedule_handles_daylight_saving_gaps_and_repeated_times_once(self):
        schedule = {
            "mode": "time",
            "time": "01:30",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "timezone": "Europe/Lisbon",
        }
        before_fall_back = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
        first = next_schedule(schedule, before_fall_back)
        after_first = next_schedule(schedule, first)

        self.assertEqual(first, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(after_first, datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc))

        missing = {**schedule, "time": "01:30"}
        self.assertEqual(
            next_schedule(missing, datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)),
            datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc),
        )

    def test_scheduler_persists_next_run_fires_once_and_skips_missed_intervals(self):
        graph = {
            "schema_version": 1,
            "name": "Every three minutes",
            "enabled": True,
            "revision": 1,
            "max_concurrent_runs": 4,
            "nodes": [
                {
                    "id": "schedule",
                    "kind": "trigger.schedule",
                    "config": {"mode": "interval", "value": 3, "unit": "minutes"},
                },
                {"id": "log", "kind": "action.log", "config": {"message": "Due"}},
            ],
            "edges": [
                {
                    "id": "due-log",
                    "from": "schedule",
                    "to": "log",
                    "outcome": "success",
                }
            ],
        }
        actions = []
        start = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "schedule.db")
            automation = database.create_automation("Every three minutes", graph, True)
            engine = AutomationEngine(
                database, lambda kind, *_args: actions.append(kind) or {}
            )
            engine.initialize_schedules(start)
            first_due = datetime.fromisoformat(
                database.automation(automation.id).next_run_at
            )

            self.assertEqual(first_due, start.replace(minute=3))
            engine.tick(first_due)
            self.assertTrue(engine.wait_for_idle(2))
            self.assertEqual(actions, ["action.log"])
            self.assertEqual(len(database.automation_runs(automation.id)), 1)

            restarted = AutomationEngine(database, lambda *_args: {})
            restarted.initialize_schedules(start.replace(minute=20))
            after_restart = datetime.fromisoformat(
                database.automation(automation.id).next_run_at
            )
            self.assertEqual(after_restart, start.replace(minute=21))


if __name__ == "__main__":
    unittest.main()
