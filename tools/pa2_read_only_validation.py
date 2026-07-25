"""One-shot, read-only PA2 validation harness with a hard command budget."""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import product
from typing import Protocol

from pa2bridge.config import AppConfig, load_config
from pa2bridge.controller import (
    CROSSOVER_AT,
    CROSSOVER_SV,
    CURRENT_PRESET,
    OUTPUT_MUTES,
    PRESET_ROOT,
    Pa2Controller,
)
from pa2bridge.mqtt_bridge import MqttBridge, MqttPublishError

COMMAND_LIMIT = 28
POLL_LIMIT = 2
STATE_POLL_INTERVAL_SECONDS = 30.0
_RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{6,30}[a-z0-9])", re.ASCII)


class ValidationSafetyError(RuntimeError):
    """The validation session attempted to exceed its read-only boundary."""


class RawPa2Client(Protocol):
    @property
    def connected(self) -> bool: ...

    @property
    def connection_generation(self) -> int: ...

    def connect(self, username: str, password: str) -> None: ...

    def close(self) -> None: ...

    def get(self, path: Iterable[str]) -> str: ...

    def get_before(self, path: Iterable[str], *, deadline: float) -> str: ...

    def ls(self, path: Iterable[str]) -> dict[str, str]: ...

    def ls_before(
        self, path: Iterable[str], *, deadline: float
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class CommandRecord:
    verb: str
    path: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ValidationReport:
    poll_count: int
    records: tuple[CommandRecord, ...]

    @property
    def command_count(self) -> int:
        return len(self.records)

    @property
    def verb_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(record.verb for record in self.records).items()))

    def public_payload(self) -> dict[str, object]:
        return {
            "command_count": self.command_count,
            "poll_count": self.poll_count,
            "verb_counts": self.verb_counts,
            "verified": True,
        }


class ReadOnlyPa2Client:
    """Permit one connection and bounded ``get``/``ls`` operations only."""

    def __init__(
        self,
        client: RawPa2Client,
        *,
        command_limit: int = COMMAND_LIMIT,
        allowed_sequences: Iterable[Sequence[CommandRecord]] | None = None,
        catalog_response_aware: bool = False,
    ) -> None:
        if command_limit <= 0:
            raise ValueError("command_limit must be positive")
        self._client = client
        self._command_limit = command_limit
        self._allowed_sequences = (
            None
            if allowed_sequences is None
            else tuple(tuple(sequence) for sequence in allowed_sequences)
        )
        if self._allowed_sequences == ():
            raise ValueError("allowed_sequences must not be empty")
        if catalog_response_aware and self._allowed_sequences is None:
            raise ValueError(
                "catalog_response_aware requires explicit allowed_sequences"
            )
        self._catalog_response_aware = catalog_response_aware
        self._catalog_has_current: list[bool] = []
        self._connection_attempted = False
        self.records: list[CommandRecord] = []

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def connection_generation(self) -> int:
        return self._client.connection_generation

    @property
    def catalog_has_current(self) -> tuple[bool, ...]:
        return tuple(self._catalog_has_current)

    def _active_sequences(self) -> tuple[tuple[CommandRecord, ...], ...] | None:
        if not self._catalog_response_aware:
            return self._allowed_sequences
        known = tuple(self._catalog_has_current)
        remaining = 4 - len(known)
        if remaining < 0:
            return ()
        return tuple(
            expected_command_records(catalog_has_current=(*known, *suffix))
            for suffix in product((False, True), repeat=remaining)
        )

    def _reserve(self, verb: str, path: Iterable[str] | None = None) -> None:
        if len(self.records) >= self._command_limit:
            raise ValidationSafetyError(
                f"read-only validation reached its {self._command_limit}-command limit"
            )
        normalized = None if path is None else tuple(path)
        record = CommandRecord(verb, normalized)
        candidate = (*self.records, record)
        active_sequences = self._active_sequences()
        if active_sequences is not None and not any(
            sequence[: len(candidate)] == candidate
            for sequence in active_sequences
        ):
            raise ValidationSafetyError(
                "read-only validation command sequence left the approved plan"
            )
        self.records.append(record)

    def connect(self, username: str, password: str) -> None:
        if self._connection_attempted:
            raise ValidationSafetyError(
                "second connection attempt is forbidden during read-only validation"
            )
        self._connection_attempted = True
        self._reserve("connect")
        self._client.connect(username, password)

    def close(self) -> None:
        self._client.close()

    def get(self, path: Iterable[str]) -> str:
        normalized = tuple(path)
        self._reserve("get", normalized)
        return self._client.get(normalized)

    def get_before(self, path: Iterable[str], *, deadline: float) -> str:
        normalized = tuple(path)
        self._reserve("get", normalized)
        return self._client.get_before(normalized, deadline=deadline)

    def ls(self, path: Iterable[str]) -> dict[str, str]:
        normalized = tuple(path)
        self._reserve("ls", normalized)
        entries = self._client.ls(normalized)
        self._observe_catalog(normalized, entries)
        return entries

    def ls_before(
        self, path: Iterable[str], *, deadline: float
    ) -> dict[str, str]:
        normalized = tuple(path)
        self._reserve("ls", normalized)
        entries = self._client.ls_before(normalized, deadline=deadline)
        self._observe_catalog(normalized, entries)
        return entries

    def _observe_catalog(
        self, path: tuple[str, ...], entries: dict[str, str]
    ) -> None:
        if self._catalog_response_aware and path == PRESET_ROOT:
            self._catalog_has_current.append("CurrentPreset" in entries)

    def set(self, path: Iterable[str], value: str) -> None:
        del path, value
        raise ValidationSafetyError("set is forbidden during read-only validation")

    def set_before(
        self,
        path: Iterable[str],
        value: str,
        *,
        deadline: float,
    ) -> None:
        del path, value, deadline
        raise ValidationSafetyError("set is forbidden during read-only validation")

    def reconnect(self) -> None:
        raise ValidationSafetyError("reconnect is forbidden during read-only validation")

    def reconnect_before(self, *, deadline: float) -> None:
        del deadline
        raise ValidationSafetyError("reconnect is forbidden during read-only validation")


