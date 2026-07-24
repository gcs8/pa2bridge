from __future__ import annotations

from collections.abc import Iterable

import pytest

from pa2bridge.controller import (
    INPUT_CLIPS,
    INPUT_LEVELS,
    OUTPUT_LEVELS,
    OUTPUT_MUTES,
    OutputVerificationError,
    Pa2Controller,
    RecallTimeout,
    RollbackDeadlineError,
    TelemetryError,
)


PRESET_ROOT = ("Storage", "Presets", "SV")
CURRENT_PRESET = (*PRESET_ROOT, "CurrentPreset")
RECALL = (*PRESET_ROOT, "Recall")


class FakeClient:
    def __init__(self, *, current: int = 1, recall_changes: bool = True) -> None:
        self.current = current
        self.recall_changes = recall_changes
        self.sets: list[tuple[tuple[str, ...], str]] = []
        self.mutes = {path: "On" for path in OUTPUT_MUTES.values()}
        self.bad_verify_path: tuple[str, ...] | None = None
        self.reconnects = 0

    def get(self, path: Iterable[str]) -> str:
        path = tuple(path)
        if path == CURRENT_PRESET:
            return str(self.current)
        if path in self.mutes:
            if path == self.bad_verify_path:
                return "On"
            return self.mutes[path]
        values = {
            ("Node", "AT", "Class_Name"): "dbxDriveRackPA2",
            ("Node", "AT", "Instance_Name"): "DriveRackPA2",
            ("Node", "AT", "Software_Version"): "1.2.0.1",
        }
        return values[path]

    def set(self, path: Iterable[str], value: str) -> None:
        path = tuple(path)
        self.sets.append((path, value))
        if path == RECALL and self.recall_changes:
            self.current = int(value)
        if path in self.mutes:
            self.mutes[path] = value

    def ls(self, path: Iterable[str]) -> dict[str, str]:
        assert tuple(path) == PRESET_ROOT
        return {
            "NumPresets": "3",
            "CurrentPreset": str(self.current),
            "Name_1": "Flat",
            "Name_2": "Alternate",
            "Name_3": "factory",
        }

    def reconnect(self) -> None:
        self.reconnects += 1

    def get_before(self, path: Iterable[str], *, deadline: float) -> str:
        del deadline
        return self.get(path)

    def set_before(
        self,
        path: Iterable[str],
        value: str,
        *,
        deadline: float,
    ) -> None:
        del deadline
        self.set(path, value)

    def ls_before(
        self,
        path: Iterable[str],
        *,
        deadline: float,
    ) -> dict[str, str]:
        del deadline
        return self.ls(path)

    def reconnect_before(self, *, deadline: float) -> None:
        del deadline
        self.reconnect()


class RecallDisconnectClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(current=1)
        self.pending: int | None = None
        self.reconnects = 0

    def set(self, path: Iterable[str], value: str) -> None:
        path = tuple(path)
        self.sets.append((path, value))
        if path == RECALL:
            self.pending = int(value)
            return
        if path in self.mutes:
            self.mutes[path] = value

    def get(self, path: Iterable[str]) -> str:
        path = tuple(path)
        if path == CURRENT_PRESET and self.pending is not None:
            raise ConnectionError("PA2 closed the console while loading")
        return super().get(path)

    def reconnect(self) -> None:
        self.reconnects += 1
        assert self.pending is not None
        self.current = self.pending
        self.pending = None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        self.now += 0.1
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_list_presets_is_bounded_to_allowlisted_slots() -> None:
    controller = Pa2Controller(FakeClient(), allowed_slots=(1, 2))

    presets = controller.list_presets()

    assert [(preset.slot, preset.name, preset.label) for preset in presets] == [
        (1, "Flat", "1: Flat"),
        (2, "Alternate", "2: Alternate"),
    ]


def test_auto_preset_mode_uses_every_device_reported_preset() -> None:
    controller = Pa2Controller(FakeClient(), allowed_slots=None)

    presets = controller.list_presets()

    assert [preset.slot for preset in presets] == [1, 2, 3]


def test_auto_preset_mode_can_activate_a_discovered_slot() -> None:
    client = FakeClient(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=None,
        post_recall_delay=0,
    )

    state = controller.activate_preset(3, unmute_after=False)

    assert state.current_preset.slot == 3
    assert (RECALL, "3") in client.sets


def test_auto_preset_mode_never_unmutes_if_target_disappears_from_catalog() -> None:
    class DisappearingPresetClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.catalog_reads = 0

        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            self.catalog_reads += 1
            if self.catalog_reads >= 2:
                catalog.pop("Name_3")
            return catalog

    client = DisappearingPresetClient()
    controller = Pa2Controller(
        client,
        allowed_slots=None,
        post_recall_delay=0,
    )

    with pytest.raises(TelemetryError, match="catalog"):
        controller.activate_preset(3)

    assert not any(value == "Off" for _, value in client.sets)
    assert all(value == "On" for value in client.mutes.values())


def test_controller_accepts_explicit_slots_across_the_pa2_range() -> None:
    controller = Pa2Controller(FakeClient(), allowed_slots=(1, 32, 75, 100))

    assert controller.allowed_slots == (1, 32, 75, 100)


