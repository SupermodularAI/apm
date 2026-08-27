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
    for prim in sorted(selected, key=lambda p: (p.name, p.kind)):
        # Prefer the path the classification already resolved: a name can map to
        # more than one primitive (``resolve-vm-id`` is both a skill and an
        # agent), and re-resolving by name alone silently returns only the first.
        rel = prim.rel_path if prim.rel_path else None
        if rel is not None and not (source_root / rel).exists():
            # Classified against a path that has since gone: fall back to a
            # name search so a moved primitive still resolves, and let the
            # miss below fail closed if it genuinely is not on disk.
            rel = None
        if rel is None:
            rel = _source_rel_path(source_root, prim.name)
        if rel is None:
            # Classified but not on disk -- e.g. tracked in git and deleted in
            # the working tree. Silently skipping makes the staged tree quietly
            # smaller than the classification claims, so this fails closed.
            unresolved.append(prim.name)
            continue
        src = source_root / rel
        pkg_rel = _package_rel_path(rel)
        dest = out_dir / pkg_rel
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
        staged.append(
            StagedPrimitive(name=prim.name, domain=prim.domain, rel_path=pkg_rel)
        )

    if unresolved and not skip_missing:
        raise StagingError(
            f"{len(unresolved)} classified primitive(s) could not be found on disk: "
            f"{', '.join(sorted(unresolved))}. They may be tracked in git but deleted "
            "in the working tree. Restore them, re-classify, or pass --skip-missing to "
            "stage without them."
        )

    if rules is not None and not is_top:
        # Two ordered passes, SCOPED FIRST:
        #   1. each primitive with a scope, using its own rules;
        #   2. the global set over everything else.
        # The order matters and is not interchangeable. Running global first
        # would replace a literal with the redaction token, leaving the scoped
        # pass nothing to match -- the specific token would be unreachable.
        # Scoped-first is safe because replacement is idempotent: pass 2 finds
        # no remaining occurrence in an already-substituted file.
        for prim in staged:
            scoped = rules.for_primitive(prim.name)
            if scoped is not rules:
                scrub_tree(out_dir / prim.rel_path, scoped)

        scrub_tree(out_dir, rules)

    return tuple(staged)


def _package_rel_path(source_rel: str) -> str:
    """Map a source-repo path to its place in an APM **package**.

    ``apm install`` discovers skills at ``skills/`` or ``.apm/skills/`` and
    agents at ``.apm/agents/``; it never reads a harness directory such as
    ``.claude/``.  Staging the source layout verbatim therefore produced a
    manifest that packed cleanly and deployed nothing -- silently, because an
    unrecognised path is not an error, just not a primitive.

    Agents additionally need the ``.agent.md`` suffix: a bare ``.md`` under
    ``.apm/agents/`` is carried into ``apm_modules/`` but never deployed.
    """
    parts = source_rel.split("/")
    # <root>/<kind_dir>/<rest...> -- roots are .claude, .apm, .agents, ...
    if len(parts) < 3:
        return source_rel
    kind_dir, rest = parts[1], parts[2:]
    if kind_dir == "skills":
        return "/".join(["skills", *rest])
    if kind_dir == "agents":
        leaf = rest[-1]
        if leaf.endswith(".md") and not leaf.endswith(".agent.md"):
            rest = [*rest[:-1], leaf[: -len(".md")] + ".agent.md"]
        return "/".join([".apm", "agents", *rest])
    if kind_dir in ("commands", "prompts"):
        return "/".join([".apm", kind_dir, *rest])
    return source_rel


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
    extra_includes: tuple[str, ...] = (),
) -> Path:
    """Write ``out_dir/apm.yml`` listing *staged* under ``includes:``.

    Emitted by explicit string building so the bytes are stable across runs and
    diffable against a golden fixture.
    """
    if not staged and not extra_includes:
        raise StagingError("refusing to emit a manifest with no includes: it would pack nothing")

    lines = [
        f"name: {_yaml_quote(name)}",
        f"version: {_yaml_quote(version)}",
    ]
    if description:
        lines.append(f"description: {_yaml_quote(description)}")
    lines.append("includes:")
    for rel in sorted({s.rel_path for s in staged} | set(extra_includes)):
        lines.append(f"  - {_yaml_quote(rel)}")

    path = out_dir / "apm.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
