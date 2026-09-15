from __future__ import annotations

import json

from pa2bridge.controller import Preset
from pa2bridge.mqtt_bridge import DeviceInfo, build_discovery_messages


def _payloads_by_topic():
    messages = build_discovery_messages(
        device=DeviceInfo(
            identifier="driverack_pa2_192_0_2_20",
            name="DriveRackPA2",
            firmware="1.2.0.1",
        ),
        presets=[Preset(1, "Flat"), Preset(2, "Alternate")],
        base_topic="driverack/pa2",
        discovery_prefix="homeassistant",
        expose_meters=False,
    )
    return {
        message.topic: json.loads(message.payload)
        for message in messages
        if message.payload
    }


def test_discovery_creates_preset_select_that_uses_verified_state_and_nonretained_commands() -> None:
    payloads = _payloads_by_topic()
    config = payloads["homeassistant/select/driverack_pa2_192_0_2_20/preset/config"]

    assert config["options"] == ["1: Flat", "2: Alternate"]
    assert config["command_topic"] == "driverack/pa2/command/preset"
    assert config["state_topic"] == "driverack/pa2/state/preset"
    assert config["availability_topic"] == "driverack/pa2/status"
    assert config["retain"] is False


def test_discovery_uses_mac_connection_for_home_assistant_device_matching() -> None:
    messages = build_discovery_messages(
        device=DeviceInfo(
            identifier="driverack_pa2_02005e100001",
            name="DriveRackPA2",
            firmware="1.2.0.1",
            mac_address="02:00:5e:10:00:01",
        ),
        presets=[Preset(1, "Flat")],
        base_topic="driverack/pa2",
        discovery_prefix="homeassistant",
        expose_meters=False,
    )

    payload = json.loads(next(message.payload for message in messages if message.payload))

    assert payload["device"]["identifiers"] == ["driverack_pa2_02005e100001"]
    assert payload["device"]["connections"] == [["mac", "02:00:5e:10:00:01"]]


def test_discovery_creates_unmute_button_and_six_mute_switches() -> None:
    payloads = _payloads_by_topic()

    unmute = payloads["homeassistant/button/driverack_pa2_192_0_2_20/unmute_outputs/config"]
    assert unmute["command_topic"] == "driverack/pa2/command/unmute"
    assert unmute["payload_press"] == "PRESS"

    mute_topics = [topic for topic in payloads if "/switch/" in topic]
    assert len(mute_topics) == 6
    high_left = payloads[
        "homeassistant/switch/driverack_pa2_192_0_2_20/high_left_mute/config"
    ]
    assert high_left["command_topic"] == "driverack/pa2/command/mute/high_left"
    assert high_left["state_topic"] == "driverack/pa2/state/mute/high_left"
    assert high_left["payload_on"] == "On"
    assert high_left["payload_off"] == "Off"


def test_meter_entities_are_opt_in_and_disabled_by_default() -> None:
    no_meter_payloads = _payloads_by_topic()
    assert not any("output_level" in topic for topic in no_meter_payloads)

    messages = build_discovery_messages(
        device=DeviceInfo("pa2", "PA2", "1.2.0.1"),
        presets=[Preset(1, "flat")],
        base_topic="driverack/pa2",
        discovery_prefix="homeassistant",
        expose_meters=True,
    )
    meter_payloads = {
        message.topic: json.loads(message.payload)
        for message in messages
        if "_level/config" in message.topic
    }
    assert len(meter_payloads) == 8
    assert all(payload["enabled_by_default"] is False for payload in meter_payloads.values())
    assert all(payload["unit_of_measurement"] == "dBFS" for payload in meter_payloads.values())
    assert all("device_class" not in payload for payload in meter_payloads.values())

    clip_payloads = {
        message.topic: json.loads(message.payload)
        for message in messages
        if "input_clip" in message.topic
    }
    assert len(clip_payloads) == 2
    assert all(payload["device_class"] == "problem" for payload in clip_payloads.values())
    assert all(payload["enabled_by_default"] is False for payload in clip_payloads.values())


def test_disabled_meters_clear_previously_retained_discovery_configs() -> None:
    messages = build_discovery_messages(
        device=DeviceInfo(
            identifier="driverack_pa2_192_0_2_20",
            name="DriveRackPA2",
            firmware="1.2.0.1",
        ),
        presets=[Preset(1, "Flat")],
        base_topic="driverack/pa2",
        discovery_prefix="homeassistant",
        expose_meters=False,
    )

    removals = [message for message in messages if not message.payload]
    assert len(removals) == 10
    assert all(message.retain for message in removals)
    removal_topics = {message.topic for message in removals}
    assert removal_topics == {
        "homeassistant/sensor/driverack_pa2_192_0_2_20/left_input_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/right_input_level/config",
        "homeassistant/binary_sensor/driverack_pa2_192_0_2_20/left_input_clip/config",
        "homeassistant/binary_sensor/driverack_pa2_192_0_2_20/right_input_clip/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/high_left_output_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/high_right_output_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/mid_left_output_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/mid_right_output_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/low_left_output_level/config",
        "homeassistant/sensor/driverack_pa2_192_0_2_20/low_right_output_level/config",
    }

    enabled_messages = build_discovery_messages(
        device=DeviceInfo(
            identifier="driverack_pa2_192_0_2_20",
            name="DriveRackPA2",
            firmware="1.2.0.1",
        ),
        presets=[Preset(1, "Flat")],
        base_topic="driverack/pa2",
        discovery_prefix="homeassistant",
        expose_meters=True,
    )
    assert all(message.payload for message in enabled_messages)
    enabled_topics = {message.topic for message in enabled_messages}
    assert removal_topics <= enabled_topics


def test_discovery_includes_read_only_preset_inventory_and_crossover_details() -> None:
    payloads = _payloads_by_topic()

    inventory = payloads[
        "homeassistant/sensor/driverack_pa2_192_0_2_20/preset_inventory/config"
    ]
    crossover = payloads[
        "homeassistant/sensor/driverack_pa2_192_0_2_20/crossover/config"
    ]

    assert inventory["state_topic"] == "driverack/pa2/state/preset_inventory"
    assert inventory["json_attributes_topic"] == inventory["state_topic"]
    assert inventory["value_template"] == "{{ value_json.count }}"
    assert inventory["availability_mode"] == "all"
    assert inventory["availability"] == [
        {
            "topic": "driverack/pa2/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
        {
            "topic": "driverack/pa2/status/details",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
    ]
    assert crossover["state_topic"] == "driverack/pa2/state/crossover"
    assert crossover["json_attributes_topic"] == crossover["state_topic"]
    assert crossover["value_template"] == "{{ value_json.summary }}"
    assert crossover["availability_mode"] == "all"
    assert crossover["availability"] == inventory["availability"]


def test_discovery_does_not_infer_unverified_system_lockout_state() -> None:
    serialized = json.dumps(_payloads_by_topic(), sort_keys=True).casefold()

    assert "access_rights" not in serialized
    assert "system lockout" not in serialized
    assert "system_lockout" not in serialized
