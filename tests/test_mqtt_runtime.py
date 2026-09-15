from __future__ import annotations

import json
import os
import signal
import socket
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from pa2bridge.config import AppConfig, MqttConfig, Pa2Config
from pa2bridge.controller import (
    ConnectionValidationError,
    CrossoverBand,
    CrossoverState,
    DeviceIdentity,
    InputMeters,
    Pa2State,
    Preset,
    TelemetryError,
)
from pa2bridge.mqtt_bridge import (
    DiscoveryStateError,
    IdentityRevalidationUnavailable,
    MqttBridge,
    MqttPublishError,
    _discover_mac_address,
    _discover_mac_address_from_home_assistant,
    _fsync_directory,
    _load_discovery_topics,
    _load_identity_state,
    _marker_payload,
    _resolve_ipv4_addresses,
    _save_discovery_topics,
    _state_directory_inner_marker,
    _state_directory_marker,
    _trusted_home_assistant_trackers,
)

TRACKER_TIMESTAMP = datetime.now(UTC).isoformat()
STALE_TRACKER_TIMESTAMP = (datetime.now(UTC) - timedelta(hours=3)).isoformat()


class FakeMqttClient:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.will = None
        self.credentials = None
        self.connected_to = None
        self.loop_started = 0
        self.loop_stopped = 0
        self.disconnected = 0
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_subscribe = None
        self.max_queued_messages = None
        self.max_inflight_messages = None
        self.reconnect_delays = None
        self.wait_for_publish_calls = 0
        self.subscribe_result = mqtt.MQTT_ERR_SUCCESS
        self.subscribe_mid = 1
        self.subscribe_reason_codes = [0]
        self.events: list[tuple[str, ...]] = []

    def username_pw_set(self, username, password) -> None:
        self.credentials = (username, password)

    def will_set(self, topic, payload, qos, retain) -> None:
        self.will = (topic, payload, qos, retain)

    def connect(self, host, port, keepalive) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started += 1
        if self.connected_to is not None and self.on_connect is not None:
            self.on_connect(
                self,
                None,
                None,
                mqtt.ReasonCode(mqtt.PacketTypes.CONNACK, "Success"),
                None,
            )
            if (
                self.subscriptions
                and self.on_subscribe is not None
                and self.subscribe_result == mqtt.MQTT_ERR_SUCCESS
            ):
                self.on_subscribe(
                    self,
                    None,
                    self.subscribe_mid,
                    self.subscribe_reason_codes,
                    None,
                )

    def loop_stop(self) -> None:
        self.loop_stopped += 1

    def disconnect(self) -> None:
        self.disconnected += 1
        self.events.append(("disconnect",))

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        self.events.append(("publish", topic, payload))
        def wait_for_publish(timeout=None):
            del timeout
            self.wait_for_publish_calls += 1
            self.events.append(("wait", topic))

        return SimpleNamespace(
            rc=mqtt.MQTT_ERR_SUCCESS,
            wait_for_publish=wait_for_publish,
            is_published=lambda: True,
        )

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        return self.subscribe_result, self.subscribe_mid

    def max_queued_messages_set(self, value) -> None:
        self.max_queued_messages = value

    def max_inflight_messages_set(self, value) -> None:
        self.max_inflight_messages = value

    def reconnect_delay_set(self, min_delay, max_delay) -> None:
        self.reconnect_delays = (min_delay, max_delay)


class FakePa2Client:
    def __init__(self) -> None:
        self.connected = False
        self.connection_generation = 0
        self.peer_ipv4: str | None = None
        self.connect_args = None
        self.closed = 0

    def connect(self, username, password) -> None:
        self.connect_args = (username, password)
        self.connected = True
        self.connection_generation += 1

    def connect_before(self, username, password, *, deadline) -> None:
        del deadline
        self.connect(username, password)

    def close(self) -> None:
        self.closed += 1
        self.connected = False


class FakeController:
    def __init__(self) -> None:
        self.activations = []
        self.all_mutes = []
        self.channel_mutes = []
        self.raise_keyboard_on_state = False
        self.identity_calls = 0
        self.state_identities: list[DeviceIdentity | None] = []
        self.preset_view_calls = 0
        self.identity_value = DeviceIdentity("dbxDriveRackPA2", "DriveRackPA2", "1.2.0.1")
        self.presets = [Preset(1, "Flat"), Preset(2, "Alternate")]
        self.all_presets = [*self.presets, Preset(3, "Factory")]
        self.crossover_value = CrossoverState(
            num_bands=1,
            mono_sub=True,
            bands=(
                CrossoverBand(
                    identifier="Band_1",
                    label="High",
                    high_pass_hz=None,
                    high_pass_type="LR 12",
                    gain_db=0.0,
                    low_pass_hz=None,
                    low_pass_type="LR 48",
                    polarity="Normal",
                ),
            ),
        )
        self.state_value = Pa2State(
            self.identity_value,
            self.presets[0],
            {
                "high_left": False,
                "high_right": False,
                "mid_left": False,
                "mid_right": False,
                "low_left": False,
                "low_right": False,
            },
        )

    def identity(self, *, deadline=None):
        del deadline
        self.identity_calls += 1
        return self.identity_value

    def list_presets(self, *, deadline=None):
        del deadline
        return self.presets

    def list_all_presets(self, *, deadline=None):
        del deadline
        return self.all_presets

    def list_preset_views(self, *, deadline=None):
        del deadline
        self.preset_view_calls += 1
        return self.presets, self.all_presets

    def crossover(self, *, deadline=None):
        del deadline
        return self.crossover_value

    def state(self, *, identity=None, deadline=None):
        del deadline
        if self.raise_keyboard_on_state:
            raise KeyboardInterrupt
        self.state_identities.append(identity)
        if identity is None:
            return self.state_value
        return Pa2State(
            identity,
            self.state_value.current_preset,
            self.state_value.output_mutes,
        )

    def output_levels(self, *, deadline=None):
        del deadline
        return {channel: -42.25 for channel in self.state_value.output_mutes}

    def input_meters(self, *, deadline=None):
        del deadline
        return InputMeters(
            levels_dbfs={"left": -18.45, "right": -19.55},
            clips={"left": False, "right": True},
        )

    def activate_preset(
        self,
        target,
        *,
        unmute_after,
        identity=None,
        start_deadline=None,
    ):
        del start_deadline
        del identity
        self.activations.append((target, unmute_after))
        return self.state_value

    def set_all_outputs_muted(self, muted, *, start_deadline=None):
        del start_deadline
        self.all_mutes.append(muted)

    def set_output_muted(self, channel, muted, *, start_deadline=None):
        del start_deadline
        self.channel_mutes.append((channel, muted))


def make_config(
    *,
    expose_meters=False,
    pa2_host="192.0.2.20",
    pa2_mac_address=None,
    replace_saved_identity=False,
):
    return AppConfig(
        pa2=Pa2Config(
            host=pa2_host,
            mac_address=pa2_mac_address,
            replace_saved_identity=replace_saved_identity,
            password="pa2-secret",
            allowed_preset_slots=(1, 2),
        ),
        mqtt=MqttConfig(
            host="homeassistant.local",
            username="bridge",
            password="mqtt-secret",
            expose_meters=expose_meters,
        ),
    )


def make_bridge(
    monkeypatch,
    *,
    expose_meters=False,
    pa2_host="192.0.2.20",
    pa2_mac_address=None,
    replace_saved_identity=False,
    home_assistant_token=None,
    discovery_state_path: Path | None = None,
):
    monkeypatch.setattr("pa2bridge.mqtt_bridge._discover_mac_address", lambda host: None)
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address_from_home_assistant",
        lambda host, token, *, timeout=3.0, deadline=None: None,
    )
    fake_mqtt = FakeMqttClient()
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.mqtt.Client",
        lambda *args, **kwargs: fake_mqtt,
    )
    bridge = MqttBridge(
        make_config(
            expose_meters=expose_meters,
            pa2_host=pa2_host,
            pa2_mac_address=pa2_mac_address,
            replace_saved_identity=replace_saved_identity,
        ),
        discovery_state_path=discovery_state_path,
        home_assistant_token=home_assistant_token,
    )
    fake_pa2 = FakePa2Client()
    fake_pa2.peer_ipv4 = pa2_host
    controller = FakeController()
    bridge.pa2_client = fake_pa2
    bridge.controller = controller
    bridge._preset_commands = frozenset(preset.label for preset in controller.presets)
    bridge._mqtt_connected = True
    return bridge, fake_mqtt, fake_pa2, controller


def test_custom_discovery_manifests_use_distinct_identity_state_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=tmp_path / "pa2-a.json",
    )
    second, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=tmp_path / "pa2-b.json",
    )

    assert first.identity_state_path == tmp_path / "pa2-a.identity.json"
    assert second.identity_state_path == tmp_path / "pa2-b.identity.json"


def test_identity_named_discovery_manifest_does_not_share_its_state_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    discovery_path = tmp_path / "identity.json"
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=discovery_path,
    )

    assert bridge.identity_state_path == tmp_path / "identity.identity.json"
    assert bridge.identity_state_path != discovery_path


def test_explicit_identity_state_cannot_alias_discovery_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("pa2bridge.mqtt_bridge.mqtt.Client", FakeMqttClient)
    state_path = tmp_path / "state.json"

    with pytest.raises(
        DiscoveryStateError,
        match="discovery and identity state paths must be different",
    ):
        MqttBridge(
            make_config(),
            discovery_state_path=state_path,
            identity_state_path=tmp_path / "nested" / ".." / "state.json",
        )


def message(topic: str, payload: str, *, retain: bool = False):
    return SimpleNamespace(topic=topic, payload=payload.encode(), retain=retain)


def test_connect_builds_discovery_and_on_connect_publishes_and_subscribes(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)

    bridge._connect_pa2()
    bridge._on_connect(client, None, None, mqtt.ReasonCode(mqtt.PacketTypes.CONNACK, "Success"), None)
    bridge._on_subscribe(client, None, client.subscribe_mid, [0], None)
    bridge._poll_once()

    assert pa2.connect_args == ("administrator", "pa2-secret")
    assert bridge.device.identifier == "driverack_pa2_192_0_2_20"
    assert client.subscriptions == [("driverack/pa2/command/#", 1)]
    assert any(topic.startswith("homeassistant/select/") for topic, *_ in client.published)
    assert ("driverack/pa2/status", "online", 1, True) in client.published
    assert ("driverack/pa2/state/firmware", "1.2.0.1", 1, True) in client.published
    published = {topic: payload for topic, payload, *_ in client.published}
    inventory = json.loads(published["driverack/pa2/state/preset_inventory"])
    crossover = json.loads(published["driverack/pa2/state/crossover"])
    assert inventory["count"] == 3
    assert inventory["presets"][2] == {
        "label": "3: Factory",
        "name": "Factory",
        "slot": 3,
    }
    assert crossover["summary"] == "1 band + mono sub"
    assert crossover["bands"][0]["high_pass_hz"] is None
    assert ("driverack/pa2/status/details", "online", 1, True) in client.published


def test_mac_identity_and_discovery_topics_stay_stable_when_host_changes(monkeypatch) -> None:
    mac_address = "02:00:5e:10:00:01"
    discovered_hosts: list[str] = []
    first, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.10",
    )
    second, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
    )

    def discover(host: str) -> str:
        discovered_hosts.append(host)
        return mac_address

    monkeypatch.setattr("pa2bridge.mqtt_bridge._discover_mac_address", discover)

    first._connect_pa2()
    second._connect_pa2()

    assert discovered_hosts == ["192.0.2.10", "192.0.2.20"]
    assert first.device.identifier == "driverack_pa2_02005e100001"
    assert second.device.identifier == first.device.identifier
    assert [message.topic for message in second.discovery] == [
        message.topic for message in first.discovery
    ]


