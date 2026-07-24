from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from pa2bridge import __version__
from pa2bridge.config import ConfigError
from pa2bridge.ha_app import load_ha_app_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MQTT_ENV = {
    "PA2BRIDGE_MQTT_HOST": "core-mosquitto",
    "PA2BRIDGE_MQTT_PORT": "1883",
    "PA2BRIDGE_MQTT_USERNAME": "app-user",
    "PA2BRIDGE_MQTT_PASSWORD": "mqtt-secret",
}


def _options(**overrides: object) -> dict[str, object]:
    options: dict[str, object] = {
        "pa2_host": "192.0.2.20",
        "pa2_port": 19272,
        "pa2_username": "administrator",
        "pa2_password": "pa2-secret",
        "allowed_preset_slots": [1, 2],
        "connect_timeout": 3.0,
        "recall_timeout": 10.0,
        "poll_interval": 0.2,
        "post_recall_delay": 1.0,
        "state_poll_interval": 5.0,
        "expose_meters": False,
        "base_topic": "driverack/pa2",
        "discovery_prefix": "homeassistant",
    }
    options.update(overrides)
    return options


def _write_options(path: Path, options: dict[str, object]) -> None:
    path.write_text(json.dumps(options), encoding="utf-8")


def test_load_ha_app_config_uses_supervisor_options_and_mqtt_service(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    _write_options(path, _options())

    config = load_ha_app_config(path, environ=MQTT_ENV)

    assert config.pa2.host == "192.0.2.20"
    assert config.pa2.allowed_preset_slots == (1, 2)
    assert config.pa2.password == "pa2-secret"
    assert config.mqtt.host == "core-mosquitto"
    assert config.mqtt.port == 1883
    assert config.mqtt.username == "app-user"
    assert config.mqtt.password == "mqtt-secret"
    assert "pa2-secret" not in repr(config)
    assert "mqtt-secret" not in repr(config)


def test_load_ha_app_config_rejects_non_network_pa2_host(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    _write_options(path, _options(pa2_host="pa2/bridge"))

    with pytest.raises(ConfigError, match="ASCII hostname or IP address"):
        load_ha_app_config(path, environ=MQTT_ENV)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pa2_port", "19272"),
        ("connect_timeout", True),
        ("recall_timeout", float("nan")),
        ("recall_timeout", 20.1),
        ("poll_interval", 0),
        ("post_recall_delay", -1),
        ("state_poll_interval", float("inf")),
        ("expose_meters", "false"),
        ("allowed_preset_slots", [1, True]),
        ("allowed_preset_slots", [1, 1]),
        ("allowed_preset_slots", [1, 3]),
        ("base_topic", "driverack/+/pa2"),
        ("base_topic", " driverack/pa2"),
        ("base_topic", "driverack/pa2\n"),
        ("base_topic", "driverack/pa2\tstatus"),
        ("base_topic", "driverack/pa2\x7fstatus"),
        ("base_topic", "driverack/pa2Å"),
        ("base_topic", "driverack/pa2\ud800"),
    ],
)
def test_load_ha_app_config_rejects_unsafe_option_values(
    tmp_path: Path, key: str, value: object
) -> None:
    path = tmp_path / "options.json"
    _write_options(path, _options(**{key: value}))

    with pytest.raises(ConfigError, match=key):
        load_ha_app_config(path, environ=MQTT_ENV)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PA2BRIDGE_MQTT_HOST", ""),
        ("PA2BRIDGE_MQTT_HOST", "broker\ninvalid"),
        ("PA2BRIDGE_MQTT_HOST", " broker"),
        ("PA2BRIDGE_MQTT_HOST", "brokerÅ"),
        ("PA2BRIDGE_MQTT_HOST", "broker invalid"),
        ("PA2BRIDGE_MQTT_HOST", "bröker"),
        ("PA2BRIDGE_MQTT_HOST", "fe80::1%eth 0"),
        ("PA2BRIDGE_MQTT_HOST", "fe80::1%é"),
        ("PA2BRIDGE_MQTT_HOST", "fe80::1%#"),
        ("PA2BRIDGE_MQTT_USERNAME", "user\x7finvalid"),
        ("PA2BRIDGE_MQTT_USERNAME", "user\ufdd0invalid"),
        ("PA2BRIDGE_MQTT_PASSWORD", "secret\ninvalid"),
        ("PA2BRIDGE_MQTT_PORT", "0"),
        ("PA2BRIDGE_MQTT_PORT", "not-a-port"),
        ("PA2BRIDGE_MQTT_PORT", "+1883"),
        ("PA2BRIDGE_MQTT_PORT", " 1883"),
        ("PA2BRIDGE_MQTT_PORT", "1883 "),
        ("PA2BRIDGE_MQTT_PORT", "1_883"),
        ("PA2BRIDGE_MQTT_PORT", "１８８３"),
        ("PA2BRIDGE_MQTT_PORT", "01883"),
    ],
)
def test_load_ha_app_config_rejects_invalid_mqtt_service_data(
    tmp_path: Path, key: str, value: str
) -> None:
    path = tmp_path / "options.json"
    _write_options(path, _options())
    environment = dict(MQTT_ENV)
    environment[key] = value

    with pytest.raises(ConfigError, match="MQTT"):
        load_ha_app_config(path, environ=environment)


