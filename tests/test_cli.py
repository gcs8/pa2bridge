from __future__ import annotations

import importlib
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

from pa2bridge import cli
from pa2bridge.controller import DeviceIdentity, Pa2State, Preset, TelemetryError
from pa2bridge.mqtt_bridge import MqttPublishError


class FakeController:
    def __init__(self) -> None:
        self.activations: list[tuple[str, bool]] = []
        self.mute_calls: list[bool] = []
        self._state = Pa2State(
            identity=DeviceIdentity("dbxDriveRackPA2", "DriveRackPA2", "1.2.0.1"),
            current_preset=Preset(1, "Flat"),
            output_mutes={
                "high_left": False,
                "high_right": False,
                "mid_left": False,
                "mid_right": False,
                "low_left": False,
                "low_right": False,
            },
        )

    def state(self) -> Pa2State:
        return self._state

    def list_presets(self) -> list[Preset]:
        return [Preset(1, "Flat"), Preset(2, "Alternate")]

    def activate_preset(self, target: str, *, unmute_after: bool) -> Pa2State:
        self.activations.append((target, unmute_after))
        return self._state

    def set_all_outputs_muted(self, muted: bool):
        self.mute_calls.append(muted)
        return {channel: muted for channel in self._state.output_mutes}


@contextmanager
def _connected(fake: FakeController):
    yield fake


def test_probe_prints_machine_readable_observed_state(monkeypatch, capsys) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(["--config", "ignored.toml", "probe"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["device"]["firmware"] == "1.2.0.1"
    assert output["current_preset"] == {"slot": 1, "name": "Flat", "label": "1: Flat"}
    assert output["allowed_presets"] == [
        {"slot": 1, "name": "Flat", "label": "1: Flat"},
        {"slot": 2, "name": "Alternate", "label": "2: Alternate"},
    ]
    assert output["all_outputs_unmuted"] is True


def test_activate_defaults_to_post_recall_unmute(monkeypatch, capsys) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(["--config", "ignored.toml", "activate", "2: Alternate"]) == 0

    assert fake.activations == [("2: Alternate", True)]
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_no_unmute_is_explicit_opt_out(monkeypatch, capsys) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(
        ["--config", "ignored.toml", "activate", "2", "--no-unmute"]
    ) == 0

    assert fake.activations == [("2", False)]
    assert json.loads(capsys.readouterr().out)["verified"] is True


def test_unmute_uses_verified_controller_operation(monkeypatch, capsys) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(["--config", "ignored.toml", "unmute"]) == 0

    assert fake.mute_calls == [False]
    assert json.loads(capsys.readouterr().out) == {
        "action": "unmute",
        "verified": True,
    }


def test_mute_uses_verified_controller_operation(monkeypatch, capsys) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(["--config", "ignored.toml", "mute"]) == 0

    assert fake.mute_calls == [True]
    assert json.loads(capsys.readouterr().out) == {
        "action": "mute",
        "verified": True,
    }


def test_telemetry_error_uses_the_cli_json_error_contract(monkeypatch, capsys) -> None:
    fake = FakeController()

    def fail_state() -> Pa2State:
        raise TelemetryError("invalid device telemetry")

    fake.state = fail_state
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "connected_controller", lambda config: _connected(fake))

    assert cli.main(["--config", "ignored.toml", "probe"]) == 2

    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert error == {"error": "invalid device telemetry", "verified": False}


def test_mqtt_publish_error_uses_the_cli_json_error_contract(monkeypatch, capsys) -> None:
    class FailedBridge:
        def __init__(self, config) -> None:
            del config

        def run_forever(self) -> None:
            raise MqttPublishError("broker rejected publication")

    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace())
    monkeypatch.setattr(cli, "MqttBridge", FailedBridge)

    assert cli.main(["--config", "ignored.toml", "daemon"]) == 2

    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert error == {"error": "broker rejected publication", "verified": False}


def test_importing_dunder_main_does_not_execute_the_cli(monkeypatch) -> None:
    calls = 0

    def record_main() -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(cli, "main", record_main)
    sys.modules.pop("pa2bridge.__main__", None)
    try:
        importlib.import_module("pa2bridge.__main__")
    finally:
        sys.modules.pop("pa2bridge.__main__", None)

    assert calls == 0
