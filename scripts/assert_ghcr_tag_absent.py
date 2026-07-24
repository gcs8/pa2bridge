#!/usr/bin/env python3
"""Fail closed unless a GHCR image tag is confirmed absent."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*"
)
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_TOKEN_RESPONSE_LIMIT = 64 * 1024
_TIMEOUT_SECONDS = 15.0
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class TagGuardError(RuntimeError):
    """GHCR did not prove that the requested immutable tag is absent."""


def _scoped_registry_token(
    repository: str,
    *,
    actor: str,
    github_token: str,
    opener: Callable[..., Any],
) -> str:
    query = urlencode(
        {
            "service": "ghcr.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    credentials = base64.b64encode(
        f"{actor}:{github_token}".encode("utf-8")
    ).decode("ascii")
    request = Request(
        f"https://ghcr.io/token?{query}",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with opener(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read(_TOKEN_RESPONSE_LIMIT + 1)
    except HTTPError as error:
        raise TagGuardError(
            f"GHCR token request failed with HTTP {error.code}"
        ) from None
    except (OSError, URLError, TimeoutError):
        raise TagGuardError("GHCR token request failed") from None
    if len(body) > _TOKEN_RESPONSE_LIMIT:
        raise TagGuardError("GHCR token response exceeded maximum size")
    try:
        payload = json.loads(body)
        token = payload["token"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise TagGuardError("GHCR token response was invalid") from None
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise TagGuardError("GHCR token response was invalid")
    return token


def check_tag_absent(
    *,
    repository: str,
    tag: str,
    actor: str,
    github_token: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Return only when GHCR authoritatively reports that ``tag`` is absent."""

    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise TagGuardError("invalid GHCR repository")
    if _TAG_RE.fullmatch(tag) is None:
        raise TagGuardError("invalid GHCR tag")
    if not actor or not github_token:
        raise TagGuardError("GitHub registry credentials are unavailable")

    registry_token = _scoped_registry_token(
        repository,
        actor=actor,
        github_token=github_token,
        opener=opener,
    )
    request = Request(
        f"https://ghcr.io/v2/{repository}/manifests/{tag}",
        headers={
            "Accept": _MANIFEST_ACCEPT,
            "Authorization": f"Bearer {registry_token}",
        },
        method="HEAD",
    )
    try:
        with opener(request, timeout=_TIMEOUT_SECONDS) as response:
            status = response.status
    except HTTPError as error:
        status = error.code
    except (OSError, URLError, TimeoutError):
        raise TagGuardError("GHCR manifest check failed") from None

    if status == 404:
        return
    if status == 200:
        raise TagGuardError(
            f"immutable GHCR image tag already exists: {repository}:{tag}"
        )
    raise TagGuardError(f"GHCR manifest check failed with HTTP {status}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail unless a GHCR image tag is confirmed absent"
    )
    parser.add_argument("repository")
    parser.add_argument("tag")
    args = parser.parse_args()
    try:
        check_tag_absent(
            repository=args.repository,
            tag=args.tag,
            actor=os.environ.get("GITHUB_ACTOR", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
        )
    except TagGuardError as error:
        parser.exit(1, f"HOLD: {error}\n")
    print(f"confirmed absent GHCR tag: {args.repository}:{args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
