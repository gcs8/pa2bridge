# Changelog

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
