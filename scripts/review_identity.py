#!/usr/bin/env python3
"""Compute the canonical SHA-256 identity of the checked-out Git tree."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import subprocess


def tracked_entries(root: pathlib.Path) -> list[tuple[str, str, bytes]]:
    output = subprocess.check_output(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
    )
    entries: list[tuple[str, str, bytes]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, _object_id, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise RuntimeError("the Git index contains unresolved entries")
        if mode not in {"100644", "100755"}:
            raise RuntimeError(f"unsupported tracked mode {mode} for {raw_path!r}")
        path = raw_path.decode("utf-8")
        data = (root / path).read_bytes()
        entries.append((path, mode, data))
    return sorted(entries)


def tree_identity(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path, mode, data in tracked_entries(root):
        file_digest = hashlib.sha256(data).hexdigest()
        digest.update(
            f"{path}\0{mode}\0{len(data)}\0{file_digest}\n".encode("utf-8")
        )
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect")
    args = parser.parse_args()
    root = pathlib.Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    identity = tree_identity(root)
    if args.expect is not None and identity != args.expect:
        raise SystemExit(
            f"reviewed tree identity mismatch: expected {args.expect}, got {identity}"
        )
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
