"""TOML configuration with environment-only secret resolution."""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MQTT_KEEPALIVE_SECONDS = 30
MAX_RECALL_TIMEOUT_SECONDS = 20.0

_TOP_LEVEL_KEYS = {"pa2", "mqtt"}
_PA2_KEYS = {
    "allowed_preset_slots",
    "connect_timeout",
    "host",
    "password",
    "password_env",
    "poll_interval",
    "port",
    "post_recall_delay",
    "recall_timeout",
    "username",
}
_MQTT_KEYS = {
    "base_topic",
    "client_id",
    "discovery_prefix",
    "expose_meters",
    "host",
    "password",
    "password_env",
    "port",
    "state_poll_interval",
    "username",
    "username_env",
}


class ConfigError(ValueError):
    """Configuration is missing, unsafe, or internally inconsistent."""


def validate_network_host(value: Any, *, description: str) -> str:
    """Return an ASCII hostname or IP address suitable for sockets and IDs."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "%" in value
    ):
        raise ConfigError(f"{description} must be an ASCII hostname or IP address")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        if len(value) > 253 or any(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is None
            for label in labels
        ):
            raise ConfigError(
                f"{description} must be an ASCII hostname or IP address"
            ) from None
    return value


@dataclass(frozen=True)
class Pa2Config:
    host: str
    port: int = 19272
    username: str = "administrator"
    password: str = field(default="administrator", repr=False)
    allowed_preset_slots: tuple[int, ...] = (1,)
    connect_timeout: float = 3.0
    recall_timeout: float = 10.0
    poll_interval: float = 0.2
    post_recall_delay: float = 1.0


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    base_topic: str = "driverack/pa2"
    discovery_prefix: str = "homeassistant"
    client_id: str = "pa2bridge"
    state_poll_interval: float = 5.0
    expose_meters: bool = False


@dataclass(frozen=True)
class AppConfig:
    pa2: Pa2Config
    mqtt: MqttConfig


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] table")
    return value


def _required_string(table: dict[str, Any], key: str, table_name: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{table_name}].{key} must be a non-empty string")
    return value.strip()


def _string_value(
    table: dict[str, Any],
    key: str,
    table_name: str,
    *,
    default: str,
) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{table_name}].{key} must be a non-empty string")
    return value.strip()


def _port_value(table: dict[str, Any], key: str, table_name: str, *, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ConfigError(f"[{table_name}].{key} must be an integer from 1 through 65535")
    return value


def _finite_number(
    table: dict[str, Any],
    key: str,
    table_name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    allow_minimum: bool = False,
) -> float:
    value = table.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"[{table_name}].{key} must be a number")
    number = float(value)
    above_minimum = number >= minimum if allow_minimum else number > minimum
    if not math.isfinite(number) or not above_minimum or number > maximum:
        relation = "at least" if allow_minimum else "greater than"
        raise ConfigError(
            f"[{table_name}].{key} must be finite, {relation} {minimum:g}, "
            f"and at most {maximum:g}"
        )
    return number


def _boolean_value(
    table: dict[str, Any], key: str, table_name: str, *, default: bool
) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{table_name}].{key} must be a boolean")
    return value


def has_disallowed_mqtt_codepoint(value: str) -> bool:
    return any(
        codepoint < 0x20
        or 0x7F <= codepoint <= 0x9F
        or 0xD800 <= codepoint <= 0xDFFF
        or 0xFDD0 <= codepoint <= 0xFDEF
        or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
        for codepoint in map(ord, value)
    )


def validate_mqtt_topic_prefix(value: object, *, description: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or has_disallowed_mqtt_codepoint(value)
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "+" in value
        or "#" in value
    ):
        raise ConfigError(
            f"{description} must not be empty, must be NFC-normalized, and must contain non-empty topic "
            "levels without wildcards, surrounding whitespace, or controls"
        )
    return value


def _mqtt_topic_prefix(
    table: dict[str, Any], key: str, *, default: str
) -> str:
    return validate_mqtt_topic_prefix(
        table.get(key, default),
        description=f"[mqtt].{key}",
    )


def _secret_from_env(
    table: dict[str, Any],
    key: str,
    environ: Mapping[str, str],
    *,
    default: str | None,
) -> str | None:
    if key in table:
        raise ConfigError(
            f"plaintext {key} is not accepted; use {key}_env instead"
        )
    env_key = table.get(f"{key}_env")
    if env_key is None:
        return default
    if not isinstance(env_key, str) or not env_key:
        raise ConfigError(f"{key}_env must name an environment variable")
    value = environ.get(env_key)
    if value is None:
        raise ConfigError(f"required environment variable {env_key} is not set")
    if not value.strip():
        raise ConfigError(f"required environment variable {env_key} must not be empty")
    return value


def load_config(path: str | Path, *, environ: Mapping[str, str] | None = None) -> AppConfig:
    path = Path(path)
    env = os.environ if environ is None else environ
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not load {path}: {error}") from error

    unknown_top = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigError(
            f"unknown top-level configuration key(s): {', '.join(unknown_top)}"
        )

    pa2_data = _table(document, "pa2")
    mqtt_data = _table(document, "mqtt")
    unknown_pa2 = sorted(set(pa2_data) - _PA2_KEYS)
    if unknown_pa2:
        raise ConfigError(
            f"unknown [pa2] configuration key(s): {', '.join(unknown_pa2)}"
        )
    unknown_mqtt = sorted(set(mqtt_data) - _MQTT_KEYS)
    if unknown_mqtt:
        raise ConfigError(
            f"unknown [mqtt] configuration key(s): {', '.join(unknown_mqtt)}"
        )

    slots_value = pa2_data.get("allowed_preset_slots")
    if not isinstance(slots_value, list) or not slots_value or not all(
        isinstance(slot, int) and not isinstance(slot, bool) for slot in slots_value
    ):
        raise ConfigError("[pa2].allowed_preset_slots must be a non-empty integer list")
    if any(slot not in {1, 2} for slot in slots_value):
        raise ConfigError("[pa2].allowed_preset_slots must contain only slots 1 and 2")
    if len(set(slots_value)) != len(slots_value):
        raise ConfigError("[pa2].allowed_preset_slots must not contain duplicates")

    pa2 = Pa2Config(
        host=validate_network_host(
            _required_string(pa2_data, "host", "pa2"),
            description="[pa2].host",
        ),
        port=_port_value(pa2_data, "port", "pa2", default=19272),
        username=_string_value(
            pa2_data, "username", "pa2", default="administrator"
        ),
        password=_secret_from_env(pa2_data, "password", env, default="administrator") or "",
        allowed_preset_slots=tuple(slots_value),
        connect_timeout=_finite_number(
            pa2_data,
            "connect_timeout",
            "pa2",
            default=3.0,
            minimum=0,
            maximum=60,
        ),
        recall_timeout=_finite_number(
            pa2_data,
            "recall_timeout",
            "pa2",
            default=10.0,
            minimum=0,
            maximum=MAX_RECALL_TIMEOUT_SECONDS,
        ),
        poll_interval=_finite_number(
            pa2_data,
            "poll_interval",
            "pa2",
            default=0.2,
            minimum=0,
            maximum=10,
        ),
        post_recall_delay=_finite_number(
            pa2_data,
            "post_recall_delay",
            "pa2",
            default=1.0,
            minimum=0,
            maximum=60,
            allow_minimum=True,
        ),
    )
    mqtt = MqttConfig(
        host=validate_network_host(
            _required_string(mqtt_data, "host", "mqtt"),
            description="[mqtt].host",
        ),
        port=_port_value(mqtt_data, "port", "mqtt", default=1883),
        username=_secret_from_env(mqtt_data, "username", env, default=None),
        password=_secret_from_env(mqtt_data, "password", env, default=None),
        base_topic=_mqtt_topic_prefix(
            mqtt_data, "base_topic", default="driverack/pa2"
        ),
        discovery_prefix=_mqtt_topic_prefix(
            mqtt_data, "discovery_prefix", default="homeassistant"
        ),
        client_id=_string_value(
            mqtt_data, "client_id", "mqtt", default="pa2bridge"
        ),
        state_poll_interval=_finite_number(
            mqtt_data,
            "state_poll_interval",
            "mqtt",
            default=5.0,
            minimum=0,
            maximum=3600,
        ),
        expose_meters=_boolean_value(
            mqtt_data, "expose_meters", "mqtt", default=False
        ),
    )
    return AppConfig(pa2=pa2, mqtt=mqtt)
