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

from apm_cli.migrate.hooks import (
    HookStagingError,
    derive_descriptor,
    discover_hooks,
    stage_hooks,
)


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
        assert "hooks/shared.sh" in rels
        assert ".claude/hooks.json" in rels
        assert (out / "hooks" / "shared.sh").is_file()

    def test_preserves_the_exec_bit(self, repo, tmp_path):
        """A hook script without +x is delivered but never fires."""
        out = tmp_path / "out"
        out.mkdir()
        stage_hooks(repo, out, restricted=(), ceiling="public", ceilings=("public", "personal"))
        mode = (out / "hooks" / "shared.sh").stat().st_mode
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
        assert not (out / "hooks" / "telemetry.sh").exists()
        assert "telemetry.sh" not in {s.name for s in staged}
        assert (out / "hooks" / "shared.sh").is_file()

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
        assert (out / "hooks" / "telemetry.sh").is_file()

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


class TestDescriptorDerivation:
    """Deriving `hooks.json` from a repo's settings.json.

    A hook script without wiring is delivered but never fires, so staging the
    script alone is not enough. Claude Code repos keep that wiring in
    `.claude/settings.json` with `$CLAUDE_PROJECT_DIR` paths; a plugin needs
    `${CLAUDE_PLUGIN_ROOT}` paths pointing at the bundle's own hooks dir.
    """

    @staticmethod
    def _settings(root, hooks):
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8"
        )

    def test_rewrites_project_dir_to_plugin_root(self, repo):
        self._settings(
            repo,
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/shared.sh",
                            }
                        ],
                    }
                ]
            },
        )
        desc = derive_descriptor(repo, staged_names=("shared.sh",))
        cmd = desc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "$CLAUDE_PROJECT_DIR" not in cmd
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/shared.sh" in cmd

    def test_preserves_the_interpreter_and_matcher(self, repo):
        self._settings(
            repo,
            {
                "PreToolUse": [
                    {
                        "matcher": "Skill",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/helper.py",
                            }
                        ],
                    }
                ]
            },
        )
        desc = derive_descriptor(repo, staged_names=("helper.py",))
        group = desc["hooks"]["PreToolUse"][0]
        assert group["matcher"] == "Skill"
        assert group["hooks"][0]["command"].startswith("python3 ")

    def test_drops_entries_whose_script_was_withheld(self, repo):
        """The credential rule again: a withheld script must not be wired.

        Wiring without its target leaves a hook command pointing at a file that
        was never staged.
        """
        self._settings(
            repo,
            {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/telemetry.sh",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/shared.sh",
                            }
                        ],
                    }
                ],
            },
        )
        desc = derive_descriptor(repo, staged_names=("shared.sh",))
        assert "SessionStart" not in desc["hooks"]
        assert "PreToolUse" in desc["hooks"]

    def test_returns_none_when_nothing_survives(self, repo):
        self._settings(
            repo,
            {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/telemetry.sh",
                            }
                        ]
                    }
                ]
            },
        )
        assert derive_descriptor(repo, staged_names=("shared.sh",)) is None

    def test_returns_none_without_settings(self, repo):
        assert derive_descriptor(repo, staged_names=("shared.sh",)) is None

    def test_ignores_a_hook_referencing_no_known_script(self, repo):
        """An inline command that names no staged script cannot be wired."""
        self._settings(
            repo,
            {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
                ]
            },
        )
        assert derive_descriptor(repo, staged_names=("shared.sh",)) is None

    def test_tolerates_malformed_settings(self, repo):
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        assert derive_descriptor(repo, staged_names=("shared.sh",)) is None

    def test_derived_descriptor_is_staged_where_the_runtime_reads_it(self, repo, tmp_path):
        """It must land in hooks/, not at the tree root.

        APM maps `.claude/hooks.json` to the bundle ROOT but
        `.claude/hooks/hooks.json` to `hooks/hooks.json` -- and Claude Code's
        plugin channel reads the latter. Staging it at the root produces a
        bundle that installs cleanly and registers NO hooks, which is exactly
        the silent failure this whole path exists to avoid.
        """
        self._settings(
            repo,
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/shared.sh",
                            }
                        ],
                    }
                ]
            },
        )
        # The fixture ships its own descriptor; derivation only runs when the
        # repo has none, so remove it to exercise the derived path.
        (repo / ".claude" / "hooks.json").unlink()

        out = tmp_path / "out"
        out.mkdir()
        staged = stage_hooks(
            repo, out, restricted=(), ceiling="public", ceilings=("public", "personal")
        )
        assert (out / "hooks" / "hooks.json").is_file()
        assert not (out / ".claude" / "hooks.json").exists()
        assert "hooks/hooks.json" in {s.rel_path for s in staged}

    def test_an_existing_descriptor_is_preferred_over_deriving_one(self, repo, tmp_path):
        """A repo that ships its own hooks.json is authoritative.

        Deriving on top would silently override wiring the author wrote by hand.
        """
        self._settings(
            repo,
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash $CLAUDE_PROJECT_DIR/.claude/hooks/shared.sh",
                            }
                        ],
                    }
                ]
            },
        )
        out = tmp_path / "out"
        out.mkdir()
        staged = stage_hooks(
            repo, out, restricted=(), ceiling="public", ceilings=("public", "personal")
        )
        rels = {s.rel_path for s in staged}
        assert ".claude/hooks.json" in rels, "the repo's own descriptor must ship"
        assert ".claude/hooks/hooks.json" not in rels, "must not also derive one"
