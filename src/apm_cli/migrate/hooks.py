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
import re
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

    # A repo whose wiring lives in settings.json ships no hooks.json of its
    # own. Derive one, or the scripts above are delivered and never fire.
    if not any(h.name == HOOKS_DESCRIPTOR.name for h in staged):
        derived = derive_descriptor(source_root, staged_names=tuple(h.name for h in staged))
        if derived is not None:
            dest = out_dir / DERIVED_DESCRIPTOR_PATH
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(derived, indent=2) + "\n", encoding="utf-8")
            staged.append(
                StagedHook(
                    name=DERIVED_DESCRIPTOR_PATH.name,
                    rel_path=DERIVED_DESCRIPTOR_PATH.as_posix(),
                )
            )

    return tuple(staged)


#: Where a Claude Code repository keeps its hook wiring.
SETTINGS_PATH = Path(".claude") / "settings.json"

#: The placeholder a plugin uses for its own installed root.
# S105 suppressed: a path placeholder substituted by the runtime, not a secret.
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"  # noqa: S105

#: Where a DERIVED descriptor is staged.  Deliberately inside the hooks dir:
#: APM maps ``.claude/hooks.json`` to the bundle ROOT but
#: ``.claude/hooks/hooks.json`` to ``hooks/hooks.json``, and Claude Code's
#: plugin channel reads the latter.  Root placement installs cleanly and
#: registers nothing -- verified against a real install.
DERIVED_DESCRIPTOR_PATH = HOOKS_DIR / "hooks.json"


def _referenced_script(command: str, staged_names: tuple[str, ...]) -> str | None:
    """Return the staged script *command* invokes, if any.

    Matched by basename rather than by parsing the command line: a hook command
    is an arbitrary shell string, and the only part that has to survive the
    rewrite is which staged file it points at.
    """
    for name in staged_names:
        if name in command:
            return name
    return None


def derive_descriptor(
    source_root: Path,
    *,
    staged_names: tuple[str, ...],
) -> dict | None:
    """Build a plugin ``hooks.json`` from the repo's ``.claude/settings.json``.

    A hook script without wiring is delivered but never fires, so staging the
    script alone is not enough.  Settings hold the wiring with
    ``$CLAUDE_PROJECT_DIR`` paths; a plugin needs ``${CLAUDE_PLUGIN_ROOT}``
    paths pointing at the bundle's own ``hooks/`` directory.

    Entries invoking a script that is **not** in *staged_names* are dropped --
    the credential rule again: wiring without its target leaves a hook command
    pointing at a file that was never staged.

    Returns ``None`` when nothing survives, so the caller emits no descriptor
    rather than an empty one.
    """
    settings = source_root / SETTINGS_PATH
    if not settings.is_file():
        return None

    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Settings are the consumer's file, not ours -- a malformed one is not
        # this command's failure to report.
        return None

    raw_hooks = payload.get("hooks")
    if not isinstance(raw_hooks, dict):
        return None

    derived: dict[str, list] = {}
    for event, groups in raw_hooks.items():
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            kept = []
            for hook in group.get("hooks", []) or []:
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                script = _referenced_script(command, staged_names)
                if script is None:
                    continue
                # Replace the whole path, keeping the interpreter prefix.
                rewritten = re.sub(
                    r"\S*" + re.escape(script),
                    f'"{PLUGIN_ROOT_TOKEN}/{HOOKS_DIR.name}/{script}"',
                    command,
                )
                kept.append({**hook, "command": rewritten})
            if kept:
                new_group = {"hooks": kept}
                if group.get("matcher"):
                    new_group = {"matcher": group["matcher"], "hooks": kept}
                kept_groups.append(new_group)
        if kept_groups:
            derived[event] = kept_groups

    if not derived:
        return None
    return {"hooks": derived}
