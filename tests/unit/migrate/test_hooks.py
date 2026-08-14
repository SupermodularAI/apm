"""Hook staging.

Hooks are a third primitive kind alongside skills and agents, and the one with
a credential-safety rule: a hook script may hold a live token, so a script the
classification marks as restricted must never reach a shareable ceiling.

Verified against APM's exporter: a ``.claude/hooks/<file>.sh`` listed in
``includes`` packs to ``hooks/<file>.sh`` **with its exec bit intact**, and
``.claude/hooks.json`` packs to ``hooks.json``.  No carrier-skill workaround is
needed.
"""

from __future__ import annotations

import json
import stat

import pytest

from apm_cli.migrate.hooks import HookStagingError, discover_hooks, stage_hooks


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("shared.sh", "telemetry.sh"):
        path = hooks / name
        path.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        path.chmod(0o755)
    (hooks / "helper.py").write_text("print('hi')\n", encoding="utf-8")
    (root / ".claude" / "hooks.json").write_text(
        json.dumps({"PreToolUse": [{"matcher": "Bash", "hooks": []}]}), encoding="utf-8"
    )
    return root


class TestDiscovery:
    def test_finds_scripts_and_the_descriptor(self, repo):
        found = discover_hooks(repo)
        names = {h.name for h in found}
        assert names == {"shared.sh", "telemetry.sh", "helper.py", "hooks.json"}

    def test_returns_nothing_when_there_are_no_hooks(self, tmp_path):
        assert discover_hooks(tmp_path) == ()

    def test_skips_junk_and_runtime_artifacts(self, repo):
        (repo / ".claude" / "hooks" / ".DS_Store").write_bytes(b"junk")
        (repo / ".claude" / "hooks" / "run.log").write_text("noise", encoding="utf-8")
        (repo / ".claude" / "hooks" / "old.sh.bak").write_text("old", encoding="utf-8")
        names = {h.name for h in discover_hooks(repo)}
        assert ".DS_Store" not in names
        assert "run.log" not in names
        assert "old.sh.bak" not in names, "a .bak can hold pre-scrub content"


class TestStaging:
    def test_maps_into_the_layout_apm_packs(self, repo, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        staged = stage_hooks(
            repo, out, restricted=(), ceiling="public", ceilings=("public", "personal")
        )
        rels = {s.rel_path for s in staged}
        assert ".claude/hooks/shared.sh" in rels
        assert ".claude/hooks.json" in rels
        assert (out / ".claude" / "hooks" / "shared.sh").is_file()

    def test_preserves_the_exec_bit(self, repo, tmp_path):
        """A hook script without +x is delivered but never fires."""
        out = tmp_path / "out"
        out.mkdir()
        stage_hooks(repo, out, restricted=(), ceiling="public", ceilings=("public", "personal"))
        mode = (out / ".claude" / "hooks" / "shared.sh").stat().st_mode
        assert stat.S_IMODE(mode) & 0o111, "exec bit lost — the hook would not fire"

    def test_restricted_scripts_are_withheld_below_the_top_ceiling(self, repo, tmp_path):
        """The credential rule: a token-bearing script must not ship.

        os-dist withholds `supermodular-os-telemetry.sh` because it hardcodes a
        live Notion token. Scrubbing cannot save this -- a token is not in any
        rules file -- so exclusion is the only safe answer.
        """
        out = tmp_path / "out"
        out.mkdir()
        staged = stage_hooks(
            repo,
            out,
            restricted=("telemetry.sh",),
            ceiling="public",
            ceilings=("public", "personal"),
        )
        assert not (out / ".claude" / "hooks" / "telemetry.sh").exists()
        assert "telemetry.sh" not in {s.name for s in staged}
        assert (out / ".claude" / "hooks" / "shared.sh").is_file()

    def test_restricted_scripts_ship_at_the_top_ceiling(self, repo, tmp_path):
        """`personal` is self-rehydration -- the owner gets their own files."""
        out = tmp_path / "out"
        out.mkdir()
        stage_hooks(
            repo,
            out,
            restricted=("telemetry.sh",),
            ceiling="personal",
            ceilings=("public", "personal"),
        )
        assert (out / ".claude" / "hooks" / "telemetry.sh").is_file()

    def test_descriptor_is_withheld_when_it_wires_a_restricted_script(self, repo, tmp_path):
        """A descriptor referencing a withheld script leaves a dangling command.

        os-dist's reasoning: shipping the wiring without its target produces a
        hook that points at a file which was never staged.
        """
        (repo / ".claude" / "hooks.json").write_text(
            json.dumps(
                {"SessionStart": [{"hooks": [{"type": "command", "command": "./telemetry.sh"}]}]}
            ),
            encoding="utf-8",
        )
        out = tmp_path / "out"
        out.mkdir()
        staged = stage_hooks(
            repo,
            out,
            restricted=("telemetry.sh",),
            ceiling="public",
            ceilings=("public", "personal"),
        )
        assert "hooks.json" not in {s.name for s in staged}

    def test_rejects_an_unparseable_descriptor(self, repo, tmp_path):
        """apm pack force-parses hooks.json; malformed JSON must fail here first."""
        (repo / ".claude" / "hooks.json").write_text("{not json", encoding="utf-8")
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(HookStagingError):
            stage_hooks(repo, out, restricted=(), ceiling="public", ceilings=("public", "personal"))
