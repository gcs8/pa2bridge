"""Home Assistant App entry point and Supervisor option loading."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import (
    AppConfig,
    ConfigError,
    MAX_RECALL_TIMEOUT_SECONDS,
    MqttConfig,
    Pa2Config,
    has_disallowed_mqtt_codepoint,
    parse_allowed_preset_slots,
    validate_mqtt_topic_prefix,
    validate_network_host,
)
from .mqtt_bridge import MqttBridge


_ALLOWED_OPTION_KEYS = {
    "allowed_preset_slots",
    "base_topic",
    "connect_timeout",
    "discovery_prefix",
    "expose_meters",
    "pa2_host",
    "pa2_password",
    "pa2_password_override",
    "pa2_port",
    "pa2_username",
    "poll_interval",
    "post_recall_delay",
    "preset_slots",
    "recall_timeout",
    "state_poll_interval",
}


def _required_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Home Assistant option {key} must be a non-empty string")
    return value.strip()


def _port(values: Mapping[str, Any], key: str, *, default: int) -> int:
    value = values.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ConfigError(
            f"Home Assistant option {key} must be an integer from 1 through 65535"
        )
    return value


def _number(
    values: Mapping[str, Any],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    allow_minimum: bool = False,
) -> float:
    value = values.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"Home Assistant option {key} must be a number")
    number = float(value)
    above_minimum = number >= minimum if allow_minimum else number > minimum
    if not math.isfinite(number) or not above_minimum or number > maximum:
        relation = "at least" if allow_minimum else "greater than"
        raise ConfigError(
            f"Home Assistant option {key} must be finite, {relation} {minimum:g}, "
            f"and at most {maximum:g}"
        )
    return number


def _boolean(values: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Home Assistant option {key} must be a boolean")
    return value


def _allowed_slots(values: Mapping[str, Any]) -> tuple[int, ...] | None:
    canonical_present = "preset_slots" in values
    legacy_present = "allowed_preset_slots" in values
    canonical = (
        parse_allowed_preset_slots(
            values["preset_slots"],
            description="Home Assistant option preset_slots",
        )
        if canonical_present
        else None
    )
    legacy = (
        parse_allowed_preset_slots(
            values["allowed_preset_slots"],
            description="Home Assistant option allowed_preset_slots",
        )
        if legacy_present
        else None
    )
    if canonical_present and legacy_present:
        if canonical is None:
            return legacy
        if legacy is None:
            return canonical
        if set(canonical) != set(legacy):
            raise ConfigError(
                "Home Assistant option preset_slots conflicts with allowed_preset_slots"
            )
        return canonical
    if canonical_present:
        return canonical
    if legacy_present:
        return legacy
    return None


def _pa2_password(values: Mapping[str, Any]) -> str:
    key = "pa2_password_override" if "pa2_password_override" in values else "pa2_password"
    value = values.get(key)
    if value is None or value == "":
        return "administrator"
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Home Assistant option {key} must be blank or a non-empty string")
    return value


def _topic_prefix(values: Mapping[str, Any], key: str, *, default: str) -> str:
    return validate_mqtt_topic_prefix(
        values.get(key, default),
        description=f"Home Assistant option {key}",
    )


def _mqtt_service_host(environment: Mapping[str, str]) -> str:
    key = "PA2BRIDGE_MQTT_HOST"
    value = environment.get(key)
    return validate_network_host(value, description=f"MQTT service data {key}")


def _mqtt_service_credential(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key)
    if value is None or not value or has_disallowed_mqtt_codepoint(value):
        raise ConfigError(f"MQTT service data {key} must be a non-empty MQTT string")
    return value


def _mqtt_service_port(environment: Mapping[str, str]) -> int:
    raw = environment.get("PA2BRIDGE_MQTT_PORT", "")
    if re.fullmatch(r"[1-9][0-9]{0,4}", raw) is None:
        raise ConfigError("MQTT service port must be an ASCII decimal integer")
    value = int(raw, 10)
    if not 1 <= value <= 65535:
        raise ConfigError("MQTT service port must be from 1 through 65535")
    return value


def load_ha_app_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load strict bridge configuration from Supervisor options and MQTT service data."""

    environment = os.environ if environ is None else environ
    try:
        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigError(f"duplicate Home Assistant option key: {key}")
                result[key] = value
            return result

        options = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except ConfigError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"could not read Home Assistant options: {error}") from error
    if not isinstance(options, dict):
        raise ConfigError("Home Assistant options must be a JSON object")
    unknown = sorted(set(options) - _ALLOWED_OPTION_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown Home Assistant option key(s): {', '.join(unknown)}"
        )

    pa2 = Pa2Config(
        host=validate_network_host(
            _required_string(options, "pa2_host"),
            description="Home Assistant option pa2_host",
        ),
        port=_port(options, "pa2_port", default=19272),
        username=_required_string(options, "pa2_username"),
        password=_pa2_password(options),
        allowed_preset_slots=_allowed_slots(options),
        connect_timeout=_number(
            options,
            "connect_timeout",
            default=3.0,
            minimum=0,
            maximum=60,
        ),
        recall_timeout=_number(
            options,
            "recall_timeout",
            default=10.0,
            minimum=0,
            maximum=MAX_RECALL_TIMEOUT_SECONDS,
        ),
        poll_interval=_number(
            options,
            "poll_interval",
            default=0.2,
            minimum=0,
            maximum=10,
        ),
        post_recall_delay=_number(
            options,
            "post_recall_delay",
            default=1.0,
            minimum=0,
            maximum=60,
            allow_minimum=True,
        ),
    )
    mqtt = MqttConfig(
        host=_mqtt_service_host(environment),
        port=_mqtt_service_port(environment),
        username=_mqtt_service_credential(environment, "PA2BRIDGE_MQTT_USERNAME"),
        password=_mqtt_service_credential(environment, "PA2BRIDGE_MQTT_PASSWORD"),
        base_topic=_topic_prefix(
            options,
            "base_topic",
            default="driverack/pa2",
        ),
        discovery_prefix=_topic_prefix(
            options,
            "discovery_prefix",
            default="homeassistant",
        ),
        client_id="pa2bridge",
        state_poll_interval=_number(
            options,
            "state_poll_interval",
            default=5.0,
            minimum=0,
            maximum=3600,
        ),
        expose_meters=_boolean(options, "expose_meters", default=False),
    )
    return AppConfig(pa2=pa2, mqtt=mqtt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run PA2Bridge as a Home Assistant App")
    parser.add_argument("--options", default="/data/options.json")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    MqttBridge(load_ha_app_config(args.options)).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