def _write_on_link_route(
    tmp_path: Path,
    interfaces: tuple[str, ...] = ("eth0",),
) -> Path:
    route_path = tmp_path / "route"
    route_path.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        + "".join(
            f"{interface} 000200C0 00000000 0001 0 0 0 00FFFFFF 0 0 0\n"
            for interface in interfaces
        ),
        encoding="ascii",
    )
    return route_path


def test_discovers_complete_unicast_mac_for_configured_ipv4_address(tmp_path: Path) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.20 0x1 0x0 02:00:5e:10:00:02 * eth0\n"
        "192.0.2.20 0x1 0x2 02:00:5E:10:00:01 * eth0\n",
        encoding="ascii",
    )

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=_write_on_link_route(tmp_path),
    ) == "02:00:5e:10:00:01"


@pytest.mark.parametrize(
    "row",
    [
        "192.0.2.20 0x2 0x2 02:00:5e:10:00:01 * eth0\n",
        "192.0.2.20 0x1 -0x2 02:00:5e:10:00:01 * eth0\n",
        "192.0.2.20 0x1 invalid 02:00:5e:10:00:01 * eth0\n",
    ],
)
def test_neighbor_discovery_rejects_unsupported_hardware_and_flags(
    tmp_path: Path,
    row: str,
) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n" + row,
        encoding="ascii",
    )

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=_write_on_link_route(tmp_path),
    ) is None


def test_neighbor_discovery_rejects_conflicting_entries(tmp_path: Path) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:01 * eth0\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:02 * eth0\n",
        encoding="ascii",
    )

    with pytest.raises(DiscoveryStateError, match="conflicting PA2 MAC"):
        _discover_mac_address(
            "192.0.2.20",
            arp_path=arp_path,
            route_path=_write_on_link_route(tmp_path),
        )


def test_neighbor_discovery_rejects_oversized_table(tmp_path: Path) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_bytes(b"x" * (64 * 1024 + 1))

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=_write_on_link_route(tmp_path),
    ) is None


def test_neighbor_discovery_rejects_proxy_arp_for_routed_peer(tmp_path: Path) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:01 * eth0\n",
        encoding="ascii",
    )
    route_path = tmp_path / "route"
    route_path.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "eth0 00000000 010200C0 0003 0 0 0 00000000 0 0 0\n",
        encoding="ascii",
    )

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=route_path,
    ) is None


@pytest.mark.parametrize(
    "route_rows",
    [
        (
            "eth0 000200C0 00000000 0001 0 0 0 00FFFFFF 0 0 0\n"
            "eth0 140200C0 invalid 0007 0 0 0 FFFFFFFF 0 0 0\n"
        ),
        "eth0 140200C0 00000000 0205 0 0 0 FFFFFFFF 0 0 0\n",
        (
            "eth0 000200C0 00000000 0001 0 0 0 00FFFFFF 0 0 0\n"
            "eth1 000200C0 00000000 0001 0 0 0 00FFFFFF 0 0 0\n"
        ),
    ],
)
def test_neighbor_discovery_fails_closed_on_unsafe_route_tables(
    tmp_path: Path,
    route_rows: str,
) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:01 * eth0\n",
        encoding="ascii",
    )
    route_path = tmp_path / "route"
    route_path.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        + route_rows,
        encoding="ascii",
    )

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=route_path,
    ) is None


def test_neighbor_discovery_uses_lowest_metric_on_link_route(tmp_path: Path) -> None:
    arp_path = tmp_path / "arp"
    arp_path.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:01 * eth0\n"
        "192.0.2.20 0x1 0x2 02:00:5e:10:00:02 * eth1\n",
        encoding="ascii",
    )
    route_path = tmp_path / "route"
    route_path.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "eth0 000200C0 00000000 0001 0 0 5 00FFFFFF 0 0 0\n"
        "eth1 000200C0 00000000 0001 0 0 10 00FFFFFF 0 0 0\n",
        encoding="ascii",
    )

    assert _discover_mac_address(
        "192.0.2.20",
        arp_path=arp_path,
        route_path=route_path,
    ) == "02:00:5e:10:00:01"


def test_hostname_with_multiple_ipv4_addresses_is_not_auto_correlated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.21", 0)),
        ],
    )

    assert _resolve_ipv4_addresses("pa2.example.test") == set()


def test_automatic_mac_identity_uses_configured_pa2_address(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch, pa2_host="192.0.2.20")
    calls: list[str] = []

    def discover(host: str) -> str:
        calls.append(host)
        return "02:00:5e:10:00:01"

    monkeypatch.setattr("pa2bridge.mqtt_bridge._discover_mac_address", discover)

    bridge._connect_pa2()

    assert calls == ["192.0.2.20"]
    assert bridge.device is not None
    assert bridge.device.identifier == "driverack_pa2_02005e100001"
    assert bridge.device.mac_address == "02:00:5e:10:00:01"


def test_automatic_mac_identity_uses_authenticated_tcp_peer(monkeypatch) -> None:
    bridge, _, pa2, _ = make_bridge(monkeypatch, pa2_host="pa2.example.test")
    pa2.peer_ipv4 = "192.0.2.20"
    calls: list[str] = []

    def discover(host: str) -> str:
        calls.append(host)
        return "02:00:5e:10:00:01"

    monkeypatch.setattr("pa2bridge.mqtt_bridge._discover_mac_address", discover)

    bridge._connect_pa2()

    assert calls == ["192.0.2.20"]
    assert bridge.device is not None
    assert bridge.device.identifier == "driverack_pa2_02005e100001"


def test_automatic_mac_identity_fails_closed_after_lookup_failure(
    monkeypatch,
) -> None:
    bridge, _, pa2, _ = make_bridge(monkeypatch, pa2_host="192.0.2.20")
    results = iter(("02:00:5e:10:00:01", None))
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: next(results),
    )

    bridge._connect_pa2()
    assert bridge.device is not None
    first_identifier = bridge.device.identifier
    pa2.connection_generation += 1
    bridge._pa2_identity = None
    with pytest.raises(IdentityRevalidationUnavailable, match="could not be revalidated"):
        bridge._identity_for_connection()

    assert first_identifier == "driverack_pa2_02005e100001"


def test_address_fallback_never_authorizes_reconnect(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    bridge._connect_pa2()

    with pytest.raises(ConnectionValidationError, match="without a stable MAC"):
        bridge._validate_reconnected_peer(time.monotonic() + 5)


def test_address_fallback_rejects_later_bridge_connection_before_pa2_operations(
    monkeypatch,
) -> None:
    bridge, _, pa2, controller = make_bridge(monkeypatch)
    bridge._connect_pa2()
    pa2.close()
    operations: list[tuple[int, str]] = []

    def unexpected_operation(name: str):
        def operation(*args, **kwargs):
            del args, kwargs
            operations.append((pa2.connection_generation, name))
            raise AssertionError(
                f"replacement PA2 {name} ran before identity validation"
            )

        return operation

    for name in (
        "identity",
        "list_presets",
        "list_all_presets",
        "list_preset_views",
        "crossover",
        "state",
        "output_levels",
        "input_meters",
        "activate_preset",
        "set_all_outputs_muted",
        "set_output_muted",
    ):
        monkeypatch.setattr(controller, name, unexpected_operation(name))

    with pytest.raises(
        IdentityRevalidationUnavailable,
        match="could not be revalidated",
    ):
        bridge._connect_pa2()

    assert pa2.connection_generation == 2
    assert operations == []


def test_automatic_mac_identity_survives_address_change_when_revalidated(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()
    first._publish_discovery(first.discovery)

    second, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.99",
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    second._connect_pa2()

    assert second.device is not None
    assert second.device.identifier == "driverack_pa2_02005e100001"


def test_persisted_mac_identity_fails_closed_on_restart_lookup_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()
    identity_state = state_path.with_name("discovery-state.identity.json")
    assert json.loads(identity_state.read_text(encoding="utf-8")) == {
        "version": 1,
        "host": "192.0.2.20",
        "mac_address": "02:00:5e:10:00:01",
    }
    assert stat.S_IMODE(identity_state.stat().st_mode) == 0o600

    second, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    with pytest.raises(IdentityRevalidationUnavailable, match="could not be revalidated"):
        second._connect_pa2()


def test_persisted_mac_identity_rejects_conflicting_rediscovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()

    second, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:02",
    )

    with pytest.raises(DiscoveryStateError, match="conflicts with persisted"):
        second._connect_pa2()


def test_explicit_mac_override_rejects_persisted_identity_conflict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()

    second, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
        pa2_mac_address="02:00:5e:10:00:02",
    )

    with pytest.raises(DiscoveryStateError, match="configured.*conflicts"):
        second._connect_pa2()


def test_explicit_replacement_updates_persisted_identity(monkeypatch, tmp_path: Path) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()
    first._publish_discovery(first.discovery)
    identity_path = state_path.with_name("discovery-state.identity.json")

    second, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
        pa2_mac_address="02:00:5e:10:00:02",
        replace_saved_identity=True,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:02",
    )
    second._connect_pa2()

    assert second.device is not None
    assert second.device.identifier == "driverack_pa2_02005e100002"
    assert json.loads(identity_path.read_text())["mac_address"] == "02:00:5e:10:00:01"

    real_wait = second._wait_for_discovery_publications
    wait_calls = 0

    def fail_current_publications(results, *, deadline):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 2:
            raise MqttPublishError("current discovery was not acknowledged")
        return real_wait(results, deadline=deadline)

    monkeypatch.setattr(second, "_wait_for_discovery_publications", fail_current_publications)
    with pytest.raises(MqttPublishError, match="not acknowledged"):
        second._publish_discovery(second.discovery)
    assert json.loads(identity_path.read_text())["mac_address"] == "02:00:5e:10:00:01"

    monkeypatch.setattr(second, "_wait_for_discovery_publications", real_wait)
    second._publish_discovery(second.discovery)
    assert json.loads(identity_path.read_text()) == {
        "version": 1,
        "host": "192.0.2.20",
        "mac_address": "02:00:5e:10:00:02",
    }


def test_identity_state_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text(
        '{"host":"192.0.2.20","mac_address":"02:00:5e:10:00:01","version":1}\n',
        encoding="utf-8",
    )
    target.chmod(0o600)
    link = tmp_path / "identity.json"
    link.symlink_to(target)

    with pytest.raises(DiscoveryStateError, match="could not read identity state"):
        _load_identity_state(link)


def test_address_change_without_mac_revalidation_stops_identity_churn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    first, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    first._connect_pa2()

    second, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.99",
        discovery_state_path=state_path,
    )

    with pytest.raises(IdentityRevalidationUnavailable, match="could not be revalidated"):
        second._connect_pa2()


def test_home_assistant_network_data_resolves_mac_across_layer_three(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._trusted_home_assistant_trackers",
        lambda entity_ids, token, *, timeout, deadline: entity_ids,
    )
    response = json.dumps(
        [
            {
                "entity_id": "device_tracker.synthetic_pa2",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.20",
                    "mac": "02-00-5E-10-00-01",
                },
            },
            {
                "entity_id": "device_tracker.other",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.21",
                    "mac": "02:00:5e:10:00:02",
                },
            },
        ]
    ).encode()
    requests = []
    read_sizes: list[int] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            assert size > len(response)
            return response

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("pa2bridge.mqtt_bridge.urlopen", urlopen)

    assert _discover_mac_address_from_home_assistant(
        "192.0.2.20",
        "synthetic-supervisor-token",
    ) == "02:00:5e:10:00:01"
    request, timeout = requests[0]
    assert request.full_url == "http://supervisor/core/api/states"
    assert request.headers["Authorization"] == "Bearer synthetic-supervisor-token"
    assert 0 < timeout <= 3.0
    assert read_sizes == [16 * 1024 * 1024 + 1]


def test_home_assistant_network_data_rejects_oversized_response(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            return b"x" * size

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.urlopen",
        lambda request, *, timeout: Response(),
    )

    assert (
        _discover_mac_address_from_home_assistant(
            "192.0.2.20",
            "synthetic-supervisor-token",
        )
        is None
    )


