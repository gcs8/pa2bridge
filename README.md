# PA2Bridge

[![Open your Home Assistant instance and show the app store with the PA2Bridge repository pre-filled.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fgcs8%2Fpa2bridge)

A small, safety-oriented bridge for a **dbx DriveRack PA2**. It keeps one authoritative PA2 TCP session and exposes:

- an allowlisted Home Assistant **Preset** select;
- automatic **output unmute only after preset recall is confirmed**;
- a separate **Unmute all outputs** button;
- six per-output mute switches with readback;
- firmware, availability, and last-command diagnostics;
- full read-only preset inventory and crossover curve parameters;
- optional two-channel input and six-channel output meter telemetry plus input clip flags (disabled by default in HA);
- local CLI commands for inspection and verified control.

The intended Stream Deck path is **Stream Deck → Home Assistant plugin → HA entity/service → MQTT → PA2Bridge → PA2**. This avoids competing PA2 sessions and gives the Stream Deck live state feedback.

## Validation status

The Console transport, preset discovery/recall behavior, six output mute paths, and six output meter paths have been exercised against a physical DriveRack PA2 running firmware `1.2.0.1`. Preset loading can terminate or stall the active Console session, so recall confirmation always uses a bounded reconnect path before any unmute.

Before migrating from the standalone service to the Home Assistant App, stop the standalone service and verify that no other PA2 client is connected. Never run both deployment models concurrently.

## Why a Home Assistant App instead of HACS?

PA2Bridge is a long-running, safety-sensitive TCP session owner. A Home Assistant App provides Supervisor-managed lifecycle, isolation from Home Assistant Core, protected options, and automatic MQTT service credentials while reusing the same tested bridge implementation. A HACS custom integration would duplicate or move the PA2 connection and recall transaction into Home Assistant Core, increasing both failure impact and protocol-maintenance surface.

The supported architecture is therefore **Home Assistant App → MQTT Discovery → Home Assistant entities**. HACS is not required and does not install Home Assistant Apps. Home Assistant OS is required; Supervisor owns the app container, so no separate VM or externally managed Docker service is needed. The standalone Python service remains available as a fallback for Home Assistant Container and other installation types.

## Why a bridge instead of modifying `pa2ui`?

[`Kattjakt/pa2ui`](https://github.com/Kattjakt/pa2ui) was useful protocol research: it confirms the PA2 console transport and output path names. However, its own README lists preset store/recall as future work, and the repository currently declares no project license. PA2Bridge is an independent Python implementation and does not import or package `pa2ui` source.

## Safety contract

Preset activation is deliberately ordered:

1. Start one finite absolute activation deadline, then resolve the requested preset against `allowed_preset_slots`, which is restricted to a non-empty subset of slots `1` and `2`.
2. Set all six output mutes to `On` and verify all six readbacks. Abort before recall if any value is unknown or not muted.
3. Write the PA2 `Recall` value only when the target differs from the current slot. The original entry deadline applies to catalog resolution, preflight muting, identity/current reads, recall, and the already-active path without reset.
4. Poll `CurrentPreset` until the target slot is confirmed, reconnecting the console within the same bounded deadline if preset loading drops or stalls the original session.
5. If confirmation times out, stop with all outputs muted; **do not unmute**.
6. Wait `post_recall_delay`.
7. Re-read direct `CurrentPreset` plus a fresh preset catalog and require both to identify the target; any conflict stops without unmuting.
8. Re-read all six output mutes and require every channel to remain `On`.
9. Set each output mute to `Off`, checking the original absolute recall deadline before and after every write.
10. Read back each value within that same deadline; report success only if every output says `Off`.
11. If unmute or readback fails while time remains, reconnect as needed, re-mute all six outputs in complete recovery rounds, and require a final all-six muted readback within the original recall deadline.
12. Once that absolute deadline expires, start no additional PA2 operation; report the mute state as unsafe or unknown and leave Home Assistant availability offline rather than silently extending the actuator transaction.

Additional guardrails:

- MQTT discovery requests non-retained commands, and the bridge rejects retained command messages before any PA2 write, so a stale broker command cannot execute after downtime.
- Every unmute route—including the direct all-output button, per-output switches, CLI, and automatic post-recall unmute—captures an allowed slot only after direct `CurrentPreset` and a fresh catalog agree, then requires those sources to agree on that same exact slot again after the verified `Off` writes.
- Preset activation and standalone mute/unmute transactions each use one finite absolute operation deadline from public entry through writes, socket reads, reconnect authentication, catalog parsing, all-six rollback, and final confirmation. The controller propagates that same deadline into the protocol layer; deadline exhaustion starts no new PA2 operation, and any exhausted rollback explicitly reports the output state as unsafe or unknown.
- Per-output switch commands are read back; a failed single-output unmute or a post-write preset change triggers the same all-six fail-closed rollback before reporting failure.
- A failed bulk or per-output mute write also triggers reconnect-aware all-six re-muting and final readback rather than leaving a partial mute operation unexamined.
- Home Assistant reports the device online only after a complete fresh PA2 snapshot; transport or verification failure immediately closes the PA2 session and reports offline.
- Preset inventory and crossover entities require both common and detail availability, so an abrupt bridge loss or failed detail refresh cannot present stale retained attributes as current.
- PA2 connect, reconnect, close, poll, and command transactions are serialized under one lifecycle lock.
- MQTT callbacks accept only exact discovered command topics and enqueue at most one command bound to the current broker-session generation. The broker-state lock linearizes final authorization with the serialized PA2 transaction; a disconnect recorded first rejects the command and closes PA2, while a rejected subscription or publish exits without reusing the failed client.
- Presets not explicitly allowlisted cannot be selected or recalled.
- Input/output meter and input clip entities are opt-in and disabled by default to avoid Home Assistant recorder churn.
- Protocol input rejects newlines, quotes, and path separators in untrusted values/components.
- MQTT topic prefixes must be exact NFC text with no controls, wildcards, empty levels, or normalization-changing Unicode. Supervisor broker host data is validated without trimming, while opaque MQTT username and password values are preserved byte-for-codepoint rather than silently normalized.

## Release integrity gate

Image publication requires the repository variable `REVIEWED_TREE_SHA256` to equal the canonical identity reported by an independent exact-tree review. A version tag emits an untrusted `Release Candidate` signal; the `workflow_run` publisher executes its protected default-branch workflow, checks out the signaled commit, and recomputes its identity with an inline isolated standard-library verifier before tests or builds. Protect the default branch so release-workflow changes require review. Each architecture is built locally, scanned before push, and its resulting registry digest is carried through an artifact; the final multi-architecture manifest references those immutable scanned digests rather than mutable architecture tags.

Do not create a release tag or change `REVIEWED_TREE_SHA256` until the exact candidate has passed all review gates. Any byte change requires a new identity and fresh independent review.

## Home Assistant App installation (recommended)

The app package lives in [`pa2bridge/`](pa2bridge/). After a reviewed release image is published:

1. In Home Assistant, open **Settings → Apps → App store → Repositories**.
2. Add `https://github.com/gcs8/pa2bridge`.
3. Install **PA2Bridge** and enter the PA2 host, credentials, and approved preset slots.
4. Confirm every other PA2 writer is stopped before starting the app.

The app requests Home Assistant's `mqtt:need` service and receives dedicated broker credentials from Supervisor. PA2 and MQTT secrets are not placed in Git or Stream Deck profiles.

`recall_timeout` is limited to 20 seconds. This preserves a 10-second margin below the bridge's 30-second MQTT keepalive while the final broker-session authorization lock is held across a PA2 transaction.

## Standalone install (development or fallback)

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv tool install .
mkdir -p ~/.config/pa2bridge
cp config.example.toml ~/.config/pa2bridge/config.toml
```

Create a dedicated MQTT user in Home Assistant:

1. **Settings → People → Users → Add user**.
2. Create a non-admin user such as `pa2bridge` with a unique password.
3. Put only the values in a user-private environment file:

```bash
install -m 600 /dev/null ~/.config/pa2bridge/pa2bridge.env
$EDITOR ~/.config/pa2bridge/pa2bridge.env
```

Contents:

```dotenv
PA2BRIDGE_MQTT_USERNAME=pa2bridge
PA2BRIDGE_MQTT_PASSWORD=replace-with-the-unique-password
```

The Home Assistant Mosquitto app accepts Home Assistant user credentials and rejects anonymous clients. Do not put either password in `config.toml` or Git.

If the PA2 administrator password is no longer the factory default, add this to `[pa2]`:

```toml
password_env = "PA2BRIDGE_PA2_PASSWORD"
```

and add the value to the same `0600` environment file.

Load the environment for an interactive smoke check:

```bash
set -a
. ~/.config/pa2bridge/pa2bridge.env
set +a
pa2bridge --config ~/.config/pa2bridge/config.toml probe
```

`probe` performs only `get`/`ls` operations.

## CLI

```bash
# Read-only identity, preset list/current slot, and all six mute states
pa2bridge --config ~/.config/pa2bridge/config.toml probe

# Recall slot 2, wait for confirmation, then unmute and verify all outputs
pa2bridge --config ~/.config/pa2bridge/config.toml activate 2

# The exact discovered label works too
pa2bridge --config ~/.config/pa2bridge/config.toml activate "2: Alternate"

# Explicitly recall without the normal post-load unmute
pa2bridge --config ~/.config/pa2bridge/config.toml activate 2 --no-unmute

# Standalone verified mute controls
pa2bridge --config ~/.config/pa2bridge/config.toml unmute
pa2bridge --config ~/.config/pa2bridge/config.toml mute

# MQTT/Home Assistant bridge
pa2bridge --config ~/.config/pa2bridge/config.toml daemon
```

A failed command exits nonzero and prints `{"verified": false, ...}` to stderr. A successful write is not reported until readback succeeds.

## Run as a standalone user service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/pa2bridge.service ~/.config/systemd/user/pa2bridge.service
systemctl --user daemon-reload
systemctl --user enable --now pa2bridge.service
systemctl --user status pa2bridge.service
journalctl --user -u pa2bridge.service -n 100 --no-pager
```

The supplied fallback service is restart-on-failure, uses the environment file above, and publishes MQTT `offline` through both explicit shutdown and broker LWT behavior. Never enable it while the Home Assistant App is running.

### Stop or roll back the deployment

The smallest reversible rollback is to stop and disable only the bridge:

```bash
systemctl --user disable --now pa2bridge.service
```

This makes the MQTT entities unavailable but leaves their retained discovery records, protected configuration, external secret-store values, and dedicated HA account intact so rollback does not destroy credentials. Re-enable with `systemctl --user enable --now pa2bridge.service`. Deleting the HA account, secret-store values, local files, and retained discovery topics is a separate destructive cleanup and should only be done after confirming the bridge will not be restored.

## Home Assistant entities

MQTT discovery creates one HA device named from the PA2's observed `Instance_Name` and these entities:

- **Preset** (`select`) — selecting an option performs recall + confirmed post-load unmute.
- **Unmute all outputs** (`button`).
- **High/Mid/Low Left/Right mute** (`switch`, six total).
- **Firmware** and **Last command** (`sensor`, diagnostic).
- **Preset inventory** (`sensor`, diagnostic) — state is the number of named presets; attributes contain every observed slot/name. Recall remains limited to the configured allowlist.
- **Crossover** (`sensor`, diagnostic) — attributes contain topology plus every reported band's HPF/LPF frequency and type, gain, and polarity, which is sufficient for a dashboard to render crossover curves.
- Optional **Left/Right input level**, **High/Mid/Low Left/Right output level**, and **Left/Right input clip** entities when `expose_meters = true`; they are disabled by default.

The PA2 front-panel **System Lockout** state is not exposed because neither the inspected PA2UI revision nor the verified PA2 object tree provided an identified lockout value. The observed `Access_Rights` field is intentionally not relabeled as System Lockout without protocol evidence.

Entity IDs are assigned by Home Assistant and can be renamed in the entity registry. Use the actual IDs shown on the PA2 device page rather than assuming an ID.

## Stream Deck

Install Christoph Giesche's **Home Assistant** plugin from the Elgato Marketplace. It can display an entity's live state and call HA services on short press, long press, tap, or dial rotation.

Recommended buttons:

### Recall preset slot 1

- Appearance entity: the discovered PA2 **Preset** select.
- Action domain/service: `select` / `select_option`.
- Target: that same select entity.
- Service data:

```json
{"option": "1: Flat"}
```

### Recall preset slot 2

```json
{"option": "2: Alternate"}
```

### Emergency/recovery unmute

- Appearance entity: the PA2 **Last command** sensor (for feedback), or any PA2 entity.
- Action domain/service: `button` / `press`.
- Target: the discovered **Unmute all outputs** button.

A preset-select press already performs the post-recall unmute; the separate button is for recovery/manual use.

## MQTT topics

Default root: `driverack/pa2`.

| Direction | Topic |
|---|---|
| HA → bridge | `driverack/pa2/command/preset` |
| HA → bridge | `driverack/pa2/command/unmute` |
| HA → bridge | `driverack/pa2/command/mute/<channel>` |
| bridge → HA | `driverack/pa2/status` |
| bridge → HA | `driverack/pa2/status/details` (inventory/crossover availability) |
| bridge → HA | `driverack/pa2/state/preset` |
| bridge → HA | `driverack/pa2/state/mute/<channel>` |
| bridge → HA | `driverack/pa2/state/level/<channel>` (optional) |
| bridge → HA | `driverack/pa2/state/level/input_<side>` (optional) |
| bridge → HA | `driverack/pa2/state/clip/input_<side>` (optional) |
| bridge → HA | `driverack/pa2/state/preset_inventory` (JSON state + attributes) |
| bridge → HA | `driverack/pa2/state/crossover` (JSON state + curve attributes) |

Discovery records use `homeassistant/<component>/<device>/<entity>/config`.

## Development and verification

```bash
uv sync --dev
uv run pytest -q
uv run pytest --cov="$PWD/src/pa2bridge" --cov-report=term-missing
uv build
```

Current suite (**277 tests, 89.18% total coverage**): protocol framing/auth/get/ls, no-response and delayed-echo `set` sequencing, preset allowlisting, confirmed recall ordering, no-unmute-on-timeout, six-output readback, MQTT discovery and broker-session locking, subscription-failure availability cleanup, standalone and Supervisor configuration bounds, app/release metadata, secret-safe loading, and complete CLI mute/unmute/error behavior.

## Protocol notes

See [`docs/protocol.md`](docs/protocol.md) for the observed console commands and state paths.

## License

PA2Bridge is released under the [MIT License](LICENSE).