@pytest.mark.parametrize("allowed", [(0,), (101,), (1, 1), (True,)])
def test_controller_rejects_control_slots_outside_unique_pa2_range(
    allowed: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="slots 1 through 100"):
        Pa2Controller(FakeClient(), allowed_slots=allowed)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("recall_timeout", float("nan")),
        ("recall_timeout", float("inf")),
        ("recall_timeout", 0.0),
        ("recall_timeout", 20.1),
        ("poll_interval", float("nan")),
        ("poll_interval", 0.0),
        ("post_recall_delay", float("inf")),
        ("post_recall_delay", -0.1),
    ],
)
def test_controller_rejects_unsafe_timing_values(setting: str, value: float) -> None:
    arguments = {setting: value}
    with pytest.raises(ValueError, match=setting):
        Pa2Controller(FakeClient(), allowed_slots=(1, 2), **arguments)


def test_activate_preset_waits_for_recall_then_unmutes_and_verifies_every_output() -> None:
    client = FakeClient(current=1)
    clock = Clock()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=20,
        poll_interval=0.05,
        post_recall_delay=0.75,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    state = controller.activate_preset("2: Alternate", unmute_after=True)

    assert client.sets[:6] == [(path, "On") for path in OUTPUT_MUTES.values()]
    assert client.sets[6] == (RECALL, "2")
    assert client.sets[7:] == [(path, "Off") for path in OUTPUT_MUTES.values()]
    assert state.current_preset.slot == 2
    assert state.all_outputs_unmuted is True
    assert 0.75 in clock.sleeps


def test_preset_is_rechecked_after_post_recall_delay_before_unmute() -> None:
    client = FakeClient(current=1)

    def sleep(seconds: float) -> None:
        if seconds == 0.75:
            client.current = 1

    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        post_recall_delay=0.75,
        sleep=sleep,
    )

    with pytest.raises(RecallTimeout, match="no longer active"):
        controller.activate_preset(2)

    assert client.sets == [
        *((path, "On") for path in OUTPUT_MUTES.values()),
        (RECALL, "2"),
        *((path, "On") for path in OUTPUT_MUTES.values()),
    ]
    assert all(value == "On" for value in client.mutes.values())


def test_recall_deadline_expiring_during_post_delay_never_unmutes() -> None:
    class RecallChangesMuteClient(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            super().set(path, value)
            if tuple(path) == RECALL:
                self.mutes[OUTPUT_MUTES["low_left"]] = "Off"

    client = RecallChangesMuteClient(current=1)

    class DelayClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = DelayClock()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=2.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(OutputVerificationError, match="immediate post-recall"):
        controller.activate_preset(2)

    assert not any(value == "Off" for _, value in client.sets)
    assert all(value == "On" for value in client.mutes.values())


def test_recall_deadline_stops_without_extending_for_rollback() -> None:
    class DeadlineClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = DeadlineClock()

    class TimedUnmuteClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.unmute_starts: list[float] = []

        def set(self, path: Iterable[str], value: str) -> None:
            if tuple(path) in OUTPUT_MUTES.values() and value == "Off":
                self.unmute_starts.append(clock.now)
                clock.now += 0.25
            super().set(path, value)

    client = TimedUnmuteClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0.9,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(OutputVerificationError, match="rollback deadline expired"):
        controller.activate_preset(2)

    assert client.unmute_starts == [0.9]
    assert client.mutes[OUTPUT_MUTES["high_left"]] == "Off"
    assert client.sets.count((OUTPUT_MUTES["high_left"], "On")) == 1


def test_recall_write_cannot_extend_absolute_recall_deadline() -> None:
    class DeadlineClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = DeadlineClock()

    class SlowRecallClient(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            if tuple(path) == RECALL:
                clock.now += 2.0
            super().set(path, value)

    client = SlowRecallClient(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0,
        sleep=lambda _: None,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RecallTimeout):
        controller.activate_preset(2)

    assert not any(value == "Off" for _, value in client.sets)
    assert all(value == "On" for value in client.mutes.values())


def test_recall_deadline_forbids_rollback_operations_after_expiry() -> None:
    class DeadlineClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = DeadlineClock()

    class LatePostRecallReadClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.timed_sets: list[tuple[float, tuple[str, ...], str]] = []
            self.expire_on_mute_read = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            self.timed_sets.append((clock.now, key, value))
            super().set(key, value)
            if key == RECALL:
                self.expire_on_mute_read = True

        def get(self, path: Iterable[str]) -> str:
            key = tuple(path)
            if self.expire_on_mute_read and key in OUTPUT_MUTES.values():
                self.expire_on_mute_read = False
                clock.now = 2.0
            return super().get(key)

    client = LatePostRecallReadClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0,
        sleep=lambda _: None,
        monotonic=clock.monotonic,
    )

    with pytest.raises(OutputVerificationError, match="rollback deadline expired"):
        controller.activate_preset(2)

    assert not [event for event in client.timed_sets if event[0] >= 1.0]


def test_already_active_target_still_uses_absolute_activation_deadline() -> None:
    class DeadlineClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = DeadlineClock()

    class SlowFinalPresetReadClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=2)
            self.current_reads = 0

        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET:
                self.current_reads += 1
                if self.current_reads == 2:
                    clock.now = 2.0
            return super().get(path)

    client = SlowFinalPresetReadClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(OutputVerificationError, match="rollback deadline expired"):
        controller.activate_preset(2)

    assert not any(value == "Off" for _, value in client.sets)


