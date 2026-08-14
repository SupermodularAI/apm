"""Paths that must never be copied into a staged tree.

Two families:

* **Junk** -- editor/OS debris (``.DS_Store``, ``*.bak``, ``*.orig``).  A
  ``.bak`` matters beyond tidiness: it can hold a pre-scrub copy of a file
  whose live version was redacted, so shipping one leaks exactly what the
  scrub removed.
* **Runtime artifacts** -- anything under a ``logs/`` or ``state/`` directory,
  plus stray ``*.log`` files.  These are output, not source.  Verified against
  a real repository: a skill's ``logs/`` held dispatcher logs containing the
  owner's Slack ID, which no scrub would have caught because ``.log`` is not a
  rewritable text extension.

One exception: a ``.gitignore`` living *directly* inside ``logs/`` or
``state/`` is tracked source -- it exists so git keeps the otherwise-empty
runtime directory present -- and is kept.
"""

from __future__ import annotations

from pathlib import Path

JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db"})
JUNK_SUFFIXES = (".bak", ".orig", ".rej", ".swp", ".swo")

RUNTIME_DIR_NAMES = frozenset({"logs", "state"})

#: Directory whose ``*.json`` files are live scheduler config -- real schedules,
#: repositories and recipients -- rather than source.  The ``.json.example``
#: template beside them IS source and is kept.
LIVE_CONFIG_DIR = "crons"


def is_junk(path: Path) -> bool:
    """True when *path* is editor/OS debris or a stale backup copy."""
    name = path.name
    if name in JUNK_NAMES:
        return True
    if name.endswith("~"):
        return True
    return name.endswith(JUNK_SUFFIXES)


def is_live_config(path: Path) -> bool:
    """True for a live ``crons/*.json`` (but not its ``.json.example``)."""
    if path.parent.name != LIVE_CONFIG_DIR:
        return False
    return path.name.endswith(".json") and not path.name.endswith(".json.example")


def is_tracked_runtime_placeholder(path: Path) -> bool:
    """True for a ``.gitignore`` directly inside a runtime directory."""
    return path.name == ".gitignore" and path.parent.name in RUNTIME_DIR_NAMES


def is_runtime_artifact(path: Path, *, relative_to: Path | None = None) -> bool:
    """True when *path* is runtime output rather than source.

    Matches a ``logs``/``state`` directory itself, anything beneath one at any
    depth, and stray ``*.log`` files.
    """
    if is_tracked_runtime_placeholder(path):
        return False

    if path.name in RUNTIME_DIR_NAMES:
        return True
    if path.suffix == ".log":
        return True

    # Any ancestor segment named logs/state -- a `*.last-run` file with no
    # distinctive name of its own must still be pruned.
    parent = path.parent
    stop = relative_to.resolve() if relative_to else None
    while True:
        if stop is not None and parent.resolve() == stop:
            break
        if parent.name in RUNTIME_DIR_NAMES:
            return True
        if parent == parent.parent:
            break
        parent = parent.parent
    return False


def should_exclude(path: Path, *, relative_to: Path | None = None) -> bool:
    """True when *path* must not be staged."""
    return (
        is_junk(path) or is_live_config(path) or is_runtime_artifact(path, relative_to=relative_to)
    )


def copytree_ignore(src: str, names: list[str]) -> set[str]:
    """``shutil.copytree(ignore=...)`` callback applying both filters.

    A ``logs``/``state`` directory entry is deliberately *not* returned here:
    ignoring the directory would drop its tracked ``.gitignore`` placeholder
    too.  Its contents are pruned individually instead.
    """
    src_path = Path(src)
    ignored: set[str] = set()
    for name in names:
        candidate = src_path / name
        if is_junk(candidate) or is_live_config(candidate):
            ignored.add(name)
            continue
        if candidate.is_dir() and name in RUNTIME_DIR_NAMES:
            # Keep the directory so the placeholder survives; its real contents
            # are filtered on the recursive call.
            continue
        if is_tracked_runtime_placeholder(candidate):
            continue
        if src_path.name in RUNTIME_DIR_NAMES or candidate.suffix == ".log":
            ignored.add(name)
    return ignored
