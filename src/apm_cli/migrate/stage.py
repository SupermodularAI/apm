"""Stage a per-ceiling tree and emit the ``apm.yml`` that packs it.

``includes:`` selects *paths*; nothing in the manifest schema can rewrite
*bytes*.  So audience filtering and identifier scrubbing both happen here, and
``apm pack`` then runs over an already-clean tree needing no further
filtering.

Staging always copies.  The source repository is never modified.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .filters import copytree_ignore, should_exclude
from .scrub import ScrubRules, scrub_tree

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .classification import Classification

#: Directory names that trigger APM's target auto-detection.  Staging into one
#: changes how the surrounding repository resolves targets -- verified: adding
#: ``.claude/`` to a checkout makes APM report "Multiple harnesses detected".
HARNESS_DIR_NAMES = frozenset({".claude", ".cursor", ".codex", ".opencode", ".github"})


class StagingError(Exception):
    """Raised when staging or emission cannot proceed safely."""


@dataclass(frozen=True)
class StagedPrimitive:
    """One primitive copied into the staging tree."""

    name: str
    domain: str
    #: Path relative to the staging root, POSIX-style -- an ``includes:`` entry.
    rel_path: str


def _audience_rank(audience: str, ceilings: tuple[str, ...]) -> int:
    return ceilings.index(audience)


def _assert_safe_destination(out_dir: Path) -> None:
    if out_dir.name in HARNESS_DIR_NAMES:
        raise StagingError(
            f"refusing to stage into '{out_dir.name}/': it is a harness directory and "
            "would corrupt APM's target auto-detection in the surrounding repository"
        )


def stage_ceiling(
    source_root: Path,
    out_dir: Path,
    *,
    classification: Classification,
    ceiling: str,
    rules: ScrubRules | None,
    ceilings: tuple[str, ...] = ("public", "company", "personal"),
    require_rules: bool = False,
    skip_missing: bool = False,
) -> tuple[StagedPrimitive, ...]:
    """Copy everything at or below *ceiling* into *out_dir*, scrubbed.

    Confidential primitives are excluded from every ceiling, unconditionally --
    their only legitimate home is a standalone package.

    Scrubbing is skipped at the top ceiling (self-rehydration keeps real
    values).  Below it, *require_rules* makes a missing rules file an error
    rather than a silent unscrubbed copy.
    """
    _assert_safe_destination(out_dir)

    if ceiling not in ceilings:
        raise StagingError(f"unknown ceiling '{ceiling}'")

    is_top = ceiling == ceilings[-1]
    if require_rules and not is_top and (rules is None or rules.is_empty):
        raise StagingError(
            f"refusing to stage ceiling '{ceiling}' without scrub rules: shareable "
            "output would carry identifiers verbatim. Pass --rules, or stage only "
            f"the '{ceilings[-1]}' ceiling."
        )

    limit = _audience_rank(ceiling, ceilings)

    selected = [
        p
        for p in classification.primitives.values()
        if not p.confidential and _audience_rank(p.audience, ceilings) <= limit
    ]

    # Rebuild from scratch so a re-run never leaves content from a previous,
    # broader ceiling behind.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    staged: list[StagedPrimitive] = []
    unresolved: list[str] = []
    for prim in sorted(selected, key=lambda p: p.name):
        rel = _source_rel_path(source_root, prim.name)
        if rel is None:
            # Classified but not on disk -- e.g. tracked in git and deleted in
            # the working tree. Silently skipping makes the staged tree quietly
            # smaller than the classification claims, so this fails closed.
            unresolved.append(prim.name)
            continue
        src = source_root / rel
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # copy2-based: preserves mode, so a hook script keeps its exec bit.
            # `ignore` prunes junk and runtime output (logs/, state/, *.log) --
            # see filters.py for why a .log leak is not merely untidy.
            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=copytree_ignore)
        elif not should_exclude(src):
            shutil.copy2(src, dest)
        else:
            continue
        staged.append(StagedPrimitive(name=prim.name, domain=prim.domain, rel_path=rel))

    if unresolved and not skip_missing:
        raise StagingError(
            f"{len(unresolved)} classified primitive(s) could not be found on disk: "
            f"{', '.join(sorted(unresolved))}. They may be tracked in git but deleted "
            "in the working tree. Restore them, re-classify, or pass --skip-missing to "
            "stage without them."
        )

    if rules is not None and not is_top:
        scrub_tree(out_dir, rules)

    return tuple(staged)


def _source_rel_path(source_root: Path, name: str) -> str | None:
    """Locate *name* under the conventional primitive roots."""
    from .enumerate import KIND_BY_DIR, PRIMITIVE_ROOTS

    for root in PRIMITIVE_ROOTS:
        for kind_dir in KIND_BY_DIR:
            candidate = source_root / root / kind_dir / name
            if candidate.is_dir():
                return f"{root}/{kind_dir}/{name}"
            parent = source_root / root / kind_dir
            if parent.is_dir():
                for entry in sorted(parent.iterdir()):
                    if entry.is_file() and entry.name.split(".")[0] == name:
                        return f"{root}/{kind_dir}/{entry.name}"
    return None


def _yaml_quote(value: str) -> str:
    """Quote a scalar for YAML output.

    Hand-rolled rather than delegated to a dumper: emission must be
    byte-predictable, and PyYAML re-quotes and re-wraps according to its own
    rules.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def emit_manifest(
    out_dir: Path,
    *,
    staged: tuple[StagedPrimitive, ...],
    name: str,
    version: str,
    description: str | None = None,
) -> Path:
    """Write ``out_dir/apm.yml`` listing *staged* under ``includes:``.

    Emitted by explicit string building so the bytes are stable across runs and
    diffable against a golden fixture.
    """
    if not staged:
        raise StagingError("refusing to emit a manifest with no includes: it would pack nothing")

    lines = [
        f"name: {_yaml_quote(name)}",
        f"version: {_yaml_quote(version)}",
    ]
    if description:
        lines.append(f"description: {_yaml_quote(description)}")
    lines.append("includes:")
    for rel in sorted({s.rel_path for s in staged}):
        lines.append(f"  - {_yaml_quote(rel)}")

    path = out_dir / "apm.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