def test_post_recall_output_reread_must_confirm_all_six_muted_before_unmute() -> None:
    class RecallChangesMuteClient(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            super().set(path, value)
            if tuple(path) == RECALL:
                self.mutes[OUTPUT_MUTES["low_left"]] = "Off"

    client = RecallChangesMuteClient(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        post_recall_delay=0,
    )

    with pytest.raises(OutputVerificationError, match="post-recall"):
        controller.activate_preset(2)

    assert all(value == "On" for value in client.mutes.values())
    assert not any(value == "Off" for _, value in client.sets)


def test_activate_reconnects_when_recall_closes_the_pa2_console() -> None:
    client = RecallDisconnectClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        sleep=lambda _: None,
        post_recall_delay=0,
    )

    state = controller.activate_preset(2)

    assert client.reconnects == 1
    assert state.current_preset.slot == 2
    assert state.all_outputs_unmuted is True


def test_recall_timeout_never_unmutes_outputs() -> None:
    client = FakeClient(current=1, recall_changes=False)
    clock = Clock()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=0.25,
        poll_interval=0.05,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RecallTimeout):
        controller.activate_preset(2, unmute_after=True)

    assert not any(value == "Off" for _, value in client.sets)
    assert all(client.mutes[path] == "On" for path in OUTPUT_MUTES.values())


def test_confirmation_after_deadline_never_unmutes_outputs() -> None:
    clock = Clock()

    class LateConfirmClient(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            self.sets.append((key, value))
            if key == RECALL:
                self.current = int(value)

        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET and self.current == 2:
                clock.now = 11.0
            return super().get(path)

    client = LateConfirmClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=10,
        poll_interval=0.01,
        post_recall_delay=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RecallTimeout):
        controller.activate_preset(2)

    assert not any(
        path in OUTPUT_MUTES.values() and value == "Off"
        for path, value in client.sets
    )
    assert all(value == "On" for value in client.mutes.values())


def test_recall_stops_immediately_when_reconnect_crosses_deadline() -> None:
    class DeadlineClock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    clock = DeadlineClock()

    class DeadlineReconnectClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.recall_reads = 0
            self.recall_started = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if key == RECALL:
                self.sets.append((key, value))
                self.recall_started = True
                return
            super().set(key, value)

        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET and self.recall_started:
                self.recall_reads += 1
                raise ConnectionError("recall disconnected")
            return super().get(path)

        def reconnect(self) -> None:
            self.reconnects += 1
            clock.now = 2.0

    client = DeadlineReconnectClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        poll_interval=0.25,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RecallTimeout):
        controller.activate_preset(2)

    assert client.recall_reads == 1
    assert clock.sleeps == []
    assert all(value == "On" for value in client.mutes.values())


def test_recall_is_never_sent_when_pre_recall_mute_cannot_be_verified() -> None:
    failed_path = OUTPUT_MUTES["mid_right"]

    class RefusesMuteClient(FakeClient):
        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == failed_path:
                return "Off"
            return super().get(path)

    client = RefusesMuteClient(current=1)
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="mid_right"):
        controller.activate_preset(2)

    assert not any(path == RECALL for path, _ in client.sets)
    assert not any(value == "Off" for _, value in client.sets)


def test_unmute_raises_when_readback_does_not_confirm_every_output() -> None:
    client = FakeClient()
    client.bad_verify_path = OUTPUT_MUTES["low_right"]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="low_right"):
        controller.set_all_outputs_muted(False)


@pytest.mark.parametrize("value", ["Unknown", "", "off", "ON"])
def test_state_rejects_unknown_mute_readback_instead_of_reporting_unmuted(
    value: str,
) -> None:
    client = FakeClient()
    client.mutes[OUTPUT_MUTES["high_left"]] = value
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="high_left.*unknown mute state"):
        controller.state()


def test_failed_unmute_rolls_back_every_output_to_muted() -> None:
    client = FakeClient()
    client.bad_verify_path = OUTPUT_MUTES["low_right"]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="rolled back to muted"):
        controller.set_all_outputs_muted(False)

    assert all(value == "On" for value in client.mutes.values())
    assert client.sets[-6:] == [(path, "On") for path in OUTPUT_MUTES.values()]


@pytest.mark.parametrize("single_channel", [False, True])
def test_public_unmute_requires_direct_and_catalog_preset_agreement(
    single_channel: bool,
) -> None:
    class ConflictingCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            entries = super().ls(path)
            entries["CurrentPreset"] = "2" if self.current == 1 else "1"
            return entries

    client = ConflictingCatalogClient(current=1)
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="conflicting CurrentPreset"):
        if single_channel:
            controller.set_output_muted("high_left", False)
        else:
            controller.set_all_outputs_muted(False)

    assert not any(value == "Off" for _, value in client.sets)
    assert all(value == "On" for value in client.mutes.values())


@pytest.mark.parametrize("single_channel", [False, True])
def test_public_unmute_rechecks_same_exact_preset_after_write(
    single_channel: bool,
) -> None:
    class PresetChangesOnUnmuteClient(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            super().set(path, value)
            if tuple(path) in OUTPUT_MUTES.values() and value == "Off":
                self.current = 2

    client = PresetChangesOnUnmuteClient(current=1)
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="changed during unmute"):
        if single_channel:
            controller.set_output_muted("high_left", False)
        else:
            controller.set_all_outputs_muted(False)

    assert all(value == "On" for value in client.mutes.values())


def test_public_mute_and_rollback_share_one_absolute_deadline() -> None:
    class AdvancingClient(FakeClient):
        def __init__(self, clock: Clock) -> None:
            super().__init__()
            self.clock = clock
            self.operation_starts: list[float] = []

        def set(self, path: Iterable[str], value: str) -> None:
            self.operation_starts.append(self.clock.now)
            self.clock.now += 0.6
            super().set(path, value)

        def get(self, path: Iterable[str]) -> str:
            self.operation_starts.append(self.clock.now)
            self.clock.now += 0.6
            return super().get(path)

        def reconnect(self) -> None:
            self.operation_starts.append(self.clock.now)
            self.clock.now += 0.6
            super().reconnect()

    clock = Clock()
    client = AdvancingClient(clock)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RollbackDeadlineError, match="unsafe or unknown"):
        controller.set_all_outputs_muted(True)

    assert client.operation_starts
    assert all(start < 1.1 for start in client.operation_starts)


