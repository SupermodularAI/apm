"""Tests for ``apm migrate`` -- bring an existing repo of primitives into APM.

Phase 1 scope: the command *surface* only.  ``init`` resolves its inputs,
enumerates the target's primitives and (under ``--dry-run``) prints the agent
prompt it would dispatch.  Classification, staging and manifest emission land
in Phase 2 and are not asserted here.

Naming note: ``migrate`` is deliberately a *group*, mirroring ``apm plugin
init`` / ``apm marketplace init``.  APM's internal use of "migrate" means
*auto-upgrade an old APM format* (``migrate_lockfile_if_needed``); the help
text disambiguates against that sense, and this file pins it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def _write_skill(root: Path, name: str, *, body: str = "Body.") -> Path:
    """Create a minimal, valid skill under ``.apm/skills/<name>/SKILL.md``."""
    skill_dir = root / ".apm" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_agent(root: Path, name: str) -> Path:
    """Create a minimal agent primitive under ``.apm/agents/``."""
    agents = root / ".apm" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / f"{name}.agent.md"
    path.write_text(f"# {name}\n", encoding="utf-8")
    return path


def _git_init(root: Path) -> None:
    """Make ``root`` a git repo with everything committed.

    ``migrate`` enumerates via ``git ls-files`` so an untracked work-in-progress
    primitive is not treated as part of the repo (the rule os-dist's
    ``assertAllSkillsClassified`` uses).  Tests that care about that distinction
    need a real repo, not a bare directory.
    """
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init", "--no-gpg-sign"],
        cwd=root,
        check=True,
        env={**env, "PATH": __import__("os").environ["PATH"]},
    )


class TestMigrateGroup:
    def test_group_is_registered(self, runner) -> None:
        """``apm migrate`` exists and lists its sub-commands."""
        result = runner.invoke(cli, ["migrate", "--help"])
        assert result.exit_code == 0, result.output
        assert "init" in result.output
        assert "check" in result.output

    def test_group_help_disambiguates_from_format_upgrade(self, runner) -> None:
        """Help must not read as 'upgrade an old APM format'.

        APM uses "migrate" internally for lockfile/marketplace format upgrades.
        The user-facing text has to say what this actually does, or the name is
        actively misleading.
        """
        result = runner.invoke(cli, ["migrate", "--help"])
        assert result.exit_code == 0, result.output
        lowered = result.output.lower()
        assert "existing" in lowered
        assert "apm.yml" in lowered

    def test_bare_group_shows_help_not_error(self, runner) -> None:
        """``apm migrate`` with no sub-command is a usage message, not a crash."""
        result = runner.invoke(cli, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "init" in result.output


class TestMigrateInitSurface:
    def test_dry_run_prints_prompt_and_writes_nothing(self, runner, tmp_path) -> None:
        """``--dry-run`` is reviewable without spending agent tokens."""
        _write_skill(tmp_path, "alpha")
        out_dir = tmp_path / "staged"

        result = runner.invoke(
            cli, ["migrate", "init", str(tmp_path), "--dry-run", "--out", str(out_dir)]
        )

        assert result.exit_code == 0, result.output
        assert "alpha" in result.output, "the prompt must name the primitives it found"
        assert not out_dir.exists(), "--dry-run must not create the staging tree"

    def test_dry_run_lists_every_enumerated_primitive(self, runner, tmp_path) -> None:
        """Enumeration covers skills *and* agents, not just skills."""
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")
        _write_agent(tmp_path, "gamma")

        result = runner.invoke(cli, ["migrate", "init", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, result.output
        for name in ("alpha", "beta", "gamma"):
            assert name in result.output

    def test_defaults_to_cwd_when_path_omitted(self, runner, tmp_path) -> None:
        """PATH is optional and defaults to the current directory."""
        _write_skill(tmp_path, "alpha")
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            _write_skill(Path(td), "local")
            result = runner.invoke(cli, ["migrate", "init", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "local" in result.output

    def test_rejects_unknown_ceiling(self, runner, tmp_path) -> None:
        """Ceilings are a closed set -- a typo must fail loudly, not silently."""
        _write_skill(tmp_path, "alpha")
        result = runner.invoke(
            cli, ["migrate", "init", str(tmp_path), "--ceiling", "nope", "--dry-run"]
        )
        assert result.exit_code != 0
        assert "nope" in result.output

    def test_rejects_unknown_agent(self, runner, tmp_path) -> None:
        """The dispatch target is a closed set too."""
        _write_skill(tmp_path, "alpha")
        result = runner.invoke(
            cli, ["migrate", "init", str(tmp_path), "--agent", "hal9000", "--dry-run"]
        )
        assert result.exit_code != 0
        assert "hal9000" in result.output

    def test_missing_path_fails_loudly(self, runner, tmp_path) -> None:
        """A non-existent target is a usage error, not an empty success.

        Asserts exit code 2 *and* that the message names the bad path: a bare
        ``exit_code != 0`` would also pass against a CLI with no ``migrate``
        command at all (Click exits 2 on an unknown command).
        """
        missing = tmp_path / "nope"
        result = runner.invoke(cli, ["migrate", "init", str(missing), "--dry-run"])
        assert result.exit_code == 2, result.output
        assert "nope" in result.output

    def test_repo_with_no_primitives_fails_loudly(self, runner, tmp_path) -> None:
        """Nothing to migrate is an error, not a silent empty manifest.

        os-dist's ``assertAllSkillsClassified`` fails *open* on a non-git tree,
        which is how a tarball could migrate to nothing at all.  This command
        must not inherit that.
        """
        result = runner.invoke(cli, ["migrate", "init", str(tmp_path), "--dry-run"])
        assert result.exit_code != 0
        assert "no primitives" in result.output.lower()


class TestMigrateInitEnumeration:
    def test_untracked_primitive_is_excluded_in_a_git_repo(self, runner, tmp_path) -> None:
        """Tracked content is the source of truth when the target is a git repo.

        Mirrors ``assertAllSkillsClassified``'s rule: a committed skill must be
        classified, but an untracked local WIP skill is legitimately not part of
        any release.
        """
        _write_skill(tmp_path, "committed")
        _git_init(tmp_path)
        _write_skill(tmp_path, "untracked_wip")

        result = runner.invoke(cli, ["migrate", "init", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "committed" in result.output
        assert "untracked_wip" not in result.output

    def test_non_git_tree_announces_the_fallback(self, runner, tmp_path) -> None:
        """A non-git target must SAY it fell back to a filesystem walk.

        The git rule cannot apply, so the operator needs to know the
        'untracked WIP is exempt' guarantee is not in force.
        """
        _write_skill(tmp_path, "alpha")

        result = runner.invoke(cli, ["migrate", "init", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "not a git repo" in result.output.lower()


class TestMigrateInitStagingSafety:
    def test_refuses_to_stage_into_a_harness_directory(self, runner, tmp_path) -> None:
        """Staging into ``.claude``/``.cursor``/... corrupts target detection.

        Verified during Phase 0: an added harness directory inside a checkout
        makes APM report "Multiple harnesses detected" and silently changes
        behaviour.  Refuse rather than produce a tree that breaks the repo it
        was generated into.
        """
        _write_skill(tmp_path, "alpha")
        result = runner.invoke(
            cli,
            ["migrate", "init", str(tmp_path), "--out", str(tmp_path / ".claude")],
        )
        assert result.exit_code != 0
        assert "harness" in result.output.lower()


class TestMigrateCheckSurface:
    def test_check_is_registered(self, runner) -> None:
        result = runner.invoke(cli, ["migrate", "check", "--help"])
        assert result.exit_code == 0, result.output

    def test_check_fails_when_no_manifest_found(self, runner, tmp_path) -> None:
        """``check`` validates an existing classification; absent input is an error.

        Pins the *reason* (a named manifest error), not merely a non-zero exit
        -- which an unregistered command would also produce.
        """
        result = runner.invoke(cli, ["migrate", "check", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "manifest" in result.output.lower()


class TestMigrateEndToEnd:
    """init -> staged tree -> check, with a classification supplied from disk.

    Live agent dispatch is not wired yet; ``--classification`` stands in for it,
    which keeps the whole downstream pipeline exercised.
    """

    @staticmethod
    def _classification(*names: str) -> str:
        import json

        return json.dumps(
            {
                "domains": {"tools": {"description": "Tools."}},
                "primitives": {
                    n: {"domain": "tools", "audience": "public", "confidential": False}
                    for n in names
                },
            }
        )

    def test_init_stages_and_check_passes(self, runner, tmp_path) -> None:
        repo = tmp_path / "repo"
        _write_skill(repo, "alpha")
        response = tmp_path / "resp.json"
        response.write_text(self._classification("alpha"), encoding="utf-8")
        out = tmp_path / "staged"

        result = runner.invoke(
            cli,
            [
                "migrate",
                "init",
                str(repo),
                "--classification",
                str(response),
                "--out",
                str(out),
                "--ceiling",
                "public",
                "--allow-unscrubbed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (out / "public" / "apm.yml").is_file()
        assert (out / "public" / "skills" / "alpha" / "SKILL.md").is_file()

        checked = runner.invoke(cli, ["migrate", "check", str(out)])
        assert checked.exit_code == 0, checked.output

    def test_init_rejects_an_incomplete_classification(self, runner, tmp_path) -> None:
        """The completeness gate must fire through the CLI, not just in unit tests."""
        repo = tmp_path / "repo"
        _write_skill(repo, "alpha")
        _write_skill(repo, "beta")
        response = tmp_path / "resp.json"
        response.write_text(self._classification("alpha"), encoding="utf-8")

        result = runner.invoke(
            cli,
            [
                "migrate",
                "init",
                str(repo),
                "--classification",
                str(response),
                "--out",
                str(tmp_path / "staged"),
                "--allow-unscrubbed",
            ],
        )
        assert result.exit_code == 1
        assert "beta" in result.output

    def test_shareable_ceiling_refuses_without_rules(self, runner, tmp_path) -> None:
        """Fail closed by default: no --rules means no shareable staging."""
        repo = tmp_path / "repo"
        _write_skill(repo, "alpha")
        response = tmp_path / "resp.json"
        response.write_text(self._classification("alpha"), encoding="utf-8")

        result = runner.invoke(
            cli,
            [
                "migrate",
                "init",
                str(repo),
                "--classification",
                str(response),
                "--out",
                str(tmp_path / "staged"),
                "--ceiling",
                "public",
            ],
        )
        assert result.exit_code == 1
        assert "rules" in result.output.lower()

    def test_dispatch_writes_the_classification_proposal(self, runner, tmp_path) -> None:
        """A dispatched classification is written out for human review.

        It carries confidentiality and PII consequences, so it must be
        inspectable and re-usable via --classification, not only held in memory.
        """
        from unittest.mock import patch

        repo = tmp_path / "repo"
        _write_skill(repo, "alpha")
        out = tmp_path / "staged"

        class _Fake:
            def execute_prompt(self, prompt_content: str, **kwargs) -> str:
                return TestMigrateEndToEnd._classification("alpha")

        with patch("apm_cli.migrate.dispatch.resolve_runtime", return_value=_Fake()):
            result = runner.invoke(
                cli,
                [
                    "migrate",
                    "init",
                    str(repo),
                    "--out",
                    str(out),
                    "--ceiling",
                    "public",
                    "--allow-unscrubbed",
                ],
            )

        assert result.exit_code == 0, result.output
        assert (out / "classification.json").is_file()
        assert (out / "public" / "apm.yml").is_file()

    def test_unknown_runtime_fails_with_the_known_list(self, runner, tmp_path) -> None:
        repo = tmp_path / "repo"
        _write_skill(repo, "alpha")
        result = runner.invoke(cli, ["migrate", "init", str(repo), "--agent", "claude"])
        # Click rejects it at parse time -- "claude" is not an APM runtime.
        assert result.exit_code != 0
        assert "claude" in result.output

    def test_rules_are_applied_to_staged_output(self, runner, tmp_path) -> None:
        import json as _json

        repo = tmp_path / "repo"
        _write_skill(repo, "alpha", body="reach me at someone@example.com")
        response = tmp_path / "resp.json"
        response.write_text(self._classification("alpha"), encoding="utf-8")
        rules = tmp_path / "rules.json"
        rules.write_text(_json.dumps({"redact": ["someone@example.com"]}), encoding="utf-8")
        out = tmp_path / "staged"

        result = runner.invoke(
            cli,
            [
                "migrate",
                "init",
                str(repo),
                "--classification",
                str(response),
                "--rules",
                str(rules),
                "--out",
                str(out),
                "--ceiling",
                "public",
            ],
        )
        assert result.exit_code == 0, result.output
        staged = (out / "public" / "skills" / "alpha" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "someone@example.com" not in staged
        assert "<REDACTED>" in staged
