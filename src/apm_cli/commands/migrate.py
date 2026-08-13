"""``apm migrate`` -- bring an existing repository of primitives into APM.

A Click *group*, mirroring ``apm plugin init`` / ``apm marketplace init``:

* ``apm migrate init``  -- classify a repo's primitives, stage them, and write
  the ``apm.yml`` manifest(s) that ``apm pack`` consumes.
* ``apm migrate check`` -- validate an existing classification.

**Disambiguation.** APM already uses "migrate" internally for *format*
upgrades (``migrate_lockfile_if_needed``, ``migrate_marketplace_yml``).  This
command is about *adoption*: a repository that is not yet an APM project
becoming one.  ``apm init`` covers the greenfield case; this covers a tree that
already holds skills and agents.

Why a manifest producer rather than a new packer: ``apm pack`` is already
declarative and owns emission.  What it has no concept of is *which* primitives
belong in a given package, so this command produces that decision as an
``includes:`` list and leaves emission untouched.
"""

from __future__ import annotations

from pathlib import Path

import click

#: Audience ceilings, ordered from most to least restrictive.  A build at a
#: given ceiling contains everything at or below it.
CEILINGS = ("public", "company", "personal")

#: Agent runtimes the classification pass can be dispatched to.
AGENTS = ("claude", "codex", "copilot", "opencode")

#: Directory names that make APM's target auto-detection fire.  Staging into
#: one of these corrupts the very repo the output is generated for -- verified:
#: adding ``.claude/`` to a checkout makes APM report "Multiple harnesses
#: detected" and silently changes install behaviour.
HARNESS_DIR_NAMES = frozenset({".claude", ".cursor", ".codex", ".opencode", ".github"})

#: Default staging root, relative to the target repo.  Deliberately *not* a
#: harness-named directory (see ``HARNESS_DIR_NAMES``).
DEFAULT_OUT = ".apm-migrate"


@click.group(
    invoke_without_command=True,
    help=(
        "Bring an existing repository of skills and agents into APM: classify "
        "each primitive, stage it, and write the apm.yml manifests that "
        "'apm pack' consumes. Use 'apm init' for a new, empty project."
    ),
)
@click.pass_context
def migrate(ctx: click.Context) -> None:
    """Adopt an existing repository of primitives into APM."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@migrate.command(
    "init",
    help=(
        "Classify an existing repository's primitives and write per-ceiling "
        "apm.yml manifests plus a staged tree for 'apm pack'."
    ),
)
@click.argument(
    "path",
    required=False,
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--ceiling",
    "ceilings",
    multiple=True,
    type=click.Choice(CEILINGS),
    help=f"Audience ceiling to emit (repeatable). Default: all of {', '.join(CEILINGS)}.",
)
@click.option(
    "--agent",
    type=click.Choice(AGENTS),
    default="claude",
    show_default=True,
    help="Agent runtime used to classify the primitives.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=None,
    help=f"Staging directory for the scrubbed tree and manifests (default: ./{DEFAULT_OUT}).",
)
@click.option(
    "--rules",
    "rules_file",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    default=None,
    help="Identifier/PII rules to apply while staging. Never bundled -- always supplied.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the classification prompt and exit without writing anything.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip interactive confirmation (matches 'apm init -y').",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output.")
def init(
    path: str,
    ceilings: tuple[str, ...],
    agent: str,
    out_dir: str | None,
    rules_file: str | None,
    dry_run: bool,
    yes: bool,
    verbose: bool,
) -> None:
    """Classify an existing repo's primitives and emit APM manifests."""
    from ..migrate.enumerate import EnumerationResult, enumerate_primitives
    from ..migrate.prompt import render_classification_prompt
    from ..utils.console import _rich_echo, _rich_error, _rich_info

    project_root = Path(path)
    staging_root = Path(out_dir) if out_dir else project_root / DEFAULT_OUT

    # Refuse before doing any work: staging into a harness directory would
    # corrupt target auto-detection in the repo we are generating output for.
    if staging_root.name in HARNESS_DIR_NAMES:
        _rich_error(
            f"Refusing to stage into '{staging_root.name}/': it is a harness directory "
            "and would corrupt APM's target auto-detection in this repo. "
            f"Use a neutral path such as ./{DEFAULT_OUT}.",
            symbol="error",
        )
        raise SystemExit(1)

    result: EnumerationResult = enumerate_primitives(project_root)

    if not result.primitives:
        _rich_error(
            f"No primitives found under {project_root}. Expected skills or agents in "
            "'.apm/', '.claude/', or a conventional 'skills/' | 'agents/' directory. "
            "Nothing to migrate.",
            symbol="error",
        )
        raise SystemExit(1)

    if not result.from_git:
        # The "untracked WIP is exempt" guarantee cannot hold here, and the
        # operator has to know that -- silence would imply a rule that is not
        # actually in force.
        _rich_info(
            f"{project_root} is not a git repo -- enumerated {len(result.primitives)} "
            "primitive(s) by walking the filesystem. Untracked work-in-progress "
            "content cannot be distinguished from released content.",
            symbol="warning",
        )

    selected = tuple(ceilings) if ceilings else CEILINGS

    if verbose:
        _rich_info(
            f"Found {len(result.primitives)} primitive(s); ceilings: {', '.join(selected)}.",
            symbol="info",
        )

    prompt = render_classification_prompt(
        primitives=result.primitives,
        ceilings=selected,
        project_root=project_root,
    )

    if dry_run:
        _rich_info(
            f"Dry run -- would dispatch to '{agent}' and stage into {staging_root}.",
            symbol="info",
        )
        click.echo()
        click.echo(prompt)
        return

    # Phase 2 wires dispatch -> validate -> repair -> stage -> emit here.
    _rich_echo(
        f"Enumerated {len(result.primitives)} primitive(s) for ceilings {', '.join(selected)}.",
        symbol="list",
    )
    _rich_error(
        "Classification is not implemented yet (Phase 2). "
        "Re-run with --dry-run to review the prompt that will be dispatched.",
        symbol="error",
    )
    raise SystemExit(1)


@migrate.command(
    "check",
    help=(
        "Validate an existing classification: every primitive in the repo is "
        "classified exactly once, and every emitted manifest is well-formed."
    ),
)
@click.argument(
    "path",
    required=False,
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=None,
    help=f"Manifest to validate (default: discovered under ./{DEFAULT_OUT}).",
)
def check(path: str, manifest_path: str | None) -> None:
    """Validate a classification produced by ``apm migrate init``."""
    from ..utils.console import _rich_error

    project_root = Path(path)
    candidate = Path(manifest_path) if manifest_path else project_root / DEFAULT_OUT

    if not candidate.exists():
        _rich_error(
            f"No manifest found at {candidate}. Run 'apm migrate init' first, "
            "or pass --manifest explicitly.",
            symbol="error",
        )
        raise SystemExit(1)

    # Phase 2 implements the completeness + shape assertions.
    _rich_error(
        "Validation is not implemented yet (Phase 2).",
        symbol="error",
    )
    raise SystemExit(1)
