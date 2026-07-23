# Home Assistant App: PA2Bridge

PA2Bridge provides a single supervised connection from Home Assistant to a dbx DriveRack PA2. Home Assistant entities are created through MQTT Discovery, and Stream Deck buttons should call those Home Assistant entities rather than connect to the PA2 directly.

## Before starting

1. Stop every other PA2 control session, including the standalone PA2Bridge service, PA2UI, and the dbx control application.
2. Enter the PA2 address and credentials in the app configuration.
3. Choose a non-empty, duplicate-free subset of slots `1` and `2` for `allowed_preset_slots`; no other slot is accepted.
4. Install and start the official Mosquitto broker app and configure Home Assistant's MQTT integration.

## Safety behavior

Preset recall starts one finite absolute deadline at public entry, validates the allowlist, mutes and verifies all six outputs, recalls and reconnects, and requires direct `CurrentPreset` plus a fresh catalog to agree on the requested slot before unmuting. That same deadline covers catalog resolution and preflight muting, applies when the requested slot is already active, and is propagated into protocol reads and reconnect authentication. Every direct or automatic unmute route independently requires direct/catalog agreement on a currently allowed slot before any output is set `Off`, explicitly unmutes and verifies the outputs, then requires those sources to agree on that same slot again. Failures trigger reconnect-aware re-muting with a final all-six confirmation while the original deadline has time remaining. After that deadline, no additional PA2 operation starts; any exhausted rollback explicitly reports the mute state unsafe or unknown, and the Home Assistant device remains unavailable until a fresh PA2 snapshot succeeds.

## Options

- `pa2_host`: DriveRack PA2 IPv4 address or resolvable hostname.
- `pa2_port`: PA2 Console TCP port; normally `19272`.
- `pa2_username` and `pa2_password`: PA2 Console credentials.
- `allowed_preset_slots`: a non-empty, duplicate-free subset of slots `1` and `2`;
  no other slot can be configured for recall.
- `connect_timeout`, `recall_timeout`, `poll_interval`, and `post_recall_delay`: bounded safety timing controls. `recall_timeout` is limited to 20 seconds so a PA2 transaction cannot consume the bridge's 30-second MQTT keepalive interval while broker-session authorization is held.
- `state_poll_interval`: PA2 state refresh interval.
- `expose_meters`: publish two input and six output dBFS meters plus two input clip diagnostics; disabled by default.
- `base_topic`: strict NFC MQTT topic namespace for this PA2; controls, wildcards, empty levels, and normalization-changing text are rejected.
- `discovery_prefix`: strict NFC Home Assistant MQTT Discovery prefix with the same lexical restrictions.

The app obtains dedicated MQTT service credentials from Home Assistant Supervisor. MQTT credentials are not entered into app options.

The app always publishes read-only preset inventory and crossover topology/curve parameters. These details have separate availability from the core preset/mute state, so a failed refresh marks them unavailable instead of presenting retained values as current. They do not expand the preset recall allowlist. PA2 front-panel System Lockout is not published because no verified Console object for that setting has been identified; the unrelated raw `Access_Rights` value is not guessed or relabeled.

## One-writer rule

Never run the standalone user service and this Home Assistant App simultaneously. Do not use PA2UI or the dbx application while PA2Bridge is active.
