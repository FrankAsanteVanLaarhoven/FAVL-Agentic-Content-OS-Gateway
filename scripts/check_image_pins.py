#!/usr/bin/env python3
"""Fail if any container image is referenced without a digest.

A tag is mutable. `postgres:17-alpine` today and `postgres:17-alpine` next
month can be different bytes, which means the stack that passed CI is not
necessarily the stack that runs — and a compromised upstream tag is pulled
silently. A digest cannot change.

Locally-built images (those with a `build:` stanza) are exempt: they are built
from this repository, not pulled.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMPOSE = ROOT / "deploy" / "docker-compose.yml"
DOCKERFILES = sorted(ROOT.glob("services/*/Dockerfile")) + sorted(
    ROOT.glob("apps/*/Dockerfile")
)

COMPOSE_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)
DOCKERFILE_FROM = re.compile(r"^\s*FROM\s+(\S+)", re.MULTILINE | re.IGNORECASE)


# Multi-stage builds refer to earlier stages by name, which have no digest.
def _stage_names(text: str) -> set[str]:
    return set(re.findall(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", text, re.MULTILINE | re.I))


def unpinned() -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []

    if COMPOSE.exists():
        for match in COMPOSE_IMAGE.finditer(COMPOSE.read_text(encoding="utf-8")):
            ref = match.group(1)
            if "@sha256:" not in ref:
                line = COMPOSE.read_text(encoding="utf-8")[: match.start()].count("\n")
                problems.append((f"{COMPOSE.relative_to(ROOT)}:{line + 1}", ref))

    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        stages = _stage_names(text)
        for match in DOCKERFILE_FROM.finditer(text):
            ref = match.group(1)
            if ref in stages or "@sha256:" in ref:
                continue
            line = text[: match.start()].count("\n")
            problems.append((f"{dockerfile.relative_to(ROOT)}:{line + 1}", ref))

    return problems


def main() -> int:
    problems = unpinned()
    if problems:
        print("Container images referenced without a digest:\n")
        for location, ref in problems:
            print(f"  {location}: {ref}")
        print(
            "\nPin with tag@sha256:... so the running stack is the stack that "
            "was tested. Refresh with scripts/update_image_digests.sh."
        )
        return 1
    print("All container images are pinned by digest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