def test_load_ha_app_config_preserves_opaque_mqtt_credentials(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    _write_options(path, _options())
    environment = dict(MQTT_ENV)
    environment["PA2BRIDGE_MQTT_USERNAME"] = " app-user "
    environment["PA2BRIDGE_MQTT_PASSWORD"] = " mqtt-secret "

    config = load_ha_app_config(path, environ=environment)

    assert config.mqtt.username == " app-user "
    assert config.mqtt.password == " mqtt-secret "


def test_load_ha_app_config_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    raw = json.dumps(_options())
    path.write_text(
        raw[:-1] + ',"allowed_preset_slots":[2]}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate.*allowed_preset_slots"):
        load_ha_app_config(path, environ=MQTT_ENV)


def test_load_ha_app_config_rejects_unknown_option_names(tmp_path: Path) -> None:
    options = _options()
    options["allowed_preset_slot"] = options.pop("allowed_preset_slots")
    path = tmp_path / "options.json"
    _write_options(path, options)

    with pytest.raises(ConfigError, match="unknown.*allowed_preset_slot"):
        load_ha_app_config(path, environ=MQTT_ENV)


def test_home_assistant_app_metadata_is_bounded_and_requires_mqtt() -> None:
    config = (PROJECT_ROOT / "pa2bridge" / "config.yaml").read_text()

    assert "slug: pa2bridge" in config
    assert "- mqtt:need" in config
    assert '- "int(1,2)"' in config
    assert "int(1,100)" not in config
    assert "host_network: true" not in config
    assert "hassio_api: true" not in config
    assert "homeassistant_api: true" not in config
    assert "privileged:" not in config
    assert 'recall_timeout: "float(0.1,20)"' in config


def test_public_install_shape_is_a_supervisor_managed_app_not_hacs() -> None:
    repository = (PROJECT_ROOT / "repository.yaml").read_text()
    readme = (PROJECT_ROOT / "README.md").read_text()
    gitignore = (PROJECT_ROOT / ".gitignore").read_text().splitlines()

    assert "name: PA2Bridge Home Assistant App Repository" in repository
    assert 'url: "https://github.com/gcs8/pa2bridge"' in repository
    assert "my.home-assistant.io/redirect/supervisor_store/" in readme
    assert "repository_url=https%3A%2F%2Fgithub.com%2Fgcs8%2Fpa2bridge" in readme
    assert "HACS is not required" in readme
    assert "Home Assistant OS is required" in readme
    assert "OS or Supervised" not in readme
    assert not (PROJECT_ROOT / "hacs.json").exists()
    assert ".hermes/" in gitignore


def test_home_assistant_app_entrypoint_never_dumps_environment_or_secrets() -> None:
    run_script = (
        PROJECT_ROOT
        / "pa2bridge"
        / "rootfs"
        / "etc"
        / "services.d"
        / "pa2bridge"
        / "run"
    ).read_text()

    assert "bashio::services mqtt" in run_script
    assert "/opt/pa2bridge/bin/python -m pa2bridge.ha_app" in run_script

    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
    assert dockerignore[0] == "**"
    assert "!pyproject.toml" in dockerignore
    assert "!LICENSE" in dockerignore
    assert "!src/**" in dockerignore
    assert "!pa2bridge/requirements.txt" in dockerignore
    assert "!pa2bridge/rootfs/**" in dockerignore
    assert not any(".git" in entry or ".env" in entry for entry in dockerignore[1:])
    assert "set -x" not in run_script
    assert "env |" not in run_script
    assert "printenv" not in run_script


def test_home_assistant_app_image_builds_the_reviewed_local_source() -> None:
    dockerfile = (PROJECT_ROOT / "pa2bridge" / "Dockerfile").read_text()

    assert dockerfile.startswith(
        "FROM ghcr.io/home-assistant/base-python:3.14-alpine3.23-2026.06.1@sha256:"
    )
    assert "apk add" not in dockerfile
    assert "COPY src /opt/pa2bridge/src" in dockerfile
    assert "COPY LICENSE /usr/share/licenses/pa2bridge/LICENSE" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "pip install --no-cache-dir /build" not in dockerfile
    assert "chmod 0755 /etc/services.d/pa2bridge/run" in dockerfile
    assert "git clone" not in dockerfile
    assert "pip install git+" not in dockerfile

    requirements = (PROJECT_ROOT / "pa2bridge" / "requirements.txt").read_text()
    assert requirements.strip() == (
        "paho-mqtt==2.1.0 "
        "--hash=sha256:6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee"
    )


def test_release_workflow_gates_publication_on_tests_scans_and_exact_image() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()

    assert 'uv run --no-sync pytest -q --cov="$PWD/src/pa2bridge"' in workflow
    assert "uv export --frozen --no-dev --no-emit-project" in workflow
    assert "uv sync --frozen --group dev --no-install-project" in workflow
    assert "uv run --no-sync pytest" in workflow
    assert "uv run --no-sync ruff check" in workflow
    assert 'for package in pa2bridge hatchling editables; do' in workflow
    assert "uv sync --frozen --group dev\n" not in workflow
    assert "uv run --no-sync pip-audit --strict" in workflow
    assert "gitleaks dir --no-banner --redact ." in workflow
    assert "aquasecurity/trivy-action@" in workflow
    assert "load: true" in workflow
    assert "push: false" in workflow
    assert "vars.REVIEWED_TREE_SHA256" in workflow
    assert "python scripts/review_identity.py" not in workflow
    assert "python -I -S -" in workflow
    assert "workflow_run:" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert workflow.index("Verify independently reviewed tree identity") < workflow.index(
        "Install uv"
    )
    assert workflow.index("Build unpushed image") < workflow.index("Scan exact image")
    assert workflow.index("Scan exact image") < workflow.index(
        "Push scanned image and capture immutable digest"
    )
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "ghcr.io/gcs8/pa2bridge@${amd64_digest}" in workflow
    assert "ghcr.io/gcs8/pa2bridge:${VERSION}-amd64" not in workflow
    assert "needs:\n      - verify\n      - prepare" in workflow
    assert "re.fullmatch(r'sha256:[0-9a-f]{64}\\n', value)" in workflow
    assert "group: pa2bridge-release" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "pa2bridge:staging-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "ghcr.io/gcs8/pa2bridge:latest" not in workflow
    assert 'test "${CURRENT_REVIEWED_TREE_SHA256}" = "${VERIFIED_REVIEWED_TREE_SHA256}"' in workflow


def test_release_workflow_digest_parser_binds_exact_staging_tag() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    digest = "sha256:" + "a" * 64
    expected_tag = "staging-123-1-amd64"
    output = f"{expected_tag}: digest: {digest} size: 1573\n"
    pattern = rf"^{re.escape(expected_tag)}: digest: (sha256:[0-9a-f]{{64}}) size: [0-9]+\s*$"

    assert re.findall(pattern, "push progress\n" + output, re.MULTILINE) == [digest]
    assert re.findall(pattern, output + output, re.MULTILINE) == [digest, digest]
    assert re.findall(
        pattern,
        "0.1.0-amd64: digest: sha256:abcd size: 1573\n",
        re.MULTILINE,
    ) == []
    assert re.findall(
        pattern,
        f"evil-tag: digest: {digest} size: 1573\n",
        re.MULTILINE,
    ) == []
    assert "expected_tag = os.environ['EXPECTED_PUSH_TAG']" in workflow
    assert "re.escape(expected_tag)" in workflow
    assert "if len(matches) != 1:" in workflow


def test_release_workflow_rejects_unexpected_digest_artifacts(tmp_path: Path) -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    marker = "python -I -S - <<'PY'\n"
    block = workflow.rsplit(marker, 1)[1].split("\n          PY", 1)[0]
    validator = "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in block.splitlines()
    )
    amd64_digest = "sha256:" + "a" * 64 + "\n"
    aarch64_digest = "sha256:" + "b" * 64 + "\n"
    amd64 = tmp_path / "pa2bridge-digest-amd64"
    aarch64 = tmp_path / "pa2bridge-digest-aarch64"
    amd64.mkdir()
    aarch64.mkdir()
    (amd64 / "amd64.txt").write_text(amd64_digest)
    (aarch64 / "aarch64.txt").write_text(aarch64_digest)
    environment = os.environ.copy()
    environment["DIGEST_ARTIFACT_DIR"] = str(tmp_path)

    valid = subprocess.run(
        [sys.executable, "-I", "-S", "-c", validator],
        env=environment,
        text=True,
        capture_output=True,
    )
    assert valid.returncode == 0, valid.stderr

    (aarch64 / "aarch64.txt").write_text(amd64_digest)
    duplicate = subprocess.run(
        [sys.executable, "-I", "-S", "-c", validator],
        env=environment,
        text=True,
        capture_output=True,
    )
    assert duplicate.returncode != 0
    assert "architecture digests must be distinct" in duplicate.stderr

    (aarch64 / "aarch64.txt").write_text(aarch64_digest)
    (tmp_path / "unexpected.txt").write_text(amd64_digest)
    unexpected = subprocess.run(
        [sys.executable, "-I", "-S", "-c", validator],
        env=environment,
        text=True,
        capture_output=True,
    )
    assert unexpected.returncode != 0
    assert "unexpected digest artifact set" in unexpected.stderr


def test_project_package_and_app_versions_match() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    app_config = (PROJECT_ROOT / "pa2bridge" / "config.yaml").read_text()
    app_version = re.search(r'^version: "([^"]+)"$', app_config, re.MULTILINE)

    assert app_version is not None
    assert project["project"]["version"] == app_version.group(1) == __version__


def test_release_workflow_binds_triggering_tag_to_app_version() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    assert "TRIGGER_TAG: ${{ github.event.workflow_run.head_branch }}" in workflow
    command = 'test "${TRIGGER_TAG}" = "v${VERSION}"'
    assert command in workflow
    publish_job = workflow.split("- name: Publish manifest", 1)[1]
    assert command in publish_job
    assert publish_job.index(command) < publish_job.index("docker buildx imagetools create")
    assert 'git fetch --force --no-tags origin "refs/tags/${TRIGGER_TAG}:refs/tags/${TRIGGER_TAG}"' in publish_job
    assert 'git rev-parse "refs/tags/v${VERSION}^{commit}"' in publish_job

    matching = subprocess.run(
        ["bash", "-c", command],
        env={"TRIGGER_TAG": "v0.1.0", "VERSION": "0.1.0"},
        check=False,
    )
    mismatched = subprocess.run(
        ["bash", "-c", command],
        env={"TRIGGER_TAG": "v0.1.1", "VERSION": "0.1.0"},
        check=False,
    )
    assert matching.returncode == 0
    assert mismatched.returncode != 0


def test_release_workflow_uses_a_workflow_run_compatible_secret_scan() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()

    assert "gitleaks/gitleaks-action" not in workflow
    assert "GITLEAKS_VERSION: 8.30.1" in workflow
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in workflow
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in workflow
    assert "sha256sum --check --strict" in workflow
    assert "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509" in workflow
    assert "gitleaks dir --no-banner --redact ." in workflow


def test_release_workflow_normalizes_the_helper_version_before_tag_checks(
    tmp_path: Path,
) -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    step = workflow.split("- name: Normalize app version", 1)[1].split(
        "- name: Verify release tag", 1
    )[0]
    block = step.split("python -I -S - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    script = "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in block.splitlines()
    )
    output = tmp_path / "github-output"
    environment = os.environ.copy()
    environment.update({"RAW_VERSION": '"0.1.0"', "GITHUB_OUTPUT": str(output)})

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "version=0.1.0\n"

    invalid_versions = (
        '"01.2.3"',
        '"1.2.3-01"',
        '"1.2.3-alpha..1"',
        f'"1.2.3-{"a" * 123}"',
    )
    for index, raw_version in enumerate(invalid_versions):
        invalid_output = tmp_path / f"invalid-output-{index}"
        invalid_environment = os.environ.copy()
        invalid_environment.update(
            {"RAW_VERSION": raw_version, "GITHUB_OUTPUT": str(invalid_output)}
        )
        invalid = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script],
            env=invalid_environment,
            text=True,
            capture_output=True,
            check=False,
        )

        assert invalid.returncode != 0
        assert not invalid_output.exists()


