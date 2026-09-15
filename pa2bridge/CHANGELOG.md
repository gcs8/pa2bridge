# Changelog

## 0.1.9

- Discover the PA2 MAC by correlating the authenticated TCP peer with bounded, provenance-verified Home Assistant network-tracker data, persist the validated binding, and retry safely when current network data is temporarily unavailable. Conflicting identities fail closed. The App includes an explicit replacement workflow and healthy startup logs for MQTT, PA2 identity, preset count, and discovery publication.
- Keep the App on Supervisor's normal container network so its MQTT service hostname retains the DNS behavior used by prior releases. Direct local-neighbour data can corroborate trusted tracker data but cannot establish identity alone, preventing same-prefix proxy ARP from assigning a router MAC to the PA2.
- Record first-use address-only admission durably so a later socket or App restart cannot admit a replacement device until a MAC is validated.
- On the first 0.1.9 start with a validated MAC, every MQTT entity unique ID changes from the prior address-derived form. Home Assistant normally reuses unchanged entity IDs after the old retained records are removed, but users should check and restore any registry customizations, automations, dashboards, or Stream Deck bindings that do not carry over.
- Mark 0.1.9 as a breaking App version so Supervisor does not auto-update past the documented entity migration before the operator can record those bindings.

## 0.1.8

- Reject invalid PA2 usernames and passwords during configuration loading, and report Home Assistant App configuration failures as concise structured errors without tracebacks or credential values.
- Remove obsolete retained Home Assistant discovery records when meter exposure, discovery prefixes, or device identities change. Persist a bounded ownership manifest before publication so interrupted cleanup remains recoverable without deleting unrelated topics.
- Prevent MQTT shutdown lock inversion and handle SIGTERM through a bounded graceful-stop path. Home Assistant App and systemd shutdown budgets are now 75 seconds.
- Skip every actuator write when the requested preset is already active, preserve the current output mute state, and report whether outputs were verified unmuted or left unchanged.
- Expire MQTT actuator commands five seconds after callback receipt and carry that deadline through lock contention and preset preflight to the first protocol write. Commands that expire before any write do not trigger rollback writes.
- Keep valid detail entities online during healthy periodic refreshes while still publishing retained offline status when refresh, identity, session, or preset validity fails.
- Stabilize Windows and slow-loopback tests and remove invalid Python escape warnings without changing runtime behavior.

### Standalone systemd upgrade

Standalone users must replace `~/.config/systemd/user/pa2bridge.service` with this release's `deploy/pa2bridge.service`, then run:

```console
systemctl --user daemon-reload
systemctl --user restart pa2bridge.service
```

This applies the 75-second shutdown budget and configures the durable discovery ownership state directory used to remove obsolete retained entities.

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
