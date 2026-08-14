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

import json
from pathlib import Path

import click

#: Audience ceilings, ordered from most to least restrictive.  A build at a
#: given ceiling contains everything at or below it.
CEILINGS = ("public", "company", "personal")


#: Runtimes the classification pass can be dispatched to.  Resolved from APM's
#: own registry rather than hardcoded, so a runtime added upstream works here
#: with no change.  (Note: "claude" is NOT an APM runtime -- use
#: --classification to consume a response produced elsewhere.)
def _agent_choices() -> tuple[str, ...]:
    try:
        from ..runtime.registry import adapter_descriptors

        return tuple(d.name for d in adapter_descriptors())
    except Exception:  # pragma: no cover - registry import is not optional in practice
        return ("copilot", "codex", "llm")


AGENTS = _agent_choices()

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
    default=AGENTS[0] if AGENTS else None,
    show_default=True,
    help="APM runtime used to classify the primitives.",
)
@click.option(
    "--max-repairs",
    type=click.IntRange(0, 5),
    default=2,
    show_default=True,
    help="How many times to re-prompt with the defects before giving up.",
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
    "--classification",
    "classification_file",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    default=None,
    help="Agent classification response to consume instead of dispatching.",
)
@click.option(
    "--require-rules/--allow-unscrubbed",
    "require_rules",
    default=True,
    show_default=True,
    help=(
        "Refuse to stage a shareable ceiling without --rules. Disable only when "
        "the source repository is known to contain no identifiers."
    ),
)
@click.option(
    "--restricted-hook",
    "restricted_hooks",
    multiple=True,
    help=(
        "Hook script that must not ship below the top ceiling (repeatable). "
        "Use for scripts holding a credential -- scrubbing cannot redact a token."
    ),
)
@click.option(
    "--skip-missing",
    is_flag=True,
    default=False,
    help=(
        "Stage without primitives that are classified but absent from the working "
        "tree, instead of failing. Use for a repo mid-refactor."
    ),
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
    classification_file: str | None,
    max_repairs: int,
    restricted_hooks: tuple[str, ...],
    require_rules: bool,
    skip_missing: bool,
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

    from ..migrate.classification import ClassificationError, parse_classification
    from ..migrate.hooks import HookStagingError, stage_hooks
    from ..migrate.scrub import RulesError, load_rules
    from ..migrate.stage import StagingError, emit_manifest, stage_ceiling

    rules = None
    if rules_file is not None:
        try:
            rules = load_rules(Path(rules_file))
        except RulesError as exc:
            _rich_error(str(exc), symbol="error")
            raise SystemExit(1) from exc

    if classification_file is not None:
        # A response captured earlier (or produced by hand). Skips dispatch
        # entirely, which keeps a re-run reproducible and costs no tokens.
        try:
            classification = parse_classification(
                Path(classification_file).read_text(encoding="utf-8"),
                primitives=result.primitives,
                ceilings=CEILINGS,
            )
        except ClassificationError as exc:
            _rich_error(str(exc), symbol="error")
            raise SystemExit(1) from exc
        except OSError as exc:
            _rich_error(f"cannot read {classification_file}: {exc}", symbol="error")
            raise SystemExit(1) from exc
    else:
        from ..migrate.dispatch import DispatchError, classify_with_runtime, resolve_runtime

        def _announce(attempt: int, budget: int) -> None:
            if attempt == 0:
                _rich_info(
                    f"Classifying {len(result.primitives)} primitive(s) via '{agent}'...",
                    symbol="info",
                )
            else:
                _rich_info(
                    f"Response rejected; re-prompting with the defects ({attempt}/{budget}).",
                    symbol="warning",
                )

        try:
            runtime = resolve_runtime(agent)
            classification = classify_with_runtime(
                runtime,
                primitives=result.primitives,
                ceilings=CEILINGS,
                project_root=project_root,
                max_repairs=max_repairs,
                on_attempt=_announce,
            )
        except DispatchError as exc:
            _rich_error(str(exc), symbol="error")
            raise SystemExit(1) from exc
        except ClassificationError as exc:
            _rich_error(
                f"{exc}\n\nThe runtime could not produce a usable classification after "
                f"{max_repairs} repair attempt(s). Review the prompt with --dry-run, or "
                "supply a response with --classification.",
                symbol="error",
            )
            raise SystemExit(1) from exc

        # Classification carries confidentiality and PII consequences, so the
        # proposal is written out for review rather than only held in memory.
        proposal = staging_root / "classification.json"
        staging_root.mkdir(parents=True, exist_ok=True)
        proposal.write_text(
            json.dumps(
                {
                    "domains": {
                        n: {"description": d.description} for n, d in classification.domains.items()
                    },
                    "primitives": {
                        n: {
                            "domain": p.domain,
                            "audience": p.audience,
                            "confidential": p.confidential,
                            "identifiers": list(p.identifiers),
                        }
                        for n, p in classification.primitives.items()
                    },
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        _rich_info(f"Wrote the classification proposal to {proposal}.", symbol="info")

        proposed_ids = sorted(
            {i for p in classification.primitives.values() for i in p.identifiers}
        )
        if proposed_ids:
            _rich_info(
                f"{len(proposed_ids)} candidate identifier(s) proposed for review — "
                "these are NOT applied automatically. Add the ones you confirm to a "
                "--rules file: " + ", ".join(proposed_ids[:8]),
                symbol="warning",
            )

    project_name = project_root.name or "migrated-project"
    for ceiling in selected:
        ceiling_dir = staging_root / ceiling
        try:
            staged = stage_ceiling(
                project_root,
                ceiling_dir,
                classification=classification,
                ceiling=ceiling,
                rules=rules,
                ceilings=CEILINGS,
                require_rules=require_rules,
                skip_missing=skip_missing,
            )
            staged_hooks = stage_hooks(
                project_root,
                ceiling_dir,
                restricted=restricted_hooks,
                ceiling=ceiling,
                ceilings=CEILINGS,
            )
            if not staged and not staged_hooks:
                _rich_info(
                    f"ceiling '{ceiling}': nothing at or below this audience -- skipped.",
                    symbol="warning",
                )
                continue
            emit_manifest(
                ceiling_dir,
                staged=staged,
                extra_includes=tuple(h.rel_path for h in staged_hooks),
                name=f"{project_name}-{ceiling}",
                version="0.1.0",
            )
        except (StagingError, HookStagingError) as exc:
            _rich_error(str(exc), symbol="error")
            raise SystemExit(1) from exc

        hook_note = f" + {len(staged_hooks)} hook file(s)" if staged_hooks else ""
        _rich_echo(
            f"  {ceiling}: staged {len(staged)} primitive(s){hook_note} -> {ceiling_dir}",
            symbol="list",
        )

    _rich_info(
        f"Wrote per-ceiling manifests under {staging_root}. "
        f"Run 'apm pack' inside a ceiling directory to produce its artifacts.",
        symbol="info",
    )


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
    from ..utils.console import _rich_error, _rich_info

    project_root = Path(path)

    if manifest_path:
        candidate = Path(manifest_path)
    else:
        # PATH may be either the source repo (manifests live under DEFAULT_OUT)
        # or a staging directory passed directly. Accept both -- appending
        # DEFAULT_OUT to a staging root would look for `staged/.apm-migrate`.
        default_root = project_root / DEFAULT_OUT
        candidate = default_root if default_root.exists() else project_root

    if not candidate.exists():
        _rich_error(
            f"No manifest found at {candidate}. Run 'apm migrate init' first, "
            "or pass --manifest explicitly.",
            symbol="error",
        )
        raise SystemExit(1)

    from ..migrate.verify import ManifestDefect, verify_staged_manifests

    manifests = [candidate] if candidate.is_file() else sorted(candidate.rglob("apm.yml"))
    if not manifests:
        _rich_error(
            f"No apm.yml manifest found under {candidate}. Run 'apm migrate init' first.",
            symbol="error",
        )
        raise SystemExit(1)

    defects: list[ManifestDefect] = verify_staged_manifests(manifests)
    if defects:
        for defect in defects:
            _rich_error(f"{defect.manifest}: {defect.message}", symbol="error")
        raise SystemExit(1)

    _rich_info(
        f"Validated {len(manifests)} manifest(s) under {candidate}.",
        symbol="info",
    )