@pytest.mark.parametrize("single_channel", [False, True])
def test_public_unmute_rejects_catalog_parsed_after_deadline(
    single_channel: bool,
) -> None:
    clock = Clock()
    clock.monotonic = lambda: clock.now

    class SlowCatalog(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            if key == "NumPresets":
                clock.now = 2.0
            return super().__getitem__(key)

    class SlowFinalCatalogClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.catalog_reads = 0

        def ls(self, path: Iterable[str]) -> dict[str, str]:
            self.catalog_reads += 1
            entries = super().ls(path)
            if self.catalog_reads == 2:
                return SlowCatalog(entries)
            return entries

    client = SlowFinalCatalogClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RollbackDeadlineError, match="unsafe or unknown"):
        if single_channel:
            controller.set_output_muted("high_left", False)
        else:
            controller.set_all_outputs_muted(False)


def test_public_unmute_propagates_one_deadline_to_protocol_operations() -> None:
    clock = Clock()
    clock.monotonic = lambda: clock.now

    class DeadlineAwareClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.deadlines: list[tuple[str, float]] = []

        def get_before(self, path: Iterable[str], *, deadline: float) -> str:
            self.deadlines.append(("get", deadline))
            return super().get(path)

        def set_before(
            self,
            path: Iterable[str],
            value: str,
            *,
            deadline: float,
        ) -> None:
            self.deadlines.append(("set", deadline))
            super().set(path, value)

        def ls_before(
            self,
            path: Iterable[str],
            *,
            deadline: float,
        ) -> dict[str, str]:
            self.deadlines.append(("ls", deadline))
            return super().ls(path)

    client = DeadlineAwareClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        monotonic=clock.monotonic,
    )

    controller.set_all_outputs_muted(False)

    assert {kind for kind, _ in client.deadlines} == {"get", "set", "ls"}
    assert {deadline for _, deadline in client.deadlines} == {1.0}


def test_activation_rejects_catalog_parsed_after_recall_deadline() -> None:
    clock = Clock()
    clock.monotonic = lambda: clock.now

    class SlowCatalog(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            if key == "NumPresets":
                clock.now = 2.0
            return super().__getitem__(key)

    class SlowActivationCatalogClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.catalog_reads = 0

        def ls(self, path: Iterable[str]) -> dict[str, str]:
            self.catalog_reads += 1
            entries = super().ls(path)
            if self.catalog_reads == 2:
                return SlowCatalog(entries)
            return entries

    client = SlowActivationCatalogClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0.0,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RollbackDeadlineError, match="unsafe or unknown"):
        controller.activate_preset(1)

    assert all(value == "On" for value in client.mutes.values())
    assert not any(value == "Off" for _, value in client.sets)


def test_activation_deadline_starts_before_catalog_resolution_and_preflight_mute() -> None:
    clock = Clock()
    clock.monotonic = lambda: clock.now

    class SlowInitialCatalog(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            if key == "NumPresets":
                clock.now = 2.0
            return super().__getitem__(key)

    class SlowInitialCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            return SlowInitialCatalog(super().ls(path))

    client = SlowInitialCatalogClient(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0.0,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RecallTimeout):
        controller.activate_preset(1)

    assert client.sets == []


def test_activation_propagates_one_deadline_through_the_entire_transaction() -> None:
    clock = Clock()
    clock.monotonic = lambda: clock.now

    class DeadlineAwareActivationClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.deadlines: list[tuple[str, float]] = []

        def get_before(self, path: Iterable[str], *, deadline: float) -> str:
            self.deadlines.append(("get", deadline))
            return self.get(path)

        def set_before(
            self,
            path: Iterable[str],
            value: str,
            *,
            deadline: float,
        ) -> None:
            self.deadlines.append(("set", deadline))
            self.set(path, value)

        def ls_before(
            self,
            path: Iterable[str],
            *,
            deadline: float,
        ) -> dict[str, str]:
            self.deadlines.append(("ls", deadline))
            return self.ls(path)

    client = DeadlineAwareActivationClient()
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        recall_timeout=1.0,
        post_recall_delay=0.0,
        monotonic=clock.monotonic,
    )

    controller.activate_preset(1)

    assert {kind for kind, _ in client.deadlines} == {"get", "set", "ls"}
    assert {deadline for _, deadline in client.deadlines} == {1.0}


def test_exhausted_rollback_reports_output_state_unsafe_or_unknown() -> None:
    path = OUTPUT_MUTES["high_left"]

    class PermanentlyUnsafeRollbackClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.off_written = False
            self.failed_readback = False

        def set(self, requested_path: Iterable[str], value: str) -> None:
            key = tuple(requested_path)
            if key == path and value == "On" and self.off_written:
                raise ConnectionError("permanent rollback write failure")
            super().set(key, value)
            if key == path and value == "Off":
                self.off_written = True

        def get(self, requested_path: Iterable[str]) -> str:
            key = tuple(requested_path)
            if key == path and self.off_written and not self.failed_readback:
                self.failed_readback = True
                raise ConnectionError("ambiguous unmute readback")
            return super().get(key)

    client = PermanentlyUnsafeRollbackClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="unsafe or unknown"):
        controller.set_output_muted("high_left", False)

    assert client.mutes[path] == "Off"