def test_home_assistant_network_data_rejects_conflicting_mac_matches(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._trusted_home_assistant_trackers",
        lambda entity_ids, token, *, timeout, deadline: entity_ids,
    )
    response = json.dumps(
        [
            {
                "entity_id": "device_tracker.first",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.20",
                    "mac": "02:00:5e:10:00:01",
                },
            },
            {
                "entity_id": "device_tracker.second",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.20",
                    "mac": "02:00:5e:10:00:02",
                },
            },
        ]
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            del size
            return response

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.urlopen",
        lambda request, *, timeout: Response(),
    )

    with pytest.raises(DiscoveryStateError, match="conflicting PA2 MAC"):
        _discover_mac_address_from_home_assistant(
            "192.0.2.20",
            "synthetic-supervisor-token",
        )


@pytest.mark.parametrize(
    (
        "entity_id",
        "state",
        "source_type",
        "tracking_type",
        "authorized",
        "last_reported",
    ),
    [
        ("sensor.synthetic", "home", "router", "connection", True, TRACKER_TIMESTAMP),
        (
            "device_tracker.synthetic",
            "unavailable",
            "router",
            "connection",
            True,
            TRACKER_TIMESTAMP,
        ),
        (
            "device_tracker.synthetic",
            "home",
            "gps",
            "connection",
            True,
            TRACKER_TIMESTAMP,
        ),
        (
            "device_tracker.synthetic",
            "home",
            "router",
            "presence",
            True,
            TRACKER_TIMESTAMP,
        ),
        (
            "device_tracker.synthetic",
            "home",
            "router",
            "connection",
            False,
            TRACKER_TIMESTAMP,
        ),
        (
            "device_tracker.synthetic",
            "home",
            "router",
            "connection",
            True,
            STALE_TRACKER_TIMESTAMP,
        ),
        (
            "device_tracker.synthetic",
            "home",
            "router",
            "connection",
            True,
            "0001-01-01T00:00:00+14:00",
        ),
    ],
)
def test_home_assistant_network_data_rejects_untrusted_state_sources(
    monkeypatch,
    entity_id: str,
    state: str,
    source_type: str,
    tracking_type: str,
    authorized: bool,
    last_reported: str,
) -> None:
    response = json.dumps(
        [
            {
                "entity_id": entity_id,
                "state": state,
                "last_reported": last_reported,
                "attributes": {
                    "source_type": source_type,
                    "tracking_type": tracking_type,
                    "authorized": authorized,
                    "ip": "192.0.2.20",
                    "mac": "02:00:5e:10:00:01",
                },
            }
        ]
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            del size
            return response

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.urlopen",
        lambda request, *, timeout: Response(),
    )

    assert (
        _discover_mac_address_from_home_assistant(
            "192.0.2.20",
            "synthetic-supervisor-token",
        )
        is None
    )


@pytest.mark.parametrize("trusted_domain", ["unifi", "unifi_insights"])
def test_home_assistant_tracker_provenance_allows_only_network_integrations(
    monkeypatch,
    trusted_domain: str,
) -> None:
    entity_ids = frozenset(
        {
            "device_tracker.unifi_pa2",
            "device_tracker.synthetic_template",
        }
    )
    responses = iter(
        (
            json.dumps(
                {
                    "device_tracker.unifi_pa2": "trusted-entry",
                    "device_tracker.synthetic_template": "untrusted-entry",
                }
            ).encode(),
            json.dumps(
                [
                    {"entry_id": "trusted-entry", "domain": trusted_domain},
                    {"entry_id": "untrusted-entry", "domain": "template"},
                ]
            ).encode(),
        )
    )
    requests = []
    timeouts = []
    monotonic_values = iter((100.0, 102.0))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            payload = next(responses)
            assert len(payload) < size
            return payload

    def urlopen(request, *, timeout):
        requests.append(request)
        timeouts.append(timeout)
        return Response()

    monkeypatch.setattr("pa2bridge.mqtt_bridge.urlopen", urlopen)
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.time.monotonic",
        lambda: next(monotonic_values),
    )

    assert _trusted_home_assistant_trackers(
        entity_ids,
        "synthetic-supervisor-token",
        timeout=3.0,
        deadline=103.0,
    ) == frozenset({"device_tracker.unifi_pa2"})
    assert requests[0].full_url == "http://supervisor/core/api/template"
    assert requests[0].get_method() == "POST"
    assert requests[1].full_url.endswith("/api/config/config_entries/entry")
    assert timeouts == [3.0, 1.0]


def test_home_assistant_network_requests_share_one_absolute_deadline(
    monkeypatch,
) -> None:
    states = json.dumps(
        [
            {
                "entity_id": "device_tracker.unifi_pa2",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.20",
                    "mac": "02:00:5e:10:00:01",
                },
            }
        ]
    ).encode()
    mapping = json.dumps(
        {"device_tracker.unifi_pa2": "trusted-entry"}
    ).encode()
    entries = json.dumps(
        [{"entry_id": "trusted-entry", "domain": "unifi"}]
    ).encode()
    responses = iter((states, mapping, entries))
    monotonic_values = iter((100.0, 103.0, 104.0))
    timeouts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            payload = next(responses)
            assert len(payload) < size
            return payload

    def urlopen(request, *, timeout):
        timeouts.append(timeout)
        return Response()

    monkeypatch.setattr("pa2bridge.mqtt_bridge.urlopen", urlopen)
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.time.monotonic",
        lambda: next(monotonic_values),
    )

    assert _discover_mac_address_from_home_assistant(
        "192.0.2.20",
        "synthetic-supervisor-token",
        timeout=3.0,
        deadline=105.0,
    ) == "02:00:5e:10:00:01"
    assert timeouts == [3.0, 2.0, 1.0]


def test_home_assistant_network_data_rejects_untrusted_tracker_provenance(
    monkeypatch,
) -> None:
    states = json.dumps(
        [
            {
                "entity_id": "device_tracker.synthetic_template",
                "state": "home",
                "last_reported": TRACKER_TIMESTAMP,
                "attributes": {
                    "source_type": "router",
                    "tracking_type": "connection",
                    "authorized": True,
                    "ip": "192.0.2.20",
                    "mac": "02:00:5e:10:00:01",
                },
            }
        ]
    ).encode()
    mapping = json.dumps(
        {"device_tracker.synthetic_template": "untrusted-entry"}
    ).encode()
    entries = json.dumps(
        [{"entry_id": "untrusted-entry", "domain": "template"}]
    ).encode()

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int) -> bytes:
            assert len(self.payload) < size
            return self.payload

    def urlopen(request, *, timeout):
        if request.full_url.endswith("/api/states"):
            return Response(states)
        if request.full_url.endswith("/api/template"):
            return Response(mapping)
        return Response(entries)

    monkeypatch.setattr("pa2bridge.mqtt_bridge.urlopen", urlopen)

    assert (
        _discover_mac_address_from_home_assistant(
            "192.0.2.20",
            "synthetic-supervisor-token",
        )
        is None
    )


def test_bridge_uses_home_assistant_mac_when_local_arp_cannot_cross_router(
    monkeypatch,
) -> None:
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        home_assistant_token="synthetic-supervisor-token",
    )
    calls: list[tuple[str, str]] = []

    def discover_from_ha(
        host: str,
        token: str,
        *,
        timeout: float,
        deadline: float | None,
    ) -> str:
        assert timeout <= 3.0
        assert deadline is not None
        calls.append((host, token))
        return "02:00:5e:10:00:01"

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address_from_home_assistant",
        discover_from_ha,
    )

    bridge._connect_pa2()

    assert calls == [("192.0.2.20", "synthetic-supervisor-token")]
    assert bridge.device is not None
    assert bridge.device.identifier == "driverack_pa2_02005e100001"


def test_explicit_mac_override_is_used_when_live_discovery_is_unavailable(
    monkeypatch,
) -> None:
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
        home_assistant_token="synthetic-supervisor-token",
    )
    calls: list[str] = []

    def unavailable_locally(host: str) -> None:
        calls.append("local")
        return None

    def unavailable_in_ha(
        host: str,
        token: str,
        *,
        timeout: float,
        deadline: float | None,
    ) -> None:
        assert deadline is not None
        calls.append("ha")
        return None

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        unavailable_locally,
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address_from_home_assistant",
        unavailable_in_ha,
    )

    bridge._connect_pa2()

    assert bridge.device is not None
    assert bridge.device.identifier == "driverack_pa2_02005e100001"
    assert calls == ["local", "ha"]


def test_explicit_mac_override_rejects_conflicting_live_peer(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:02",
    )

    with pytest.raises(DiscoveryStateError, match="conflicts with the connected peer"):
        bridge._connect_pa2()


def test_local_mac_discovery_skips_home_assistant_fallback(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        home_assistant_token="synthetic-supervisor-token",
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address",
        lambda host: "02:00:5e:10:00:01",
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._discover_mac_address_from_home_assistant",
        lambda host, token, *, timeout, deadline: pytest.fail(
            "Home Assistant discovery must not run"
        ),
    )

    bridge._connect_pa2()

    assert bridge.device is not None
    assert bridge.device.identifier == "driverack_pa2_02005e100001"


def test_healthy_startup_logs_mqtt_pa2_and_discovery_milestones(
    monkeypatch,
    caplog,
) -> None:
    bridge, client, _, _ = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    caplog.set_level("INFO", logger="pa2bridge.mqtt_bridge")

    bridge._mqtt_connected = False
    original_poll = bridge._poll_once

    def poll_once_and_stop() -> None:
        original_poll()
        bridge._stop_event.set()

    monkeypatch.setattr(bridge, "_poll_once", poll_once_and_stop)

    bridge.run_forever()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "connected to MQTT broker; command subscription ready",
        "connected to PA2 'DriveRackPA2' at 192.0.2.20:19272 "
        "(firmware '1.2.0.1'); 2 presets available",
        "published MQTT discovery for 'DriveRackPA2': 12 entities, "
        "0 stale topics removed",
    ]
    assert "pa2-secret" not in caplog.text
    assert "mqtt-secret" not in caplog.text


def test_transient_identity_revalidation_failure_retries_without_app_exit(
    monkeypatch,
    caplog,
) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    caplog.set_level("WARNING", logger="pa2bridge.mqtt_bridge")

    def temporary_failure() -> None:
        bridge._stop_event.set()
        raise IdentityRevalidationUnavailable("network tracker temporarily unavailable")

    monkeypatch.setattr(bridge, "_poll_once", temporary_failure)

    bridge.run_forever()

    assert any(
        "retrying safely" in record.getMessage() and "optional PA2 MAC" in record.getMessage()
        for record in caplog.records
    )


def test_discovery_publish_clears_persisted_old_topics_before_current_topics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    old_bridge, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.10",
        discovery_state_path=state_path,
    )
    old_bridge._connect_pa2()
    old_bridge._publish_discovery(old_bridge.discovery)
    old_topics = sorted(
        message.topic for message in old_bridge.discovery if message.payload
    )

    bridge, client, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()

    bridge._poll_once()

    config_publishes = [
        (topic, payload)
        for topic, payload, _, retain in client.published
        if topic.endswith("/config") and retain
    ]
    assert config_publishes[: len(old_topics)] == [
        (topic, "") for topic in old_topics
    ]
    first_current = next(
        index for index, (_, payload) in enumerate(config_publishes) if payload
    )
    assert first_current == len(old_topics)
    last_cleanup_ack = max(
        client.events.index(("wait", topic)) for topic in old_topics
    )
    first_current_publish = client.events.index(
        ("publish", bridge.discovery[0].topic, bridge.discovery[0].payload)
    )
    assert last_cleanup_ack < first_current_publish
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    expected_topics = sorted(
        message.topic for message in bridge.discovery if message.payload
    )
    assert persisted == {"version": 1, "topics": expected_topics}
    assert all(topic not in persisted["topics"] for topic in old_topics)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_mac_migration_clears_host_identity_before_publishing_current_topics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    old_bridge, _, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        discovery_state_path=state_path,
    )
    old_bridge._connect_pa2()
    old_bridge._publish_discovery(old_bridge.discovery)
    old_topics = sorted(message.topic for message in old_bridge.discovery if message.payload)

    bridge, client, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.20",
        pa2_mac_address="02:00:5e:10:00:01",
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()
    bridge._publish_discovery(bridge.discovery)

    current_topics = sorted(message.topic for message in bridge.discovery if message.payload)
    config_publishes = [
        (topic, payload)
        for topic, payload, _, retain in client.published
        if topic.endswith("/config") and retain
    ]
    assert config_publishes[: len(old_topics)] == [
        (topic, "") for topic in old_topics
    ]
    assert all(topic.startswith("homeassistant/") for topic in current_topics)
    assert all("driverack_pa2_02005e100001" in topic for topic in current_topics)
    first_current_publish = min(
        client.events.index(("publish", topic, payload))
        for topic, payload in config_publishes
        if payload
    )
    last_old_ack = max(client.events.index(("wait", topic)) for topic in old_topics)
    assert last_old_ack < first_current_publish
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "topics": current_topics,
    }


