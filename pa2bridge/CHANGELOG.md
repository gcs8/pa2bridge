# Changelog

## 0.1.7

- Reduce modeled steady-state PA2 request traffic by 27.2% by reusing device identity only within the current authenticated connection generation and deriving preset views from one validated catalog snapshot per refresh.
- Refresh device identity and Home Assistant discovery metadata after reconnects before republishing state, while preserving live CLI probe behavior, serialized transport, bounded retries, exact response correlation, and no replay on reconnect.
- Add a repository-local, fail-closed read-only validation harness that permits one connection and exactly two polls using existing reads only; the merged behavior completed live validation with 24 commands and normal PA2 front-panel and audio operation.

## 0.1.6

- Verify each output mute write before sending the next channel and apply bounded inter-channel pacing, preventing the PA2 Console and front-panel telemetry from being wedged by the pre-recall six-write burst.
- Retain a final all-six readback and the existing absolute recall, rollback, and fail-closed unmute deadlines.

## 0.1.5

- Accept only exact correlated PA2 `setr` write acknowledgements, fixing verified mute, unmute, and preset-recall commands while continuing to reject mismatched or unsolicited frames.
- Accept the narrow auxiliary crossover-topology metadata observed on PA2 firmware while preserving strict rejection of unknown or malformed topology fields.
- Recover from transient MQTT disconnects, invalidate queued command sessions until a fresh subscription acknowledgement, and refresh discovery when allowed preset labels change.
- Reconcile retained legacy and canonical preset allowlists without widening recall access or locking out equivalent restrictions.
- Bound protocol response lines to 64 KiB without extending operation deadlines.
- Prevent release workflow reruns from replacing an existing versioned GHCR image tag.

## 0.1.4

- Accept the eight auxiliary preset-storage keys observed on DriveRack PA2 firmware 1.2.0.1 without treating their values as preset-catalog data.
- Continue to reject unrecognized catalog keys while preserving contiguous slot, current-preset, deadline, and fail-closed unmute validation.

## 0.1.3

- Accept PA2 preset catalogs that omit optional `NumPresets` or embedded `CurrentPreset` metadata while continuing to require a contiguous device-reported slot range from 1 through 100.
- Preserve fail-closed preset verification by bracketing metadata-light catalog reads with bounded direct `CurrentPreset` checks.

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
