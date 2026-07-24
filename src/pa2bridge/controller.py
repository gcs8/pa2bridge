"""Safety-oriented PA2 preset and output control."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from .config import MAX_PRESET_SLOT, MAX_RECALL_TIMEOUT_SECONDS
from .protocol import ProtocolError


PRESET_ROOT = ("Storage", "Presets", "SV")
CURRENT_PRESET = (*PRESET_ROOT, "CurrentPreset")
RECALL = (*PRESET_ROOT, "Recall")

OUTPUT_MUTES: dict[str, tuple[str, ...]] = {
    f"{band}_{side}": ("Preset", "OutputGains", "SV", f"{band.title()}{side.title()}OutputMute")
    for band in ("high", "mid", "low")
    for side in ("left", "right")
}

OUTPUT_LEVELS: dict[str, tuple[str, ...]] = {
    f"{band}_{side}": ("Preset", "OutputMeters", "SV", f"{band.title()}{side.title()}Output")
    for band in ("high", "mid", "low")
    for side in ("left", "right")
}

INPUT_LEVELS: dict[str, tuple[str, ...]] = {
    side: ("Preset", "InputMeters", "SV", f"{side.title()}Input")
    for side in ("left", "right")
}

INPUT_CLIPS: dict[str, tuple[str, ...]] = {
    side: ("Preset", "InputMeters", "SV", f"{side.title()}InputClip")
    for side in ("left", "right")
}

CROSSOVER_AT = ("Preset", "Crossover", "AT")
CROSSOVER_SV = ("Preset", "Crossover", "SV")
_DB_PATTERN = re.compile(
    r"(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)(?:dB)?", re.IGNORECASE | re.ASCII
)
_FREQUENCY_PATTERN = re.compile(
    r"((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)(?:(k)?Hz)?", re.IGNORECASE | re.ASCII
)
_ASCII_UNSIGNED_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)
_FILTER_TYPES = {
    "BW 6",
    "BW 12",
    "BW 18",
    "BW 24",
    "BW 30",
    "BW 36",
    "BW 42",
    "BW 48",
    "LR 12",
    "LR 24",
    "LR 36",
    "LR 48",
}


class Client(Protocol):
    def get(self, path: Iterable[str]) -> str: ...

    def get_before(self, path: Iterable[str], *, deadline: float) -> str: ...

    def set(self, path: Iterable[str], value: str) -> None: ...

    def set_before(
        self, path: Iterable[str], value: str, *, deadline: float
    ) -> None: ...

    def ls(self, path: Iterable[str]) -> dict[str, str]: ...

    def ls_before(
        self, path: Iterable[str], *, deadline: float
    ) -> dict[str, str]: ...

    def reconnect(self) -> None: ...

    def reconnect_before(self, *, deadline: float) -> None: ...


class RecallTimeout(RuntimeError):
    """The PA2 did not confirm the requested preset before the deadline."""


class OutputVerificationError(RuntimeError):
    """One or more output mute readbacks did not match the request."""


class RollbackDeadlineError(OutputVerificationError, RecallTimeout):
    """Rollback could not continue without exceeding the absolute recall deadline."""


class TelemetryError(RuntimeError):
    """A PA2 telemetry value was missing, unknown, or non-finite."""


def _parse_unsigned_integer(
    label: str,
    value: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if _ASCII_UNSIGNED_INTEGER.fullmatch(value) is None:
        raise TelemetryError(f"{label} returned invalid integer value {value!r}")
    parsed = int(value, 10)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise TelemetryError(f"{label} returned out-of-range integer value {value!r}")
    return parsed


def _parse_output_mute(channel: str, value: str) -> bool:
    if value == "On":
        return True
    if value == "Off":
        return False
    raise OutputVerificationError(
        f"{channel} returned unknown mute state {value!r}; expected 'On' or 'Off'"
    )


def _parse_db(label: str, value: str) -> float:
    match = _DB_PATTERN.fullmatch(value)
    if match is None:
        raise TelemetryError(f"{label} returned invalid decibel value {value!r}")
    number = float(match.group(1))
    if not math.isfinite(number):
        raise TelemetryError(f"{label} returned non-finite decibel value")
    return number


def _parse_binary_flag(label: str, value: str) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise TelemetryError(f"{label} returned unknown binary value {value!r}")


def _parse_frequency(label: str, value: str) -> float | None:
    if value == "Out":
        return None
    match = _FREQUENCY_PATTERN.fullmatch(value)
    if match is None:
        raise TelemetryError(f"{label} returned invalid frequency {value!r}")
    number = float(match.group(1))
    if match.group(2):
        number *= 1000
    if not math.isfinite(number) or number <= 0:
        raise TelemetryError(f"{label} returned invalid frequency {value!r}")
    return number


def _require_filter_type(label: str, value: str) -> str:
    if value not in _FILTER_TYPES:
        raise TelemetryError(f"{label} returned unknown filter type {value!r}")
    return value


def _require_polarity(label: str, value: str) -> str:
    if value not in {"Normal", "Inverted"}:
        raise TelemetryError(f"{label} returned unknown polarity {value!r}")
    return value


@dataclass(frozen=True)
class Preset:
    slot: int
    name: str

    @property
    def label(self) -> str:
        return f"{self.slot}: {self.name}"


@dataclass(frozen=True)
class DeviceIdentity:
    class_name: str
    instance_name: str
    firmware: str


@dataclass(frozen=True)
class Pa2State:
    identity: DeviceIdentity
    current_preset: Preset
    output_mutes: dict[str, bool]

    @property
    def all_outputs_unmuted(self) -> bool:
        return not any(self.output_mutes.values())


@dataclass(frozen=True)
class InputMeters:
    levels_dbfs: dict[str, float]
    clips: dict[str, bool]


@dataclass(frozen=True)
class CrossoverBand:
    identifier: str
    label: str
    high_pass_hz: float | None
    high_pass_type: str
    gain_db: float
    low_pass_hz: float | None
    low_pass_type: str
    polarity: str


@dataclass(frozen=True)
class CrossoverState:
    num_bands: int
    mono_sub: bool
    bands: tuple[CrossoverBand, ...]


class Pa2Controller:
    """Coordinates recall and readback so unmute never races an unconfirmed load."""

    def __init__(
        self,
        client: Client,
        *,
        allowed_slots: Iterable[int] | None,
        recall_timeout: float = 10.0,
        poll_interval: float = 0.2,
        post_recall_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.allowed_slots = None if allowed_slots is None else tuple(allowed_slots)
        if self.allowed_slots is not None:
            if not self.allowed_slots:
                raise ValueError("at least one allowed preset slot is required")
            if (
                len(set(self.allowed_slots)) != len(self.allowed_slots)
                or any(
                    not isinstance(slot, int)
                    or isinstance(slot, bool)
                    or not 1 <= slot <= MAX_PRESET_SLOT
                    for slot in self.allowed_slots
                )
            ):
                raise ValueError(
                    f"allowed preset slots must be unique members of slots 1 through {MAX_PRESET_SLOT}"
                )
        timings = (
            (
                "recall_timeout",
                recall_timeout,
                0.0,
                MAX_RECALL_TIMEOUT_SECONDS,
                False,
            ),
            ("poll_interval", poll_interval, 0.0, 10.0, False),
            ("post_recall_delay", post_recall_delay, 0.0, 60.0, True),
        )
        validated: dict[str, float] = {}
        for name, value, minimum, maximum, allow_minimum in timings:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a finite number")
            number = float(value)
            minimum_ok = number >= minimum if allow_minimum else number > minimum
            if not math.isfinite(number) or not minimum_ok or number > maximum:
                raise ValueError(f"{name} is outside its finite safety bounds")
            validated[name] = number
        self.recall_timeout = validated["recall_timeout"]
        self.poll_interval = validated["poll_interval"]
        self.post_recall_delay = validated["post_recall_delay"]
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._active_recall_deadline: float | None = None

    def identity(self) -> DeviceIdentity:
        with self._lock:
            return DeviceIdentity(
                class_name=self.client.get(("Node", "AT", "Class_Name")),
                instance_name=self.client.get(("Node", "AT", "Instance_Name")),
                firmware=self.client.get(("Node", "AT", "Software_Version")),
            )

    def list_presets(self) -> list[Preset]:
        with self._lock:
            catalog = self._preset_catalog()
            if self.allowed_slots is None:
                return [preset for _, preset in sorted(catalog.items())]
            return [catalog[slot] for slot in self.allowed_slots if slot in catalog]

    def list_all_presets(self) -> list[Preset]:
        """Return the complete device catalog without expanding recall access."""

        with self._lock:
            return [preset for _, preset in sorted(self._preset_catalog().items())]

    def current_preset(self) -> Preset:
        with self._lock:
            slot = _parse_unsigned_integer(
                "CurrentPreset", self.client.get(CURRENT_PRESET), minimum=1
            )
            try:
                return self._preset_catalog(expected_current=slot)[slot]
            except KeyError as error:
                raise TelemetryError(
                    f"CurrentPreset slot {slot} was absent from the complete preset catalog"
                ) from error

    def state(self) -> Pa2State:
        with self._lock:
            return Pa2State(
                identity=self.identity(),
                current_preset=self.current_preset(),
                output_mutes={
                    channel: _parse_output_mute(channel, self.client.get(path))
                    for channel, path in OUTPUT_MUTES.items()
                },
            )

    def activate_preset(self, target: str | int, *, unmute_after: bool = True) -> Pa2State:
        with self._lock:
            deadline = self._new_operation_deadline()
            self._active_recall_deadline = deadline
            outputs_touched = False
            try:
                preset = self._resolve_preset(target, deadline=deadline)
                outputs_touched = True
                return self._activate_resolved_preset(
                    preset,
                    unmute_after=unmute_after,
                    deadline=deadline,
                )
            except Exception as error:
                if not outputs_touched:
                    raise
                try:
                    self._rollback_outputs_to_muted(
                        OUTPUT_MUTES,
                        deadline=self._active_recall_deadline,
                    )
                except Exception as rollback_error:
                    if isinstance(rollback_error, RollbackDeadlineError):
                        raise rollback_error from error
                    raise OutputVerificationError(
                        f"preset activation failed ({error}); fail-closed rollback "
                        f"also failed ({rollback_error})"
                    ) from error
                raise
            finally:
                self._active_recall_deadline = None

    def _activate_resolved_preset(
        self,
        preset: Preset,
        *,
        unmute_after: bool,
        deadline: float,
    ) -> Pa2State:
        with self._lock:
            # Recall is only allowed after every output has been positively
            # verified muted. A failed mute readback aborts before Recall.
            initial_mutes = self._set_all_outputs_muted(
                True,
                deadline=deadline,
            )
            identity = DeviceIdentity(
                class_name=self._client_get(
                    ("Node", "AT", "Class_Name"), deadline=deadline
                ),
                instance_name=self._client_get(
                    ("Node", "AT", "Instance_Name"), deadline=deadline
                ),
                firmware=self._client_get(
                    ("Node", "AT", "Software_Version"), deadline=deadline
                ),
            )
            current = _parse_unsigned_integer(
                "CurrentPreset",
                self._client_get(CURRENT_PRESET, deadline=deadline),
                minimum=1,
            )
            if current != preset.slot:
                self._client_set(RECALL, str(preset.slot), deadline=deadline)
                if self._monotonic() >= deadline:
                    raise RecallTimeout(
                        f"preset {preset.label!r} recall deadline expired during the Recall write"
                    )
                while True:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        raise RecallTimeout(
                            f"preset {preset.label!r} was not confirmed within {self.recall_timeout:g}s; outputs were not unmuted"
                        )
                    try:
                        confirmed = _parse_unsigned_integer(
                            "CurrentPreset",
                            self._client_get(CURRENT_PRESET, deadline=deadline),
                            minimum=1,
                        )
                    except (ProtocolError, OSError) as error:
                        remaining = deadline - self._monotonic()
                        if remaining <= 0:
                            raise RecallTimeout(
                                f"preset {preset.label!r} was not confirmed within "
                                f"{self.recall_timeout:g}s after the PA2 console "
                                "disconnected; outputs were not unmuted"
                            ) from error
                        try:
                            self._client_reconnect(deadline=deadline)
                        except (ProtocolError, OSError):
                            pass
                        remaining = deadline - self._monotonic()
                        if remaining <= 0:
                            raise RecallTimeout(
                                f"preset {preset.label!r} was not confirmed within "
                                f"{self.recall_timeout:g}s after the PA2 console "
                                "disconnected; outputs were not unmuted"
                            ) from error
                        self._sleep(min(self.poll_interval, remaining))
                        continue
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        raise RecallTimeout(
                            f"preset {preset.label!r} was not confirmed within {self.recall_timeout:g}s; outputs were not unmuted"
                        )
                    if confirmed == preset.slot:
                        break
                    self._sleep(min(self.poll_interval, remaining))

            immediate_mutes = self._read_all_output_mutes(deadline=deadline)
            immediate_unmuted = [
                channel
                for channel, muted in immediate_mutes.items()
                if not muted
            ]
            if immediate_unmuted:
                raise OutputVerificationError(
                    "immediate post-recall output readback was not muted for: "
                    + ", ".join(immediate_unmuted)
                )
            if not unmute_after:
                return Pa2State(
                    identity=identity,
                    current_preset=preset,
                    output_mutes=initial_mutes,
                )

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise RecallTimeout(
                    f"preset {preset.label!r} recall deadline expired before post-recall verification; outputs were not unmuted"
                )
            self._sleep(min(self.post_recall_delay, remaining))
            if self._monotonic() >= deadline:
                raise RecallTimeout(
                    f"preset {preset.label!r} recall deadline expired during post-recall delay; outputs were not unmuted"
                )
            final_preset = _parse_unsigned_integer(
                "CurrentPreset",
                self._client_get(CURRENT_PRESET, deadline=deadline),
                minimum=1,
            )
            if self._monotonic() >= deadline:
                raise RecallTimeout(
                    f"preset {preset.label!r} recall deadline expired during final verification; outputs were not unmuted"
                )
            if final_preset != preset.slot:
                raise RecallTimeout(
                    f"preset {preset.label!r} was no longer active immediately before unmute; outputs were not unmuted"
                )
            self._preset_catalog(
                expected_current=final_preset,
                deadline=deadline,
            )
            if self._monotonic() >= deadline:
                raise RecallTimeout(
                    f"preset {preset.label!r} recall deadline expired during catalog verification; outputs were not unmuted"
                )
            post_recall_mutes = self._read_all_output_mutes(deadline=deadline)
            unmuted = [
                channel for channel, muted in post_recall_mutes.items() if not muted
            ]
            if unmuted:
                raise OutputVerificationError(
                    "post-recall output readback was not muted for: "
                    + ", ".join(unmuted)
                )
            if self._monotonic() >= deadline:
                raise RecallTimeout(
                    f"preset {preset.label!r} recall deadline expired during post-recall output verification; outputs were not unmuted"
                )
            unmuted_outputs = self._set_all_outputs_muted(
                False,
                deadline=deadline,
            )
            final_verified_preset = self._verify_current_preset_before_unmute(
                deadline=deadline,
            )
            if final_verified_preset.slot != preset.slot:
                raise OutputVerificationError(
                    f"preset {preset.label!r} changed during unmute"
                )
            return Pa2State(
                identity=identity,
                current_preset=preset,
                output_mutes=unmuted_outputs,
            )

    def _verify_current_preset_before_unmute(
        self,
        *,
        deadline: float | None = None,
    ) -> Preset:
        self._require_unmute_before(deadline)
        current = _parse_unsigned_integer(
            "CurrentPreset",
            self._client_get(CURRENT_PRESET, deadline=deadline),
            minimum=1,
        )
        self._require_unmute_before(deadline)
        catalog = self._preset_catalog(
            expected_current=current,
            deadline=deadline,
        )
        if self.allowed_slots is not None and current not in self.allowed_slots:
            raise OutputVerificationError(
                f"current preset slot {current} is not allowed; outputs were not unmuted"
            )
        try:
            return catalog[current]
        except KeyError as error:
            raise TelemetryError(
                f"current preset slot {current} was absent from the fresh catalog"
            ) from error

    def set_output_muted(self, channel: str, muted: bool) -> bool:
        try:
            path = OUTPUT_MUTES[channel]
        except KeyError as error:
            raise ValueError(f"unknown output channel: {channel}") from error
        expected = "On" if muted else "Off"
        with self._lock:
            deadline = self._new_operation_deadline()
            expected_preset = None
            if not muted:
                expected_preset = self._verify_current_preset_before_unmute(
                    deadline=deadline
                )
            try:
                self._require_unmute_before(deadline)
                self._client_set(path, expected, deadline=deadline)
                self._require_unmute_before(deadline)
                actual = self._client_get(path, deadline=deadline)
                self._require_unmute_before(deadline)
                if _parse_output_mute(channel, actual) != muted:
                    raise OutputVerificationError(
                        f"{channel} readback was {actual!r}, expected {expected!r}"
                    )
                if expected_preset is not None:
                    final_preset = self._verify_current_preset_before_unmute(
                        deadline=deadline
                    )
                    if final_preset.slot != expected_preset.slot:
                        raise OutputVerificationError(
                            "current preset changed during unmute"
                        )
            except Exception as error:
                try:
                    self._rollback_outputs_to_muted(
                        OUTPUT_MUTES,
                        deadline=deadline,
                        reconnect_first=True,
                    )
                except Exception as rollback_error:
                    if isinstance(rollback_error, RollbackDeadlineError):
                        raise rollback_error from error
                    operation = "mute" if muted else "unmute"
                    raise OutputVerificationError(
                        f"{channel} {operation} failed ({error}); rollback to muted also failed "
                        f"({rollback_error})"
                    ) from error
                if muted:
                    raise
                raise OutputVerificationError(
                    f"{channel} unmute failed ({error}); rolled back to muted"
                ) from error
            return muted

    def _require_unmute_before(self, deadline: float | None) -> None:
        if deadline is not None and self._monotonic() >= deadline:
            raise RecallTimeout(
                "recall deadline expired; no additional PA2 operation may start"
            )

    def _new_operation_deadline(self) -> float:
        now = self._monotonic()
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            raise RecallTimeout("monotonic clock returned a non-numeric value")
        deadline = float(now) + self.recall_timeout
        if not math.isfinite(deadline):
            raise RecallTimeout("monotonic clock could not establish a finite deadline")
        return deadline

    def _client_get(self, path: Iterable[str], *, deadline: float | None) -> str:
        if deadline is not None:
            return self.client.get_before(path, deadline=deadline)
        return self.client.get(path)

    def _client_set(
        self,
        path: Iterable[str],
        value: str,
        *,
        deadline: float | None,
    ) -> None:
        if deadline is not None:
            self.client.set_before(path, value, deadline=deadline)
            return
        self.client.set(path, value)

    def _client_ls(
        self,
        path: Iterable[str],
        *,
        deadline: float | None,
    ) -> dict[str, str]:
        if deadline is not None:
            return self.client.ls_before(path, deadline=deadline)
        return self.client.ls(path)

    def _client_reconnect(self, *, deadline: float | None) -> None:
        if deadline is not None:
            self.client.reconnect_before(deadline=deadline)
            return
        self.client.reconnect()

    def _write_all_outputs(
        self,
        expected: str,
        *,
        deadline: float | None = None,
    ) -> dict[str, bool]:
        for path in OUTPUT_MUTES.values():
            self._require_unmute_before(deadline)
            self._client_set(path, expected, deadline=deadline)
            self._require_unmute_before(deadline)
        readback = self._read_all_output_mutes(deadline=deadline)
        expected_muted = expected == "On"
        mismatches = [
            channel for channel, value in readback.items() if value != expected_muted
        ]
        if mismatches:
            raise OutputVerificationError(
                f"output readback did not confirm {expected!r}: {', '.join(mismatches)}"
            )
        return readback

    def _read_all_output_mutes(
        self,
        *,
        deadline: float | None = None,
    ) -> dict[str, bool]:
        readback: dict[str, bool] = {}
        for channel, path in OUTPUT_MUTES.items():
            self._require_unmute_before(deadline)
            readback[channel] = _parse_output_mute(
                channel,
                self._client_get(path, deadline=deadline),
            )
            self._require_unmute_before(deadline)
        return readback

    def _rollback_outputs_to_muted(
        self,
        channels: Iterable[str],
        *,
        deadline: float | None = None,
        reconnect_first: bool = False,
    ) -> None:
        requested = tuple(dict.fromkeys(channels))
        if any(channel not in OUTPUT_MUTES for channel in requested):
            raise ValueError("rollback included an unknown output channel")
        # A reconnect can allow an asynchronous recall to complete and alter a
        # channel that was already checked. Therefore every recovery round
        # writes all six outputs, and success requires one final all-six
        # readback with no intervening reconnect.
        ordered = tuple(OUTPUT_MUTES)
        errors: list[str] = []
        if reconnect_first:
            self._require_rollback_before(deadline, "initial recovery reconnect")
            try:
                self._client_reconnect(deadline=deadline)
            except Exception as error:
                errors.append(f"initial recovery reconnect: {error}")
            self._require_rollback_before(deadline, "starting recovery writes")
        for _ in range(len(ordered) + 2):
            self._require_rollback_before(deadline, "starting a recovery round")
            round_failed = False
            for channel in ordered:
                path = OUTPUT_MUTES[channel]
                self._require_rollback_before(deadline, f"writing {channel}")
                try:
                    self._client_set(path, "On", deadline=deadline)
                except Exception as error:
                    errors.append(f"{channel}: {error}")
                    self._require_rollback_before(deadline, "reconnecting")
                    try:
                        self._client_reconnect(deadline=deadline)
                    except Exception as reconnect_error:
                        errors.append(f"reconnect failed: {reconnect_error}")
                    self._require_rollback_before(deadline, "continuing after reconnect")
                    round_failed = True
                    continue
                self._require_rollback_before(deadline, f"reading {channel}")
                try:
                    actual = self._client_get(path, deadline=deadline)
                    if not _parse_output_mute(channel, actual):
                        raise OutputVerificationError(
                            f"rollback readback was {actual!r}, expected 'On'"
                        )
                except Exception as error:
                    errors.append(f"{channel}: {error}")
                    self._require_rollback_before(deadline, "reconnecting")
                    try:
                        self._client_reconnect(deadline=deadline)
                    except Exception as reconnect_error:
                        errors.append(f"reconnect failed: {reconnect_error}")
                    self._require_rollback_before(deadline, "continuing after reconnect")
                    round_failed = True
                    continue
                self._require_rollback_before(deadline, f"finishing {channel}")
            if round_failed:
                continue
            final: dict[str, bool] = {}
            try:
                for channel, path in OUTPUT_MUTES.items():
                    self._require_rollback_before(
                        deadline, f"final confirmation of {channel}"
                    )
                    final[channel] = _parse_output_mute(
                        channel,
                        self._client_get(path, deadline=deadline),
                    )
                    self._require_rollback_before(
                        deadline, f"finishing final confirmation of {channel}"
                    )
            except Exception as error:
                errors.append(f"final readback failed: {error}")
                self._require_rollback_before(deadline, "reconnecting")
                try:
                    self._client_reconnect(deadline=deadline)
                except Exception as reconnect_error:
                    errors.append(f"reconnect failed: {reconnect_error}")
                self._require_rollback_before(deadline, "continuing after reconnect")
                continue
            unsafe = [channel for channel, muted in final.items() if not muted]
            if not unsafe:
                return
            errors.append(f"final readback was unmuted: {', '.join(unsafe)}")

        raise OutputVerificationError(
            "rollback exhausted without verifying all outputs muted; output mute state "
            "is unsafe or unknown: "
            + "; ".join(errors)
        )

    def _require_rollback_before(self, deadline: float | None, action: str) -> None:
        if deadline is not None and self._monotonic() >= deadline:
            raise RollbackDeadlineError(
                f"rollback deadline expired before {action}; output mute state is unsafe or unknown"
            )

    def set_all_outputs_muted(
        self,
        muted: bool,
    ) -> dict[str, bool]:
        with self._lock:
            deadline = self._new_operation_deadline()
            return self._set_all_outputs_muted(muted, deadline=deadline)

    def _set_all_outputs_muted(
        self,
        muted: bool,
        *,
        deadline: float,
    ) -> dict[str, bool]:
        expected = "On" if muted else "Off"
        with self._lock:
            expected_preset = None
            if not muted:
                expected_preset = self._verify_current_preset_before_unmute(
                    deadline=deadline
                )
            try:
                readback = self._write_all_outputs(expected, deadline=deadline)
                if expected_preset is not None:
                    final_preset = self._verify_current_preset_before_unmute(
                        deadline=deadline
                    )
                    if final_preset.slot != expected_preset.slot:
                        raise OutputVerificationError(
                            "current preset changed during unmute"
                        )
                return readback
            except Exception as error:
                try:
                    self._rollback_outputs_to_muted(
                        OUTPUT_MUTES,
                        deadline=deadline,
                        reconnect_first=True,
                    )
                except Exception as rollback_error:
                    if isinstance(rollback_error, RollbackDeadlineError):
                        raise rollback_error from error
                    operation = "mute" if muted else "unmute"
                    raise OutputVerificationError(
                        f"{operation} failed ({error}); rollback to muted also failed "
                        f"({rollback_error})"
                    ) from error
                if muted or isinstance(error, RecallTimeout):
                    raise
                raise OutputVerificationError(
                    f"unmute failed ({error}); rolled back to muted"
                ) from error

    def output_levels(self) -> dict[str, float]:
        with self._lock:
            return {
                channel: _parse_db(channel, self.client.get(path))
                for channel, path in OUTPUT_LEVELS.items()
            }

    def input_meters(self) -> InputMeters:
        with self._lock:
            return InputMeters(
                levels_dbfs={
                    side: _parse_db(f"{side} input", self.client.get(path))
                    for side, path in INPUT_LEVELS.items()
                },
                clips={
                    side: _parse_binary_flag(
                        f"{side} input clip", self.client.get(path)
                    )
                    for side, path in INPUT_CLIPS.items()
                },
            )

    def crossover(self) -> CrossoverState:
        """Read topology and every reported HPF/LPF curve parameter."""

        with self._lock:
            attributes = self.client.ls(CROSSOVER_AT)
            values = self.client.ls(CROSSOVER_SV)
            if set(attributes) != {"NumBands", "MonoSub"}:
                raise TelemetryError(
                    "crossover topology keys were not exactly NumBands and MonoSub"
                )
            try:
                num_bands = _parse_unsigned_integer(
                    "crossover NumBands", attributes["NumBands"], minimum=1
                )
                mono_sub = _parse_binary_flag(
                    "crossover MonoSub", attributes["MonoSub"]
                )
            except (KeyError, ValueError) as error:
                raise TelemetryError(
                    "crossover topology was incomplete or invalid"
                ) from error
            if num_bands not in {1, 2, 3}:
                raise TelemetryError(
                    f"crossover returned unsupported NumBands value {num_bands}"
                )

            suffixes = {
                "HPFrequency",
                "HPType",
                "Gain",
                "LPFrequency",
                "LPType",
                "Polarity",
            }
            reported: dict[str, set[str]] = {}
            for key in values:
                suffix = next(
                    (
                        candidate
                        for candidate in suffixes
                        if key.endswith(f"_{candidate}")
                    ),
                    None,
                )
                if suffix is None:
                    raise TelemetryError(
                        f"crossover returned unexpected curve attribute {key!r}"
                    )
                identifier = key.removesuffix(f"_{suffix}")
                if not identifier:
                    raise TelemetryError(
                        f"crossover returned invalid curve attribute {key!r}"
                    )
                reported.setdefault(identifier, set()).add(suffix)

            expected_identifiers = {f"Band_{index}" for index in range(1, num_bands + 1)}
            if mono_sub:
                expected_identifiers.add("MonoSub")
            if set(reported) != expected_identifiers:
                raise TelemetryError(
                    "crossover band identifiers did not match topology: "
                    f"expected {sorted(expected_identifiers)}, got {sorted(reported)}"
                )
            for identifier, fields in reported.items():
                missing = suffixes - fields
                if missing:
                    raise TelemetryError(
                        f"crossover band {identifier} was missing {', '.join(sorted(missing))}"
                    )

            bands = [
                self._parse_crossover_band(
                    identifier,
                    values,
                    num_bands=num_bands,
                    mono_sub=mono_sub,
                )
                for identifier in reported
            ]
            label_order = {"High": 0, "Mid": 1, "Low": 2}
            bands.sort(
                key=lambda band: (label_order.get(band.label, 3), band.identifier)
            )
            return CrossoverState(num_bands, mono_sub, tuple(bands))

    def _parse_crossover_band(
        self,
        identifier: str,
        values: dict[str, str],
        *,
        num_bands: int,
        mono_sub: bool,
    ) -> CrossoverBand:
        def required(suffix: str) -> str:
            key = f"{identifier}_{suffix}"
            try:
                return values[key]
            except KeyError as error:
                raise TelemetryError(
                    f"crossover band {identifier} was missing {suffix}"
                ) from error

        return CrossoverBand(
            identifier=identifier,
            label=self._crossover_band_label(
                identifier,
                num_bands=num_bands,
                mono_sub=mono_sub,
            ),
            high_pass_hz=_parse_frequency(
                f"{identifier} high pass", required("HPFrequency")
            ),
            high_pass_type=_require_filter_type(
                f"{identifier} high pass", required("HPType")
            ),
            gain_db=_parse_db(f"{identifier} gain", required("Gain")),
            low_pass_hz=_parse_frequency(
                f"{identifier} low pass", required("LPFrequency")
            ),
            low_pass_type=_require_filter_type(
                f"{identifier} low pass", required("LPType")
            ),
            polarity=_require_polarity(identifier, required("Polarity")),
        )

    @staticmethod
    def _crossover_band_label(
        identifier: str,
        *,
        num_bands: int,
        mono_sub: bool,
    ) -> str:
        if identifier == "MonoSub":
            return "Low"
        if identifier == "Band_1":
            return "High"
        if identifier == "Band_2":
            return "Mid" if num_bands >= 3 or mono_sub else "Low"
        if identifier == "Band_3":
            return "Low"
        return identifier.replace("_", " ")

    def _preset_catalog(
        self,
        *,
        expected_current: int | None = None,
        deadline: float | None = None,
    ) -> dict[int, Preset]:
        self._require_unmute_before(deadline)
        entries = self._client_ls(PRESET_ROOT, deadline=deadline)
        self._require_unmute_before(deadline)
        reported_count = "NumPresets" in entries
        if reported_count:
            count = _parse_unsigned_integer(
                "preset NumPresets", entries["NumPresets"], minimum=1, maximum=100
            )
        else:
            name_slots: set[int] = set()
            for key in entries:
                if key in {"CurrentPreset", "NumPresets"}:
                    continue
                if not key.startswith("Name_"):
                    raise TelemetryError("preset catalog returned an unexpected key")
                suffix = key.removeprefix("Name_")
                if not _ASCII_UNSIGNED_INTEGER.fullmatch(suffix):
                    raise TelemetryError("preset catalog returned an invalid name key")
                slot = int(suffix, 10)
                if not 1 <= slot <= MAX_PRESET_SLOT:
                    raise TelemetryError(
                        f"preset catalog inferred a slot outside 1 through {MAX_PRESET_SLOT}"
                    )
                name_slots.add(slot)
            if not name_slots or name_slots != set(range(1, max(name_slots) + 1)):
                raise TelemetryError(
                    "preset catalog names were not a non-empty contiguous slot range"
                )
            count = max(name_slots)

        catalog_has_current = "CurrentPreset" in entries
        if catalog_has_current:
            current = _parse_unsigned_integer(
                "preset CurrentPreset", entries["CurrentPreset"], minimum=1
            )
        else:
            current = _parse_unsigned_integer(
                "preset CurrentPreset",
                self._client_get(CURRENT_PRESET, deadline=deadline),
                minimum=1,
            )
            self._require_unmute_before(deadline)

        expected = ({"NumPresets"} if reported_count else set()) | (
            {"CurrentPreset"} if catalog_has_current else set()
        ) | {
            f"Name_{slot}" for slot in range(1, count + 1)
        }
        if set(entries) != expected:
            raise TelemetryError(
                "preset catalog keys did not exactly match its reported slot range"
            )
        if current > count:
            count_source = "NumPresets" if reported_count else "inferred preset count"
            raise TelemetryError(
                f"preset CurrentPreset slot {current} exceeded {count_source} {count}"
            )
        if expected_current is not None and current != expected_current:
            raise TelemetryError(
                "conflicting CurrentPreset values between direct and catalog reads"
            )
        catalog: dict[int, Preset] = {}
        for slot in range(1, count + 1):
            name = entries[f"Name_{slot}"]
            if not name or any(ord(character) < 32 or ord(character) == 127 for character in name):
                raise TelemetryError(f"preset slot {slot} returned an invalid name")
            catalog[slot] = Preset(slot, name)
        self._require_unmute_before(deadline)
        return catalog

    def _resolve_preset(
        self,
        target: str | int,
        *,
        deadline: float | None = None,
    ) -> Preset:
        catalog = self._preset_catalog(deadline=deadline)
        if self.allowed_slots is None:
            presets = [preset for _, preset in sorted(catalog.items())]
        else:
            presets = [catalog[slot] for slot in self.allowed_slots if slot in catalog]
        by_slot = {preset.slot: preset for preset in presets}
        if isinstance(target, int) and not isinstance(target, bool):
            slot = target
            if slot < 1:
                raise ValueError(f"preset slot {slot} is not allowed")
        elif isinstance(target, str) and _ASCII_UNSIGNED_INTEGER.fullmatch(target):
            slot = int(target, 10)
        else:
            slot = None
        if slot is not None:
            if slot not in by_slot:
                raise ValueError(f"preset slot {slot} is not allowed")
            return by_slot[slot]

        text = str(target).strip()
        label_matches = [preset for preset in presets if preset.label == text]
        if len(label_matches) == 1:
            return label_matches[0]
        name_matches = [preset for preset in presets if preset.name == text]
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            raise ValueError(f"preset name {text!r} is ambiguous; use a slot or full label")
        raise ValueError(f"preset {text!r} is not allowed")
