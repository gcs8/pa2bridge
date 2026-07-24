from __future__ import annotations

from pathlib import Path

import pytest

from pa2bridge.config import ConfigError, load_config


def _config_text(*, pa2_extra: str = "", mqtt_extra: str = "") -> str:
    return (
        '[pa2]\nhost="pa2"\nallowed_preset_slots=[1,2]\n'
        f"{pa2_extra}"
        '[mqtt]\nhost="broker"\n'
        f"{mqtt_extra}"
    )


def test_load_config_resolves_secret_env_names_without_persisting_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[pa2]
host = "192.0.2.20"
allowed_preset_slots = [1, 2]
password_env = "PA2_PASSWORD"

[mqtt]
host = "homeassistant.local"
username_env = "MQTT_USERNAME"
password_env = "MQTT_PASSWORD"
""".strip()
    )

    config = load_config(
        path,
        environ={
            "PA2_PASSWORD": "pa2-secret",
            "MQTT_USERNAME": "bridge",
            "MQTT_PASSWORD": "mqtt-secret",
        },
    )

    assert config.pa2.host == "192.0.2.20"
    assert config.pa2.allowed_preset_slots == (1, 2)
    assert config.pa2.password == "pa2-secret"
    assert config.mqtt.username == "bridge"
    assert config.mqtt.password == "mqtt-secret"
    assert "secret" not in repr(config)


@pytest.mark.parametrize(("section", "invalid_host"), [("pa2", "pa2/bridge"), ("mqtt", "broker/#")])
def test_load_config_rejects_hosts_that_are_not_ascii_hostnames_or_ip_addresses(
    tmp_path: Path, section: str, invalid_host: str
) -> None:
    path = tmp_path / "config.toml"
    text = _config_text()
    valid_host = "pa2" if section == "pa2" else "broker"
    path.write_text(
        text.replace(f'host="{valid_host}"', f'host="{invalid_host}"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ASCII hostname or IP address"):
        load_config(path, environ={})


def test_missing_named_secret_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[pa2]
host = "192.0.2.20"
password_env = "PA2_PASSWORD"
allowed_preset_slots = [1]

[mqtt]
host = "homeassistant.local"
""".strip()
    )

    with pytest.raises(ConfigError, match="PA2_PASSWORD"):
        load_config(path, environ={})


