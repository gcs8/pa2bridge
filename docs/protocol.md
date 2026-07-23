# Observed PA2 console protocol

PA2Bridge uses a TCP text console on port `19272`. The implementation is intentionally narrow: authentication, `get`, `ls`, and `set` only.

## Authentication

Client:

```text
connect administrator "<password>"
```

Observed success:

```text
HiQnet Console
connect logged in as administrator
```

The bridge accepts either the exact login banner directly or one exact `HiQnet Console` greeting followed immediately by that banner. Any other intervening frame invalidates the session rather than being skipped as stale traffic.

Commands are newline terminated. Device responses observed on firmware `1.2.0.1` use CRLF, which the parser normalizes.

## Absolute paths

An absolute path begins with two backslash characters; subsequent components use one:

```text
\\Storage\Presets\SV\CurrentPreset
```

Quoted path/value responses look like:

```text
get "\\Storage\Presets\SV\CurrentPreset" "1"
```

## Presets

Read current slot:

```text
get "\\Storage\Presets\SV\CurrentPreset"
```

List slot names:

```text
ls "\\Storage\Presets\SV"
```

The validated PA2 reported `NumPresets : 100`; examples below use generic names for user slots 1 and 2.

Recall slot 2:

```text
set "\\Storage\Presets\SV\Recall" "2"
```

On the validated firmware, ordinary `set` writes are fire-and-forget: the PA2 accepts the command without returning an echo or acknowledgement frame. PA2Bridge does not wait for an acknowledgement. It records each exact write and, before the next `get` or `ls` response, defensively discards only matching delayed `set` echoes in TCP order. The protocol fixture covers both no-response firmware behavior and multiple echoed writes followed by a same-session `get`.

After this write, PA2Bridge polls `CurrentPreset` and will not continue to the unmute phase until it reads `2`.

On the live firmware, a recall can make the active console session stop responding while the preset loads. PA2Bridge treats that as an expected bounded recovery case: it reconnects with the already validated in-memory credentials, resumes polling `CurrentPreset`, and still refuses to unmute unless the target slot is observed before `recall_timeout`.

## Output mute state

Each mute state uses `On` for muted and `Off` for unmuted:

```text
\\Preset\OutputGains\SV\HighLeftOutputMute
\\Preset\OutputGains\SV\HighRightOutputMute
\\Preset\OutputGains\SV\MidLeftOutputMute
\\Preset\OutputGains\SV\MidRightOutputMute
\\Preset\OutputGains\SV\LowLeftOutputMute
\\Preset\OutputGains\SV\LowRightOutputMute
```

PA2Bridge writes all requested values and then performs a `get` on every path. It reports success only when every readback matches.

## Output meter telemetry

Observed paths:

```text
\\Preset\OutputMeters\SV\HighLeftOutput
\\Preset\OutputMeters\SV\HighRightOutput
\\Preset\OutputMeters\SV\MidLeftOutput
\\Preset\OutputMeters\SV\MidRightOutput
\\Preset\OutputMeters\SV\LowLeftOutput
\\Preset\OutputMeters\SV\LowRightOutput
```

The live device returned numeric dB values including `-120.0`. MQTT meter publication is optional because these values can create high-cardinality recorder churn.

## Discovery

The PA2 also responds to UDP broadcast on port `19272`. `pa2ui` broadcasts read commands and learns the TCP endpoint from the UDP response source. PA2Bridge does not require UDP discovery because it uses a stable configured address. This avoids cross-VLAN broadcast dependence.

## Concurrency

PA2Bridge uses one client/session. `HiQnetClient` serializes command/response exchanges with a re-entrant lock, and `Pa2Controller` holds its own operation lock across the complete recall/confirm/unmute transaction. This prevents a polling `get` from consuming a command's response on the same socket.

## Read-only telemetry evidence

The following paths were confirmed with authorized `ls`/`get` operations against firmware `1.2.0.1` and are exposed without adding write capability:

| Data | Path or key family | Observed format |
|---|---|---|
| Full preset inventory | `\\Storage\Presets\SV\Name_<slot>` | preset name text |
| Input levels | `\\Preset\InputMeters\SV\{Left,Right}Input` | decimal `dB` value |
| Input clip | `\\Preset\InputMeters\SV\{Left,Right}InputClip` | exact `0` or `1` |
| Output levels | `\\Preset\OutputMeters\SV\{High,Mid,Low}{Left,Right}Output` | decimal `dB` value |
| Crossover topology | `\\Preset\Crossover\AT\{NumBands,MonoSub}` | bounded integer / exact `0` or `1` |
| Crossover curves | `\\Preset\Crossover\SV\{Band_1,Band_2,Band_3,MonoSub}_{HPFrequency,HPType,Gain,LPFrequency,LPType,Polarity}` | frequency or `Out`, `BW`/`LR` type, dB gain, polarity |

The active firmware reports only band keys that exist in the current preset. PA2Bridge groups those reported keys rather than inventing absent bands. Meter numbers and topology flags are parsed strictly; malformed, unknown, `nan`, and infinite values are not published.

No verified Console object for the front-panel **System Lockout** setting was found in PA2UI or in the inspected PA2 object tree. `\\Node\AT\Access_Rights` exists, but its semantics are not equivalent evidence for System Lockout, so PA2Bridge intentionally does not expose it as a lock state.
