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

Commands are newline terminated. Device responses observed on firmware `1.2.0.1` use CRLF, which the parser normalizes. A single response line is bounded to 64 KiB before UTF-8 decoding; an oversized line closes the session, and receive chunks continue to share the caller's original absolute deadline.

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

The validated PA2 reported `NumPresets : 100`, but later catalog responses showed that `NumPresets` and catalog-embedded `CurrentPreset` are optional metadata. PA2Bridge accepts their omission only when the device reports a non-empty contiguous `Name_1` through `Name_n` range bounded to slots 1–100. Firmware 1.2.0.1 also returned the known sibling keys `Bypass`, `Changed`, `Enable`, `Recall`, `ReloadPreset`, `RenamePreset`, `Store`, and `StoreCount`; PA2Bridge ignores those fields when constructing the read-only catalog but continues to reject any unrecognized key. When `CurrentPreset` is omitted from `ls`, the bridge performs a bounded direct read and requires it to agree with any safety-sensitive pre-catalog read. Examples below use generic names for user slots 1 and 2.

Recall slot 2:

```text
set "\\Storage\Presets\SV\Recall" "2"
```

PA2Bridge treats ordinary `set` writes as asynchronous and does not block waiting for a reply. Observed PA2 behavior includes no response, an exact delayed `set` echo, and an exact `setr` acknowledgement. PA2Bridge records each queued write and, before the next `get` or `ls` response, discards only an exact `set` or `setr` frame with the same path and value, in TCP order. Mismatched or unsolicited acknowledgements remain protocol errors and invalidate the session.

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

PA2 firmware can stop servicing the Console and freeze front-panel telemetry when several output writes are sent as a burst. PA2Bridge therefore writes one output, immediately reads that same path back, and applies bounded inter-channel pacing before writing the next output. It then performs a final `get` on every path and reports success only when every readback matches. A failed immediate readback prevents the next normal output write.

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
| Crossover topology | `\\Preset\Crossover\AT\{NumBands,MonoSub}` | bounded integer / exact `0` or `1`; firmware `1.2.0.1` also reports `Class_Name`, `Flags`, `Instance_Name`, and `NumSlots` siblings |
| Crossover curves | `\\Preset\Crossover\SV\{Band_1,Band_2,Band_3,MonoSub}_{HPFrequency,HPType,Gain,LPFrequency,LPType,Polarity}` | frequency or `Out`, `BW`/`LR` type, dB gain, polarity |

The active firmware reports only band keys that exist in the current preset. PA2Bridge groups those reported keys rather than inventing absent bands. The four observed crossover-topology siblings are ignored for curve construction; they are accepted only by exact, case-sensitive key name, while any other sibling is rejected and named in diagnostics. Meter numbers and authoritative topology flags are parsed strictly; malformed, unknown, `nan`, and infinite values are not published.

No verified Console object for the front-panel **System Lockout** setting was found in PA2UI or in the inspected PA2 object tree. `\\Node\AT\Access_Rights` exists, but its semantics are not equivalent evidence for System Lockout, so PA2Bridge intentionally does not expose it as a lock state.
