"""Staging a scrubbed per-ceiling tree and emitting its ``apm.yml``.

``includes:`` selects *paths*; it cannot rewrite *bytes*.  So audience
filtering and identifier scrubbing both happen here, at staging time, and
``apm pack`` then runs over an already-clean tree with no further filtering.
"""

from __future__ import annotations

import json
import stat

import pytest
import yaml

from apm_cli.migrate.classification import parse_classification
from apm_cli.migrate.enumerate import Primitive
from apm_cli.migrate.scrub import ScrubRules
from apm_cli.migrate.stage import StagingError, emit_manifest, stage_ceiling

CEILINGS = ("public", "company", "personal")


@pytest.fixture
def repo(tmp_path):
    """A source repo with three skills at three different audiences."""
    root = tmp_path / "src"
    for name in ("pub", "corp", "priv"):
        d = root / ".apm" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}.\n---\n\ncontact x@y.z\n",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def primitives():
    return tuple(
        Primitive(name=n, kind="skill", rel_path=f".apm/skills/{n}")
        for n in ("pub", "corp", "priv")
    )


@pytest.fixture
def classification(primitives):
    return parse_classification(
        {
            "domains": {"tools": {"description": "Tools."}},
            "primitives": {
                "pub": {"domain": "tools", "audience": "public"},
                "corp": {"domain": "tools", "audience": "company"},
                "priv": {"domain": "tools", "audience": "personal"},
            },
        },
        primitives=primitives,
        ceilings=CEILINGS,
    )


def _staged_names(out_dir):
    skills = out_dir / ".apm" / "skills"
    return {p.name for p in skills.iterdir()} if skills.is_dir() else set()


