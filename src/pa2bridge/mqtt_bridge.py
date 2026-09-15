"""Home Assistant MQTT discovery and runtime bridge."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

from .config import (
    MQTT_KEEPALIVE_SECONDS,
    AppConfig,
    ConfigError,
    normalize_mac_address,
    validate_mqtt_topic_prefix,
    validate_network_host,
)
from .controller import (
    ConnectionValidationError,
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
_MAX_DISCOVERY_STATE_BYTES = 64 * 1024
_MAX_DISCOVERY_TOPICS = 100
_MAX_IDENTITY_STATE_BYTES = 1024
_MAX_ARP_TABLE_BYTES = 64 * 1024
_MAX_ROUTE_TABLE_BYTES = 64 * 1024
_MAX_HA_STATES_BYTES = 16 * 1024 * 1024
_MAX_HA_TEMPLATE_BYTES = 64 * 1024
_MAX_HA_CONFIG_ENTRIES_BYTES = 2 * 1024 * 1024
_MAX_HA_TRACKER_CANDIDATES = 32
_MAX_TRACKER_AGE_SECONDS = 2 * 60 * 60
_TRUSTED_NETWORK_INTEGRATIONS = frozenset({"unifi", "unifi_insights"})
_HA_TRACKER_ENTITY_ID = re.compile(r"device_tracker\.[a-z0-9_]{1,255}", re.ASCII)
_DISCOVERY_NODE = re.compile(r"driverack_pa2_[A-Za-z0-9_-]{1,253}", re.ASCII)
_DISCOVERY_OBJECTS = frozenset(
    {
        ("select", "preset"),
        ("button", "unmute_outputs"),
        ("sensor", "firmware"),
        ("sensor", "last_command"),
        ("sensor", "preset_inventory"),
        ("sensor", "crossover"),
        *(("switch", f"{channel}_mute") for channel in OUTPUT_MUTES),
        *(("sensor", f"{side}_input_level") for side in INPUT_LEVELS),
        *(("binary_sensor", f"{side}_input_clip") for side in INPUT_CLIPS),
        *(("sensor", f"{channel}_output_level") for channel in OUTPUT_LEVELS),
    }
)
COMMAND_TTL_SECONDS = 5.0
PA2_READ_CYCLE_TIMEOUT = 60.0


def _resolve_ipv4_addresses(host: str) -> set[str]:
    try:
        address = ipaddress.ip_address(host)
        return {str(address)} if address.version == 4 else set()
    except ValueError:
        try:
            addresses = {
                str(result[4][0])
                for result in socket.getaddrinfo(
                    host,
                    None,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
            }
            return addresses if len(addresses) == 1 else set()
        except OSError:
            return set()


def _proc_ipv4(value: str) -> int:
    if len(value) != 8:
        raise ValueError("invalid proc IPv4 field")
    return int.from_bytes(bytes.fromhex(value), "little")


def _on_link_interfaces(
    address: str,
    *,
    route_path: Path,
) -> set[str]:
    """Return interfaces whose most-specific route reaches the peer directly."""

    try:
        target = int(ipaddress.IPv4Address(address))
        with route_path.open("rb") as handle:
            raw = handle.read(_MAX_ROUTE_TABLE_BYTES + 1)
    except (OSError, ValueError):
        return set()
    if len(raw) > _MAX_ROUTE_TABLE_BYTES:
        return set()
    try:
        lines = raw.decode("ascii").splitlines()[1:]
    except UnicodeDecodeError:
        return set()
    routes: list[tuple[int, int, bool, str]] = []
    for line in lines:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 8:
            return set()
        try:
            destination = _proc_ipv4(fields[1])
            gateway = _proc_ipv4(fields[2])
            flags = int(fields[3], 16)
            metric = int(fields[6], 10)
            mask = _proc_ipv4(fields[7])
        except ValueError:
            return set()
        inverse_mask = (~mask) & 0xFFFFFFFF
        if (
            inverse_mask & (inverse_mask + 1)
            or destination & inverse_mask
            or flags < 0
            or metric < 0
        ):
            return set()
        if not flags & 0x1 or target & mask != destination & mask:
            continue
        routes.append(
            (
                mask.bit_count(),
                metric,
                gateway == 0 and not flags & (0x2 | 0x0200),
                fields[0],
            )
        )
    if not routes:
        return set()
    longest_prefix = max(prefix for prefix, _, _, _ in routes)
    longest = [route for route in routes if route[0] == longest_prefix]
    lowest_metric = min(metric for _, metric, _, _ in longest)
    best = [route for route in longest if route[1] == lowest_metric]
    if any(not on_link for _, _, on_link, _ in best):
        return set()
    interfaces = {interface for _, _, _, interface in best}
    return interfaces if len(interfaces) == 1 else set()


def _discover_mac_address(
    host: str,
    *,
    arp_path: Path = Path("/proc/net/arp"),
    route_path: Path = Path("/proc/net/route"),
) -> str | None:
    """Read an on-link IPv4 peer's complete entry from the local ARP table."""

    addresses = _resolve_ipv4_addresses(host)
    if not addresses:
        return None
    on_link_interfaces = _on_link_interfaces(
        next(iter(addresses)),
        route_path=route_path,
    )
    if not on_link_interfaces:
        return None
    try:
        with arp_path.open("rb") as handle:
            raw = handle.read(_MAX_ARP_TABLE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_ARP_TABLE_BYTES:
        return None
    try:
        lines = raw.decode("ascii").splitlines()[1:]
    except UnicodeDecodeError:
        return None
    matches: set[str] = set()
    for line in lines:
        fields = line.split()
        if (
            len(fields) < 6
            or fields[0] not in addresses
            or fields[5] not in on_link_interfaces
        ):
            continue
        try:
            flags = int(fields[2], 16)
            mac_address = normalize_mac_address(
                fields[3],
                description="neighbor MAC address",
            )
        except (ConfigError, ValueError):
            continue
        if fields[1] == "0x1" and flags >= 0 and flags & 0x2 and mac_address is not None:
            matches.add(mac_address)
    if len(matches) > 1:
        raise DiscoveryStateError("local neighbour table has conflicting PA2 MAC entries")
    if not matches:
        return None
    return matches.pop()


def _remaining_ha_timeout(deadline: float, limit: float) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(limit, remaining)


def _discover_mac_address_from_home_assistant(
    host: str,
    token: str,
    *,
    timeout: float = 3.0,
    deadline: float | None = None,
) -> str | None:
    """Correlate an IPv4 peer with network-integration state held by Home Assistant."""

    addresses = _resolve_ipv4_addresses(host)
    if not addresses or not token:
        return None
    if deadline is None:
        deadline = time.monotonic() + timeout
    request_timeout = _remaining_ha_timeout(deadline, timeout)
    if request_timeout is None:
        return None
    try:
        request = Request(
            "http://supervisor/core/api/states",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=request_timeout) as response:
            raw = response.read(_MAX_HA_STATES_BYTES + 1)
    except (OSError, URLError, ValueError):
        return None
    if len(raw) > _MAX_HA_STATES_BYTES:
        return None
    try:
        states = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(states, list):
        return None
    candidates: dict[str, set[str]] = {}
    for state in states:
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id")
        if (
            not isinstance(entity_id, str)
            or _HA_TRACKER_ENTITY_ID.fullmatch(entity_id) is None
            or state.get("state") != "home"
        ):
            continue
        timestamp = state.get("last_reported", state.get("last_updated"))
        if not isinstance(timestamp, str):
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if observed_at.tzinfo is None:
            continue
        try:
            age = (datetime.now(UTC) - observed_at.astimezone(UTC)).total_seconds()
        except (OverflowError, ValueError):
            continue
        if age < -300 or age > _MAX_TRACKER_AGE_SECONDS:
            continue
        attributes = state.get("attributes")
        if (
            not isinstance(attributes, dict)
            or attributes.get("source_type") != "router"
            or attributes.get("tracking_type") != "connection"
            or attributes.get("authorized") is not True
        ):
            continue
        observed_ips: set[str] = set()
        for key in ("ip", "ip_address", "ip_addresses"):
            value = attributes.get(key)
            values = value if isinstance(value, list) else [value]
            for candidate in values:
                if not isinstance(candidate, str):
                    continue
                try:
                    parsed = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if parsed.version == 4:
                    observed_ips.add(str(parsed))
        if not addresses.intersection(observed_ips):
            continue
        for key in ("mac", "mac_address"):
            try:
                mac_address = normalize_mac_address(
                    attributes.get(key),
                    description="Home Assistant network MAC address",
                )
            except ConfigError:
                continue
            if mac_address is not None:
                candidates.setdefault(entity_id, set()).add(mac_address)
    trusted = _trusted_home_assistant_trackers(
        frozenset(candidates),
        token,
        timeout=timeout,
        deadline=deadline,
    )
    matches = {
        mac_address
        for entity_id in trusted
        for mac_address in candidates[entity_id]
    }
    if len(matches) > 1:
        raise DiscoveryStateError(
            "Home Assistant network data has conflicting PA2 MAC entries"
        )
    if not matches:
        return None
    return matches.pop()


def _trusted_home_assistant_trackers(
    entity_ids: frozenset[str],
    token: str,
    *,
    timeout: float = 3.0,
    deadline: float | None = None,
) -> frozenset[str]:
    """Verify candidate tracker ownership through Home Assistant's registry data."""

    if not entity_ids or len(entity_ids) > _MAX_HA_TRACKER_CANDIDATES:
        return frozenset()
    if deadline is None:
        deadline = time.monotonic() + timeout
    mapping_items = ",".join(
        f"{json.dumps(entity_id)}:config_entry_id({json.dumps(entity_id)})"
        for entity_id in sorted(entity_ids)
    )
    template = "{{ {" + mapping_items + "} | to_json }}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        request_timeout = _remaining_ha_timeout(deadline, timeout)
        if request_timeout is None:
            return frozenset()
        request = Request(
            "http://supervisor/core/api/template",
            data=json.dumps({"template": template}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=request_timeout) as response:
            raw_mapping = response.read(_MAX_HA_TEMPLATE_BYTES + 1)
        request_timeout = _remaining_ha_timeout(deadline, timeout)
        if request_timeout is None:
            return frozenset()
        request = Request(
            "http://supervisor/core/api/config/config_entries/entry",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=request_timeout) as response:
            raw_entries = response.read(_MAX_HA_CONFIG_ENTRIES_BYTES + 1)
    except (OSError, URLError, ValueError):
        return frozenset()
    if (
        len(raw_mapping) > _MAX_HA_TEMPLATE_BYTES
        or len(raw_entries) > _MAX_HA_CONFIG_ENTRIES_BYTES
    ):
        return frozenset()
    try:
        mapping = json.loads(
            raw_mapping.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        entries = json.loads(
            raw_entries.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (DiscoveryStateError, UnicodeDecodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(mapping, dict) or not isinstance(entries, list):
        return frozenset()
    trusted_entries = {
        entry.get("entry_id")
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("domain") in _TRUSTED_NETWORK_INTEGRATIONS
        and isinstance(entry.get("entry_id"), str)
    }
    return frozenset(
        entity_id
        for entity_id in entity_ids
        if isinstance(mapping.get(entity_id), str)
        and mapping[entity_id] in trusted_entries
    )


class MqttPublishError(RuntimeError):
    """The broker did not accept a required state or availability update."""


class DiscoveryStateError(RuntimeError):
    """Persisted MQTT discovery ownership state was missing or unsafe."""


class IdentityRevalidationUnavailable(RuntimeError):
    """Live network data could not temporarily confirm the saved PA2 identity."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryStateError(f"duplicate state key: {key}")
        result[key] = value
    return result


def _validate_discovery_topic(topic: Any) -> str:
    try:
        validated = validate_mqtt_topic_prefix(
            topic,
            description="persisted discovery topic",
        )
    except ConfigError as error:
        raise DiscoveryStateError(
            "discovery state contains an invalid topic"
        ) from error
    parts = validated.split("/")
    if len(parts) < 5:
        raise DiscoveryStateError("discovery state contains an invalid topic")
    component, node, object_id, suffix = parts[-4:]
    if (
        suffix != "config"
        or _DISCOVERY_NODE.fullmatch(node) is None
        or (component, object_id) not in _DISCOVERY_OBJECTS
        or len(validated.encode("utf-8")) > 65_535
    ):
        raise DiscoveryStateError("discovery state contains an invalid topic")
    return validated


def _load_discovery_topics(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_DISCOVERY_STATE_BYTES + 1)
    except FileNotFoundError:
        return frozenset()
    except OSError as error:
        raise DiscoveryStateError(f"could not read discovery state: {error}") from error
    if len(raw) > _MAX_DISCOVERY_STATE_BYTES:
        raise DiscoveryStateError("discovery state exceeds its size limit")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except DiscoveryStateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryStateError(f"could not parse discovery state: {error}") from error
    if not isinstance(value, dict) or set(value) != {"version", "topics"}:
        raise DiscoveryStateError("discovery state has an invalid schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise DiscoveryStateError("discovery state has an unsupported version")
    topics = value["topics"]
    if not isinstance(topics, list) or len(topics) > _MAX_DISCOVERY_TOPICS:
        raise DiscoveryStateError("discovery state has an invalid topic list")
    validated = tuple(_validate_discovery_topic(topic) for topic in topics)
    if len(set(validated)) != len(validated):
        raise DiscoveryStateError("discovery state contains duplicate topics")
    return frozenset(validated)


def _load_identity_state(path: Path | None) -> tuple[str, str] | None:
    if path is None:
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DiscoveryStateError("identity state file has unsafe ownership or mode")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_IDENTITY_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except DiscoveryStateError:
        raise
    except OSError as error:
        raise DiscoveryStateError(f"could not read identity state: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > _MAX_IDENTITY_STATE_BYTES:
        raise DiscoveryStateError("identity state exceeds its size limit")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except DiscoveryStateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryStateError(f"could not parse identity state: {error}") from error
    if not isinstance(value, dict) or set(value) != {"version", "host", "mac_address"}:
        raise DiscoveryStateError("identity state has an invalid schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise DiscoveryStateError("identity state has an unsupported version")
    try:
        host = _normalize_identity_host(value["host"])
        mac_address = normalize_mac_address(
            value["mac_address"], description="persisted PA2 MAC address"
        )
    except (ConfigError, ValueError, TypeError) as error:
        raise DiscoveryStateError("identity state has invalid values") from error
    if mac_address is None:
        raise DiscoveryStateError("identity state has invalid values")
    return host, mac_address


def _normalize_identity_host(value: Any) -> str:
    host = validate_network_host(value, description="persisted PA2 host")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.lower()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    sync_error: OSError | None = None
    close_error: OSError | None = None
    try:
        os.fsync(descriptor)
    except OSError as error:
        sync_error = error
    try:
        os.close(descriptor)
    except OSError as error:
        close_error = error
    if sync_error is not None:
        raise sync_error
    if close_error is not None:
        raise close_error


def _ensure_state_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    if not candidate.is_dir():
        raise NotADirectoryError(candidate)
    if candidate.parent != candidate and _state_directory_pending(candidate):
        _create_state_directory(candidate)
    for directory in reversed(missing):
        _create_state_directory(directory)


def _state_directory_marker(directory: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(directory.name)).hexdigest()[:16]
    return directory.parent / f".pa2bridge-state-dir-{digest}.pending"


def _state_directory_inner_marker(directory: Path) -> Path:
    return directory / ".pa2bridge-state-directory.pending"


def _marker_payload(directory: Path) -> str:
    identity = hashlib.sha256(os.fsencode(os.path.abspath(directory))).hexdigest()
    return f"pa2bridge-state-directory-v1:{identity}"


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _state_directory_pending(directory: Path) -> bool:
    return _entry_exists(_state_directory_marker(directory)) or _entry_exists(
        _state_directory_inner_marker(directory)
    )


def _validate_state_directory_marker(marker: Path, payload: str) -> None:
    metadata = marker.lstat()
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or os.readlink(marker) != payload
    ):
        raise OSError("invalid discovery state directory marker")


def _install_state_directory_marker(marker: Path, payload: str) -> None:
    try:
        marker.symlink_to(payload)
    except FileExistsError:
        pass
    _validate_state_directory_marker(marker, payload)


def _create_state_directory(directory: Path) -> None:
    marker = _state_directory_marker(directory)
    inner_marker = _state_directory_inner_marker(directory)
    payload = _marker_payload(directory)
    parent_pending = _entry_exists(marker)
    inner_pending = directory.is_dir() and _entry_exists(inner_marker)
    if parent_pending and inner_pending:
        raise OSError("duplicate discovery state directory markers")
    if inner_pending:
        _validate_state_directory_marker(inner_marker, payload)
        _fsync_directory(directory.parent)
        _fsync_directory(directory)
        _validate_state_directory_marker(inner_marker, payload)
        inner_marker.unlink()
        return
    _install_state_directory_marker(marker, payload)
    _fsync_directory(directory.parent)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        if not directory.is_dir():
            raise NotADirectoryError(directory)
    _fsync_directory(directory.parent)
    if _entry_exists(inner_marker):
        raise OSError("unexpected discovery state directory marker")
    _validate_state_directory_marker(marker, payload)
    os.replace(marker, inner_marker)
    _fsync_directory(directory.parent)
    _fsync_directory(directory)
    _validate_state_directory_marker(inner_marker, payload)
    inner_marker.unlink()


def _save_state_payload(path: Path, payload: bytes, *, description: str) -> None:
    descriptor: int | None = None
    temporary_path: str | None = None
    write_error: OSError | None = None
    cleanup_error: OSError | None = None
    try:
        _ensure_state_directory(path.parent)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"{description} write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    except OSError as error:
        write_error = error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = error
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
    if write_error is not None:
        raise DiscoveryStateError(
            f"could not write {description}: {write_error}"
        ) from write_error
    if cleanup_error is not None:
        raise DiscoveryStateError(
            f"could not clean up {description}: {cleanup_error}"
        ) from cleanup_error


def _save_discovery_topics(path: Path, topics: frozenset[str]) -> None:
    if len(topics) > _MAX_DISCOVERY_TOPICS:
        raise DiscoveryStateError("discovery state has too many owned topics")
    validated_topics = frozenset(
        _validate_discovery_topic(topic) for topic in topics
    )
    payload = (
        json.dumps(
            {"version": 1, "topics": sorted(validated_topics)},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_DISCOVERY_STATE_BYTES:
        raise DiscoveryStateError("discovery state exceeds its size limit")
    _save_state_payload(path, payload, description="discovery state")


def _save_identity_state(path: Path, host: str, mac_address: str) -> None:
    try:
        normalized_host = _normalize_identity_host(host)
        normalized_mac = normalize_mac_address(
            mac_address, description="persisted PA2 MAC address"
        )
    except (ConfigError, ValueError, TypeError) as error:
        raise DiscoveryStateError("identity state has invalid values") from error
    if normalized_mac is None:
        raise DiscoveryStateError("identity state has invalid values")
    payload = (
        json.dumps(
            {
                "version": 1,
                "host": normalized_host,
                "mac_address": normalized_mac,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_IDENTITY_STATE_BYTES:
        raise DiscoveryStateError("identity state exceeds its size limit")
    _save_state_payload(path, payload, description="identity state")


@dataclass(frozen=True)
class DeviceInfo:
    identifier: str
    name: str
    firmware: str
    mac_address: str | None = None


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
    received_at: float


def _device_payload(device: DeviceInfo) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "identifiers": [device.identifier],
        "name": device.name,
        "manufacturer": "dbx",
        "model": "DriveRack PA2",
        "sw_version": device.firmware,
    }
    if device.mac_address is not None:
        payload["connections"] = [["mac", device.mac_address]]
    return payload


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
    else:
        # A previous run with expose_meters enabled left retained discovery
        # configs on the broker; Home Assistant would keep those meter
        # entities online forever with no state source. An empty retained
        # payload clears the broker copy and removes the entity, and is a
        # no-op when nothing was retained.
        for component, object_id in (
            *(("sensor", f"{side}_input_level") for side in INPUT_LEVELS),
            *(("binary_sensor", f"{side}_input_clip") for side in INPUT_CLIPS),
            *(("sensor", f"{channel}_output_level") for channel in OUTPUT_LEVELS),
        ):
            messages.append(
                MqttPublish(
                    topic=(
                        f"{prefix}/{component}/{device.identifier}/{object_id}/config"
                    ),
                    payload="",
                )
            )
    return messages


class MqttBridge:
    """Owns the authoritative PA2 session and maps MQTT commands to verified operations."""

    def __init__(
        self,
        config: AppConfig,
        *,
        discovery_state_path: Path | None = None,
        identity_state_path: Path | None = None,
        home_assistant_token: str | None = None,
    ) -> None:
        self.config = config
        self.discovery_state_path = discovery_state_path
        if identity_state_path is None and discovery_state_path is not None:
            suffix = discovery_state_path.suffix
            identity_state_path = discovery_state_path.with_name(
                f"{discovery_state_path.stem}.identity{suffix or '.json'}"
            )
        if (
            discovery_state_path is not None
            and identity_state_path is not None
            and os.path.abspath(discovery_state_path)
            == os.path.abspath(identity_state_path)
        ):
            raise DiscoveryStateError(
                "discovery and identity state paths must be different"
            )
        self.identity_state_path = identity_state_path
        self._home_assistant_token = home_assistant_token
        self._persisted_discovery_topics = _load_discovery_topics(
            discovery_state_path
        )
        self._persisted_identity = _load_identity_state(self.identity_state_path)
        self._pending_identity_state: tuple[str, str] | None = None
        self._stable_mac_address = config.pa2.mac_address
        self._address_only_connection_generation: int | None = None
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
            reconnect_validator=self._validate_reconnected_peer,
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
        self._sigterm_requested = False
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
        self._mqtt_ready.clear()
        self._stop_event.clear()
        self._sigterm_requested = False
        previous_sigterm_handler = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm_handler = signal.signal(
                signal.SIGTERM,
                self._handle_sigterm,
            )
        try:
            self._run_forever()
        finally:
            if previous_sigterm_handler is not None:
                signal.signal(signal.SIGTERM, previous_sigterm_handler)

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        del signum, frame
        self._sigterm_requested = True

    def _apply_pending_sigterm(self) -> bool:
        if not self._sigterm_requested:
            return False
        if not self._stop_event.is_set():
            LOGGER.info("stopping after SIGTERM")
        self._stop_event.set()
        self._mqtt_ready.set()
        self._mqtt_state_changed.set()
        return True

    def _run_forever(self) -> None:
        loop_started = False
        try:
            self.mqtt.connect(
                self.config.mqtt.host,
                self.config.mqtt.port,
                keepalive=MQTT_KEEPALIVE_SECONDS,
            )
            self.mqtt.loop_start()
            loop_started = True
            self._apply_pending_sigterm()
            mqtt_ready = self._mqtt_ready.wait(timeout=10.0)
            if self._apply_pending_sigterm():
                return
            if not mqtt_ready:
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
            while True:
                self._apply_pending_sigterm()
                if self._stop_event.is_set():
                    break
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
                    self._apply_pending_sigterm()
                    reconnect_delay = 1.0
                except (
                    IdentityRevalidationUnavailable,
                    ProtocolError,
                    OSError,
                    ValueError,
                    OutputVerificationError,
                    RecallTimeout,
                    TelemetryError,
                ) as error:
                    if self._apply_pending_sigterm():
                        continue
                    if isinstance(error, IdentityRevalidationUnavailable):
                        LOGGER.warning(
                            "PA2 identity could not be confirmed from current network data; "
                            "retrying safely. Check UniFi tracking or enter the optional PA2 "
                            "MAC address if this continues."
                        )
                    else:
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
            with self._pa2_lock:
                self.pa2_client.close()

    def publish_state(
        self,
        state: Pa2State,
        *,
        deadline: float | None = None,
    ) -> None:
        with self._pa2_lock:
            if not self._mqtt_connected:
                return
            levels = (
                self.controller.output_levels(deadline=deadline)
                if self.config.mqtt.expose_meters
                else {}
            )
            input_meters = (
                self.controller.input_meters(deadline=deadline)
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

    def publish_details(self, *, deadline: float | None = None) -> None:
        """Publish slow-changing read-only data needed for inventory and curves."""

        with self._pa2_lock:
            if not self._mqtt_connected:
                return
            allowed_presets, presets = self.controller.list_preset_views(
                deadline=deadline
            )
            crossover = self.controller.crossover(deadline=deadline)
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
                self._publish_discovery(refreshed_discovery)
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

    def _refresh_details(
        self,
        *,
        current_slot: int,
        deadline: float | None = None,
    ) -> bool:
        try:
            self.publish_details(deadline=deadline)
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
            deadline = time.monotonic() + PA2_READ_CYCLE_TIMEOUT
            try:
                reconnected = not self.pa2_client.connected
                if reconnected:
                    self._connect_pa2(deadline=deadline)
                identity = self._identity_for_connection(deadline=deadline)
                if not self._discovery_published:
                    self._publish_discovery(self.discovery)
                    self._discovery_published = True
                state = self.controller.state(identity=identity, deadline=deadline)
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
                self.publish_state(state, deadline=deadline)
                refresh_details = (
                    invalidate_details
                    or refresh_overdue
                )
                if refresh_details:
                    self._refresh_details(
                        current_slot=state.current_preset.slot,
                        deadline=deadline,
                    )
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

    def _connect_pa2(self, *, deadline: float | None = None) -> None:
        with self._pa2_lock:
            if deadline is None:
                deadline = time.monotonic() + PA2_READ_CYCLE_TIMEOUT
            self._pa2_identity = None
            self.pa2_client.connect_before(
                self.config.pa2.username,
                self.config.pa2.password,
                deadline=deadline,
            )
            validated_peer = self._device_info(
                DeviceIdentity("dbxDriveRackPA2", "DriveRackPA2", "unknown"),
                deadline=deadline,
            )
            identity = self.controller.identity(deadline=deadline)
            self._pa2_identity = (
                self.pa2_client.connection_generation,
                identity,
            )
            presets = self.controller.list_presets(deadline=deadline)
            self._allowed_presets = tuple(presets)
            self._preset_commands = frozenset(preset.label for preset in presets)
            self.device = DeviceInfo(
                identifier=validated_peer.identifier,
                name=identity.instance_name,
                firmware=identity.firmware,
                mac_address=validated_peer.mac_address,
            )
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
            LOGGER.info(
                "connected to PA2 %r at %s:%d (firmware %r); %d presets available",
                identity.instance_name,
                self.config.pa2.host,
                self.config.pa2.port,
                identity.firmware,
                len(presets),
            )

    def _device_info(
        self,
        identity: DeviceIdentity,
        *,
        deadline: float | None = None,
    ) -> DeviceInfo:
        configured_mac = self.config.pa2.mac_address
        persisted_mac = (
            self._persisted_identity[1]
            if self._persisted_identity is not None
            else None
        )
        peer_ipv4 = getattr(self.pa2_client, "peer_ipv4", None)
        discovered: str | None = None
        source: str | None = None
        if deadline is not None and time.monotonic() >= deadline:
            raise DiscoveryStateError("PA2 peer validation deadline expired")
        if peer_ipv4 is not None:
            discovered = _discover_mac_address(peer_ipv4)
            source = "local network"
        if (
            peer_ipv4 is not None
            and discovered is None
            and self._home_assistant_token is not None
        ):
            timeout = 3.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DiscoveryStateError("PA2 peer validation deadline expired")
                timeout = min(timeout, remaining)
            discovered = _discover_mac_address_from_home_assistant(
                peer_ipv4,
                self._home_assistant_token,
                timeout=timeout,
                deadline=deadline,
            )
            source = "Home Assistant network data"

        if configured_mac is not None:
            if discovered is not None and configured_mac != discovered:
                raise DiscoveryStateError(
                    "configured PA2 MAC conflicts with the connected peer"
                )
            if (
                persisted_mac is not None
                and configured_mac != persisted_mac
                and not self.config.pa2.replace_saved_identity
            ):
                raise DiscoveryStateError(
                    "configured PA2 MAC conflicts with persisted identity state"
                )
            if persisted_mac is not None and configured_mac != persisted_mac:
                LOGGER.warning(
                    "replacing the saved PA2 identity after explicit operator request"
                )
            mac_address = configured_mac
        elif discovered is not None:
            if (
                self._stable_mac_address is not None
                and discovered != self._stable_mac_address
            ):
                raise DiscoveryStateError(
                    "discovered PA2 MAC conflicts with the active stable identity"
                )
            if persisted_mac is not None and discovered != persisted_mac:
                raise DiscoveryStateError(
                    "discovered PA2 MAC conflicts with persisted identity state"
                )
            newly_discovered = self._stable_mac_address is None
            mac_address = discovered
            self._stable_mac_address = discovered
            if newly_discovered:
                LOGGER.info(
                    "discovered PA2 MAC from %s for stable MQTT identity",
                    source,
                )
        elif self._stable_mac_address is not None or persisted_mac is not None:
            raise IdentityRevalidationUnavailable(
                "the connected PA2 peer MAC could not be revalidated"
            )
        else:
            current_generation = self.pa2_client.connection_generation
            if (
                self._address_only_connection_generation is not None
                and self._address_only_connection_generation != current_generation
            ):
                raise IdentityRevalidationUnavailable(
                    "the connected PA2 peer MAC could not be revalidated"
                )
            mac_address = None
            self._address_only_connection_generation = current_generation
            LOGGER.warning(
                "PA2 MAC was not found for %s; using address-based MQTT identity. "
                "Any later physical connection will be refused until a stable MAC is "
                "available; check UniFi tracking or enter the optional PA2 MAC address.",
                self.config.pa2.host,
            )

        if mac_address is not None:
            self._address_only_connection_generation = None

        if mac_address is not None and self.identity_state_path is not None:
            persisted_host = peer_ipv4 or self.config.pa2.host
            persisted_value = (
                _normalize_identity_host(persisted_host),
                mac_address,
            )
            if self._persisted_identity != persisted_value:
                replacing_physical_device = (
                    persisted_mac is not None
                    and persisted_mac != mac_address
                    and self.config.pa2.replace_saved_identity
                )
                if replacing_physical_device:
                    self._pending_identity_state = (persisted_host, mac_address)
                else:
                    _save_identity_state(
                        self.identity_state_path,
                        persisted_host,
                        mac_address,
                    )
                    self._persisted_identity = persisted_value
        if mac_address is None:
            stable_id = self.config.pa2.host.replace(".", "_").replace(":", "_")
        else:
            stable_id = mac_address.replace(":", "")
        return DeviceInfo(
            identifier=f"driverack_pa2_{stable_id}",
            name=identity.instance_name,
            firmware=identity.firmware,
            mac_address=mac_address,
        )

    def _validate_reconnected_peer(self, deadline: float | None) -> None:
        cached = self._pa2_identity
        identity = (
            cached[1]
            if cached is not None
            else DeviceIdentity("dbxDriveRackPA2", "DriveRackPA2", "unknown")
        )
        try:
            device = self._device_info(identity, deadline=deadline)
            if device.mac_address is None:
                raise DiscoveryStateError(
                    "PA2 reconnect cannot continue without a stable MAC identity"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise DiscoveryStateError("PA2 peer validation deadline expired")
        except (DiscoveryStateError, IdentityRevalidationUnavailable) as error:
            raise ConnectionValidationError(str(error)) from error

    def _identity_for_connection(
        self,
        *,
        deadline: float | None = None,
    ) -> DeviceIdentity:
        if deadline is None:
            deadline = time.monotonic() + PA2_READ_CYCLE_TIMEOUT
        generation = self.pa2_client.connection_generation
        cached = self._pa2_identity
        if cached is None or cached[0] != generation:
            identity = self.controller.identity(deadline=deadline)
            self._pa2_identity = (generation, identity)
            self.device = self._device_info(identity, deadline=deadline)
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

    def _state_with_current_identity(
        self,
        state: Pa2State,
        *,
        deadline: float | None = None,
    ) -> Pa2State:
        return Pa2State(
            identity=self._identity_for_connection(deadline=deadline),
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
                LOGGER.info("connected to MQTT broker; command subscription ready")
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
        received_at = time.monotonic()
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
                    QueuedCommand(
                        topic,
                        payload,
                        self._mqtt_generation,
                        received_at,
                    )
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
        if self._command_expired(command):
            self._publish_stale_command()
            return True
        self._execute_command(command)
        return True

    def _command_expired(self, command: QueuedCommand) -> bool:
        return time.monotonic() - command.received_at >= COMMAND_TTL_SECONDS

    def _command_deadline(self, command: QueuedCommand) -> float:
        return command.received_at + COMMAND_TTL_SECONDS

    def _publish_stale_command(self) -> None:
        self._publish(
            f"{self.config.mqtt.base_topic}/state/last_command",
            "ERROR: stale command discarded",
            retain=True,
        )

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
                if self._command_expired(command):
                    self._publish_stale_command()
                    return
                try:
                    if self._apply_pending_sigterm():
                        return
                    refresh_details = False
                    if command.topic == f"{base}/command/preset":
                        self._details_valid = False
                        self._publish(
                            f"{base}/status/details", "offline", retain=True
                        )
                        identity = self._identity_for_connection(
                            deadline=self._command_deadline(command)
                        )
                        if self._apply_pending_sigterm():
                            return
                        device_touched = True
                        state = self.controller.activate_preset(
                            command.payload,
                            unmute_after=True,
                            identity=identity,
                            start_deadline=self._command_deadline(command),
                        )
                        if self._apply_pending_sigterm():
                            return
                        post_command_deadline = (
                            time.monotonic() + PA2_READ_CYCLE_TIMEOUT
                        )
                        state = self._state_with_current_identity(
                            state,
                            deadline=post_command_deadline,
                        )
                        output_result = (
                            "outputs verified unmuted"
                            if state.all_outputs_unmuted
                            else "output mute state preserved"
                        )
                        result = f"recalled {state.current_preset.label}; {output_result}"
                        refresh_details = True
                    elif command.topic == f"{base}/command/unmute":
                        device_touched = True
                        self.controller.set_all_outputs_muted(
                            False,
                            start_deadline=self._command_deadline(command),
                        )
                        if self._apply_pending_sigterm():
                            return
                        post_command_deadline = (
                            time.monotonic() + PA2_READ_CYCLE_TIMEOUT
                        )
                        state = self.controller.state(
                            identity=self._identity_for_connection(
                                deadline=post_command_deadline
                            ),
                            deadline=post_command_deadline,
                        )
                        result = "all outputs verified unmuted"
                    else:
                        channel = command.topic.rsplit("/", 1)[-1]
                        device_touched = True
                        self.controller.set_output_muted(
                            channel,
                            command.payload == "On",
                            start_deadline=self._command_deadline(command),
                        )
                        if self._apply_pending_sigterm():
                            return
                        post_command_deadline = (
                            time.monotonic() + PA2_READ_CYCLE_TIMEOUT
                        )
                        state = self.controller.state(
                            identity=self._identity_for_connection(
                                deadline=post_command_deadline
                            ),
                            deadline=post_command_deadline,
                        )
                        result = f"{channel} mute verified {command.payload}"
                    self.publish_state(state, deadline=post_command_deadline)
                    if refresh_details:
                        self._refresh_details(
                            current_slot=state.current_preset.slot,
                            deadline=post_command_deadline,
                        )
                    self._publish(
                        f"{base}/state/last_command", result, retain=True
                    )
                except Exception as error:
                    if self._apply_pending_sigterm():
                        return
                    if isinstance(error, ConnectionValidationError):
                        LOGGER.error(
                            "command stopped because the reconnected PA2 identity "
                            "could not be verified; no writes were sent after reconnect"
                        )
                    else:
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
                    user_result = "ERROR: command failed"
                    if isinstance(error, ConnectionValidationError):
                        user_result = (
                            "ERROR: PA2 identity could not be verified; check network "
                            "tracking or the optional MAC setting"
                        )
                    self._publish(
                        f"{base}/state/last_command",
                        user_result,
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

    def _publish_discovery(self, messages: list[MqttPublish]) -> None:
        deadline = time.monotonic() + SHUTDOWN_PUBLISH_TIMEOUT
        validated_messages = [
            MqttPublish(
                topic=_validate_discovery_topic(message.topic),
                payload=message.payload,
                retain=message.retain,
            )
            for message in messages
        ]
        current_topics = frozenset(
            message.topic for message in validated_messages if message.payload
        )
        pending_topics = self._persisted_discovery_topics | current_topics
        if self.discovery_state_path is not None:
            _save_discovery_topics(self.discovery_state_path, pending_topics)
        self._persisted_discovery_topics = pending_topics
        stale_topics = sorted(self._persisted_discovery_topics - current_topics)
        cleanup_results = [
            (topic, self._publish(topic, "", retain=True)) for topic in stale_topics
        ]
        self._wait_for_discovery_publications(cleanup_results, deadline=deadline)
        current_results = [
            (
                message.topic,
                self._publish(
                    message.topic,
                    message.payload,
                    retain=message.retain,
                ),
            )
            for message in validated_messages
        ]
        self._wait_for_discovery_publications(current_results, deadline=deadline)
        if self._pending_identity_state is not None:
            pending_host, pending_mac = self._pending_identity_state
            if self.identity_state_path is None:
                raise DiscoveryStateError(
                    "replacement identity state has no durable destination"
                )
            _save_identity_state(
                self.identity_state_path,
                pending_host,
                pending_mac,
            )
            self._persisted_identity = (
                _normalize_identity_host(pending_host),
                pending_mac,
            )
            self._pending_identity_state = None
        if (
            self.discovery_state_path is not None
            and current_topics != self._persisted_discovery_topics
        ):
            _save_discovery_topics(self.discovery_state_path, current_topics)
        self._persisted_discovery_topics = current_topics
        LOGGER.info(
            "published MQTT discovery for %r: %d entities, %d stale topics removed",
            self.device.name if self.device is not None else "PA2",
            len(current_topics),
            len(stale_topics),
        )

    def _wait_for_discovery_publications(
        self,
        publications,
        *,
        deadline: float,
    ) -> None:
        for topic, result in publications:
            remaining = deadline - time.monotonic()
            if result is None or remaining <= 0:
                raise self._publication_timeout(topic)
            self._wait_for_publication(
                result,
                topic=topic,
                timeout=remaining,
            )
            if time.monotonic() >= deadline:
                raise self._publication_timeout(topic)

    def _publication_timeout(self, topic: str) -> MqttPublishError:
        failure = MqttPublishError(
            f"MQTT publication acknowledgement timed out for {topic}"
        )
        self._mqtt_connected = False
        self._mqtt_failure = failure
        self._stop_event.set()
        return failure

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

    def _wait_for_publication(
        self,
        result: Any,
        *,
        topic: str,
        timeout: float = SHUTDOWN_PUBLISH_TIMEOUT,
    ) -> None:
        try:
            result.wait_for_publish(timeout=timeout)
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
            raise self._publication_timeout(topic)
