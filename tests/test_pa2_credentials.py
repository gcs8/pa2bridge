"""PA2 credentials are validated at configuration load, not at the socket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa2bridge.config import ConfigError, load_config
from pa2bridge.ha_app import load_ha_app_config
from pa2bridge.protocol import HiQnetClient


_MQTT_ENV = {
    "PA2BRIDGE_MQTT_HOST": "core-mosquitto",
    "PA2BRIDGE_MQTT_PORT": "1883",
    "PA2BRIDGE_MQTT_USERNAME": "app-user",
    "PA2BRIDGE_MQTT_PASSWORD": "mqtt-secret",
}
_BAD_USERNAMES = ["front desk", "admin\ttab", 'ad"min']
_BAD_PASSWORDS = ['pa"ss', "pa\nss", "pa\x7fss"]


def _toml(path: Path, *, username: str = "administrator", password_env: bool = False) -> Path:
    password_line = 'password_env = "PA2_PASSWORD"\n' if password_env else ""
    path.write_text(
        "[pa2]\n"
        'host = "192.0.2.20"\n'
        f"username = {json.dumps(username)}\n"
        f"{password_line}"
        "[mqtt]\n"
        'host = "192.0.2.10"\n',
        encoding="utf-8",
    )
    return path


def _options(path: Path, **overrides: object) -> Path:
    options: dict[str, object] = {
        "pa2_host": "192.0.2.20",
        "pa2_username": "administrator",
        "pa2_password_override": "pa2-secret",
    }
    options.update(overrides)
    path.write_text(json.dumps(options), encoding="utf-8")
    return path


@pytest.mark.parametrize("username", _BAD_USERNAMES)
def test_toml_rejects_username_the_connect_frame_cannot_carry(
    tmp_path: Path, username: str
) -> None:
    with pytest.raises(ConfigError, match=r"\[pa2\]\.username"):
        load_config(_toml(tmp_path / "config.toml", username=username), environ={})


@pytest.mark.parametrize("password", _BAD_PASSWORDS)
def test_toml_rejects_password_the_connect_frame_cannot_carry(
    tmp_path: Path, password: str
) -> None:
    with pytest.raises(ConfigError, match=r"\[pa2\]\.password") as excinfo:
        load_config(
            _toml(tmp_path / "config.toml", password_env=True),
            environ={"PA2_PASSWORD": password},
        )
    assert password not in str(excinfo.value)


@pytest.mark.parametrize("username", _BAD_USERNAMES)
def test_ha_app_rejects_username_the_connect_frame_cannot_carry(
    tmp_path: Path, username: str
) -> None:
    with pytest.raises(ConfigError, match="pa2_username"):
        load_ha_app_config(
            _options(tmp_path / "options.json", pa2_username=username),
            environ=_MQTT_ENV,
        )


@pytest.mark.parametrize("password", _BAD_PASSWORDS)
def test_ha_app_rejects_password_the_connect_frame_cannot_carry(
    tmp_path: Path, password: str
) -> None:
    with pytest.raises(ConfigError, match="pa2_password_override") as excinfo:
        load_ha_app_config(
            _options(tmp_path / "options.json", pa2_password_override=password),
            environ=_MQTT_ENV,
        )
    assert password not in str(excinfo.value)


def test_loaders_still_accept_ordinary_custom_credentials(tmp_path: Path) -> None:
    config = load_config(
        _toml(tmp_path / "config.toml", username="tech-1", password_env=True),
        environ={"PA2_PASSWORD": r"S3cret pass\word!"},
    )
    assert config.pa2.username == "tech-1"
    assert config.pa2.password == r"S3cret pass\word!"

    ha_config = load_ha_app_config(
        _options(
            tmp_path / "options.json",
            pa2_username="tech-1",
            pa2_password_override=r"S3cret pass\word!",
        ),
        environ=_MQTT_ENV,
    )
    assert ha_config.pa2.username == "tech-1"
    assert ha_config.pa2.password == r"S3cret pass\word!"


def test_protocol_rejects_whitespace_username_before_opening_a_socket() -> None:
    client = HiQnetClient("example.invalid", timeout=1.0)
    with pytest.raises(ValueError, match="whitespace"):
        client.connect("front desk", "administrator")
    assert client.connected is False