def test_failed_public_bulk_mute_performs_reconnect_aware_all_six_rollback() -> None:
    failed_path = OUTPUT_MUTES["high_right"]

    class PartialMuteFailureClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.mutes = {path: "Off" for path in OUTPUT_MUTES.values()}
            self.failed = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if key == failed_path and value == "On" and not self.failed:
                self.failed = True
                raise ConnectionError("mute write failed")
            super().set(key, value)

    client = PartialMuteFailureClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(ConnectionError, match="mute write failed"):
        controller.set_all_outputs_muted(True)

    assert client.reconnects >= 1
    assert all(value == "On" for value in client.mutes.values())


def test_unknown_or_disallowed_preset_is_rejected_without_device_write() -> None:
    client = FakeClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(ValueError, match="not allowed"):
        controller.activate_preset(3)

    assert client.sets == []


def test_single_output_control_is_verified_and_unknown_channel_is_rejected() -> None:
    client = FakeClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    assert controller.set_output_muted("high_left", True) is True
    assert client.sets == [(OUTPUT_MUTES["high_left"], "On")]
    with pytest.raises(ValueError, match="unknown output channel"):
        controller.set_output_muted("bogus", False)


def test_failed_single_output_unmute_rolls_that_output_back_to_muted() -> None:
    client = FakeClient()
    client.bad_verify_path = OUTPUT_MUTES["high_left"]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="rolled back to muted"):
        controller.set_output_muted("high_left", False)

    assert all(value == "On" for value in client.mutes.values())
    assert client.sets[0] == (OUTPUT_MUTES["high_left"], "Off")
    assert set(path for path, value in client.sets[1:] if value == "On") == set(
        OUTPUT_MUTES.values()
    )


def test_single_output_rollback_reconnects_after_ambiguous_transport_failure() -> None:
    class AmbiguousDisconnectClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.connected = True
            self.failed = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if not self.connected:
                raise ConnectionError("not connected")
            super().set(key, value)
            if key == OUTPUT_MUTES["high_left"] and value == "Off" and not self.failed:
                self.failed = True
                self.connected = False
                raise ConnectionError("connection dropped after ambiguous write")

        def get(self, path: Iterable[str]) -> str:
            if not self.connected:
                raise ConnectionError("not connected")
            return super().get(path)

        def reconnect(self) -> None:
            super().reconnect()
            self.connected = True

    client = AmbiguousDisconnectClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="rolled back to muted"):
        controller.set_output_muted("high_left", False)

    assert client.reconnects >= 1
    assert client.mutes[OUTPUT_MUTES["high_left"]] == "On"


def test_bulk_rollback_reconnects_and_remutes_after_partial_unmute_disconnect() -> None:
    failed_path = OUTPUT_MUTES["mid_left"]

    class PartialUnmuteDisconnectClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.connected = True
            self.failed = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if not self.connected:
                raise ConnectionError("not connected")
            super().set(key, value)
            if key == failed_path and value == "Off" and not self.failed:
                self.failed = True
                self.connected = False
                raise ConnectionError("connection dropped after partial unmute")

        def get(self, path: Iterable[str]) -> str:
            if not self.connected:
                raise ConnectionError("not connected")
            return super().get(path)

        def reconnect(self) -> None:
            super().reconnect()
            self.connected = True

    client = PartialUnmuteDisconnectClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="rolled back to muted"):
        controller.set_all_outputs_muted(False)

    assert client.reconnects >= 1
    assert all(value == "On" for value in client.mutes.values())


def test_bulk_rollback_attempts_later_channels_after_first_channel_failure() -> None:
    first_path = OUTPUT_MUTES["high_left"]

    class FirstRollbackChannelFails(FakeClient):
        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if key == first_path and value == "On" and any(
                current == "Off" for current in self.mutes.values()
            ):
                self.sets.append((key, value))
                raise ConnectionError("high_left mute write failed")
            super().set(key, value)

    client = FirstRollbackChannelFails()
    client.bad_verify_path = OUTPUT_MUTES["low_right"]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="high_left"):
        controller.set_all_outputs_muted(False)

    assert client.mutes[first_path] == "Off"
    assert all(
        value == "On"
        for path, value in client.mutes.items()
        if path != first_path
    )


