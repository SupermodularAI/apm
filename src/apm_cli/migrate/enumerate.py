"""Discover the primitives a repository already contains.

Git is the source of truth when the target is a git repository: a *tracked*
skill must be classified, but an untracked work-in-progress skill is
legitimately not part of any release and must not be swept into a manifest.
When the target is not a git repo (a tarball, an export), that distinction
cannot be made -- the caller is told so explicitly rather than being given a
guarantee that is not in force.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Directories that conventionally hold primitives, in preference order.  ``.apm``
#: is APM's own layout; the rest are harness directories a repo may already use.
PRIMITIVE_ROOTS = (".apm", ".claude", ".agents", ".github", ".cursor")

#: Sub-directory -> primitive kind.
KIND_BY_DIR = {
    "skills": "skill",
    "agents": "agent",
    "commands": "command",
    "prompts": "command",
}


@dataclass(frozen=True)
class Primitive:
    """One discovered primitive."""

    name: str
    kind: str
    #: Path to the primitive's root, relative to the project root, POSIX-style.
    rel_path: str


@dataclass(frozen=True)
class EnumerationResult:
    """What was found, and how."""

    primitives: tuple[Primitive, ...]
    #: True when the listing came from ``git ls-files`` rather than a walk.
    from_git: bool


def _git_tracked_files(project_root: Path) -> list[str] | None:
    """Return tracked paths, or ``None`` when *project_root* is not a git repo."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=project_root,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    # A git repo with nothing committed yet is still a git repo -- but it gives
    # us no listing to work from, so treat it as a filesystem case.
    paths = [p for p in proc.stdout.split("\0") if p]
    return paths or None


def _classify_path(rel_parts: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Map a repo-relative path to ``(name, kind, rel_path)``.

    Recognises ``<root>/<kind-dir>/<name>/...`` (a skill directory) and
    ``<root>/<kind-dir>/<name>.<ext>`` (a single-file agent or command).
    Returns ``None`` for anything that is not a primitive.
    """
    if len(rel_parts) < 3:
        return None
    root, kind_dir, leaf, *rest = rel_parts
    if root not in PRIMITIVE_ROOTS:
        return None
    kind = KIND_BY_DIR.get(kind_dir)
    if kind is None:
        return None

    if rest:
        # <root>/<kind-dir>/<name>/... -- a directory-shaped primitive.
        return leaf, kind, f"{root}/{kind_dir}/{leaf}"

    # <root>/<kind-dir>/<file> -- strip compound suffixes (foo.agent.md -> foo).
    name = leaf
    for _ in range(2):
        stem, dot, _ext = name.rpartition(".")
        if not dot:
            break
        name = stem
    return name, kind, f"{root}/{kind_dir}/{leaf}"


def _from_paths(paths: list[str]) -> tuple[Primitive, ...]:
    """Fold a list of repo-relative paths into de-duplicated primitives."""
    seen: dict[tuple[str, str], Primitive] = {}
    for raw in paths:
        parts = tuple(Path(raw).as_posix().split("/"))
        hit = _classify_path(parts)
        if hit is None:
            continue
        name, kind, rel_path = hit
        seen.setdefault((kind, name), Primitive(name=name, kind=kind, rel_path=rel_path))
    return tuple(sorted(seen.values(), key=lambda p: (p.kind, p.name)))


def _walk(project_root: Path) -> list[str]:
    """List candidate files under the known primitive roots."""
    out: list[str] = []
    for root_name in PRIMITIVE_ROOTS:
        root = project_root / root_name
        if not root.is_dir():
            continue
        for entry in root.rglob("*"):
            if entry.is_file():
                out.append(entry.relative_to(project_root).as_posix())
    return out


def enumerate_primitives(project_root: Path) -> EnumerationResult:
    """Discover primitives under *project_root*.

    Prefers ``git ls-files`` so untracked work-in-progress content is excluded;
    falls back to a filesystem walk, reporting which path was taken via
    :attr:`EnumerationResult.from_git`.
    """
    tracked = _git_tracked_files(project_root)
    if tracked is not None:
        return EnumerationResult(primitives=_from_paths(tracked), from_git=True)
    return EnumerationResult(primitives=_from_paths(_walk(project_root)), from_git=False)