@pytest.mark.parametrize(
    "state",
    [
        '{"version":1,"topics":["homeassistant/select/driverack_pa2_host/preset/config",7]}',
        '{"version":1.0,"topics":[]}',
        '{"version":1,"topics":["homeassistant/light/unrelated/device/config"]}',
        '{"version":1,"topics":["homeassistant/select/driverack_pa2_host/preset/config","homeassistant/select/driverack_pa2_host/preset/config"]}',
        r'{"version":1,"topics":["homeassistant/select/driverack_pa2_host/\ud800/config"]}',
    ],
)
def test_malformed_discovery_state_fails_closed(
    monkeypatch,
    tmp_path: Path,
    state: str,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    state_path.write_text(state, encoding="utf-8")
    fake_mqtt = FakeMqttClient()
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.mqtt.Client",
        lambda *args, **kwargs: fake_mqtt,
    )

    with pytest.raises(DiscoveryStateError, match="discovery state"):
        MqttBridge(make_config(), discovery_state_path=state_path)

    assert fake_mqtt.published == []


def test_discovery_state_read_is_bounded_before_size_validation() -> None:
    read_sizes: list[int] = []

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            del args

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    class StatePath:
        def open(self, mode: str) -> Reader:
            assert mode == "rb"
            return Reader()

    with pytest.raises(DiscoveryStateError, match="size limit"):
        _load_discovery_topics(StatePath())  # type: ignore[arg-type]

    assert read_sizes == [64 * 1024 + 1]


def test_discovery_ack_failure_tracks_partial_publication_for_future_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    old_topic = "homeassistant/select/driverack_pa2_192_0_2_10/preset/config"
    state_path.write_text(
        json.dumps({"version": 1, "topics": [old_topic]}) + "\n",
        encoding="utf-8",
    )
    bridge, client, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()
    original_publish = client.publish
    current_count = 0

    def fail_second_current(topic, payload, qos, retain):
        nonlocal current_count
        result = original_publish(topic, payload, qos, retain)
        if payload and topic.endswith("/config"):
            current_count += 1
            if current_count == 2:
                result.is_published = lambda: False
        return result

    client.publish = fail_second_current

    with pytest.raises(MqttPublishError, match="acknowledgement timed out"):
        bridge._publish_discovery(bridge.discovery)

    current_topics = {
        message.topic for message in bridge.discovery if message.payload
    }
    pending_topics = current_topics | {old_topic}
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "topics": sorted(pending_topics),
    }
    assert bridge._persisted_discovery_topics == pending_topics

    next_bridge, next_client, _, _ = make_bridge(
        monkeypatch,
        pa2_host="192.0.2.30",
        discovery_state_path=state_path,
    )
    next_bridge._connect_pa2()
    next_bridge._publish_discovery(next_bridge.discovery)
    config_publishes = [
        (topic, payload)
        for topic, payload, _, retain in next_client.published
        if topic.endswith("/config") and retain
    ]
    assert config_publishes[: len(pending_topics)] == [
        (topic, "") for topic in sorted(pending_topics)
    ]


def test_discovery_state_replace_failure_preserves_previous_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    original = json.dumps(
        {
            "version": 1,
            "topics": [
                "homeassistant/select/driverack_pa2_192_0_2_10/preset/config"
            ],
        }
    ) + "\n"
    state_path.write_text(original, encoding="utf-8")
    bridge, client, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()

    def fail_replace(source, target) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.os.replace",
        fail_replace,
    )

    with pytest.raises(DiscoveryStateError, match="could not write"):
        bridge._publish_discovery(bridge.discovery)

    assert state_path.read_text(encoding="utf-8") == original
    assert [entry.name for entry in tmp_path.iterdir()] == [state_path.name]
    assert client.published == []


def test_discovery_state_cleanup_failure_preserves_primary_error_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()
    original_unlink = os.unlink

    def fail_replace(source, target) -> None:
        del source, target
        raise OSError("replace failed")

    def fail_unlink(path) -> None:
        del path
        raise OSError("unlink failed")

    monkeypatch.setattr("pa2bridge.mqtt_bridge.os.replace", fail_replace)
    monkeypatch.setattr("pa2bridge.mqtt_bridge.os.unlink", fail_unlink)

    with pytest.raises(DiscoveryStateError, match="replace failed"):
        bridge._publish_discovery(bridge.discovery)

    for entry in tmp_path.iterdir():
        original_unlink(entry)


def test_discovery_state_write_error_precedes_descriptor_close_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    temporary_path = tmp_path / ".discovery.json.temporary"
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.tempfile.mkstemp",
        lambda **kwargs: (12345, str(temporary_path)),
    )
    monkeypatch.setattr("pa2bridge.mqtt_bridge.os.fchmod", lambda *args: None)
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.os.write",
        lambda *args: (_ for _ in ()).throw(OSError("write-primary")),
    )
    original_close = os.close

    def fail_temporary_close(descriptor: int) -> None:
        if descriptor == 12345:
            raise OSError("close-secondary")
        original_close(descriptor)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.os.close",
        fail_temporary_close,
    )

    with pytest.raises(DiscoveryStateError, match="write-primary"):
        _save_discovery_topics(
            tmp_path / "discovery.json",
            frozenset(
                {
                    "homeassistant/select/driverack_pa2_host/preset/config"
                }
            ),
        )


def test_new_state_directory_entries_are_fsynced(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "new" / "nested" / "discovery.json"
    synced: list[Path] = []
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        lambda path: synced.append(path),
    )

    _save_discovery_topics(
        state_path,
        frozenset(
            {"homeassistant/select/driverack_pa2_host/preset/config"}
        ),
    )

    assert synced == [
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path / "new",
        tmp_path / "new",
        tmp_path / "new",
        tmp_path / "new",
        state_path.parent,
        state_path.parent,
    ]
    assert not list(tmp_path.rglob(".pa2bridge-state-dir-*.pending"))


def test_state_save_does_not_fsync_preexisting_ancestors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "new" / "nested" / "discovery.json"
    forbidden_ancestor = tmp_path.parent
    original_fsync_directory = _fsync_directory
    synced: list[Path] = []

    def reject_preexisting_ancestor(path: Path) -> None:
        synced.append(path)
        if path == forbidden_ancestor:
            raise PermissionError("ancestor is traversal-only")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        reject_preexisting_ancestor,
    )

    _save_discovery_topics(
        state_path,
        frozenset(
            {"homeassistant/select/driverack_pa2_host/preset/config"}
        ),
    )

    assert forbidden_ancestor not in synced


@pytest.mark.parametrize("collision", ["file", "directory", "symlink"])
def test_unowned_state_directory_marker_collision_fails_closed(
    tmp_path: Path,
    collision: str,
) -> None:
    directory = tmp_path / "new"
    marker = _state_directory_marker(directory)
    if collision == "file":
        marker.write_text("unrelated", encoding="utf-8")
    elif collision == "directory":
        marker.mkdir()
    else:
        marker.symlink_to("unrelated")

    with pytest.raises(DiscoveryStateError, match="invalid.*marker"):
        _save_discovery_topics(
            directory / "discovery.json",
            frozenset(
                {"homeassistant/select/driverack_pa2_host/preset/config"}
            ),
        )

    assert not directory.exists()
    if collision == "file":
        assert marker.read_text(encoding="utf-8") == "unrelated"
    elif collision == "directory":
        assert marker.is_dir()
    else:
        assert marker.is_symlink()
        assert os.readlink(marker) == "unrelated"


def test_valid_pending_state_directory_marker_is_recovered(tmp_path: Path) -> None:
    directory = tmp_path / "new"
    marker = _state_directory_marker(directory)
    marker.symlink_to(_marker_payload(directory))

    _save_discovery_topics(
        directory / "discovery.json",
        frozenset(
            {"homeassistant/select/driverack_pa2_host/preset/config"}
        ),
    )

    assert directory.is_dir()
    assert not marker.exists() and not marker.is_symlink()


def test_unowned_inner_state_directory_marker_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "new"
    directory.mkdir()
    marker = _state_directory_inner_marker(directory)
    marker.write_text("unrelated", encoding="utf-8")

    with pytest.raises(DiscoveryStateError, match="invalid.*marker"):
        _save_discovery_topics(
            directory / "discovery.json",
            frozenset(
                {"homeassistant/select/driverack_pa2_host/preset/config"}
            ),
        )

    assert marker.read_text(encoding="utf-8") == "unrelated"


def test_replaced_parent_marker_is_not_moved_or_deleted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "new"
    marker = _state_directory_marker(directory)
    original_fsync_directory = _fsync_directory
    replaced = False

    def replace_after_validation(path: Path) -> None:
        nonlocal replaced
        if path == tmp_path and not replaced:
            replaced = True
            marker.unlink()
            marker.write_text("unrelated", encoding="utf-8")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        replace_after_validation,
    )

    with pytest.raises(DiscoveryStateError, match="invalid.*marker"):
        _save_discovery_topics(
            directory / "discovery.json",
            frozenset(
                {"homeassistant/select/driverack_pa2_host/preset/config"}
            ),
        )

    assert marker.read_text(encoding="utf-8") == "unrelated"
    assert not _state_directory_inner_marker(directory).exists()


def test_replaced_inner_marker_is_not_deleted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "new"
    directory.mkdir()
    marker = _state_directory_inner_marker(directory)
    marker.symlink_to(_marker_payload(directory))
    original_fsync_directory = _fsync_directory
    replaced = False

    def replace_after_validation(path: Path) -> None:
        nonlocal replaced
        if path == tmp_path and not replaced:
            replaced = True
            marker.unlink()
            marker.write_text("unrelated", encoding="utf-8")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        replace_after_validation,
    )

    with pytest.raises(DiscoveryStateError, match="invalid.*marker"):
        _save_discovery_topics(
            directory / "discovery.json",
            frozenset(
                {"homeassistant/select/driverack_pa2_host/preset/config"}
            ),
        )

    assert marker.read_text(encoding="utf-8") == "unrelated"


def test_inner_state_directory_marker_is_recovered_after_rename_fsync_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "new"
    state_path = directory / "discovery.json"
    original_fsync_directory = _fsync_directory
    parent_attempts = 0

    def fail_after_marker_move(path: Path) -> None:
        nonlocal parent_attempts
        if path == tmp_path:
            parent_attempts += 1
            if parent_attempts == 3:
                raise OSError("marker move fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        fail_after_marker_move,
    )
    topics = frozenset(
        {"homeassistant/select/driverack_pa2_host/preset/config"}
    )

    with pytest.raises(DiscoveryStateError, match="marker move fsync failed"):
        _save_discovery_topics(state_path, topics)
    inner_marker = _state_directory_inner_marker(directory)
    assert inner_marker.is_symlink()

    _save_discovery_topics(state_path, topics)

    assert state_path.exists()
    assert not inner_marker.exists() and not inner_marker.is_symlink()


