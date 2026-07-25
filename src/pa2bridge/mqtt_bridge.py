"""Home Assistant MQTT discovery and runtime bridge."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any

import paho.mqtt.client as mqtt

from .config import AppConfig, MQTT_KEEPALIVE_SECONDS
from .controller import (
    DeviceIdentity,
    INPUT_CLIPS,
    INPUT_LEVELS,
    OUTPUT_LEVELS,
    OUTPUT_MUTES,
    OutputVerificationError,
    Pa2Controller,
    Pa2State,
    Preset,
    RecallTimeout,
    TelemetryError,
)
from .protocol import HiQnetClient, ProtocolError


LOGGER = logging.getLogger(__name__)
DETAIL_REFRESH_INTERVAL = 60.0
SHUTDOWN_PUBLISH_TIMEOUT = 5.0


class MqttPublishError(RuntimeError):
    """The broker did not accept a required state or availability update."""


@dataclass(frozen=True)
class DeviceInfo:
    identifier: str
    name: str
    firmware: str


@dataclass(frozen=True)
class MqttPublish:
    topic: str
    payload: str
    retain: bool = True


@dataclass(frozen=True)
class QueuedCommand:
    topic: str
    payload: str
    mqtt_generation: int


def _device_payload(device: DeviceInfo) -> dict[str, Any]:
    return {
        "identifiers": [device.identifier],
        "name": device.name,
        "manufacturer": "dbx",
        "model": "DriveRack PA2",
        "sw_version": device.firmware,
    }


def build_discovery_messages(
    *,
    device: DeviceInfo,
    presets: list[Preset],
    base_topic: str,
    discovery_prefix: str,
    expose_meters: bool,
) -> list[MqttPublish]:
    """Build retained discovery records; every state is device-observed."""
    prefix = discovery_prefix.rstrip("/")
    base = base_topic.rstrip("/")
    common = {"device": _device_payload(device)}
    messages: list[MqttPublish] = []

    def add(
        component: str,
        object_id: str,
        payload: dict[str, Any],
        *,
        details: bool = False,
    ) -> None:
        if details:
            availability: dict[str, Any] = {
                "availability": [
                    {
                        "topic": f"{base}/status",
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                    {
                        "topic": f"{base}/status/details",
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                ],
                "availability_mode": "all",
            }
        else:
            availability = {
                "availability_topic": f"{base}/status",
                "payload_available": "online",
                "payload_not_available": "offline",
            }
        config = {
            **common,
            **availability,
            **payload,
            "unique_id": f"{device.identifier}_{object_id}",
        }
        messages.append(
            MqttPublish(
                topic=f"{prefix}/{component}/{device.identifier}/{object_id}/config",
                payload=json.dumps(config, separators=(",", ":"), sort_keys=True),
            )
        )

    add(
        "select",
        "preset",
        {
            "name": "Preset",
            "icon": "mdi:tune-variant",
            "options": [preset.label for preset in presets],
            "command_topic": f"{base}/command/preset",
            "state_topic": f"{base}/state/preset",
            "retain": False,
        },
    )
    add(
        "button",
        "unmute_outputs",
        {
            "name": "Unmute all outputs",
            "icon": "mdi:volume-high",
            "command_topic": f"{base}/command/unmute",
            "payload_press": "PRESS",
            "retain": False,
        },
    )
    add(
        "sensor",
        "firmware",
        {
            "name": "Firmware",
            "state_topic": f"{base}/state/firmware",
            "entity_category": "diagnostic",
            "icon": "mdi:chip",
        },
    )
    add(
        "sensor",
        "last_command",
        {
            "name": "Last command",
            "state_topic": f"{base}/state/last_command",
            "entity_category": "diagnostic",
            "icon": "mdi:message-check-outline",
        },
    )
    add(
        "sensor",
        "preset_inventory",
        {
            "name": "Preset inventory",
            "state_topic": f"{base}/state/preset_inventory",
            "value_template": "{{ value_json.count }}",
            "json_attributes_topic": f"{base}/state/preset_inventory",
            "entity_category": "diagnostic",
            "icon": "mdi:playlist-music-outline",
        },
        details=True,
    )
    add(
        "sensor",
        "crossover",
        {
            "name": "Crossover",
            "state_topic": f"{base}/state/crossover",
            "value_template": "{{ value_json.summary }}",
            "json_attributes_topic": f"{base}/state/crossover",
            "entity_category": "diagnostic",
            "icon": "mdi:sine-wave",
        },
        details=True,
    )

    for channel in OUTPUT_MUTES:
        title = channel.replace("_", " ").title()
        add(
            "switch",
            f"{channel}_mute",
            {
                "name": f"{title} mute",
                "icon": "mdi:volume-mute",
                "command_topic": f"{base}/command/mute/{channel}",
                "state_topic": f"{base}/state/mute/{channel}",
                "payload_on": "On",
                "payload_off": "Off",
                "retain": False,
            },
        )

    if expose_meters:
        for side in INPUT_LEVELS:
            title = side.title()
            add(
                "sensor",
                f"{side}_input_level",
                {
                    "name": f"{title} input level",
                    "state_topic": f"{base}/state/level/input_{side}",
                    "unit_of_measurement": "dBFS",
                    "suggested_display_precision": 1,
                    "state_class": "measurement",
                    "enabled_by_default": False,
                    "icon": "mdi:waveform",
                },
            )
        for side in INPUT_CLIPS:
            title = side.title()
            add(
                "binary_sensor",
                f"{side}_input_clip",
                {
                    "name": f"{title} input clip",
                    "state_topic": f"{base}/state/clip/input_{side}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": "problem",
                    "enabled_by_default": False,
                    "entity_category": "diagnostic",
                },
            )
        for channel in OUTPUT_LEVELS:
            title = channel.replace("_", " ").title()
            add(
                "sensor",
                f"{channel}_output_level",
                {
                    "name": f"{title} output level",
                    "state_topic": f"{base}/state/level/{channel}",
                    "unit_of_measurement": "dBFS",
                    "suggested_display_precision": 1,
                    "state_class": "measurement",
                    "enabled_by_default": False,
                    "icon": "mdi:waveform",
                },
            )
    return messages


class MqttBridge:
    """Owns the authoritative PA2 session and maps MQTT commands to verified operations."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.pa2_client = HiQnetClient(
            config.pa2.host,
            port=config.pa2.port,
            timeout=config.pa2.connect_timeout,
        )
        self.controller = Pa2Controller(
            self.pa2_client,
            allowed_slots=config.pa2.allowed_preset_slots,
            recall_timeout=config.pa2.recall_timeout,
            poll_interval=config.pa2.poll_interval,
            post_recall_delay=config.pa2.post_recall_delay,
        )
        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.mqtt.client_id,
            reconnect_on_failure=True,
        )
        self.mqtt.max_queued_messages_set(100)
        self.mqtt.max_inflight_messages_set(20)
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=30)

        if config.mqtt.username is not None:
            self.mqtt.username_pw_set(config.mqtt.username, config.mqtt.password)
        status_topic = f"{config.mqtt.base_topic}/status"
        self.mqtt.will_set(status_topic, "offline", qos=1, retain=True)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message = self._on_message
        self.mqtt.on_subscribe = self._on_subscribe
        self.device: DeviceInfo | None = None
        self.discovery: list[MqttPublish] = []
        self._pa2_lock = threading.RLock()
        self._mqtt_state_lock = threading.RLock()
        self._mqtt_connected = False
        self._mqtt_transport_connected = False
        self._mqtt_generation = 0
        self._mqtt_ready = threading.Event()
        self._mqtt_state_changed = threading.Event()
        self._stop_event = threading.Event()
        self._mqtt_failure: MqttPublishError | None = None
        self._pending_subscribe_mid: int | None = None
        self._stopping = False
        self._discovery_published = False
        self._details_valid = False
        self._last_detail_refresh = 0.0
        self._last_detail_slot: int | None = None
        self._pa2_identity: tuple[int, DeviceIdentity] | None = None
        self._allowed_presets: tuple[Preset, ...] = ()
        self._discovery_needs_refresh = False
        self._preset_commands: frozenset[str] = frozenset()
        # At most one command may wait behind the serialized worker. A bounded
        # single-slot queue prevents stale actuator sequences from accumulating.
        self._commands: Queue[QueuedCommand] = Queue(maxsize=1)
        self._diagnostics: Queue[str] = Queue(maxsize=1)

    def run_forever(self) -> None:
        loop_started = False
        try:
            self._mqtt_ready.clear()
            self._stop_event.clear()
            self.mqtt.connect(
                self.config.mqtt.host,
                self.config.mqtt.port,
                keepalive=MQTT_KEEPALIVE_SECONDS,
            )
            self.mqtt.loop_start()
            loop_started = True
            if not self._mqtt_ready.wait(timeout=10.0):
                raise MqttPublishError("MQTT connection callback timed out")
            if self._mqtt_failure is not None:
                raise self._mqtt_failure

            self._publish(
                f"{self.config.mqtt.base_topic}/status/details",
                "offline",
                retain=True,
            )
            self._publish(
                f"{self.config.mqtt.base_topic}/status", "offline", retain=True
            )

            next_poll = 0.0
            reconnect_delay = 1.0
            while not self._stop_event.is_set():
                if self._mqtt_failure is not None:
                    raise self._mqtt_failure
                with self._mqtt_state_lock:
                    mqtt_connected = self._mqtt_connected
                if not mqtt_connected:
                    self._mqtt_state_changed.clear()
                    with self._mqtt_state_lock:
                        mqtt_connected = self._mqtt_connected
                        mqtt_failure = self._mqtt_failure
                    if mqtt_failure is not None:
                        raise mqtt_failure
                    if not mqtt_connected:
                        self._mqtt_state_changed.wait(timeout=0.5)
                        continue
                if self._process_queued_diagnostic():
                    continue
                if self._process_queued_command():
                    continue
                now = time.monotonic()
                if now < next_poll:
                    self._stop_event.wait(timeout=min(0.5, next_poll - now))
                    continue
                next_poll = now + self.config.mqtt.state_poll_interval
                try:
                    self._poll_once()
                    reconnect_delay = 1.0
                except (
                    ProtocolError,
                    OSError,
                    ValueError,
                    OutputVerificationError,
                    RecallTimeout,
                    TelemetryError,
                ) as error:
                    LOGGER.warning("PA2 poll failed: %s", error)
                    self._stop_event.wait(timeout=reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30.0)
        except KeyboardInterrupt:
            LOGGER.info("stopping")
        finally:
            with self._pa2_lock:
                with self._mqtt_state_lock:
                    self._stopping = True
                    transport_connected = self._mqtt_transport_connected
                availability_offline = False
                if transport_connected:
                    try:
                        details_result = self._publish_checked(
                            f"{self.config.mqtt.base_topic}/status/details",
                            "offline",
                            retain=True,
                        )
                        status_result = self._publish_checked(
                            f"{self.config.mqtt.base_topic}/status",
                            "offline",
                            retain=True,
                        )
                        self._wait_for_publication(
                            details_result,
                            topic=f"{self.config.mqtt.base_topic}/status/details",
                        )
                        self._wait_for_publication(
                            status_result,
                            topic=f"{self.config.mqtt.base_topic}/status",
                        )
                        availability_offline = True
                    except MqttPublishError:
                        pass
                if transport_connected and availability_offline:
                    self.mqtt.disconnect()
                with self._mqtt_state_lock:
                    self._mqtt_connected = False
                    self._mqtt_transport_connected = False
                if loop_started:
                    self.mqtt.loop_stop()
                self.pa2_client.close()

    def publish_state(self, state: Pa2State) -> None:
        with self._pa2_lock:
            if not self._mqtt_connected:
                return
            levels = (
                self.controller.output_levels()
                if self.config.mqtt.expose_meters
                else {}
            )
            input_meters = (
                self.controller.input_meters()
                if self.config.mqtt.expose_meters
                else None
            )
            base = self.config.mqtt.base_topic
            self._publish(
                f"{base}/state/preset", state.current_preset.label, retain=True
            )
            self._publish(
                f"{base}/state/firmware", state.identity.firmware, retain=True
            )
            for channel, muted in state.output_mutes.items():
                self._publish(
                    f"{base}/state/mute/{channel}",
                    "On" if muted else "Off",
                    retain=True,
                )
            for channel, level in levels.items():
                self._publish(
                    f"{base}/state/level/{channel}",
                    f"{level:.1f}",
                    retain=False,
                )
            if input_meters is not None:
                for side, level in input_meters.levels_dbfs.items():
                    self._publish(
                        f"{base}/state/level/input_{side}",
                        f"{level:.1f}",
                        retain=False,
                    )
                for side, clipped in input_meters.clips.items():
                    self._publish(
                        f"{base}/state/clip/input_{side}",
                        "ON" if clipped else "OFF",
                        retain=False,
                    )
            self._publish(f"{base}/status", "online", retain=True)

    def publish_details(self) -> None:
        """Publish slow-changing read-only data needed for inventory and curves."""

        with self._pa2_lock:
            if not self._mqtt_connected:
                return
            allowed_presets, presets = self.controller.list_preset_views()
            crossover = self.controller.crossover()
            allowed_commands = frozenset(
                preset.label for preset in allowed_presets
            )
            if allowed_commands != self._preset_commands or self._discovery_needs_refresh:
                if self.device is None:
                    raise MqttPublishError(
                        "cannot refresh MQTT discovery before PA2 identity is available"
                    )
                self._allowed_presets = tuple(allowed_presets)
                refreshed_discovery = build_discovery_messages(
                    device=self.device,
                    presets=allowed_presets,
                    base_topic=self.config.mqtt.base_topic,
                    discovery_prefix=self.config.mqtt.discovery_prefix,
                    expose_meters=self.config.mqtt.expose_meters,
                )
                for message in refreshed_discovery:
                    self._publish(
                        message.topic, message.payload, retain=message.retain
                    )
                self.discovery = refreshed_discovery
                self._preset_commands = allowed_commands
                self._discovery_published = True
                self._discovery_needs_refresh = False
            inventory_payload = {
                "count": len(presets),
                "presets": [
                    {"slot": preset.slot, "name": preset.name, "label": preset.label}
                    for preset in presets
                ],
            }
            crossover_payload = {
                "summary": (
                    f"{crossover.num_bands} "
                    f"{'band' if crossover.num_bands == 1 else 'bands'}"
                    f"{' + mono sub' if crossover.mono_sub else ''}"
                ),
                "num_bands": crossover.num_bands,
                "mono_sub": crossover.mono_sub,
                "bands": [
                    {
                        "identifier": band.identifier,
                        "label": band.label,
                        "high_pass_hz": band.high_pass_hz,
                        "high_pass_type": band.high_pass_type,
                        "gain_db": band.gain_db,
                        "low_pass_hz": band.low_pass_hz,
                        "low_pass_type": band.low_pass_type,
                        "polarity": band.polarity,
                    }
                    for band in crossover.bands
                ],
            }
            base = self.config.mqtt.base_topic
            self._publish(
                f"{base}/state/preset_inventory",
                json.dumps(inventory_payload, separators=(",", ":"), sort_keys=True),
                retain=True,
            )
            self._publish(
                f"{base}/state/crossover",
                json.dumps(crossover_payload, separators=(",", ":"), sort_keys=True),
                retain=True,
            )
            self._publish(f"{base}/status/details", "online", retain=True)

    def _refresh_details(self, *, current_slot: int) -> bool:
        try:
            self.publish_details()
        except MqttPublishError:
            self._details_valid = False
            raise
        except Exception as error:
            self._details_valid = False
            LOGGER.warning("PA2 detail telemetry refresh failed: %s", error)
            self._publish(
                f"{self.config.mqtt.base_topic}/status/details",
                "offline",
                retain=True,
            )
            return False
        self._details_valid = True
        self._last_detail_refresh = time.monotonic()
        self._last_detail_slot = current_slot
        return True

    def _poll_once(self) -> None:
        with self._pa2_lock:
            try:
                reconnected = not self.pa2_client.connected
                if reconnected:
                    self._connect_pa2()
                identity = self._identity_for_connection()
                if not self._discovery_published:
                    for message in self.discovery:
                        self._publish(
                            message.topic, message.payload, retain=message.retain
                        )
                    self._discovery_published = True
                state = self.controller.state(identity=identity)
                now = time.monotonic()
                refresh_overdue = (
                    now - self._last_detail_refresh >= DETAIL_REFRESH_INTERVAL
                )
                preset_changed = (
                    self._last_detail_slot is not None
                    and self._last_detail_slot != state.current_preset.slot
                )
                invalidate_details = (
                    reconnected
                    or not self._details_valid
                    or preset_changed
                    or refresh_overdue
                )
                if invalidate_details:
                    self._details_valid = False
                    self._publish(
                        f"{self.config.mqtt.base_topic}/status/details",
                        "offline",
                        retain=True,
                    )
                # If the observed preset changed, details are offline before
                # exposing the new core preset so stale crossover data is
                # never advertised as belonging to it.
                self.publish_state(state)
                refresh_details = (
                    invalidate_details
                    or refresh_overdue
                )
                if refresh_details:
                    self._refresh_details(current_slot=state.current_preset.slot)
            except Exception:
                self._details_valid = False
                self._publish(
                    f"{self.config.mqtt.base_topic}/status/details",
                    "offline",
                    retain=True,
                )
                self._publish(
                    f"{self.config.mqtt.base_topic}/status", "offline", retain=True
                )
                self.pa2_client.close()
                raise

    def _connect_pa2(self) -> None:
        with self._pa2_lock:
            self._pa2_identity = None
            self.pa2_client.connect(self.config.pa2.username, self.config.pa2.password)
            identity = self.controller.identity()
            self._pa2_identity = (
                self.pa2_client.connection_generation,
                identity,
            )
            presets = self.controller.list_presets()
            self._allowed_presets = tuple(presets)
            self._preset_commands = frozenset(preset.label for preset in presets)
            self.device = self._device_info(identity)
            self.discovery = build_discovery_messages(
                device=self.device,
                presets=presets,
                base_topic=self.config.mqtt.base_topic,
                discovery_prefix=self.config.mqtt.discovery_prefix,
                expose_meters=self.config.mqtt.expose_meters,
            )
            self._discovery_published = False
            self._discovery_needs_refresh = False
            self._details_valid = False

    def _device_info(self, identity: DeviceIdentity) -> DeviceInfo:
        safe_host = self.config.pa2.host.replace(".", "_").replace(":", "_")
        return DeviceInfo(
            identifier=f"driverack_pa2_{safe_host}",
            name=identity.instance_name,
            firmware=identity.firmware,
        )

    def _identity_for_connection(self) -> DeviceIdentity:
        generation = self.pa2_client.connection_generation
        cached = self._pa2_identity
        if cached is None or cached[0] != generation:
            identity = self.controller.identity()
            self._pa2_identity = (generation, identity)
            self.device = self._device_info(identity)
            self.discovery = build_discovery_messages(
                device=self.device,
                presets=list(self._allowed_presets),
                base_topic=self.config.mqtt.base_topic,
                discovery_prefix=self.config.mqtt.discovery_prefix,
                expose_meters=self.config.mqtt.expose_meters,
            )
            self._discovery_published = False
            self._discovery_needs_refresh = True
            self._details_valid = False
            return identity
        return cached[1]

    def _state_with_current_identity(self, state: Pa2State) -> Pa2State:
        return Pa2State(
            identity=self._identity_for_connection(),
            current_preset=state.current_preset,
            output_mutes=state.output_mutes,
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        del userdata, flags, properties
        with self._mqtt_state_lock:
            if reason_code != 0:
                self._mqtt_connected = False
                self._mqtt_transport_connected = False
                self._mqtt_failure = MqttPublishError(
                    f"MQTT connection failed: {reason_code}"
                )
                self._stop_event.set()
                self._mqtt_ready.set()
                LOGGER.error("%s", self._mqtt_failure)
                return
            self._mqtt_transport_connected = True
            if self._mqtt_failure is not None:
                self._mqtt_connected = False
                self._mqtt_ready.set()
                return
            self._mqtt_connected = False
            base = self.config.mqtt.base_topic
            result, mid = client.subscribe(f"{base}/command/#", qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._mqtt_connected = False
                self._mqtt_failure = MqttPublishError(
                    f"MQTT command subscription failed with result {result}"
                )
                self._stop_event.set()
                self._mqtt_ready.set()
                return
            self._pending_subscribe_mid = mid
            self._discovery_published = False
            # Publish and device I/O happen on the main/command paths. Waiting for
            # a QoS acknowledgement inside Paho's network callback would deadlock
            # the same thread that must receive that acknowledgement.

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties) -> None:
        del userdata, properties
        with self._mqtt_state_lock:
            failed = not reason_codes or any(
                bool(code.is_failure) if hasattr(code, "is_failure") else int(code) >= 128
                for code in reason_codes
            )
            if self._pending_subscribe_mid != mid or failed:
                self._mqtt_connected = False
                self._mqtt_failure = MqttPublishError(
                    "MQTT command subscription was rejected"
                )
                self._stop_event.set()
            else:
                self._mqtt_connected = True
            self._pending_subscribe_mid = None
            self._mqtt_ready.set()
            self._mqtt_state_changed.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        del client, userdata, disconnect_flags, reason_code, properties
        with self._mqtt_state_lock:
            self._mqtt_connected = False
            self._mqtt_transport_connected = False
            self._mqtt_generation += 1
            self._pending_subscribe_mid = None
        if not self._stopping:
            while True:
                try:
                    self._commands.get_nowait()
                except Empty:
                    break
            with self._pa2_lock:
                self._details_valid = False
                self._discovery_published = False
                self.pa2_client.close()
            self._mqtt_state_changed.set()
        else:
            self._mqtt_ready.set()

    def _on_message(self, client, userdata, message) -> None:
        del client, userdata
        base = self.config.mqtt.base_topic
        try:
            topic = message.topic
            if getattr(message, "retain", False):
                raise ValueError("retained commands are not accepted")
            payload = message.payload.decode("utf-8", errors="strict")
            valid = (
                (
                    topic == f"{base}/command/preset"
                    and payload in self._preset_commands
                )
                or (topic == f"{base}/command/unmute" and payload == "PRESS")
                or (
                    topic in {
                        f"{base}/command/mute/{channel}"
                        for channel in OUTPUT_MUTES
                    }
                    and payload in {"On", "Off"}
                )
            )
            if not valid:
                raise ValueError("unsupported command topic or payload")
            with self._mqtt_state_lock:
                if self._mqtt_failure is not None or not self._mqtt_connected:
                    raise ValueError("MQTT command arrived outside an active session")
                self._commands.put_nowait(
                    QueuedCommand(topic, payload, self._mqtt_generation)
                )
        except (UnicodeDecodeError, ValueError, Full):
            try:
                self._diagnostics.put_nowait("ERROR: command rejected")
            except Full:
                pass

    def _process_queued_diagnostic(self) -> bool:
        try:
            payload = self._diagnostics.get_nowait()
        except Empty:
            return False
        self._publish(
            f"{self.config.mqtt.base_topic}/state/last_command",
            payload,
            retain=True,
        )
        return True

    def _process_queued_command(self) -> bool:
        try:
            command = self._commands.get_nowait()
        except Empty:
            return False
        with self._mqtt_state_lock:
            stale = (
                self._mqtt_failure is not None
                or not self._mqtt_connected
                or command.mqtt_generation != self._mqtt_generation
            )
        if stale:
            with self._pa2_lock:
                self._details_valid = False
                self.pa2_client.close()
            return True
        self._execute_command(command)
        return True

    def _execute_command(self, command: QueuedCommand) -> None:
        base = self.config.mqtt.base_topic
        device_touched = False
        with self._pa2_lock:
            # This state lock is the command-authorization linearization boundary.
            # A disconnect callback cannot be recorded between the final session
            # check and the PA2 transaction; it runs before this block (rejecting
            # the command) or after the transaction has completed.
            with self._mqtt_state_lock:
                stale = (
                    self._mqtt_failure is not None
                    or not self._mqtt_connected
                    or command.mqtt_generation != self._mqtt_generation
                )
                if stale:
                    self._details_valid = False
                    self.pa2_client.close()
                    return
                try:
                    refresh_details = False
                    if command.topic == f"{base}/command/preset":
                        self._details_valid = False
                        self._publish(
                            f"{base}/status/details", "offline", retain=True
                        )
                        device_touched = True
                        state = self.controller.activate_preset(
                            command.payload,
                            unmute_after=True,
                            identity=self._identity_for_connection(),
                        )
                        state = self._state_with_current_identity(state)
                        result = (
                            f"recalled {state.current_preset.label}; outputs verified unmuted"
                        )
                        refresh_details = True
                    elif command.topic == f"{base}/command/unmute":
                        device_touched = True
                        self.controller.set_all_outputs_muted(False)
                        state = self.controller.state(
                            identity=self._identity_for_connection()
                        )
                        result = "all outputs verified unmuted"
                    else:
                        channel = command.topic.rsplit("/", 1)[-1]
                        device_touched = True
                        self.controller.set_output_muted(
                            channel, command.payload == "On"
                        )
                        state = self.controller.state(
                            identity=self._identity_for_connection()
                        )
                        result = f"{channel} mute verified {command.payload}"
                    self.publish_state(state)
                    if refresh_details:
                        self._refresh_details(current_slot=state.current_preset.slot)
                    self._publish(
                        f"{base}/state/last_command", result, retain=True
                    )
                except Exception as error:
                    LOGGER.error("command failed (%s)", type(error).__name__)
                    if device_touched:
                        try:
                            self.pa2_client.close()
                        except Exception as close_error:
                            LOGGER.error(
                                "PA2 close failed (%s)",
                                type(close_error).__name__,
                            )
                        self._publish(
                            f"{base}/status/details", "offline", retain=True
                        )
                        self._publish(
                            f"{base}/status", "offline", retain=True
                        )
                    self._publish(
                        f"{base}/state/last_command",
                        "ERROR: command failed",
                        retain=True,
                    )

    def _publish(self, topic: str, payload: str, *, retain: bool):
        with self._mqtt_state_lock:
            if self._mqtt_failure is not None or not self._mqtt_connected:
                return None
            try:
                result = self._publish_checked(topic, payload, retain=retain)
            except MqttPublishError as error:
                self._mqtt_connected = False
                self._mqtt_generation += 1
                self._mqtt_failure = error
                self._stop_event.set()
                raise error
        return result

    def _publish_checked(self, topic: str, payload: str, *, retain: bool):
        """Publish without the command-session fence, for bounded shutdown use."""
        try:
            result = self.mqtt.publish(topic, payload, qos=1, retain=retain)
        except Exception as cause:
            raise MqttPublishError(f"MQTT publish raised for {topic}") from cause
        result_code = getattr(result, "rc", mqtt.MQTT_ERR_SUCCESS)
        if result_code != mqtt.MQTT_ERR_SUCCESS:
            raise MqttPublishError(
                f"MQTT publish failed for {topic} with result {result_code}"
            )
        return result

    def _wait_for_publication(self, result: Any, *, topic: str) -> None:
        try:
            result.wait_for_publish(timeout=SHUTDOWN_PUBLISH_TIMEOUT)
            published = result.is_published()
        except Exception as error:
            failure = MqttPublishError(
                f"MQTT publication acknowledgement failed for {topic}"
            )
            self._mqtt_connected = False
            self._mqtt_failure = failure
            self._stop_event.set()
            raise failure from error
        if not published:
            failure = MqttPublishError(
                f"MQTT publication acknowledgement timed out for {topic}"
            )
            self._mqtt_connected = False
            self._mqtt_failure = failure
            self._stop_event.set()
            raise failure