def test_bulk_rollback_revisits_earlier_channel_after_later_reconnect() -> None:
    first_path = OUTPUT_MUTES["high_left"]

    class LaterReconnectRecoversSession(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.connected = True
            self.first_rollback_failed = False

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if not self.connected:
                raise ConnectionError("not connected")
            if key == first_path and value == "On" and not self.first_rollback_failed:
                self.first_rollback_failed = True
                self.connected = False
                raise ConnectionError("first rollback write disconnected")
            super().set(key, value)

        def get(self, path: Iterable[str]) -> str:
            if not self.connected:
                raise ConnectionError("not connected")
            return super().get(path)

        def reconnect(self) -> None:
            self.reconnects += 1
            if self.reconnects == 1:
                raise ConnectionError("transient reconnect failure")
            self.connected = True

    client = LaterReconnectRecoversSession()
    client.bad_verify_path = OUTPUT_MUTES["low_right"]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(OutputVerificationError, match="rolled back to muted"):
        controller.set_all_outputs_muted(False)

    assert client.reconnects >= 2
    assert all(value == "On" for value in client.mutes.values())


def test_bulk_rollback_rechecks_all_channels_after_a_later_reconnect() -> None:
    high_left = OUTPUT_MUTES["high_left"]
    mid_left = OUTPUT_MUTES["mid_left"]

    class RecallCompletesDuringRollbackReconnect(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.mutes = {path: "Off" for path in OUTPUT_MUTES.values()}
            self.mid_left_failures = 2

        def set(self, path: Iterable[str], value: str) -> None:
            key = tuple(path)
            if key == mid_left and value == "On" and self.mid_left_failures:
                self.mid_left_failures -= 1
                raise ConnectionError("rollback write disconnected")
            super().set(key, value)

        def reconnect(self) -> None:
            self.reconnects += 1
            if self.reconnects == 1:
                raise ConnectionError("transient reconnect failure")
            if self.reconnects == 2:
                self.mutes[high_left] = "Off"

    client = RecallCompletesDuringRollbackReconnect()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    controller._rollback_outputs_to_muted(OUTPUT_MUTES)

    assert client.reconnects == 2
    assert all(value == "On" for value in client.mutes.values())


def test_output_levels_parse_observed_db_values() -> None:
    class MeterClient(FakeClient):
        def get(self, path: Iterable[str]) -> str:
            path = tuple(path)
            if path in OUTPUT_LEVELS.values():
                return "-42.5dB"
            return super().get(path)

    controller = Pa2Controller(MeterClient(), allowed_slots=(1, 2))

    assert controller.output_levels() == {channel: -42.5 for channel in OUTPUT_LEVELS}


@pytest.mark.parametrize("value", ["01dB", "1.dB", ".5dB", "-01.5dB"])
def test_output_levels_reject_noncanonical_decimal_tokens(value: str) -> None:
    class PaddedMeterClient(FakeClient):
        def get(self, path: Iterable[str]) -> str:
            path = tuple(path)
            if path in OUTPUT_LEVELS.values():
                return value
            return super().get(path)

    controller = Pa2Controller(PaddedMeterClient(), allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="invalid decibel"):
        controller.output_levels()


def test_current_preset_rejects_conflicting_direct_and_catalog_values() -> None:
    client = FakeClient(current=2)

    def stale_catalog(path: Iterable[str]) -> dict[str, str]:
        assert tuple(path) == PRESET_ROOT
        return {
            "NumPresets": "3",
            "CurrentPreset": "1",
            "Name_1": "Flat",
            "Name_2": "Alternate",
            "Name_3": "Factory",
        }

    client.ls = stale_catalog  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="conflicting CurrentPreset"):
        controller.current_preset()


def test_preset_catalog_accepts_contiguous_names_when_metadata_is_omitted() -> None:
    class NamesOnlyCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog.pop("NumPresets")
            catalog.pop("CurrentPreset")
            return catalog

    controller = Pa2Controller(NamesOnlyCatalogClient(), allowed_slots=None)

    assert [preset.slot for preset in controller.list_all_presets()] == [1, 2, 3]


def test_preset_catalog_accepts_observed_pa2_auxiliary_keys() -> None:
    class ObservedCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            assert tuple(path) == PRESET_ROOT
            return {
                "CurrentPreset": "1",
                **{f"Name_{slot}": f"Preset {slot}" for slot in range(1, 101)},
                **{
                    key: "ignored"
                    for key in {
                        "Bypass",
                        "Changed",
                        "Enable",
                        "Recall",
                        "ReloadPreset",
                        "RenamePreset",
                        "Store",
                        "StoreCount",
                    }
                },
            }

    controller = Pa2Controller(ObservedCatalogClient(), allowed_slots=None)

    presets = controller.list_all_presets()

    assert len(presets) == 100
    assert presets[0].slot == 1
    assert presets[-1].slot == 100


def test_preset_catalog_rejects_unobserved_auxiliary_key() -> None:
    class UnknownCatalogKeyClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog["UnobservedField"] = "ignored"
            return catalog

    controller = Pa2Controller(UnknownCatalogKeyClient(), allowed_slots=None)

    with pytest.raises(TelemetryError, match="UnobservedField"):
        controller.list_all_presets()


def test_current_preset_brackets_catalog_without_embedded_current_read() -> None:
    class CatalogWithoutCurrentClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=2)
            self.current_reads = 0

        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET:
                self.current_reads += 1
            return super().get(path)

        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog.pop("CurrentPreset")
            return catalog

    client = CatalogWithoutCurrentClient()
    controller = Pa2Controller(client, allowed_slots=None)

    assert controller.current_preset().slot == 2
    assert client.current_reads == 2


def test_current_preset_fails_closed_if_direct_value_changes_around_catalog() -> None:
    class ChangingCurrentClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(current=1)
            self.current_reads = 0

        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET:
                self.current_reads += 1
                if self.current_reads == 2:
                    self.current = 2
            return super().get(path)

        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog.pop("CurrentPreset")
            return catalog

    controller = Pa2Controller(ChangingCurrentClient(), allowed_slots=None)

    with pytest.raises(TelemetryError, match="conflicting CurrentPreset"):
        controller.current_preset()


def test_inferred_preset_catalog_rejects_noncontiguous_name_slots() -> None:
    class GappedCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog.pop("NumPresets")
            catalog.pop("Name_2")
            return catalog

    controller = Pa2Controller(GappedCatalogClient(), allowed_slots=None)

    with pytest.raises(TelemetryError, match="contiguous"):
        controller.list_all_presets()