def test_failed_ancestor_directory_fsync_is_retried(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "new" / "nested" / "discovery.json"
    original_fsync_directory = _fsync_directory
    attempts: list[Path] = []
    failed = False
    tmp_path_attempts = 0

    def fail_once(path: Path) -> None:
        nonlocal failed, tmp_path_attempts
        attempts.append(path)
        if path == tmp_path:
            tmp_path_attempts += 1
        if path == tmp_path and tmp_path_attempts == 2 and not failed:
            failed = True
            raise OSError("ancestor fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        fail_once,
    )
    topics = frozenset(
        {"homeassistant/select/driverack_pa2_host/preset/config"}
    )

    with pytest.raises(DiscoveryStateError, match="ancestor fsync failed"):
        _save_discovery_topics(state_path, topics)
    assert (tmp_path / "new").is_dir()
    assert list(tmp_path.glob(".pa2bridge-state-dir-*.pending"))
    attempts.clear()
    _save_discovery_topics(state_path, topics)

    assert tmp_path in attempts
    assert not list(tmp_path.glob(".pa2bridge-state-dir-*.pending"))


def test_visible_manifest_after_parent_fsync_failure_is_resaved_before_publish(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery.json"
    bridge, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()
    original_fsync_directory = _fsync_directory
    failed = False

    def fail_final_parent_fsync(path: Path) -> None:
        nonlocal failed
        if path == tmp_path and state_path.exists() and not failed:
            failed = True
            raise OSError("final parent fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        fail_final_parent_fsync,
    )
    with pytest.raises(DiscoveryStateError, match="final parent fsync failed"):
        bridge._publish_discovery(bridge.discovery)
    assert state_path.exists()

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._fsync_directory",
        original_fsync_directory,
    )
    original_save = _save_discovery_topics
    save_calls = 0

    def record_save(path: Path, topics: frozenset[str]) -> None:
        nonlocal save_calls
        save_calls += 1
        original_save(path, topics)

    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge._save_discovery_topics",
        record_save,
    )
    retry_bridge, _, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    retry_bridge._connect_pa2()
    retry_bridge._publish_discovery(retry_bridge.discovery)

    assert save_calls == 1


def test_directory_fsync_error_precedes_close_error(monkeypatch) -> None:
    monkeypatch.setattr("pa2bridge.mqtt_bridge.os.open", lambda *args: 12345)
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.os.fsync",
        lambda *args: (_ for _ in ()).throw(OSError("fsync-primary")),
    )
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.os.close",
        lambda *args: (_ for _ in ()).throw(OSError("close-secondary")),
    )

    with pytest.raises(OSError, match="fsync-primary"):
        _fsync_directory(Path("ignored"))


def test_final_state_replace_failure_leaves_durable_pending_superset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discovery-state.json"
    old_topic = "homeassistant/select/driverack_pa2_192_0_2_10/preset/config"
    state_path.write_text(
        json.dumps({"version": 1, "topics": [old_topic]}) + "\n",
        encoding="utf-8",
    )
    bridge, client, _, _ = make_bridge(
        monkeypatch,
        discovery_state_path=state_path,
    )
    bridge._connect_pa2()
    original_replace = os.replace
    replace_calls = 0

    def fail_second_replace(source, target) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("replace failed")
        original_replace(source, target)

    monkeypatch.setattr("pa2bridge.mqtt_bridge.os.replace", fail_second_replace)

    with pytest.raises(DiscoveryStateError, match="could not write"):
        bridge._publish_discovery(bridge.discovery)

    current_topics = {
        message.topic for message in bridge.discovery if message.payload
    }
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "topics": sorted(current_topics | {old_topic}),
    }
    assert any(payload for topic, payload, *_ in client.published if topic.endswith("/config"))
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [state_path.name]


def test_discovery_ack_waits_share_one_aggregate_deadline(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    waits: list[float] = []

    def result():
        return SimpleNamespace(
            wait_for_publish=lambda timeout: waits.append(timeout),
            is_published=lambda: True,
        )

    clock = iter((100.0, 101.0, 103.5, 104.0))
    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: next(clock))

    bridge._wait_for_discovery_publications(
        [("one/config", result()), ("two/config", result())],
        deadline=105.0,
    )

    assert waits == [5.0, 1.5]


def test_discovery_ack_after_aggregate_deadline_is_rejected(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    waits: list[float] = []
    result = SimpleNamespace(
        wait_for_publish=lambda timeout: waits.append(timeout),
        is_published=lambda: True,
    )
    clock = iter((100.0, 106.0))
    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: next(clock))

    with pytest.raises(MqttPublishError, match="timed out"):
        bridge._wait_for_discovery_publications(
            [("one/config", result)],
            deadline=105.0,
        )

    assert waits == [5.0]
    assert bridge._mqtt_connected is False
    assert bridge._stop_event.is_set()


def test_poll_reuses_identity_until_the_pa2_connection_generation_changes(monkeypatch) -> None:
    bridge, _, pa2, controller = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    bridge._connect_pa2()
    bridge._discovery_published = True
    bridge._details_valid = True
    bridge._last_detail_slot = 1
    bridge._last_detail_refresh = time.monotonic()

    bridge._poll_once()
    assert controller.identity_calls == 1
    assert controller.state_identities == [controller.identity_value]

    pa2.connection_generation += 1
    bridge._poll_once()
    assert controller.identity_calls == 2
    assert controller.state_identities == [
        controller.identity_value,
        controller.identity_value,
    ]


def test_poll_refreshes_identity_before_republishing_discovery(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    bridge._connect_pa2()
    bridge._discovery_published = False
    bridge._details_valid = True
    bridge._last_detail_refresh = time.monotonic()
    bridge._last_detail_slot = controller.state_value.current_preset.slot
    pa2.connection_generation += 1
    controller.identity_value = DeviceIdentity(
        "dbxDriveRackPA2", "Renamed PA2", "1.2.0.2"
    )

    bridge._poll_once()

    discovery_payloads = [
        json.loads(payload)
        for topic, payload, *_ in client.published
        if topic.startswith("homeassistant/") and payload
    ]
    assert discovery_payloads
    assert all(
        payload["device"]["sw_version"] == "1.2.0.2"
        for payload in discovery_payloads
    )


def test_publish_details_uses_one_preset_catalog_snapshot(monkeypatch) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)

    bridge.publish_details()

    assert controller.preset_view_calls == 1


def test_mute_commands_reuse_identity_from_the_current_connection(monkeypatch) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)
    bridge._connect_pa2()

    bridge._on_message(
        None, None, message("driverack/pa2/command/unmute", "PRESS")
    )
    assert bridge._process_queued_command() is True
    bridge._on_message(
        None, None, message("driverack/pa2/command/mute/high_left", "On")
    )
    assert bridge._process_queued_command() is True

    assert controller.identity_calls == 1
    assert controller.state_identities == [
        controller.identity_value,
        controller.identity_value,
    ]


def test_recall_reconnect_refreshes_identity_before_publishing_state(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    bridge._connect_pa2()
    bridge._discovery_published = True

    def reconnecting_activation(
        payload, *, unmute_after, identity=None, start_deadline=None
    ):
        del payload, unmute_after, identity, start_deadline
        pa2.connection_generation += 1
        controller.identity_value = DeviceIdentity(
            "dbxDriveRackPA2", "DriveRackPA2", "1.2.0.2"
        )
        return controller.state_value

    controller.activate_preset = reconnecting_activation
    bridge._on_message(
        None, None, message("driverack/pa2/command/preset", "2: Alternate")
    )

    assert bridge._process_queued_command() is True
    assert controller.identity_calls == 2
    assert bridge.device is not None
    assert bridge.device.name == "DriveRackPA2"
    assert bridge.device.firmware == "1.2.0.2"
    assert bridge._discovery_published is True
    discovery_payloads = [
        json.loads(message.payload)
        for message in bridge.discovery
        if message.payload
    ]
    assert discovery_payloads
    assert all(
        payload["device"]["sw_version"] == "1.2.0.2"
        for payload in discovery_payloads
    )
    assert any(
        topic.startswith("homeassistant/")
        and payload
        and json.loads(payload)["device"]["sw_version"] == "1.2.0.2"
        for topic, payload, *_ in client.published
    )
    assert (
        "driverack/pa2/state/firmware",
        "1.2.0.2",
        1,
        True,
    ) in client.published


def test_initial_pa2_failure_connects_mqtt_and_marks_device_offline(monkeypatch) -> None:
    bridge, client, pa2, _ = make_bridge(monkeypatch)
    bridge._mqtt_connected = False

    def unavailable(*, deadline=None) -> None:
        del deadline
        raise OSError("PA2 unavailable")

    def stop_after_retry_delay(timeout: float | None = None) -> None:
        del timeout
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "_connect_pa2", unavailable)
    monkeypatch.setattr(bridge._stop_event, "wait", stop_after_retry_delay)

    bridge.run_forever()

    assert client.connected_to == ("homeassistant.local", 1883, 30)
    assert ("driverack/pa2/status", "offline", 1, True) in client.published
    assert pa2.closed >= 1


def test_connect_callback_survives_pa2_failure_and_publishes_offline(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    bridge._connect_pa2()

    def unavailable(*, identity=None, deadline=None):
        del identity, deadline
        raise OSError("PA2 unavailable")

    controller.state = unavailable
    bridge._on_connect(client, None, None, 0, None)
    bridge._on_subscribe(client, None, client.subscribe_mid, [0], None)
    with pytest.raises(OSError, match="PA2 unavailable"):
        bridge._poll_once()

    assert pa2.connected is False
    status_messages = [item for item in client.published if item[0] == "driverack/pa2/status"]
    assert [item[1] for item in status_messages] == ["offline"]


def test_command_routes_cover_preset_unmute_and_per_channel_mute(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    bridge._on_message(None, None, message("driverack/pa2/command/preset", "2: Alternate"))
    assert controller.activations == []
    assert bridge._process_queued_command() is True
    bridge._on_message(None, None, message("driverack/pa2/command/unmute", "PRESS"))
    assert bridge._process_queued_command() is True
    bridge._on_message(None, None, message("driverack/pa2/command/mute/high_left", "On"))
    assert bridge._process_queued_command() is True
    assert bridge._process_queued_command() is False

    assert controller.activations == [("2: Alternate", True)]
    assert controller.all_mutes == [False]
    assert controller.channel_mutes == [("high_left", True)]
    discovery_publishes = [
        item for item in client.published if item[0].endswith("/config")
    ]
    assert client.wait_for_publish_calls == len(discovery_publishes)
    last_commands = [payload for topic, payload, *_ in client.published if topic.endswith("last_command")]
    assert last_commands[-1] == "high_left mute verified On"


def test_active_preset_command_reports_preserved_mute_state(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    controller.state_value = Pa2State(
        controller.identity_value,
        controller.presets[0],
        {**controller.state_value.output_mutes, "high_left": True},
    )

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "1: Flat"),
    )
    assert bridge._process_queued_command() is True

    last_commands = [
        payload
        for topic, payload, *_ in client.published
        if topic.endswith("last_command")
    ]
    assert last_commands[-1] == "recalled 1: Flat; output mute state preserved"


def test_preset_command_reports_verified_unmuted_state(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "1: Flat"),
    )
    assert bridge._process_queued_command() is True

    last_commands = [
        payload
        for topic, payload, *_ in client.published
        if topic.endswith("last_command")
    ]
    assert last_commands[-1] == "recalled 1: Flat; outputs verified unmuted"


def test_command_queued_before_disconnect_is_discarded_and_pa2_closed(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "1: Flat"),
    )

    bridge._on_disconnect(client, None, None, 1, None)

    assert bridge._process_queued_command() is False
    assert controller.activations == []
    assert pa2.closed == 1


