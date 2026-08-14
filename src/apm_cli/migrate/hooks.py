"""Discover and stage a repository's hooks.

Hooks are a third primitive kind alongside skills and agents, and the one that
can ship a *credential*: a hook script often holds a token for the service it
talks to.  Scrubbing cannot help there -- a token is not a known literal in any
rules file -- so a script the caller marks restricted is **excluded**, not
rewritten.

A descriptor that wires a withheld script is withheld too: shipping the wiring
without its target produces a hook command pointing at a file that was never
staged.

Layout is what APM's exporter already understands (verified against
``_plugin_rel_for_deployed_path`` and an end-to-end pack)::

    .claude/hooks/<file>   ->  hooks/<file>     (exec bit preserved)
    .claude/hooks.json     ->  hooks.json
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .filters import should_exclude

#: Where hooks live in a Claude Code repository.
HOOKS_DIR = Path(".claude") / "hooks"
HOOKS_DESCRIPTOR = Path(".claude") / "hooks.json"


class HookStagingError(Exception):
    """Raised when hooks cannot be staged safely."""


@dataclass(frozen=True)
class StagedHook:
    """One hook file copied into the staging tree."""

    name: str
    #: Path relative to the staging root, POSIX-style -- an ``includes:`` entry.
    rel_path: str


def discover_hooks(source_root: Path) -> tuple[StagedHook, ...]:
    """List hook files under *source_root*, excluding junk and runtime output."""
    found: list[StagedHook] = []

    hooks_dir = source_root / HOOKS_DIR
    if hooks_dir.is_dir():
        for entry in sorted(hooks_dir.iterdir()):
            if not entry.is_file() or entry.is_symlink():
                continue
            if should_exclude(entry):
                continue
            found.append(
                StagedHook(name=entry.name, rel_path=f"{HOOKS_DIR.as_posix()}/{entry.name}")
            )

    descriptor = source_root / HOOKS_DESCRIPTOR
    if descriptor.is_file():
        found.append(StagedHook(name=descriptor.name, rel_path=HOOKS_DESCRIPTOR.as_posix()))

    return tuple(found)


def _descriptor_references(descriptor: Path, names: tuple[str, ...]) -> bool:
    """True when *descriptor* mentions any of *names* in a command."""
    try:
        raw = descriptor.read_text(encoding="utf-8")
    except OSError as exc:
        raise HookStagingError(f"cannot read {descriptor}: {exc}") from exc
    try:
        json.loads(raw)
    except ValueError as exc:
        # `apm pack` force-parses this file; a malformed descriptor would fail
        # the pack with a less useful message, so reject it here.
        raise HookStagingError(f"{descriptor} is not valid JSON: {exc}") from exc
    return any(name in raw for name in names)


def stage_hooks(
    source_root: Path,
    out_dir: Path,
    *,
    restricted: tuple[str, ...],
    ceiling: str,
    ceilings: tuple[str, ...],
) -> tuple[StagedHook, ...]:
    """Copy hooks into *out_dir*, withholding restricted ones below the top ceiling.

    Returns the hooks actually staged, as ``includes:`` entries.
    """
    is_top = ceiling == ceilings[-1]
    discovered = discover_hooks(source_root)
    if not discovered:
        return ()

    withheld = () if is_top else tuple(restricted)

    staged: list[StagedHook] = []
    for hook in discovered:
        if hook.name in withheld:
            continue

        src = source_root / hook.rel_path
        if hook.name == HOOKS_DESCRIPTOR.name and withheld:
            # Wiring without its target is worse than no wiring at all.
            if _descriptor_references(src, withheld):
                continue
        elif hook.name == HOOKS_DESCRIPTOR.name:
            _descriptor_references(src, ())  # validates JSON, ignores the result

        dest = out_dir / hook.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)  # copy2 preserves the exec bit
        staged.append(hook)

    return tuple(staged)