def test_final_publication_gate_refetches_and_rejects_a_moved_tag(
    tmp_path: Path,
) -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    publish_job = workflow.split("- name: Publish manifest", 1)[1]
    final_gate = publish_job.split(
        '          test -n "${VERIFIED_REVIEWED_TREE_SHA256}"', 1
    )[1].split("          docker buildx imagetools create", 1)[0]
    script = 'test -n "${VERIFIED_REVIEWED_TREE_SHA256}"\n' + "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in final_gate.splitlines()
    )
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    release = tmp_path / "release"

    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=seed, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=seed,
        check=True,
    )
    (seed / "candidate").write_text("A")
    subprocess.run(["git", "add", "candidate"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-m", "candidate A"], cwd=seed, check=True)
    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=seed,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "tag", "v0.1.0"], cwd=seed, check=True)
    subprocess.run(
        ["git", "push", "origin", "HEAD", "refs/tags/v0.1.0"],
        cwd=seed,
        check=True,
    )
    subprocess.run(["git", "clone", str(remote), str(release)], check=True)

    environment = os.environ.copy()
    environment.update(
        {
            "VERIFIED_REVIEWED_TREE_SHA256": "reviewed",
            "CURRENT_REVIEWED_TREE_SHA256": "reviewed",
            "TRIGGER_EVENT": "push",
            "TRIGGER_TAG": "v0.1.0",
            "VERSION": "0.1.0",
            "CANDIDATE_SHA": candidate_sha,
        }
    )
    valid = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=release,
        env=environment,
        check=False,
    )
    assert valid.returncode == 0

    (seed / "candidate").write_text("B")
    subprocess.run(["git", "add", "candidate"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-m", "candidate B"], cwd=seed, check=True)
    subprocess.run(["git", "tag", "--force", "v0.1.0"], cwd=seed, check=True)
    subprocess.run(
        ["git", "push", "--force", "origin", "refs/tags/v0.1.0"],
        cwd=seed,
        check=True,
    )

    moved = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=release,
        env=environment,
        check=False,
    )
    assert moved.returncode != 0


def test_release_identity_verifier_is_inline_isolated_and_fail_closed(
    tmp_path: Path,
) -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    marker = "python -I -S - <<'PY'\n"
    block = workflow.split(marker, 1)[1].split("\n          PY", 1)[0]
    verifier = "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in block.splitlines()
    )

    (tmp_path / "scripts").mkdir()
    malicious_marker = tmp_path / "candidate-code-ran"
    (tmp_path / "scripts" / "review_identity.py").write_text(
        f"from pathlib import Path\nPath({str(malicious_marker)!r}).write_text('ran')\n"
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed bytes\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)

    entries: list[tuple[str, str, bytes]] = []
    output = subprocess.check_output(
        ["git", "ls-files", "--stage", "-z"], cwd=tmp_path
    )
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, _object_id, _stage = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        entries.append((path, mode, (tmp_path / path).read_bytes()))
    digest = hashlib.sha256()
    for path, mode, data in sorted(entries):
        digest.update(
            f"{path}\0{mode}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}\n".encode()
        )

    environment = os.environ.copy()
    environment["EXPECTED_REVIEWED_TREE_SHA256"] = digest.hexdigest()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", verifier],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert not malicious_marker.exists()

    tracked.write_text("unreviewed bytes\n")
    mismatch = subprocess.run(
        [sys.executable, "-I", "-S", "-c", verifier],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert mismatch.returncode != 0
    assert "reviewed tree identity mismatch" in mismatch.stderr


def test_review_identity_script_hashes_exact_tracked_bytes_and_modes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "plain.txt").write_text("plain\n", encoding="utf-8")
    executable = tmp_path / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    subprocess.run(["git", "add", "plain.txt", "run.sh"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "review_identity.py")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "COVERAGE_RCFILE": str(PROJECT_ROOT / "pyproject.toml"),
        },
    )

    assert len(result.stdout.strip()) == 64


def test_public_release_metadata_declares_mit_license() -> None:
    license_text = (PROJECT_ROOT / "LICENSE").read_text()
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    dockerfile = (PROJECT_ROOT / "pa2bridge" / "Dockerfile").read_text()

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 gcs8" in license_text
    assert 'license = "MIT"' in pyproject
    assert 'org.opencontainers.image.licenses="MIT"' in dockerfile
