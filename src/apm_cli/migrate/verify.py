"""Validate manifests emitted by ``apm migrate init``.

Checks the two properties that make a staged tree packable: every
``includes:`` entry resolves to something that exists, and the manifest
carries the fields ``apm pack`` requires.  A manifest that lists a path which
was never staged packs *nothing* for that entry, silently -- so this is the
guard against a half-written staging run reaching ``apm pack``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ManifestDefect:
    """One problem found in one manifest."""

    manifest: Path
    message: str


def _verify_one(path: Path) -> list[ManifestDefect]:
    defects: list[ManifestDefect] = []

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [ManifestDefect(manifest=path, message=f"cannot parse: {exc}")]

    if not isinstance(payload, dict):
        return [ManifestDefect(manifest=path, message="expected a YAML mapping")]

    for required in ("name", "version"):
        value = payload.get(required)
        if not isinstance(value, str) or not value:
            defects.append(ManifestDefect(manifest=path, message=f"missing required '{required}'"))

    includes = payload.get("includes")
    if includes is None:
        defects.append(ManifestDefect(manifest=path, message="no 'includes' -- would pack nothing"))
        return defects

    if includes == "auto":
        # Valid upstream, but never what `migrate init` emits: the whole point
        # is an explicit, audience-filtered list.
        defects.append(
            ManifestDefect(
                manifest=path,
                message="'includes: auto' defeats audience filtering; expected an explicit list",
            )
        )
        return defects

    if not isinstance(includes, list) or not includes:
        defects.append(ManifestDefect(manifest=path, message="'includes' must be a non-empty list"))
        return defects

    root = path.parent
    for entry in includes:
        if not isinstance(entry, str) or not entry:
            defects.append(
                ManifestDefect(manifest=path, message=f"invalid includes entry: {entry!r}")
            )
            continue
        if not (root / entry).exists():
            defects.append(
                ManifestDefect(
                    manifest=path,
                    message=f"includes entry does not exist in the staged tree: {entry}",
                )
            )

    return defects


def verify_staged_manifests(manifests: list[Path]) -> list[ManifestDefect]:
    """Verify each manifest, returning every defect found across all of them."""
    defects: list[ManifestDefect] = []
    for path in manifests:
        defects.extend(_verify_one(path))
    return defects