def test_disconnect_after_final_session_check_prevents_command_actuation(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/unmute", "PRESS"),
    )
    events: list[str] = []
    original_unmute = controller.set_all_outputs_muted

    def record_unmute(muted: bool, *, start_deadline=None):
        del start_deadline
        events.append("controller")
        return original_unmute(muted)

    controller.set_all_outputs_muted = record_unmute
    original_lock = bridge._mqtt_state_lock

    class DisconnectAfterSecondAuthorizationBoundary:
        outer_depth = 0
        outer_exits = 0

        def __enter__(self):
            self.outer_depth += 1
            return original_lock.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            result = original_lock.__exit__(exc_type, exc_value, traceback)
            self.outer_depth -= 1
            if self.outer_depth == 0:
                self.outer_exits += 1
                if self.outer_exits == 2:
                    events.append("disconnect")
                    bridge._on_disconnect(client, None, None, 1, None)
            return result

    bridge._mqtt_state_lock = DisconnectAfterSecondAuthorizationBoundary()

    assert bridge._process_queued_command() is True
    assert events == ["controller", "disconnect"]


def test_nested_output_command_topic_is_rejected_without_actuation(monkeypatch) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/mute/extra/high_left", "On"),
    )

    assert bridge._process_queued_command() is False
    assert bridge._process_queued_diagnostic() is True
    assert controller.channel_mutes == []


def test_publish_exception_marks_mqtt_failed_and_stops(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    def explode(topic, payload, qos, retain):
        raise ValueError("synthetic publish failure")

    client.publish = explode

    with pytest.raises(MqttPublishError, match="MQTT publish raised"):
        bridge._publish("driverack/pa2/status", "online", retain=True)

    assert bridge._mqtt_connected is False
    assert isinstance(bridge._mqtt_failure, MqttPublishError)
    assert bridge._stop_event.is_set()


def test_bad_command_is_reported_without_raising_out_of_callback(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    bridge._on_message(None, None, message("driverack/pa2/command/unmute", "STALE"))
    assert client.published == []
    assert bridge._process_queued_diagnostic() is True

    errors = [payload for topic, payload, *_ in client.published if topic.endswith("last_command")]
    assert errors and errors[-1].startswith("ERROR:")


def test_arbitrary_preset_payload_is_never_retained_or_logged(
    monkeypatch,
    caplog,
) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    sentinel = "PRIVATE-OPERATOR-NOTE-7f91"

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", sentinel),
    )

    assert bridge._process_queued_command() is False
    assert bridge._process_queued_diagnostic() is True
    assert controller.activations == []
    assert sentinel not in caplog.text
    assert all(sentinel not in payload for _, payload, *_ in client.published)


def test_retained_command_is_rejected_without_device_write(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "1: Flat", retain=True),
    )
    assert client.published == []
    assert bridge._process_queued_diagnostic() is True

    assert controller.activations == []
    errors = [payload for topic, payload, *_ in client.published if topic.endswith("last_command")]
    assert errors[-1] == "ERROR: command rejected"


def test_non_utf8_command_is_reported_without_killing_mqtt_callback(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    malformed = SimpleNamespace(
        topic="driverack/pa2/command/preset",
        payload=b"\xff\xfe",
    )

    bridge._on_message(None, None, malformed)
    assert client.published == []
    assert bridge._process_queued_diagnostic() is True

    errors = [payload for topic, payload, *_ in client.published if topic.endswith("last_command")]
    assert errors and errors[-1].startswith("ERROR:")


def test_callback_rejection_never_publishes_or_raises_under_backpressure(
    monkeypatch,
) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    def queue_full(topic, payload, qos, retain):
        client.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_QUEUE_SIZE)

    client.publish = queue_full

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "NOT-ALLOWED", retain=True),
    )

    assert client.published == []
    with pytest.raises(RuntimeError, match="MQTT publish failed"):
        bridge._process_queued_diagnostic()


def test_publish_state_includes_opt_in_nonretained_live_meters(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch, expose_meters=True)

    bridge.publish_state(controller.state_value)

    meter_messages = [item for item in client.published if "/state/level/" in item[0]]
    assert len(meter_messages) == 8
    output_messages = [item for item in meter_messages if "/input_" not in item[0]]
    input_messages = [item for item in meter_messages if "/input_" in item[0]]
    assert all(
        payload == "-42.2" and retain is False
        for _, payload, _, retain in output_messages
    )
    assert [payload for _, payload, *_ in input_messages] == ["-18.4", "-19.6"]
    clip_messages = [item for item in client.published if "/state/clip/" in item[0]]
    assert [payload for _, payload, *_ in clip_messages] == ["OFF", "ON"]
    assert all(retain is False for *_, retain in clip_messages)


def test_mqtt_client_uses_bounded_outgoing_queues(monkeypatch) -> None:
    _, client, _, _ = make_bridge(monkeypatch)

    assert client.max_queued_messages == 100
    assert client.max_inflight_messages == 20
    assert client.reconnect_delays == (1, 30)


def test_mqtt_client_enables_automatic_reconnect(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    captured: dict[str, object] = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeMqttClient()

    monkeypatch.setattr(mqtt, "Client", client_factory)
    MqttBridge(bridge.config)

    assert captured["reconnect_on_failure"] is True


def test_disconnect_invalidates_sessions_and_waits_for_fresh_subscription(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    bridge._details_valid = True
    bridge._discovery_published = True
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "2: Alternate"),
    )
    generation = bridge._mqtt_generation

    bridge._on_disconnect(client, None, None, 7, None)

    assert bridge._mqtt_connected is False
    assert bridge._mqtt_failure is None
    assert bridge._mqtt_generation == generation + 1
    assert not bridge._stop_event.is_set()
    assert bridge._mqtt_state_changed.is_set()
    assert bridge._details_valid is False
    assert bridge._discovery_published is False
    assert pa2.closed >= 1
    assert bridge._process_queued_command() is False
    assert controller.activations == []
    assert client.disconnected == 0

    bridge._on_connect(client, None, None, 0, None)
    assert bridge._mqtt_connected is False
    bridge._on_subscribe(client, None, client.subscribe_mid, [0], None)
    assert bridge._mqtt_connected is True
    bridge._publish("driverack/pa2/status", "online", retain=True)
    assert ("driverack/pa2/status", "online", 1, True) in client.published


def test_expired_queued_command_is_discarded_without_pa2_access(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    now = 100.0
    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: now)
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "2: Alternate"),
    )
    now = 105.0

    assert bridge._process_queued_command() is True

    assert controller.activations == []
    assert pa2.closed == 0
    assert client.published[-1] == (
        "driverack/pa2/state/last_command",
        "ERROR: stale command discarded",
        1,
        True,
    )


def test_command_age_starts_when_the_mqtt_callback_receives_it(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    now = 100.0
    timestamp_captured = threading.Event()

    def monotonic() -> float:
        timestamp_captured.set()
        return now

    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", monotonic)
    bridge._mqtt_state_lock.acquire()
    callback = threading.Thread(
        target=bridge._on_message,
        args=(
            None,
            None,
            message("driverack/pa2/command/preset", "2: Alternate"),
        ),
    )
    callback.start()
    try:
        assert timestamp_captured.wait(timeout=1)
        now = 105.0
    finally:
        bridge._mqtt_state_lock.release()
        callback.join(timeout=2)

    assert callback.is_alive() is False
    assert bridge._process_queued_command() is True
    assert controller.activations == []
    assert pa2.closed == 0
    assert client.published[-1][1] == "ERROR: stale command discarded"


def test_command_expiring_after_dequeue_is_rechecked_before_pa2_access(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    now = 100.0
    precheck_finished = threading.Event()
    clock_calls = 0

    def monotonic() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls >= 2:
            precheck_finished.set()
        return now

    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", monotonic)
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "2: Alternate"),
    )
    bridge._pa2_lock.acquire()
    worker = threading.Thread(target=bridge._process_queued_command)
    worker.start()
    try:
        assert precheck_finished.wait(timeout=1)
        now = 105.0
    finally:
        bridge._pa2_lock.release()
        worker.join(timeout=2)

    assert worker.is_alive() is False
    assert controller.activations == []
    assert pa2.closed == 0
    assert client.published[-1] == (
        "driverack/pa2/state/last_command",
        "ERROR: stale command discarded",
        1,
        True,
    )


@pytest.mark.parametrize(
    ("topic", "payload", "operation"),
    [
        ("preset", "2: Alternate", "preset"),
        ("unmute", "PRESS", "all"),
        ("mute/high_left", "On", "single"),
    ],
)
def test_command_deadline_reaches_the_actuator_transaction_boundary(
    monkeypatch,
    topic: str,
    payload: str,
    operation: str,
) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)
    now = 100.0
    deadlines: list[float] = []
    identity_deadlines: list[float] = []
    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: now)

    def identity(*, deadline: float):
        identity_deadlines.append(deadline)
        return controller.identity_value

    def activate(target, *, unmute_after, identity, start_deadline: float):
        del target, unmute_after, identity
        deadlines.append(start_deadline)
        return controller.state_value

    def set_all(muted: bool, *, start_deadline: float) -> None:
        assert muted is False
        deadlines.append(start_deadline)

    def set_single(channel: str, muted: bool, *, start_deadline: float) -> None:
        assert (channel, muted) == ("high_left", True)
        deadlines.append(start_deadline)

    monkeypatch.setattr(controller, "identity", identity)
    if operation == "preset":
        monkeypatch.setattr(controller, "activate_preset", activate)
    elif operation == "all":
        monkeypatch.setattr(controller, "set_all_outputs_muted", set_all)
    else:
        monkeypatch.setattr(controller, "set_output_muted", set_single)
    bridge._on_message(
        None,
        None,
        message(f"driverack/pa2/command/{topic}", payload),
    )

    bridge._process_queued_command()

    assert deadlines == [105.0]
    if operation == "preset":
        assert identity_deadlines == [105.0]


@pytest.mark.parametrize(
    ("topic", "payload", "operation"),
    [
        ("preset", "2: Alternate", "preset"),
        ("unmute", "PRESS", "all"),
        ("mute/high_left", "On", "single"),
    ],
)
def test_post_command_reads_use_full_read_cycle_deadline(
    monkeypatch,
    topic: str,
    payload: str,
    operation: str,
) -> None:
    bridge, _, pa2, controller = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    identity_deadlines: list[float] = []
    state_deadlines: list[float] = []
    publish_deadlines: list[float] = []
    detail_deadlines: list[float] = []
    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: 100.0)

    def identity(*, deadline: float):
        identity_deadlines.append(deadline)
        return controller.identity_value

    def activate(target, *, unmute_after, identity, start_deadline):
        del target, unmute_after, identity, start_deadline
        pa2.connection_generation += 1
        return controller.state_value

    def state(*, identity, deadline: float):
        state_deadlines.append(deadline)
        return Pa2State(
            identity,
            controller.state_value.current_preset,
            controller.state_value.output_mutes,
        )

    monkeypatch.setattr(controller, "identity", identity)
    monkeypatch.setattr(controller, "state", state)
    if operation == "preset":
        monkeypatch.setattr(controller, "activate_preset", activate)
    elif operation == "all":
        monkeypatch.setattr(
            controller,
            "set_all_outputs_muted",
            lambda muted, *, start_deadline: None,
        )
    else:
        monkeypatch.setattr(
            controller,
            "set_output_muted",
            lambda channel, muted, *, start_deadline: None,
        )
    monkeypatch.setattr(
        bridge,
        "publish_state",
        lambda state, *, deadline: publish_deadlines.append(deadline),
    )
    monkeypatch.setattr(
        bridge,
        "_refresh_details",
        lambda *, current_slot, deadline: detail_deadlines.append(deadline),
    )

    bridge._on_message(
        None,
        None,
        message(f"driverack/pa2/command/{topic}", payload),
    )
    bridge._process_queued_command()

    assert publish_deadlines == [160.0]
    if operation == "preset":
        assert identity_deadlines == [105.0, 160.0]
        assert state_deadlines == []
        assert detail_deadlines == [160.0]
    else:
        assert identity_deadlines == [160.0]
        assert state_deadlines == [160.0]
        assert detail_deadlines == []


