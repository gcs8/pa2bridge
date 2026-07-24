from __future__ import annotations

import importlib.util
import io
from email.message import Message
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "assert_ghcr_tag_absent.py"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assert_ghcr_tag_absent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, *, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _opener_for_manifest_status(status: int):
    calls = []

    def opener(request, *, timeout: float):
        assert timeout == 15.0
        calls.append(request)
        if len(calls) == 1:
            return _Response(status=200, body=b'{"token":"scoped-registry-token"}')
        if status == 404:
            raise HTTPError(
                request.full_url, 404, "Not Found", Message(), io.BytesIO()
            )
        if status >= 400:
            raise HTTPError(
                request.full_url, status, "Failure", Message(), io.BytesIO()
            )
        return _Response(status=status)

    return opener, calls


def test_absent_ghcr_tag_is_accepted_without_exposing_credentials() -> None:
    guard = _load_guard()
    opener, calls = _opener_for_manifest_status(404)

    guard.check_tag_absent(
        repository="gcs8/pa2bridge",
        tag="0.1.5",
        actor="release-actor",
        github_token="PRIVATE-GITHUB-TOKEN",
        opener=opener,
    )

    assert len(calls) == 2
    assert calls[1].get_method() == "HEAD"
    assert calls[1].get_header("Authorization") == "Bearer scoped-registry-token"
    assert "PRIVATE-GITHUB-TOKEN" not in repr(calls)


def test_existing_ghcr_tag_blocks_publication() -> None:
    guard = _load_guard()
    opener, _ = _opener_for_manifest_status(200)

    with pytest.raises(guard.TagGuardError, match="already exists"):
        guard.check_tag_absent(
            repository="gcs8/pa2bridge",
            tag="0.1.5",
            actor="release-actor",
            github_token="PRIVATE-GITHUB-TOKEN",
            opener=opener,
        )


def test_unexpected_registry_status_fails_closed_without_exposing_token() -> None:
    guard = _load_guard()
    opener, _ = _opener_for_manifest_status(503)

    with pytest.raises(guard.TagGuardError, match="HTTP 503") as raised:
        guard.check_tag_absent(
            repository="gcs8/pa2bridge",
            tag="0.1.5",
            actor="release-actor",
            github_token="PRIVATE-GITHUB-TOKEN",
            opener=opener,
        )

    assert "PRIVATE-GITHUB-TOKEN" not in str(raised.value)


def test_release_workflow_checks_version_tag_before_build_and_before_publish() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-app.yaml").read_text()
    command = 'python -I -S scripts/assert_ghcr_tag_absent.py "gcs8/pa2bridge" "${VERSION}"'

    assert workflow.count(command) == 2
    prepare = workflow.split("prepare:", 1)[1].split("  build:", 1)[0]
    assert "packages: read" in prepare
    assert command in prepare
    manifest = workflow.split("  manifest:", 1)[1]
    assert manifest.rindex(command) < manifest.index("docker buildx imagetools create")
