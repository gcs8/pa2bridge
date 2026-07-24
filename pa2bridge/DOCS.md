# Home Assistant App: PA2Bridge

PA2Bridge provides a single supervised connection from Home Assistant to a dbx DriveRack PA2. Home Assistant entities are created through MQTT Discovery, and Stream Deck buttons should call those Home Assistant entities rather than connect to the PA2 directly.

## Before starting

1. Stop every other PA2 control session, including the standalone PA2Bridge service, PA2UI, and the dbx control application.
2. Enter the PA2 address. `pa2_password_override` may be left blank to use the PA2 factory default.
3. Leave `preset_slots` set to `auto` to expose every named preset reported by the PA2. To restrict recall, enter a comma-separated set of unique slots from `1` through `100`, such as `1, 2, 32`.
4. Install and start the official Mosquitto broker app and configure Home Assistant's MQTT integration.

## Upgrading from 0.1.1

Version 0.1.2 is a manual update because it replaces option fields that Supervisor cannot migrate in place. Leave `pa2_password_override` blank when the PA2 still uses its factory password; otherwise re-enter the PA2's custom password there before starting the updated app. Leave `preset_slots` at `auto` unless you want an explicit comma-separated restriction. A legacy `allowed_preset_slots` compatibility field may remain visible after the update; do not edit it.

## Safety behavior

Preset recall starts one finite absolute deadline at public entry, validates the target against a fresh device-reported catalog and any explicit allowlist, mutes and verifies all six outputs, recalls and reconnects, and requires direct `CurrentPreset` plus a fresh catalog to agree on the requested slot before unmuting. That same deadline covers catalog resolution and preflight muting, applies when the requested slot is already active, and is propagated into protocol reads and reconnect authentication. Every direct or automatic unmute route independently requires direct/catalog agreement on a currently allowed slot before any output is set `Off`, explicitly unmutes and verifies the outputs, then requires those sources to agree on that same slot again. Failures trigger reconnect-aware re-muting with a final all-six confirmation while the original deadline has time remaining. After that deadline, no additional PA2 operation starts; any exhausted rollback explicitly reports the mute state unsafe or unknown, and the Home Assistant device remains unavailable until a fresh PA2 snapshot succeeds.

## Options

- `pa2_host`: DriveRack PA2 IPv4 address or resolvable hostname.
- `pa2_port`: PA2 Console TCP port; normally `19272`.
- `pa2_username` and `pa2_password_override`: PA2 Console credentials. A blank password override uses the factory default, `administrator`.
- `preset_slots`: `auto` publishes every named preset reported by the device. A comma-separated set of unique slots from `1` through `100` narrows recall to those slots.
- `allowed_preset_slots`: legacy v0.1.1 compatibility field. Do not edit it after upgrading; use `preset_slots` instead.
- `connect_timeout`, `recall_timeout`, `poll_interval`, and `post_recall_delay`: bounded safety timing controls. `recall_timeout` is limited to 20 seconds so a PA2 transaction cannot consume the bridge's 30-second MQTT keepalive interval while broker-session authorization is held.
- `state_poll_interval`: PA2 state refresh interval.
- `expose_meters`: publish two input and six output dBFS meters plus two input clip diagnostics; disabled by default.
- `base_topic`: strict NFC MQTT topic namespace for this PA2; controls, wildcards, empty levels, and normalization-changing text are rejected.
- `discovery_prefix`: strict NFC Home Assistant MQTT Discovery prefix with the same lexical restrictions.

The app obtains dedicated MQTT service credentials from Home Assistant Supervisor. MQTT credentials are not entered into app options.

The app always publishes read-only preset inventory and crossover topology/curve parameters. These details have separate availability from the core preset/mute state, so a failed refresh marks them unavailable instead of presenting retained values as current. The preset inventory defines automatic-mode recall choices but does not override an explicit allowlist. PA2 front-panel System Lockout is not published because no verified Console object for that setting has been identified; the unrelated raw `Access_Rights` value is not guessed or relabeled.

## One-writer rule

Never run the standalone user service and this Home Assistant App simultaneously. Do not use PA2UI or the dbx application while PA2Bridge is active.
