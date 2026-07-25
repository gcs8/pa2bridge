from __future__ import annotations

import json
import importlib.util
import logging
import sys
from collections.abc import Iterable
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa2bridge.config import AppConfig, MqttConfig, Pa2Config
from pa2bridge.controller import (
    CROSSOVER_AT,
    CROSSOVER_SV,
    CURRENT_PRESET,
    OUTPUT_MUTES,
    PRESET_ROOT,
)

MODULE_PATH = Path(__file__).parents[1] / "tools" / "pa2_read_only_validation.py"
SPEC = importlib.util.spec_from_file_location("pa2_read_only_validation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validation
SPEC.loader.exec_module(validation)

PA2_TEST_PASSWORD = "-".join(("pa2", "test", "credential"))
MQTT_TEST_PASSWORD = "-".join(("mqtt", "test", "credential"))


class FakeRawPa2Client:
    def __init__(
        self,
        *,
        catalog_has_current: bool | Iterable[bool] = True,
    ) -> None:
        self.connected = False
        self.connection_generation = 0
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []
        self.catalog_has_current = (
            catalog_has_current
            if isinstance(catalog_has_current, bool)
            else iter(catalog_has_current)
        )

    def connect(self, username: str, password: str) -> None:
        del username, password
        self.calls.append(("connect", None))
        self.connected = True
        self.connection_generation += 1

    def close(self) -> None:
        self.connected = False

    def get(self, path: Iterable[str]) -> str:
        normalized = tuple(path)
        self.calls.append(("get", normalized))
        if normalized == CURRENT_PRESET:
            return "1"
        if normalized in OUTPUT_MUTES.values():
            return "Off"
        return {
            ("Node", "AT", "Class_Name"): "dbxDriveRackPA2",
            ("Node", "AT", "Instance_Name"): "DriveRackPA2",
            ("Node", "AT", "Software_Version"): "1.2.0.1",
        }[normalized]

    def get_before(self, path: Iterable[str], *, deadline: float) -> str:
        del deadline
        return self.get(path)

    def ls(self, path: Iterable[str]) -> dict[str, str]:
        normalized = tuple(path)
        self.calls.append(("ls", normalized))
        if normalized == PRESET_ROOT:
            entries = {
                "NumPresets": "2",
                "Name_1": "Flat",
                "Name_2": "Alternate",
            }
            has_current = (
                self.catalog_has_current
                if isinstance(self.catalog_has_current, bool)
                else next(self.catalog_has_current)
            )
            if has_current:
                entries["CurrentPreset"] = "1"
            return entries
        if normalized == CROSSOVER_AT:
            return {"NumBands": "1", "MonoSub": "0"}
        if normalized == CROSSOVER_SV:
            return {
                "Band_1_HPFrequency": "Out",
                "Band_1_HPType": "LR 12",
                "Band_1_Gain": "0.0",
                "Band_1_LPFrequency": "Out",
                "Band_1_LPType": "LR 48",
                "Band_1_Polarity": "Normal",
            }
        raise AssertionError(normalized)

    def ls_before(
        self, path: Iterable[str], *, deadline: float
    ) -> dict[str, str]:
        del deadline
        return self.ls(path)

    def set(self, path: Iterable[str], value: str) -> None:
        self.calls.append(("set", tuple(path)))
        del value

    def set_before(
        self, path: Iterable[str], value: str, *, deadline: float
    ) -> None:
        del deadline
        self.set(path, value)

    def reconnect(self) -> None:
        self.calls.append(("reconnect", None))

    def reconnect_before(self, *, deadline: float) -> None:
        del deadline
        self.reconnect()


def make_config() -> AppConfig:
    return AppConfig(
        pa2=Pa2Config(
            host="192.0.2.20",
            password=PA2_TEST_PASSWORD,
            allowed_preset_slots=(1, 2),
        ),
        mqtt=MqttConfig(
            host="mqtt.example.invalid",
            username="bridge",
            password=MQTT_TEST_PASSWORD,
        ),
    )


def test_guard_rejects_writes_reconnects_and_second_connections() -> None:
    raw = FakeRawPa2Client()
    guard = validation.ReadOnlyPa2Client(raw)

    guard.connect("administrator", "secret")
    guard.get(("Node", "AT", "Class_Name"))
    guard.ls(PRESET_ROOT)

    with pytest.raises(validation.ValidationSafetyError, match="set is forbidden"):
        guard.set(("Storage", "Presets", "SV", "Recall"), "2")
    with pytest.raises(validation.ValidationSafetyError, match="reconnect is forbidden"):
        guard.reconnect()
    with pytest.raises(validation.ValidationSafetyError, match="second connection"):
        guard.connect("administrator", "secret")

    assert raw.calls == [
        ("connect", None),
        ("get", ("Node", "AT", "Class_Name")),
        ("ls", PRESET_ROOT),
    ]


def test_guard_refuses_command_twenty_nine_before_transmission() -> None:
    raw = FakeRawPa2Client()
    guard = validation.ReadOnlyPa2Client(raw)

    guard.connect("administrator", "secret")
    for _ in range(27):
        guard.get(CURRENT_PRESET)

    with pytest.raises(validation.ValidationSafetyError, match="28-command limit"):
        guard.get(CURRENT_PRESET)

    assert len(raw.calls) == 28
    assert len(guard.records) == 28


def test_validation_bridge_rejects_an_out_of_sequence_read_before_transmission() -> None:
    raw = FakeRawPa2Client()
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=raw
    )

    with pytest.raises(validation.ValidationSafetyError, match="command sequence"):
        bridge.read_only_client.get(CURRENT_PRESET)

    assert raw.calls == []
    assert bridge.read_only_client.records == []