def test_activation_rejects_direct_catalog_conflict_before_any_unmute() -> None:
    class StaleCatalogClient(FakeClient):
        def ls(self, path: Iterable[str]) -> dict[str, str]:
            catalog = super().ls(path)
            catalog["CurrentPreset"] = "1"
            return catalog

    client = StaleCatalogClient(current=1)
    controller = Pa2Controller(client, allowed_slots=(1, 2), post_recall_delay=0)

    with pytest.raises(TelemetryError, match="conflicting CurrentPreset"):
        controller.activate_preset("2: Alternate", unmute_after=True)

    assert not any(
        path in OUTPUT_MUTES.values() and value == "Off"
        for path, value in client.sets
    )
    assert all(value == "On" for value in client.mutes.values())


def test_activation_rechecks_exact_target_after_unmute_and_rolls_back() -> None:
    class PresetChangesDuringUnmuteClient(FakeClient):
        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET and all(
                value == "Off" for value in self.mutes.values()
            ):
                self.current = 1
            return super().get(path)

    client = PresetChangesDuringUnmuteClient(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        post_recall_delay=0,
    )

    with pytest.raises(OutputVerificationError, match="changed during unmute"):
        controller.activate_preset(2)

    assert any(value == "Off" for _, value in client.sets)
    assert all(value == "On" for value in client.mutes.values())


def test_preset_inventory_rejects_more_than_one_hundred_slots() -> None:
    client = FakeClient()

    def oversized_catalog(path: Iterable[str]) -> dict[str, str]:
        assert tuple(path) == PRESET_ROOT
        return {
            "NumPresets": "101",
            "CurrentPreset": "1",
            **{f"Name_{slot}": f"Preset {slot}" for slot in range(1, 102)},
        }

    client.ls = oversized_catalog  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="NumPresets"):
        controller.list_all_presets()


def test_current_preset_fails_closed_if_slot_has_no_catalog_entry() -> None:
    client = FakeClient(current=4)
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="exceeded NumPresets"):
        controller.current_preset()


def test_duplicate_preset_name_requires_slot_or_full_label() -> None:
    client = FakeClient()

    def duplicate_names(path: Iterable[str]) -> dict[str, str]:
        assert tuple(path) == PRESET_ROOT
        return {
            "NumPresets": "2",
            "CurrentPreset": "1",
            "Name_1": "same",
            "Name_2": "same",
        }

    client.ls = duplicate_names  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(ValueError, match="ambiguous"):
        controller.activate_preset("same")
    with pytest.raises(ValueError, match="not allowed"):
        controller.activate_preset("missing")


class TelemetryClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.values = {
            INPUT_LEVELS["left"]: "-12.4dB",
            INPUT_LEVELS["right"]: "-13.5dB",
            INPUT_CLIPS["left"]: "0",
            INPUT_CLIPS["right"]: "1",
            **{path: "-24.5dB" for path in OUTPUT_LEVELS.values()},
        }
        self.crossover_sv = {
            "Band_1_HPFrequency": "Out",
            "Band_1_HPType": "LR 12",
            "Band_1_Gain": "0.0dB",
            "Band_1_LPFrequency": "Out",
            "Band_1_LPType": "LR 48",
            "Band_1_Polarity": "Normal",
            "MonoSub_HPFrequency": "35.5Hz",
            "MonoSub_HPType": "BW 6",
            "MonoSub_Gain": "-1.5dB",
            "MonoSub_LPFrequency": "0.08kHz",
            "MonoSub_LPType": "LR 12",
            "MonoSub_Polarity": "Inverted",
        }

    def get(self, path: Iterable[str]) -> str:
        key = tuple(path)
        if key in self.values:
            return self.values[key]
        return super().get(key)

    def ls(self, path: Iterable[str]) -> dict[str, str]:
        key = tuple(path)
        if key == ("Preset", "Crossover", "AT"):
            return {"NumBands": "1", "MonoSub": "1"}
        if key == ("Preset", "Crossover", "SV"):
            return dict(self.crossover_sv)
        return super().ls(key)


def test_read_only_telemetry_exposes_full_inventory_input_and_output_meters() -> None:
    client = TelemetryClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    assert [preset.slot for preset in controller.list_all_presets()] == [1, 2, 3]
    assert controller.input_meters().levels_dbfs == {"left": -12.4, "right": -13.5}
    assert controller.input_meters().clips == {"left": False, "right": True}
    assert controller.output_levels() == {
        channel: -24.5 for channel in OUTPUT_LEVELS
    }
    assert client.sets == []


def test_crossover_telemetry_contains_curve_parameters_for_every_reported_band() -> None:
    client = TelemetryClient()
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    crossover = controller.crossover()

    assert crossover.num_bands == 1
    assert crossover.mono_sub is True
    assert [(band.identifier, band.label) for band in crossover.bands] == [
        ("Band_1", "High"),
        ("MonoSub", "Low"),
    ]
    high, low = crossover.bands
    assert high.high_pass_hz is None
    assert high.low_pass_hz is None
    assert high.high_pass_type == "LR 12"
    assert low.high_pass_hz == 35.5
    assert low.low_pass_hz == 80.0
    assert low.gain_db == -1.5
    assert low.polarity == "Inverted"
    assert client.sets == []


def test_crossover_accepts_observed_pa2_topology_auxiliary_keys() -> None:
    client = TelemetryClient()
    original_ls = client.ls

    def observed_ls(path: Iterable[str]) -> dict[str, str]:
        attributes = original_ls(path)
        if tuple(path) == ("Preset", "Crossover", "AT"):
            attributes.update(
                {
                    "Class_Name": "ignored",
                    "Flags": "ignored",
                    "Instance_Name": "ignored",
                    "NumSlots": "ignored",
                }
            )
        return attributes

    client.ls = observed_ls  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    crossover = controller.crossover()

    assert crossover.num_bands == 1
    assert crossover.mono_sub is True
    assert [band.identifier for band in crossover.bands] == ["Band_1", "MonoSub"]
    assert client.sets == []