def expected_command_records(
    *,
    catalog_has_current: Sequence[bool] = (True, True, True, True),
) -> tuple[CommandRecord, ...]:
    if len(catalog_has_current) != 4:
        raise ValueError("catalog_has_current must describe exactly four catalog reads")

    def catalog(flag: bool) -> tuple[CommandRecord, ...]:
        fallback = () if flag else (CommandRecord("get", CURRENT_PRESET),)
        return (CommandRecord("ls", PRESET_ROOT), *fallback)

    def state(flag: bool) -> tuple[CommandRecord, ...]:
        return (
            CommandRecord("get", CURRENT_PRESET),
            *catalog(flag),
            *(CommandRecord("get", path) for path in OUTPUT_MUTES.values()),
        )

    identity = (
        CommandRecord("get", ("Node", "AT", "Class_Name")),
        CommandRecord("get", ("Node", "AT", "Instance_Name")),
        CommandRecord("get", ("Node", "AT", "Software_Version")),
    )
    details = (
        *catalog(catalog_has_current[2]),
        CommandRecord("ls", CROSSOVER_AT),
        CommandRecord("ls", CROSSOVER_SV),
    )
    return (
        CommandRecord("connect"),
        *identity,
        *catalog(catalog_has_current[0]),
        *state(catalog_has_current[1]),
        *details,
        *state(catalog_has_current[3]),
    )


def allowed_command_sequences() -> tuple[tuple[CommandRecord, ...], ...]:
    return tuple(
        expected_command_records(catalog_has_current=flags)
        for flags in product((False, True), repeat=4)
    )


def isolated_validation_config(config: AppConfig, *, run_id: str) -> AppConfig:
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError(
            "run_id must be 8 through 32 lowercase ASCII letters, digits, or hyphens"
        )
    prefix = f"pa2bridge-validation-{run_id}"
    return replace(
        config,
        mqtt=replace(
            config.mqtt,
            base_topic=f"pa2bridge-validation/{run_id}",
            discovery_prefix=prefix,
            client_id=prefix,
            state_poll_interval=STATE_POLL_INTERVAL_SECONDS,
            expose_meters=False,
        ),
    )