def test_catalog_response_controls_whether_a_fallback_read_is_allowed() -> None:
    raw = FakeRawPa2Client(catalog_has_current=True)
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=raw
    )
    bridge._connect_pa2()
    bridge.read_only_client.get(CURRENT_PRESET)

    with pytest.raises(validation.ValidationSafetyError, match="command sequence"):
        bridge.read_only_client.get(CURRENT_PRESET)

    assert raw.calls[-1] == ("get", CURRENT_PRESET)
    assert raw.calls.count(("get", CURRENT_PRESET)) == 1


def test_validation_bridge_uses_the_exact_read_only_two_poll_budget(monkeypatch) -> None:
    raw = FakeRawPa2Client()
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=raw
    )
    bridge._mqtt_connected = True
    monkeypatch.setattr(bridge, "_publish", lambda *args, **kwargs: None)

    bridge._poll_once()
    assert bridge._stop_event.is_set() is False
    bridge._poll_once()

    report = bridge.validation_report()
    assert bridge._stop_event.is_set() is True
    assert report.poll_count == 2
    assert report.records == validation.expected_command_records()
    assert report.command_count == 24
    assert report.verb_counts == {"connect": 1, "get": 17, "ls": 6}
    assert raw.calls == [(record.verb, record.path) for record in report.records]
    assert bridge.config.mqtt.state_poll_interval == 30.0
    assert bridge.config.mqtt.expose_meters is False
    assert bridge.config.mqtt.base_topic == "pa2bridge-validation/offline-test"
    assert bridge.config.mqtt.discovery_prefix == "pa2bridge-validation-offline-test"


@pytest.mark.parametrize("catalog_has_current", product((False, True), repeat=4))
def test_validation_bridge_accepts_documented_catalog_metadata_combinations(
    monkeypatch,
    catalog_has_current: tuple[bool, bool, bool, bool],
) -> None:
    raw = FakeRawPa2Client(catalog_has_current=catalog_has_current)
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=raw
    )
    bridge._mqtt_connected = True
    monkeypatch.setattr(bridge, "_publish", lambda *args, **kwargs: None)

    bridge._poll_once()
    bridge._poll_once()

    report = bridge.validation_report()
    assert report.poll_count == 2
    assert report.records == validation.expected_command_records(
        catalog_has_current=catalog_has_current
    )
    fallback_count = catalog_has_current.count(False)
    assert report.command_count == 24 + fallback_count
    assert report.verb_counts == {
        "connect": 1,
        "get": 17 + fallback_count,
        "ls": 6,
    }
    assert raw.calls == [(record.verb, record.path) for record in report.records]


def test_validation_report_rejects_an_incomplete_session() -> None:
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=FakeRawPa2Client()
    )

    with pytest.raises(validation.ValidationSafetyError, match="expected two polls"):
        bridge.validation_report()


