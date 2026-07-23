"""Command-line entry point for read-only inspection, verified control, and MQTT."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Sequence

from .config import AppConfig, ConfigError, load_config
from .controller import (
    OutputVerificationError,
    Pa2Controller,
    Pa2State,
    RecallTimeout,
    TelemetryError,
)
from .mqtt_bridge import MqttBridge, MqttPublishError
from .protocol import HiQnetClient, ProtocolError


LOGGER = logging.getLogger(__name__)


@contextmanager
def connected_controller(config: AppConfig) -> Iterator[Pa2Controller]:
    client = HiQnetClient(
        config.pa2.host,
        port=config.pa2.port,
        timeout=config.pa2.connect_timeout,
    )
    client.connect(config.pa2.username, config.pa2.password)
    try:
        yield Pa2Controller(
            client,
            allowed_slots=config.pa2.allowed_preset_slots,
            recall_timeout=config.pa2.recall_timeout,
            poll_interval=config.pa2.poll_interval,
            post_recall_delay=config.pa2.post_recall_delay,
        )
    finally:
        client.close()


def _preset_payload(preset) -> dict[str, object]:
    return {"slot": preset.slot, "name": preset.name, "label": preset.label}


def _state_payload(state: Pa2State, allowed_presets=None) -> dict[str, object]:
    payload: dict[str, object] = {
        "device": asdict(state.identity),
        "current_preset": _preset_payload(state.current_preset),
        "output_mutes": state.output_mutes,
        "all_outputs_unmuted": state.all_outputs_unmuted,
    }
    if allowed_presets is not None:
        payload["allowed_presets"] = [_preset_payload(preset) for preset in allowed_presets]
    return payload


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control a dbx DriveRack PA2 safely")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML configuration path (default: config.toml)",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("probe", help="read identity, preset, and output mute state")

    activate = commands.add_parser(
        "activate", help="recall an allowlisted preset and unmute after confirmed load"
    )
    activate.add_argument("preset", help="slot, exact preset name, or 'slot: name' label")
    activate.add_argument(
        "--no-unmute",
        action="store_true",
        help="recall only; explicitly skip the normal post-recall output unmute",
    )

    commands.add_parser("unmute", help="unmute all six outputs and verify readback")
    commands.add_parser("mute", help="mute all six outputs and verify readback")
    commands.add_parser("daemon", help="run the Home Assistant MQTT bridge")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.command == "daemon":
            MqttBridge(config).run_forever()
            return 0

        with connected_controller(config) as controller:
            if args.command == "probe":
                _print(_state_payload(controller.state(), controller.list_presets()))
            elif args.command == "activate":
                state = controller.activate_preset(
                    args.preset,
                    unmute_after=not args.no_unmute,
                )
                _print(
                    {
                        "action": "activate",
                        "preset": _preset_payload(state.current_preset),
                        "outputs_unmuted": state.all_outputs_unmuted,
                        "verified": True,
                    }
                )
            elif args.command in {"mute", "unmute"}:
                muted = args.command == "mute"
                controller.set_all_outputs_muted(muted)
                _print({"action": args.command, "verified": True})
            else:  # pragma: no cover - argparse prevents this
                raise AssertionError(args.command)
        return 0
    except (
        ConfigError,
        MqttPublishError,
        OutputVerificationError,
        ProtocolError,
        RecallTimeout,
        TelemetryError,
        ValueError,
    ) as error:
        LOGGER.error("%s", error)
        print(json.dumps({"error": str(error), "verified": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