class TestAudienceFiltering:
    def test_public_ceiling_takes_only_public(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert _staged_names(out) == {"pub"}

    def test_company_ceiling_includes_public_and_company(self, repo, classification, tmp_path):
        """A ceiling contains everything at or below it."""
        out = tmp_path / "out" / "company"
        stage_ceiling(repo, out, classification=classification, ceiling="company", rules=None)
        assert _staged_names(out) == {"pub", "corp"}

    def test_personal_ceiling_includes_everything(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "personal"
        stage_ceiling(repo, out, classification=classification, ceiling="personal", rules=None)
        assert _staged_names(out) == {"pub", "corp", "priv"}


class TestConfidentialExclusion:
    def test_confidential_never_ships_in_a_shared_tree(self, repo, primitives, tmp_path):
        """Confidential content is excluded from every ceiling, unconditionally."""
        classification = parse_classification(
            {
                "domains": {"tools": {"description": "Tools."}},
                "primitives": {
                    "pub": {"domain": "tools", "audience": "public"},
                    "corp": {"domain": "tools", "audience": "public"},
                    "priv": {
                        "domain": "tools",
                        "audience": "public",
                        "confidential": True,
                    },
                },
            },
            primitives=primitives,
            ceilings=CEILINGS,
        )
        out = tmp_path / "out" / "personal"
        stage_ceiling(repo, out, classification=classification, ceiling="personal", rules=None)
        assert "priv" not in _staged_names(out)


class TestScrubIntegration:
    def test_applies_rules_below_the_top_ceiling(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        rules = ScrubRules(redact=("x@y.z",), parametrize={}, redaction_token="<REDACTED>")
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=rules)
        text = (out / ".apm" / "skills" / "pub" / "SKILL.md").read_text(encoding="utf-8")
        assert "x@y.z" not in text
        assert "<REDACTED>" in text

    def test_top_ceiling_keeps_identifiers(self, repo, classification, tmp_path):
        """`personal` is self-rehydration -- the real values must survive."""
        out = tmp_path / "out" / "personal"
        rules = ScrubRules(redact=("x@y.z",), parametrize={}, redaction_token="<REDACTED>")
        stage_ceiling(repo, out, classification=classification, ceiling="personal", rules=rules)
        text = (out / ".apm" / "skills" / "pub" / "SKILL.md").read_text(encoding="utf-8")
        assert "x@y.z" in text

    def test_refuses_to_stage_below_top_ceiling_without_rules_when_required(
        self, repo, classification, tmp_path
    ):
        """Fail closed: staging shareable content with no rules is how a
        credential ships. os-dist guards a file holding a live Notion token."""
        out = tmp_path / "out" / "public"
        with pytest.raises(StagingError) as exc:
            stage_ceiling(
                repo,
                out,
                classification=classification,
                ceiling="public",
                rules=None,
                require_rules=True,
            )
        assert "rules" in str(exc.value).lower()


class TestStagingMechanics:
    def test_preserves_file_modes(self, repo, classification, tmp_path):
        """A hook script that loses its exec bit stops firing, silently."""
        script = repo / ".apm" / "skills" / "pub" / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        script.chmod(0o755)

        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        staged = out / ".apm" / "skills" / "pub" / "run.sh"
        assert stat.S_IMODE(staged.stat().st_mode) & 0o111, "exec bit lost in staging"

    def test_is_idempotent(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        for _ in range(2):
            stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert _staged_names(out) == {"pub"}

    def test_refuses_a_harness_directory(self, repo, classification, tmp_path):
        with pytest.raises(StagingError) as exc:
            stage_ceiling(
                repo,
                tmp_path / ".claude",
                classification=classification,
                ceiling="public",
                rules=None,
            )
        assert "harness" in str(exc.value).lower()

    def test_never_writes_into_the_source_tree(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        source = (repo / ".apm" / "skills" / "pub" / "SKILL.md").read_text(encoding="utf-8")
        assert "x@y.z" in source, "source was mutated"


class TestUnresolvablePrimitives:
    """A classified primitive that is not on disk must never vanish silently.

    Found against a real repo: skills tracked in git but deleted in the working
    tree were skipped without a word, so the staged tree was quietly smaller
    than the classification claimed.
    """

    def test_unresolvable_primitive_is_reported(self, repo, primitives, tmp_path):
        classification = parse_classification(
            {
                "domains": {"tools": {"description": "Tools."}},
                "primitives": {
                    "pub": {"domain": "tools", "audience": "public"},
                    "corp": {"domain": "tools", "audience": "public"},
                    "priv": {"domain": "tools", "audience": "public"},
                },
            },
            primitives=primitives,
            ceilings=CEILINGS,
        )
        import shutil as _shutil

        _shutil.rmtree(repo / ".apm" / "skills" / "corp")

        out = tmp_path / "out" / "public"
        with pytest.raises(StagingError) as exc:
            stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert "corp" in str(exc.value)

    def test_skip_missing_downgrades_to_a_warning(self, repo, primitives, tmp_path):
        """Opt-in tolerance, for a repo mid-refactor."""
        classification = parse_classification(
            {
                "domains": {"tools": {"description": "Tools."}},
                "primitives": {
                    "pub": {"domain": "tools", "audience": "public"},
                    "corp": {"domain": "tools", "audience": "public"},
                    "priv": {"domain": "tools", "audience": "public"},
                },
            },
            primitives=primitives,
            ceilings=CEILINGS,
        )
        import shutil as _shutil

        _shutil.rmtree(repo / ".apm" / "skills" / "corp")

        out = tmp_path / "out" / "public"
        staged = stage_ceiling(
            repo,
            out,
            classification=classification,
            ceiling="public",
            rules=None,
            skip_missing=True,
        )
        assert {s.name for s in staged} == {"pub", "priv"}


class TestRuntimeArtifactExclusion:
    """Runtime output must never be staged.

    Found against a real repo: a skill's ``logs/`` directory held dispatcher
    logs containing the owner's Slack ID, which reached a `public` staging tree
    -- both because ``.log`` is not a scrubbable text extension and, more
    fundamentally, because runtime output is not source and should never be
    copied at all.
    """

    def test_excludes_a_logs_directory(self, repo, classification, tmp_path):
        logs = repo / ".apm" / "skills" / "pub" / "logs"
        logs.mkdir()
        (logs / "run-20260101.log").write_text("secret U0123", encoding="utf-8")

        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert not (out / ".apm" / "skills" / "pub" / "logs" / "run-20260101.log").exists()

    def test_excludes_a_state_directory(self, repo, classification, tmp_path):
        state = repo / ".apm" / "skills" / "pub" / "state"
        state.mkdir()
        (state / "cron.last-run").write_text("2026-01-01", encoding="utf-8")

        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert not (out / ".apm" / "skills" / "pub" / "state" / "cron.last-run").exists()

    def test_keeps_the_tracked_gitignore_placeholder(self, repo, classification, tmp_path):
        """A `.gitignore` directly inside logs/ is tracked source, not runtime."""
        logs = repo / ".apm" / "skills" / "pub" / "logs"
        logs.mkdir()
        (logs / ".gitignore").write_text("*\n", encoding="utf-8")
        (logs / "noise.log").write_text("noise", encoding="utf-8")

        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        staged_logs = out / ".apm" / "skills" / "pub" / "logs"
        assert (staged_logs / ".gitignore").is_file()
        assert not (staged_logs / "noise.log").exists()

    def test_excludes_a_stray_log_file(self, repo, classification, tmp_path):
        (repo / ".apm" / "skills" / "pub" / "debug.log").write_text("x", encoding="utf-8")
        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        assert not (out / ".apm" / "skills" / "pub" / "debug.log").exists()

    def test_excludes_live_crons_json(self, repo, classification, tmp_path):
        """A crons/*.json is live scheduler config, not shippable source.

        It names real schedules, repos and recipients. The `.json.example`
        template beside it IS source and must survive.
        """
        crons = repo / ".apm" / "skills" / "pub" / "crons"
        crons.mkdir()
        (crons / "nightly.json").write_text('{"schedule":"0 2 * * *"}', encoding="utf-8")
        (crons / "nightly.json.example").write_text("{}", encoding="utf-8")
        (crons / "README.md").write_text("docs", encoding="utf-8")

        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        staged = out / ".apm" / "skills" / "pub" / "crons"
        assert not (staged / "nightly.json").exists()
        assert (staged / "nightly.json.example").is_file()
        assert (staged / "README.md").is_file()

    def test_excludes_junk_files(self, repo, classification, tmp_path):
        (repo / ".apm" / "skills" / "pub" / ".DS_Store").write_bytes(b"junk")
        (repo / ".apm" / "skills" / "pub" / "SKILL.md.bak").write_text("old", encoding="utf-8")
        out = tmp_path / "out" / "public"
        stage_ceiling(repo, out, classification=classification, ceiling="public", rules=None)
        staged = out / ".apm" / "skills" / "pub"
        assert not (staged / ".DS_Store").exists()
        assert not (staged / "SKILL.md.bak").exists(), "a .bak can hold pre-scrub content"


class TestManifestEmission:
    def test_writes_includes_for_staged_primitives_only(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        staged = stage_ceiling(
            repo, out, classification=classification, ceiling="public", rules=None
        )
        emit_manifest(out, staged=staged, name="probe", version="0.1.0")

        data = yaml.safe_load((out / "apm.yml").read_text(encoding="utf-8"))
        assert data["includes"] == [".apm/skills/pub"]

    def test_manifest_carries_required_fields(self, repo, classification, tmp_path):
        """name + version are the only schema-required keys."""
        out = tmp_path / "out" / "public"
        staged = stage_ceiling(
            repo, out, classification=classification, ceiling="public", rules=None
        )
        emit_manifest(out, staged=staged, name="probe", version="0.1.0")

        data = yaml.safe_load((out / "apm.yml").read_text(encoding="utf-8"))
        assert data["name"] == "probe"
        assert data["version"] == "0.1.0"

    def test_includes_are_sorted(self, repo, classification, tmp_path):
        """Deterministic output: the same inputs give the same bytes."""
        out = tmp_path / "out" / "personal"
        staged = stage_ceiling(
            repo, out, classification=classification, ceiling="personal", rules=None
        )
        emit_manifest(out, staged=staged, name="probe", version="0.1.0")

        data = yaml.safe_load((out / "apm.yml").read_text(encoding="utf-8"))
        assert data["includes"] == sorted(data["includes"])

    def test_refuses_to_emit_an_empty_manifest(self, tmp_path):
        """An includes-less manifest packs nothing -- fail rather than ship it."""
        out = tmp_path / "out" / "empty"
        out.mkdir(parents=True)
        with pytest.raises(StagingError):
            emit_manifest(out, staged=(), name="probe", version="0.1.0")

    def test_emitted_manifest_is_valid_json_compatible_yaml(self, repo, classification, tmp_path):
        out = tmp_path / "out" / "public"
        staged = stage_ceiling(
            repo, out, classification=classification, ceiling="public", rules=None
        )
        emit_manifest(out, staged=staged, name="probe", version="0.1.0")
        raw = (out / "apm.yml").read_text(encoding="utf-8")
        json.dumps(yaml.safe_load(raw))  # must round-trip