def test_validation_bridge_stops_without_queueing_an_mqtt_command() -> None:
    raw = FakeRawPa2Client()
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=raw
    )
    message = SimpleNamespace(
        topic=f"{bridge.config.mqtt.base_topic}/command/mute/high_left",
        payload=b"On",
        retain=False,
    )

    bridge._on_message(None, None, message)

    assert bridge._stop_event.is_set()
    assert bridge._mqtt_failure is not None
    assert bridge._commands.empty()
    assert bridge._diagnostics.empty()
    assert raw.connected is False
    assert raw.calls == []


def test_forbidden_mqtt_input_prevents_a_success_report_after_poll_two(
    monkeypatch,
) -> None:
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=FakeRawPa2Client()
    )
    bridge._mqtt_connected = True
    monkeypatch.setattr(bridge, "_publish", lambda *args, **kwargs: None)
    bridge._poll_once()
    bridge._poll_once()
    message = SimpleNamespace(
        topic=f"{bridge.config.mqtt.base_topic}/command/mute/high_left",
        payload=b"On",
        retain=False,
    )

    bridge._on_message(None, None, message)

    with pytest.raises(validation.ValidationSafetyError, match="MQTT input"):
        bridge.validation_report()


def test_unexpected_mqtt_disconnect_prevents_a_success_report_after_poll_two(
    monkeypatch,
) -> None:
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=FakeRawPa2Client()
    )
    bridge._mqtt_connected = True
    bridge._mqtt_transport_connected = True
    monkeypatch.setattr(bridge, "_publish", lambda *args, **kwargs: None)
    bridge._poll_once()
    bridge._poll_once()

    bridge._on_disconnect(None, None, None, 7, None)

    assert bridge._mqtt_failure is not None
    with pytest.raises(validation.ValidationSafetyError, match="disconnect"):
        bridge.validation_report()


def test_expected_shutdown_disconnect_preserves_a_complete_success_report(
    monkeypatch,
) -> None:
    bridge = validation.ReadOnlyValidationBridge(
        make_config(), run_id="offline-test", pa2_client=FakeRawPa2Client()
    )
    bridge._mqtt_connected = True
    bridge._mqtt_transport_connected = True
    monkeypatch.setattr(bridge, "_publish", lambda *args, **kwargs: None)
    bridge._poll_once()
    bridge._poll_once()
    bridge._stopping = True

    bridge._on_disconnect(None, None, None, 0, None)

    assert bridge._mqtt_failure is None
    assert bridge.validation_report().command_count == 24


def test_main_prints_only_a_sanitized_success_report(monkeypatch, capsys) -> None:
    expected = validation.ValidationReport(
        poll_count=2,
        records=validation.expected_command_records(),
    )

    class FakeBridge:
        def __init__(self, config: AppConfig, *, run_id: str) -> None:
            assert config == make_config()
            assert run_id == "offline-test"

        def run_validation(self) -> validation.ValidationReport:
            return expected

    monkeypatch.setattr(validation, "load_config", lambda path: make_config())
    monkeypatch.setattr(validation, "ReadOnlyValidationBridge", FakeBridge)

    assert (
        validation.main(
            ["--config", "ignored.toml", "--run-id", "offline-test"]
        )
        == 0
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "command_count": 24,
        "poll_count": 2,
        "verb_counts": {"connect": 1, "get": 17, "ls": 6},
        "verified": True,
    }
    assert PA2_TEST_PASSWORD not in output
    assert MQTT_TEST_PASSWORD not in output
    assert "192.0.2.20" not in output


def test_main_suppresses_sensitive_inherited_failure_logging(
    monkeypatch,
    capsys,
    caplog,
) -> None:
    sensitive_detail = "192.0.2.20:19272 raw-response-value"

    def fail_with_sensitive_log(bridge) -> None:
        del bridge
        logging.getLogger("pa2bridge.mqtt_bridge").error(sensitive_detail)
        raise OSError(sensitive_detail)

    monkeypatch.setattr(validation, "load_config", lambda path: make_config())
    monkeypatch.setattr(validation.MqttBridge, "run_forever", fail_with_sensitive_log)
    caplog.set_level(logging.ERROR, logger="pa2bridge.mqtt_bridge")

    assert (
        validation.main(
            ["--config", "ignored.toml", "--run-id", "offline-test"]
        )
        == 2
    )

    output = capsys.readouterr().out
    assert json.loads(output) == {"error_class": "OSError", "verified": False}
    assert sensitive_detail not in output
    assert sensitive_detail not in caplog.text
    assert PA2_TEST_PASSWORD not in output
    assert MQTT_TEST_PASSWORD not in output
