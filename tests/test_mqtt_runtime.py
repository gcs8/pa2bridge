from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from pa2bridge.config import AppConfig, MqttConfig, Pa2Config
from pa2bridge.controller import (
    CrossoverBand,
    CrossoverState,
    DeviceIdentity,
    InputMeters,
    Pa2State,
    Preset,
    TelemetryError,
)
from pa2bridge.mqtt_bridge import MqttBridge, MqttPublishError


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
        self.connect_args = None
        self.closed = 0

    def connect(self, username, password) -> None:
        self.connect_args = (username, password)
        self.connected = True

    def close(self) -> None:
        self.closed += 1
        self.connected = False


class FakeController:
    def __init__(self) -> None:
        self.activations = []
        self.all_mutes = []
        self.channel_mutes = []
        self.raise_keyboard_on_state = False
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

    def identity(self):
        return self.identity_value

    def list_presets(self):
        return self.presets

    def list_all_presets(self):
        return self.all_presets

    def crossover(self):
        return self.crossover_value

    def state(self):
        if self.raise_keyboard_on_state:
            raise KeyboardInterrupt
        return self.state_value

    def output_levels(self):
        return {channel: -42.25 for channel in self.state_value.output_mutes}

    def input_meters(self):
        return InputMeters(
            levels_dbfs={"left": -18.45, "right": -19.55},
            clips={"left": False, "right": True},
        )

    def activate_preset(self, payload, *, unmute_after):
        self.activations.append((payload, unmute_after))
        return self.state_value

    def set_all_outputs_muted(self, muted):
        self.all_mutes.append(muted)

    def set_output_muted(self, channel, muted):
        self.channel_mutes.append((channel, muted))


def make_config(*, expose_meters=False):
    return AppConfig(
        pa2=Pa2Config(
            host="192.0.2.20",
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


def make_bridge(monkeypatch, *, expose_meters=False):
    fake_mqtt = FakeMqttClient()
    monkeypatch.setattr(
        "pa2bridge.mqtt_bridge.mqtt.Client",
        lambda *args, **kwargs: fake_mqtt,
    )
    bridge = MqttBridge(make_config(expose_meters=expose_meters))
    fake_pa2 = FakePa2Client()
    controller = FakeController()
    bridge.pa2_client = fake_pa2
    bridge.controller = controller
    bridge._preset_commands = frozenset(preset.label for preset in controller.presets)
    bridge._mqtt_connected = True
    return bridge, fake_mqtt, fake_pa2, controller


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


def test_initial_pa2_failure_connects_mqtt_and_marks_device_offline(monkeypatch) -> None:
    bridge, client, pa2, _ = make_bridge(monkeypatch)
    bridge._mqtt_connected = False

    def unavailable() -> None:
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

    def unavailable():
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
    assert client.wait_for_publish_calls == 0
    last_commands = [payload for topic, payload, *_ in client.published if topic.endswith("last_command")]
    assert last_commands[-1] == "high_left mute verified On"


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

    def record_unmute(muted: bool):
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

    def invalid_crossover():
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

    def transient_crossover():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient crossover failure")
        return real_crossover()

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


def test_periodic_detail_refresh_marks_details_offline_before_refresh(monkeypatch) -> None:
    bridge, client, pa2, _ = make_bridge(monkeypatch)
    pa2.connected = True
    bridge._discovery_published = True

    bridge._poll_once()
    client.published.clear()
    bridge._last_detail_refresh = float("-inf")

    bridge._poll_once()

    events = [(topic, payload) for topic, payload, *_ in client.published]
    offline_index = events.index(("driverack/pa2/status/details", "offline"))
    crossover_index = next(
        index
        for index, (topic, _) in enumerate(events)
        if topic == "driverack/pa2/state/crossover"
    )
    online_index = events.index(("driverack/pa2/status/details", "online"))
    assert offline_index < crossover_index < online_index


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

    def unavailable_levels():
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

    def transport_failure(payload, *, unmute_after):
        del payload, unmute_after
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

    def unexpected_failure(muted):
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
    monkeypatch.setattr(bridge, "_connect_pa2", lambda: setattr(pa2, "connected", True))
    controller.raise_keyboard_on_state = True

    bridge.run_forever()

    assert client.connected_to == ("homeassistant.local", 1883, 30)
    assert client.loop_started == 1
    assert client.loop_stopped == 1
    assert client.disconnected == 1
    assert client.wait_for_publish_calls == 2
    assert pa2.closed == 1
    assert ("driverack/pa2/status", "offline", 1, True) in client.published


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
    bridge, _, pa2, controller = make_bridge(monkeypatch)
    command_entered = threading.Event()
    release_command = threading.Event()
    connect_started = threading.Event()

    def blocking_activation(payload, *, unmute_after):
        del payload, unmute_after
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

    def blocking_activation(payload, *, unmute_after):
        del payload, unmute_after
        command_entered.set()
        assert release_command.wait(timeout=2)
        return controller.state_value

    def failed_state():
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

    def unavailable():
        raise OSError("PA2 unavailable")

    controller.state = unavailable
    with pytest.raises(OSError):
        bridge._poll_once()

    assert ("driverack/pa2/status", "offline", 1, True) in client.published
    assert ("driverack/pa2/status/details", "offline", 1, True) in client.published


def test_preset_command_invalidates_details_before_device_recall(monkeypatch) -> None:
    bridge, client, _, controller = make_bridge(monkeypatch)

    def activation(payload, *, unmute_after):
        del payload, unmute_after
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

    def invalid_meters():
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