class ReadOnlyValidationBridge(MqttBridge):
    """Run the production bridge path for exactly two bounded read-only polls."""

    def __init__(
        self,
        config: AppConfig,
        *,
        run_id: str,
        pa2_client: RawPa2Client | None = None,
    ) -> None:
        super().__init__(isolated_validation_config(config, run_id=run_id))
        raw_client = self.pa2_client if pa2_client is None else pa2_client
        self.read_only_client = ReadOnlyPa2Client(
            raw_client,
            allowed_sequences=allowed_command_sequences(),
            catalog_response_aware=True,
        )
        self.pa2_client = self.read_only_client  # type: ignore[assignment]
        self.controller = Pa2Controller(
            self.read_only_client,
            allowed_slots=self.config.pa2.allowed_preset_slots,
            recall_timeout=self.config.pa2.recall_timeout,
            poll_interval=self.config.pa2.poll_interval,
            post_recall_delay=self.config.pa2.post_recall_delay,
        )
        self._completed_polls = 0
        self._forbidden_mqtt_input = False
        self._unexpected_mqtt_disconnect = False

    def _on_message(self, client, userdata, message) -> None:
        del client, userdata, message
        with self._mqtt_state_lock:
            self._forbidden_mqtt_input = True
            self._mqtt_failure = MqttPublishError(
                "MQTT input is forbidden during read-only validation"
            )
        self._stop_event.set()
        with self._pa2_lock:
            self.pa2_client.close()
        self._mqtt_state_changed.set()

    def _on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ) -> None:
        if not self._stopping:
            with self._mqtt_state_lock:
                self._unexpected_mqtt_disconnect = True
                self._mqtt_failure = MqttPublishError(
                    "unexpected MQTT disconnect during read-only validation"
                )
            self._stop_event.set()
        super()._on_disconnect(
            client,
            userdata,
            disconnect_flags,
            reason_code,
            properties,
        )

    def _poll_once(self) -> None:
        if self._completed_polls >= POLL_LIMIT:
            raise ValidationSafetyError("read-only validation attempted a third poll")
        super()._poll_once()
        self._completed_polls += 1
        if self._completed_polls == POLL_LIMIT:
            self._stop_event.set()

    def validation_report(self) -> ValidationReport:
        with self._mqtt_state_lock:
            if self._unexpected_mqtt_disconnect:
                raise ValidationSafetyError(
                    "unexpected MQTT disconnect invalidated read-only validation"
                )
            if self._forbidden_mqtt_input:
                raise ValidationSafetyError(
                    "MQTT input invalidated read-only validation"
                )
            if self._mqtt_failure is not None:
                raise ValidationSafetyError(
                    "MQTT transport failure invalidated read-only validation"
                )
        if self._completed_polls != POLL_LIMIT:
            raise ValidationSafetyError(
                f"expected two polls, completed {self._completed_polls}"
            )
        records = tuple(self.read_only_client.records)
        catalog_has_current = self.read_only_client.catalog_has_current
        if len(catalog_has_current) != 4:
            raise ValidationSafetyError(
                "read-only validation did not complete four catalog reads"
            )
        expected = expected_command_records(
            catalog_has_current=catalog_has_current
        )
        if records != expected:
            raise ValidationSafetyError(
                "read-only validation command sequence did not match the approved plan"
            )
        return ValidationReport(poll_count=self._completed_polls, records=records)

    def run_validation(self) -> ValidationReport:
        mqtt_logger = logging.getLogger("pa2bridge.mqtt_bridge")
        logger_was_disabled = mqtt_logger.disabled
        mqtt_logger.disabled = True
        try:
            self.run_forever()
        finally:
            mqtt_logger.disabled = logger_was_disabled
        return self.validation_report()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one capped, read-only PA2 validation session"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = ReadOnlyValidationBridge(
            load_config(args.config),
            run_id=args.run_id,
        ).run_validation()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_class": type(error).__name__,
                    "verified": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report.public_payload(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