def test_crossover_rejects_topology_with_missing_reported_bands() -> None:
    client = TelemetryClient()

    def incomplete_ls(path: Iterable[str]) -> dict[str, str]:
        key = tuple(path)
        if key == ("Preset", "Crossover", "AT"):
            return {"NumBands": "3", "MonoSub": "0"}
        if key == ("Preset", "Crossover", "SV"):
            return {
                name: value
                for name, value in client.crossover_sv.items()
                if name.startswith("Band_1_")
            }
        return FakeClient.ls(client, key)

    client.ls = incomplete_ls  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="band identifiers"):
        controller.crossover()


def test_crossover_rejects_orphaned_band_attributes() -> None:
    client = TelemetryClient()
    client.crossover_sv.pop("MonoSub_HPFrequency")
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="MonoSub.*HPFrequency"):
        controller.crossover()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("Band_1_HPType", "Unknown 99"),
        ("Band_1_Polarity", "Sideways"),
        ("Band_1_Gain", "nan"),
        ("Band_1_Gain", "１２.５dB"),
        ("Band_1_Gain", " 12.5dB"),
        ("Band_1_Gain", "1.dB"),
        ("MonoSub_HPFrequency", "８０Hz"),
        ("MonoSub_HPFrequency", "80Hz "),
        ("MonoSub_HPFrequency", "01Hz"),
        ("MonoSub_HPFrequency", "1.Hz"),
        ("MonoSub_HPFrequency", ".5Hz"),
    ],
)
def test_crossover_rejects_unknown_or_nonfinite_curve_values(
    attribute: str, value: str
) -> None:
    client = TelemetryClient()
    client.crossover_sv[attribute] = value
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError):
        controller.crossover()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (INPUT_LEVELS["left"], "nan"),
        (INPUT_CLIPS["left"], "2"),
        (OUTPUT_LEVELS["high_left"], "inf"),
    ],
)
def test_meter_telemetry_rejects_unknown_or_nonfinite_values(
    path: tuple[str, ...], value: str
) -> None:
    client = TelemetryClient()
    client.values[path] = value
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError):
        if path in INPUT_LEVELS.values() or path in INPUT_CLIPS.values():
            controller.input_meters()
        else:
            controller.output_levels()


@pytest.mark.parametrize("malformed", ["+2", "2_0", " 2", "２"])
def test_recall_rejects_malformed_current_preset_before_unmuting(malformed: str) -> None:
    class MalformedCurrentPreset(FakeClient):
        def get(self, path: Iterable[str]) -> str:
            if tuple(path) == CURRENT_PRESET and any(
                written_path == RECALL for written_path, _ in self.sets
            ):
                return malformed
            return super().get(path)

    client = MalformedCurrentPreset(current=1)
    controller = Pa2Controller(
        client,
        allowed_slots=(1, 2),
        post_recall_delay=0,
        sleep=lambda _: None,
    )

    with pytest.raises((TelemetryError, RecallTimeout)):
        controller.activate_preset(2)

    assert all(value == "On" for value in client.mutes.values())
    assert not any(
        path in OUTPUT_MUTES.values() and value == "Off" for path, value in client.sets
    )


@pytest.mark.parametrize(
    "catalog",
    [
        {"NumPresets": "3", "CurrentPreset": "1", "Name_1": "Flat", "Name_3": "Factory"},
        {
            "NumPresets": "2",
            "CurrentPreset": "1",
            "Name_1": "Flat",
            "Name_01": "Alias",
        },
        {
            "NumPresets": "0_2",
            "CurrentPreset": "1",
            "Name_1": "Flat",
            "Name_2": "Alternate",
        },
    ],
)
def test_full_preset_inventory_rejects_incomplete_or_ambiguous_catalogs(
    catalog: dict[str, str],
) -> None:
    client = FakeClient()
    client.ls = lambda path: dict(catalog)  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError):
        controller.list_all_presets()


@pytest.mark.parametrize(
    "attributes",
    [
        {"NumBands": "0_1", "MonoSub": "1"},
        {"NumBands": "+1", "MonoSub": "1"},
        {"NumBands": "1", "MonoSub": "1", "FutureField": "surprise"},
    ],
)
def test_crossover_rejects_malformed_or_unknown_topology_metadata(
    attributes: dict[str, str],
) -> None:
    client = TelemetryClient()
    original_ls = client.ls

    def topology_ls(path: Iterable[str]) -> dict[str, str]:
        if tuple(path) == ("Preset", "Crossover", "AT"):
            return dict(attributes)
        return original_ls(path)

    client.ls = topology_ls  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError):
        controller.crossover()


def test_crossover_names_unobserved_topology_key_in_error() -> None:
    client = TelemetryClient()
    original_ls = client.ls

    def topology_ls(path: Iterable[str]) -> dict[str, str]:
        attributes = original_ls(path)
        if tuple(path) == ("Preset", "Crossover", "AT"):
            attributes["FutureField"] = "surprise"
        return attributes

    client.ls = topology_ls  # type: ignore[method-assign]
    controller = Pa2Controller(client, allowed_slots=(1, 2))

    with pytest.raises(TelemetryError, match="FutureField"):
        controller.crossover()
