# Changelog

## 0.1.2

- Default a blank or omitted PA2 password to the factory administrator password.
- Discover and expose all device-reported presets by default; the PA2 supports user slots 1–75 and factory slots 76–100.
- Replace the broken Home Assistant numeric-array field with `auto` or an optional comma-separated slot allowlist.
- Preserve Supervisor upgrade compatibility with v0.1.1 options. The manual update introduces `pa2_password_override` and `preset_slots`; users with a custom PA2 password must re-enter it, and the legacy numeric-list key is accepted only for migration.

## 0.1.1

- Initial public Home Assistant App release.
- Upgrade fixed Alpine packages during the image build.
- Remove the unused inherited `tempio` binary and its unreachable vulnerable Go dependencies.

## 0.1.0

- Initial experimental Home Assistant App package.
- Supervisor-managed MQTT service discovery.
- Verified preset recall and output mute controls.
- Read-only preset inventory and validated crossover topology.
- Optional read-only input/output meters and input clip diagnostics.
- Fail-closed MQTT availability and bounded publish behavior.
- Twenty-second recall deadline ceiling below the MQTT keepalive interval.
- Public MIT-licensed Home Assistant App repository metadata and one-click repository link.