def test_disconnect_before_suback_keeps_startup_gate_closed(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    bridge._on_connect(client, None, None, 0, None)
    assert bridge._mqtt_ready.is_set() is False

    bridge._on_disconnect(client, None, None, 7, None)

    assert bridge._mqtt_failure is None
    assert bridge._mqtt_connected is False
    assert bridge._mqtt_ready.is_set() is False

    bridge._on_connect(client, None, None, 0, None)
    bridge._on_subscribe(client, None, client.subscribe_mid, [0], None)
    assert bridge._mqtt_ready.is_set() is True
    assert bridge._mqtt_connected is True


def test_startup_gate_recovers_from_disconnect_before_suback(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    def reconnect_and_subscribe() -> None:
        assert client.on_connect is not None
        assert client.on_subscribe is not None
        client.on_connect(client, None, None, 0, None)
        client.on_subscribe(client, None, client.subscribe_mid, [0], None)
        bridge._stop_event.set()

    def disconnecting_loop_start() -> None:
        client.loop_started += 1
        assert client.on_connect is not None
        assert client.on_disconnect is not None
        client.on_connect(client, None, None, 0, None)
        client.on_disconnect(client, None, None, 7, None)
        threading.Timer(0.01, reconnect_and_subscribe).start()

    client.loop_start = disconnecting_loop_start  # type: ignore[method-assign]

    bridge.run_forever()

    assert client.loop_started == 1
    assert client.loop_stopped == 1
    assert bridge._mqtt_generation == 1


def test_startup_gate_allows_transient_disconnect_after_suback(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    def reconnect_after_startup_disconnect() -> None:
        assert client.on_connect is not None
        assert client.on_subscribe is not None
        client.on_connect(client, None, None, 0, None)
        client.on_subscribe(client, None, client.subscribe_mid, [0], None)
        bridge._stop_event.set()

    def disconnecting_loop_start() -> None:
        client.loop_started += 1
        assert client.on_connect is not None
        assert client.on_subscribe is not None
        assert client.on_disconnect is not None
        client.on_connect(client, None, None, 0, None)
        client.on_subscribe(client, None, client.subscribe_mid, [0], None)
        client.on_disconnect(client, None, None, 7, None)
        threading.Timer(0.01, reconnect_after_startup_disconnect).start()

    client.loop_start = disconnecting_loop_start  # type: ignore[method-assign]

    bridge.run_forever()

    assert client.loop_started == 1
    assert client.loop_stopped == 1
    assert bridge._mqtt_generation == 1


def test_failed_subscribe_result_never_marks_mqtt_ready(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    bridge._mqtt_connected = False
    client.subscribe_result = mqtt.MQTT_ERR_NO_CONN

    bridge._on_connect(client, None, None, 0, None)

    assert bridge._mqtt_failure is not None
    assert bridge._mqtt_connected is False


def test_failed_suback_forces_fresh_client_restart(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    bridge._mqtt_connected = False
    bridge._on_connect(client, None, None, 0, None)

    bridge._on_subscribe(client, None, client.subscribe_mid, [128], None)

    assert bridge._mqtt_failure is not None
    assert bridge._mqtt_connected is False


@pytest.mark.parametrize("failure", ["subscribe_result", "suback"])
def test_subscription_failure_publishes_both_availability_domains_offline_before_disconnect(
    monkeypatch, failure
) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    base = bridge.config.mqtt.base_topic
    client.publish(f"{base}/status/details", "online", qos=1, retain=True)
    client.publish(f"{base}/status", "online", qos=1, retain=True)
    if failure == "subscribe_result":
        client.subscribe_result = mqtt.MQTT_ERR_NO_CONN
    else:
        client.subscribe_reason_codes = [128]

    with pytest.raises(MqttPublishError):
        bridge.run_forever()

    disconnect_index = client.events.index(("disconnect",))
    for topic in (f"{base}/status/details", f"{base}/status"):
        offline = ("publish", topic, "offline")
        assert offline in client.events
        assert client.events.index(offline) < disconnect_index


def test_subscription_failure_preserves_lwt_when_offline_cannot_be_published(
    monkeypatch,
) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    client.subscribe_result = mqtt.MQTT_ERR_NO_CONN

    def no_connection(topic, payload, qos, retain):
        del topic, payload, qos, retain
        return SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN)

    client.publish = no_connection

    with pytest.raises(MqttPublishError):
        bridge.run_forever()

    assert client.disconnected == 0


@pytest.mark.parametrize(("mid", "rejected"), [(1, False), (2, True)])
def test_suback_requires_the_pending_mid_and_clears_it(
    monkeypatch, mid, rejected
) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    bridge._on_connect(client, None, None, 0, None)

    bridge._on_subscribe(client, None, mid, [0], None)

    assert (bridge._mqtt_failure is not None) is rejected
    assert bridge._pending_subscribe_mid is None


@pytest.mark.parametrize("callback", ["connect", "subscribe"])
def test_connection_callbacks_serialize_mqtt_state_updates(monkeypatch, callback) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)

    class CountingLock:
        def __init__(self) -> None:
            self.entries = 0
            self.lock = threading.RLock()

        def __enter__(self):
            self.entries += 1
            return self.lock.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            return self.lock.__exit__(exc_type, exc_value, traceback)

    state_lock = CountingLock()
    bridge._mqtt_state_lock = state_lock
    bridge._mqtt_connected = False

    if callback == "connect":
        bridge._on_connect(client, None, None, 0, None)
    else:
        bridge._on_subscribe(client, None, client.subscribe_mid, [0], None)

    assert state_lock.entries == 1


def test_queue_full_publish_fails_closed_before_online(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    def queue_full(topic, payload, qos, retain):
        client.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_QUEUE_SIZE)

    client.publish = queue_full

    with pytest.raises(RuntimeError, match="MQTT publish failed"):
        bridge.publish_state(controller.state_value)

    assert bridge._mqtt_connected is False
    assert not any(
        topic == "driverack/pa2/status" and payload == "online"
        for topic, payload, *_ in client.published
    )


def test_failed_detail_refresh_marks_retained_details_unavailable(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    bridge.publish_details()

    def invalid_crossover(*, deadline=None):
        del deadline
        raise OSError("crossover unavailable")

    controller.crossover = invalid_crossover
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "2: Alternate"),
    )
    bridge._process_queued_command()

    detail_statuses = [
        payload
        for topic, payload, *_ in client.published
        if topic == "driverack/pa2/status/details"
    ]
    assert detail_statuses[-3:] == ["online", "offline", "offline"]


def test_failed_detail_refresh_is_retried_on_next_healthy_poll(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True
    bridge._discovery_published = True
    real_crossover = controller.crossover
    attempts = 0

    def transient_crossover(*, deadline=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient crossover failure")
        return real_crossover(deadline=deadline)

    controller.crossover = transient_crossover

    bridge._poll_once()
    bridge._poll_once()

    crossover_payloads = [
        payload
        for topic, payload, *_ in client.published
        if topic == "driverack/pa2/state/crossover"
    ]
    assert attempts == 2
    assert len(crossover_payloads) == 1
    assert ("driverack/pa2/status/details", "online", 1, True) in client.published


def test_observed_preset_change_refreshes_crossover_details(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True
    bridge._discovery_published = True

    bridge._poll_once()
    before = sum(
        topic == "driverack/pa2/state/crossover" for topic, *_ in client.published
    )
    controller.state_value = Pa2State(
        controller.identity_value,
        controller.presets[1],
        controller.state_value.output_mutes,
    )
    client.published.clear()

    bridge._poll_once()

    events = [(topic, payload) for topic, payload, *_ in client.published]
    after = sum(topic == "driverack/pa2/state/crossover" for topic, _ in events)
    assert before == 1
    assert after == 1
    offline_index = events.index(("driverack/pa2/status/details", "offline"))
    preset_index = events.index(("driverack/pa2/state/preset", "2: Alternate"))
    crossover_index = next(
        index
        for index, (topic, _) in enumerate(events)
        if topic == "driverack/pa2/state/crossover"
    )
    online_index = events.index(("driverack/pa2/status/details", "online"))
    assert offline_index < preset_index < crossover_index < online_index


def test_periodic_detail_refresh_keeps_valid_details_online(monkeypatch) -> None:
    bridge, client, pa2, _ = make_bridge(monkeypatch)
    pa2.connected = True
    bridge._discovery_published = True

    bridge._poll_once()
    client.published.clear()
    bridge._last_detail_refresh = float("-inf")

    bridge._poll_once()

    events = [(topic, payload) for topic, payload, *_ in client.published]
    crossover_index = next(
        index
        for index, (topic, _) in enumerate(events)
        if topic == "driverack/pa2/state/crossover"
    )
    online_index = events.index(("driverack/pa2/status/details", "online"))
    assert ("driverack/pa2/status/details", "offline") not in events
    assert crossover_index < online_index


def test_failed_periodic_detail_refresh_marks_details_offline_once(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True
    bridge._discovery_published = True

    bridge._poll_once()
    client.published.clear()
    bridge._last_detail_refresh = float("-inf")

    def invalid_crossover():
        raise OSError("crossover unavailable")

    controller.crossover = invalid_crossover
    bridge._poll_once()

    assert [
        payload
        for topic, payload, *_ in client.published
        if topic == "driverack/pa2/status/details"
    ] == ["offline"]
    assert bridge._details_valid is False


def test_detail_refresh_republishes_discovery_when_allowed_preset_labels_change(
    monkeypatch,
) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    bridge._connect_pa2()
    bridge._discovery_published = True
    client.published.clear()
    controller.presets = [Preset(1, "Renamed"), Preset(2, "Alternate")]
    controller.all_presets = [*controller.presets, Preset(3, "Factory")]

    bridge.publish_details()

    select_payloads = [
        json.loads(payload)
        for topic, payload, *_ in client.published
        if topic.startswith("homeassistant/select/")
    ]
    assert len(select_payloads) == 1
    assert select_payloads[0]["options"] == ["1: Renamed", "2: Alternate"]
    assert bridge._preset_commands == frozenset({"1: Renamed", "2: Alternate"})


def test_failed_discovery_refresh_does_not_authorize_changed_preset_labels(
    monkeypatch,
) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)
    bridge._connect_pa2()
    bridge._discovery_published = True
    old_commands = bridge._preset_commands
    controller.presets = [Preset(1, "Renamed"), Preset(2, "Alternate")]
    controller.all_presets = [*controller.presets, Preset(3, "Factory")]
    original_publish = bridge._publish

    def fail_select_discovery(topic: str, payload: str, *, retain: bool):
        if topic.startswith("homeassistant/select/"):
            raise MqttPublishError("discovery publish failed")
        return original_publish(topic, payload, retain=retain)

    monkeypatch.setattr(bridge, "_publish", fail_select_discovery)

    with pytest.raises(MqttPublishError, match="discovery publish failed"):
        bridge.publish_details()

    assert bridge._preset_commands == old_commands
    assert "1: Renamed" not in bridge._preset_commands


def test_detail_refresh_does_not_republish_unchanged_discovery(monkeypatch) -> None:
    bridge, client, _, _ = make_bridge(monkeypatch)
    bridge._connect_pa2()
    bridge._discovery_published = True
    client.published.clear()

    bridge.publish_details()

    assert not any(
        topic.startswith("homeassistant/") for topic, *_ in client.published
    )


def test_meter_collection_failure_never_publishes_online(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch, expose_meters=True)

    def unavailable_levels(*, deadline=None):
        del deadline
        raise OSError("meter read failed")

    controller.output_levels = unavailable_levels

    with pytest.raises(OSError, match="meter read failed"):
        bridge.publish_state(controller.state_value)

    assert not any(
        topic == "driverack/pa2/status" and payload == "online"
        for topic, payload, *_ in client.published
    )


def test_disconnected_bridge_drops_poll_state_instead_of_queueing_it(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)
    bridge._mqtt_connected = False

    bridge.publish_state(controller.state_value)

    assert client.published == []


def test_command_transport_failure_closes_pa2_and_publishes_offline(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True

    def transport_failure(payload, *, unmute_after, identity=None, start_deadline=None):
        del payload, unmute_after, identity, start_deadline
        raise OSError("PA2 connection lost")

    controller.activate_preset = transport_failure

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/preset", "2: Alternate"),
    )
    bridge._process_queued_command()

    assert pa2.connected is False
    assert ("driverack/pa2/status", "offline", 1, True) in client.published


def test_unexpected_post_actuation_failure_closes_pa2_and_publishes_offline(
    monkeypatch,
    caplog,
) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True
    sentinel = "PRIVATE-RUNTIME-PAYLOAD-41"

    def unexpected_failure(muted, *, start_deadline=None):
        del start_deadline
        assert muted is False
        raise RuntimeError(sentinel)

    controller.set_all_outputs_muted = unexpected_failure

    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/unmute", "PRESS"),
    )
    bridge._process_queued_command()

    assert pa2.connected is False
    assert ("driverack/pa2/status/details", "offline", 1, True) in client.published
    assert ("driverack/pa2/status", "offline", 1, True) in client.published
    assert (
        "driverack/pa2/state/last_command",
        "ERROR: command failed",
        1,
        True,
    ) in client.published
    assert sentinel not in caplog.text
    assert all(sentinel not in payload for _, payload, *_ in client.published)


def test_run_forever_uses_paho_background_loop_for_automatic_broker_reconnect(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge,
        "_connect_pa2",
        lambda **_: setattr(pa2, "connected", True),
    )
    controller.raise_keyboard_on_state = True

    bridge.run_forever()

    assert client.connected_to == ("homeassistant.local", 1883, 30)
    assert client.loop_started == 1
    assert client.loop_stopped == 1
    assert client.disconnected == 1
    discovery_publishes = [
        item for item in client.published if item[0].endswith("/config")
    ]
    assert client.wait_for_publish_calls == len(discovery_publishes) + 2
    assert pa2.closed == 1
    assert ("driverack/pa2/status", "offline", 1, True) in client.published


def test_run_forever_releases_pa2_lock_before_stopping_mqtt_loop(monkeypatch) -> None:
    bridge, client, pa2, controller = make_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge,
        "_connect_pa2",
        lambda **_: setattr(pa2, "connected", True),
    )
    controller.raise_keyboard_on_state = True

    def loop_stop() -> None:
        callback_finished = threading.Event()

        def disconnect_callback() -> None:
            with bridge._pa2_lock:
                callback_finished.set()

        callback_thread = threading.Thread(target=disconnect_callback)
        callback_thread.start()
        callback_thread.join(timeout=1)
        assert callback_finished.is_set()
        client.loop_stopped += 1

    client.loop_stop = loop_stop  # type: ignore[method-assign]

    bridge.run_forever()

    assert client.loop_stopped == 1


def test_run_forever_installs_and_restores_graceful_sigterm_handler(monkeypatch) -> None:
    bridge, client, pa2, _ = make_bridge(monkeypatch)
    monkeypatch.setattr(
        bridge,
        "_connect_pa2",
        lambda **_: setattr(pa2, "connected", True),
    )
    registrations: list[tuple[int, object]] = []
    previous_handler = signal.SIG_DFL
    original_loop_start = client.loop_start

    def record_signal(signum: int, handler: object) -> object:
        registrations.append((signum, handler))
        return previous_handler

    def stop_after_mqtt_start() -> None:
        original_loop_start()
        assert registrations
        bridge._mqtt_ready.clear()
        bridge._mqtt_state_changed.clear()
        registrations[0][1](signal.SIGTERM, None)  # type: ignore[operator]

    monkeypatch.setattr(signal, "signal", record_signal)
    client.loop_start = stop_after_mqtt_start  # type: ignore[method-assign]

    bridge.run_forever()

    assert len(registrations) == 2
    signum, _ = registrations[0]
    assert signum == signal.SIGTERM
    assert registrations[1] == (signal.SIGTERM, previous_handler)
    assert bridge._stop_event.is_set()
    assert bridge._mqtt_ready.is_set()
    assert bridge._mqtt_state_changed.is_set()
    assert client.loop_stopped == 1
    assert client.disconnected == 1
    assert pa2.closed == 1


def test_sigterm_handler_defers_logging_and_event_synchronization(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)

    def forbidden(*args, **kwargs) -> None:
        del args, kwargs
        raise AssertionError("signal handler used synchronization")

    monkeypatch.setattr("pa2bridge.mqtt_bridge.LOGGER.info", forbidden)
    monkeypatch.setattr(bridge._stop_event, "set", forbidden)
    monkeypatch.setattr(bridge._mqtt_ready, "set", forbidden)
    monkeypatch.setattr(bridge._mqtt_state_changed, "set", forbidden)

    bridge._handle_sigterm(signal.SIGTERM, None)

    assert bridge._sigterm_requested is True


def test_poll_uses_one_absolute_deadline_for_all_pa2_reads(monkeypatch) -> None:
    bridge, _, pa2, controller = make_bridge(monkeypatch, expose_meters=True)
    pa2.connected = True
    bridge._discovery_published = True
    deadlines: list[float] = []

    def record(result):
        def operation(*args, deadline, **kwargs):
            del args, kwargs
            deadlines.append(deadline)
            return result

        return operation

    monkeypatch.setattr("pa2bridge.mqtt_bridge.time.monotonic", lambda: 100.0)
    controller.identity = record(controller.identity_value)
    controller.state = record(controller.state_value)
    controller.output_levels = record(
        {channel: -42.25 for channel in controller.state_value.output_mutes}
    )
    controller.input_meters = record(controller.input_meters())
    controller.list_preset_views = record((controller.presets, controller.all_presets))
    controller.crossover = record(controller.crossover_value)

    bridge._poll_once()

    assert deadlines
    assert set(deadlines) == {160.0}


def test_sigterm_waits_for_inflight_pa2_mutation_and_skips_followup_reads(
    monkeypatch,
) -> None:
    bridge, _, _, controller = make_bridge(monkeypatch)
    mutation_finished = False
    followup_reads: list[tuple[object, object]] = []

    def mutate(muted: bool, *, start_deadline: float) -> None:
        nonlocal mutation_finished
        assert muted is False
        assert start_deadline > time.monotonic()
        bridge._handle_sigterm(signal.SIGTERM, None)
        assert bridge._stop_event.is_set() is False
        mutation_finished = True

    def record_state(*, identity=None, deadline=None):
        followup_reads.append((identity, deadline))
        return controller.state_value

    monkeypatch.setattr(controller, "set_all_outputs_muted", mutate)
    monkeypatch.setattr(controller, "state", record_state)
    bridge._on_message(
        None,
        None,
        message("driverack/pa2/command/unmute", "PRESS"),
    )

    bridge._process_queued_command()

    assert mutation_finished is True
    assert followup_reads == []
    assert bridge._stop_event.is_set()


def test_unacknowledged_shutdown_publication_fails_closed(monkeypatch) -> None:
    bridge, _, _, _ = make_bridge(monkeypatch)
    result = SimpleNamespace(
        wait_for_publish=lambda timeout=None: None,
        is_published=lambda: False,
    )

    with pytest.raises(MqttPublishError, match="acknowledgement timed out"):
        bridge._wait_for_publication(result, topic="driverack/pa2/status")

    assert bridge._mqtt_connected is False
    assert isinstance(bridge._mqtt_failure, MqttPublishError)
    assert bridge._stop_event.is_set()


def test_pa2_connect_waits_for_inflight_command_transaction(monkeypatch) -> None:
    bridge, _, pa2, controller = make_bridge(
        monkeypatch,
        pa2_mac_address="02:00:5e:10:00:01",
    )
    command_entered = threading.Event()
    release_command = threading.Event()
    connect_started = threading.Event()

    def blocking_activation(
        payload, *, unmute_after, identity=None, start_deadline=None
    ):
        del payload, unmute_after, identity, start_deadline
        command_entered.set()
        assert release_command.wait(timeout=2)
        return controller.state_value

    controller.activate_preset = blocking_activation

    bridge._on_message(
        None, None, message("driverack/pa2/command/preset", "2: Alternate")
    )
    command_thread = threading.Thread(target=bridge._process_queued_command)

    def connect_worker() -> None:
        connect_started.set()
        bridge._connect_pa2()

    connect_thread = threading.Thread(target=connect_worker)
    command_thread.start()
    assert command_entered.wait(timeout=1)
    connect_thread.start()
    assert connect_started.wait(timeout=1)
    time.sleep(0.05)

    try:
        assert pa2.connect_args is None
    finally:
        release_command.set()
        command_thread.join(timeout=2)
        connect_thread.join(timeout=2)
    assert not command_thread.is_alive()
    assert not connect_thread.is_alive()
    assert pa2.connect_args == ("administrator", "pa2-secret")


def test_failed_poll_waits_for_command_then_closes_inside_same_transaction(monkeypatch) -> None:
    bridge, _, pa2, controller = make_bridge(monkeypatch)
    pa2.connected = True
    command_entered = threading.Event()
    release_command = threading.Event()
    poll_started = threading.Event()
    poll_finished = threading.Event()

    def blocking_activation(
        payload, *, unmute_after, identity=None, start_deadline=None
    ):
        del payload, unmute_after, identity, start_deadline
        command_entered.set()
        assert release_command.wait(timeout=2)
        return controller.state_value

    def failed_state(*, identity=None, deadline=None):
        del identity, deadline
        raise OSError("poll failed")

    controller.activate_preset = blocking_activation
    controller.state = failed_state

    bridge._on_message(
        None, None, message("driverack/pa2/command/preset", "2: Alternate")
    )
    command_thread = threading.Thread(target=bridge._process_queued_command)

    def poll_worker() -> None:
        poll_started.set()
        try:
            bridge._poll_once()
        except OSError:
            pass
        finally:
            poll_finished.set()

    poll_thread = threading.Thread(target=poll_worker)
    command_thread.start()
    assert command_entered.wait(timeout=1)
    poll_thread.start()
    assert poll_started.wait(timeout=1)
    time.sleep(0.05)

    try:
        assert pa2.closed == 0
        assert poll_finished.is_set() is False
    finally:
        release_command.set()
        command_thread.join(timeout=2)
        poll_thread.join(timeout=2)

    assert pa2.connected is False
    assert poll_finished.is_set() is True


def test_poll_failure_invalidates_core_and_detail_availability(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    def unavailable(*, identity=None, deadline=None):
        del identity, deadline
        raise OSError("PA2 unavailable")

    controller.state = unavailable
    with pytest.raises(OSError):
        bridge._poll_once()

    assert ("driverack/pa2/status", "offline", 1, True) in client.published
    assert ("driverack/pa2/status/details", "offline", 1, True) in client.published


def test_preset_command_invalidates_details_before_device_recall(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    def activation(payload, *, unmute_after, identity=None, start_deadline=None):
        del payload, unmute_after, identity, start_deadline
        assert client.published[-1] == (
            "driverack/pa2/status/details",
            "offline",
            1,
            True,
        )
        return controller.state_value

    controller.activate_preset = activation
    bridge._on_message(
        None, None, message("driverack/pa2/command/preset", "2: Alternate")
    )

    bridge._process_queued_command()


def test_command_meter_failure_marks_core_and_details_offline(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch, expose_meters=True)

    def invalid_meters(*, deadline=None):
        del deadline
        raise TelemetryError("non-finite output meter")

    controller.output_levels = invalid_meters
    bridge._on_message(None, None, message("driverack/pa2/command/unmute", "PRESS"))
    bridge._process_queued_command()

    assert ("driverack/pa2/status", "offline", 1, True) in client.published
    assert ("driverack/pa2/status/details", "offline", 1, True) in client.published


def test_runtime_publications_never_expose_unverified_lock_state(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch, expose_meters=True)
    bridge.publish_state(controller.state_value)
    bridge.publish_details()

    serialized = json.dumps(client.published, sort_keys=True).casefold()
    assert "access_rights" not in serialized
    assert "system lockout" not in serialized
    assert "system_lockout" not in serialized