@pytest.mark.parametrize(
    ("section", "setting"),
    [("pa2", "password"), ("mqtt", "username"), ("mqtt", "password")],
)
def test_plaintext_credential_keys_are_rejected(
    tmp_path: Path, section: str, setting: str
) -> None:
    path = tmp_path / "config.toml"
    extra = f'{setting}="secret"\n'
    path.write_text(
        _config_text(
            pa2_extra=extra if section == "pa2" else "",
            mqtt_extra=extra if section == "mqtt" else "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="plaintext"):
        load_config(path, environ={})


def test_empty_environment_secret_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config_text(pa2_extra='password_env="PA2_PASSWORD"\n'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(path, environ={"PA2_PASSWORD": ""})


@pytest.mark.parametrize(
    "text",
    [
        _config_text(pa2_extra="recall_timout=0.1\n"),
        _config_text(mqtt_extra="state_poll_intervall=123\n"),
        _config_text() + "[unexpected]\nvalue=true\n",
    ],
)
def test_unknown_standalone_configuration_keys_are_rejected(
    tmp_path: Path,
    text: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown"):
        load_config(path, environ={})


def test_allowed_preset_slots_are_restricted_to_the_pa2_range(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[pa2]
host = "192.0.2.20"
allowed_preset_slots = [1, 101]

[mqtt]
host = "homeassistant.local"
""".strip()
    )

    with pytest.raises(ConfigError, match="slots 1 through 100"):
        load_config(path, environ={})


def test_duplicate_slots_and_missing_tables_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(
        '[pa2]\nhost="pa2"\nallowed_preset_slots=[1,1]\n[mqtt]\nhost="broker"\n'
    )
    with pytest.raises(ConfigError, match="duplicates"):
        load_config(duplicate, environ={})

    missing = tmp_path / "missing.toml"
    missing.write_text('[pa2]\nhost="pa2"\nallowed_preset_slots=[1]\n')
    with pytest.raises(ConfigError, match=r"missing \[mqtt\] table"):
        load_config(missing, environ={})


def test_default_credentials_are_redacted_and_bad_topic_prefix_is_rejected(tmp_path: Path) -> None:
    valid = tmp_path / "valid.toml"
    valid.write_text(
        '[pa2]\nhost="pa2"\nallowed_preset_slots=[1]\n[mqtt]\nhost="broker"\n'
    )
    config = load_config(valid, environ={})
    assert config.pa2.password == "administrator"
    assert "password=" not in repr(config)

    invalid = tmp_path / "invalid.toml"
    invalid.write_text(
        '[pa2]\nhost="pa2"\nallowed_preset_slots=[1]\n[mqtt]\nhost="broker"\nbase_topic="/"\n'
    )
    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(invalid, environ={})


def test_standalone_config_auto_discovers_presets_when_allowlist_is_omitted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[pa2]\nhost="pa2"\n[mqtt]\nhost="broker"\n')

    config = load_config(path, environ={})

    assert config.pa2.allowed_preset_slots is None


def test_standalone_config_accepts_full_pa2_preset_range(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[pa2]\nhost="pa2"\nallowed_preset_slots=[1,32,75,100]\n'
        '[mqtt]\nhost="broker"\n'
    )

    config = load_config(path, environ={})

    assert config.pa2.allowed_preset_slots == (1, 32, 75, 100)


@pytest.mark.parametrize(
    ("section", "setting", "value"),
    [
        ("pa2", "port", "0"),
        ("pa2", "port", "65536"),
        ("pa2", "connect_timeout", "0.0"),
        ("pa2", "connect_timeout", "nan"),
        ("pa2", "recall_timeout", "inf"),
        ("pa2", "poll_interval", "-0.1"),
        ("pa2", "post_recall_delay", "-0.1"),
        ("mqtt", "port", "true"),
        ("mqtt", "state_poll_interval", "0"),
        ("mqtt", "state_poll_interval", "-inf"),
    ],
)
def test_numeric_configuration_is_type_correct_finite_and_bounded(
    tmp_path: Path,
    section: str,
    setting: str,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    extra = f"{setting}={value}\n"
    path.write_text(
        _config_text(
            pa2_extra=extra if section == "pa2" else "",
            mqtt_extra=extra if section == "mqtt" else "",
        )
    )

    with pytest.raises(ConfigError, match=setting):
        load_config(path, environ={})


def test_recall_timeout_stays_below_the_mqtt_keepalive_margin(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config_text(pa2_extra="recall_timeout=20.1\n"))

    with pytest.raises(ConfigError, match=r"recall_timeout.*at most 20"):
        load_config(path, environ={})


@pytest.mark.parametrize(
    ("section", "setting", "value"),
    [
        ("pa2", "port", '"19272"'),
        ("pa2", "connect_timeout", "true"),
        ("pa2", "username", "42"),
        ("mqtt", "port", '"1883"'),
        ("mqtt", "state_poll_interval", '"5.0"'),
        ("mqtt", "expose_meters", '"false"'),
        ("mqtt", "client_id", "false"),
    ],
)
def test_configuration_does_not_coerce_wrong_types(
    tmp_path: Path,
    section: str,
    setting: str,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    extra = f"{setting}={value}\n"
    path.write_text(
        _config_text(
            pa2_extra=extra if section == "pa2" else "",
            mqtt_extra=extra if section == "mqtt" else "",
        )
    )

    with pytest.raises(ConfigError, match=setting):
        load_config(path, environ={})


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("base_topic", '"driverack/+/pa2"'),
        ("base_topic", '"driverack//pa2"'),
        ("base_topic", '"/driverack/pa2"'),
        ("base_topic", '"driverack/\\npa2"'),
        ("base_topic", '" driverack/pa2"'),
        ("base_topic", '"driverack/pa2\\u007fstatus"'),
        ("base_topic", '"driverack/pa2Å"'),
        ("discovery_prefix", '"homeassistant/#"'),
        ("discovery_prefix", '"homeassistant/"'),
        ("discovery_prefix", '"homeassistantÅ"'),
    ],
)
def test_mqtt_topic_prefixes_reject_wildcards_and_empty_levels(
    tmp_path: Path,
    setting: str,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config_text(mqtt_extra=f"{setting}={value}\n"))

    with pytest.raises(ConfigError, match=setting):
        load_config(path, environ={})
