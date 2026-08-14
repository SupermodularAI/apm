"""Identifier redaction and parametrization applied while staging.

Rule *data* is always supplied by the caller (``--rules``); nothing
identifying is bundled with APM.  These tests pin the mechanism: literal
replacement over text files only, never at the top ceiling, and fail-closed
when a rules file is malformed.
"""

from __future__ import annotations

import json

import pytest

from apm_cli.migrate.scrub import (
    RulesError,
    ScrubRules,
    load_rules,
    scrub_text,
    scrub_tree,
)


def _rules(**kw) -> ScrubRules:
    return ScrubRules(
        redact=tuple(kw.get("redact", ())),
        parametrize=dict(kw.get("parametrize", {})),
        redaction_token=kw.get("redaction_token", "<REDACTED>"),
    )


class TestScrubText:
    def test_redacts_a_literal(self) -> None:
        rules = _rules(redact=["someone@example.com"])
        assert scrub_text("mail someone@example.com now", rules) == "mail <REDACTED> now"

    def test_parametrizes_a_literal(self) -> None:
        rules = _rules(parametrize={"U0123": "<SLACK_USER_ID>"})
        assert scrub_text("id U0123 here", rules) == "id <SLACK_USER_ID> here"

    def test_replacement_is_literal_not_regex(self) -> None:
        """A '.' or '+' in a rule must not behave as a metacharacter.

        Rule data is user-supplied; treating it as a pattern would silently
        over-match (e.g. 'a.b@x.com' matching 'axb@x.com').
        """
        rules = _rules(redact=["a.b@x.com"])
        assert scrub_text("axb@x.com", rules) == "axb@x.com"
        assert scrub_text("a.b@x.com", rules) == "<REDACTED>"

    def test_replaces_every_occurrence(self) -> None:
        rules = _rules(redact=["x@y.z"])
        assert scrub_text("x@y.z and x@y.z", rules) == "<REDACTED> and <REDACTED>"

    def test_returns_input_unchanged_when_no_rule_matches(self) -> None:
        rules = _rules(redact=["absent@example.com"])
        assert scrub_text("nothing here", rules) == "nothing here"

    def test_longer_rules_apply_first(self) -> None:
        """Overlapping literals must not leave a partially-scrubbed remainder."""
        rules = _rules(
            parametrize={"abc": "<SHORT>", "abcdef": "<LONG>"},
        )
        assert scrub_text("abcdef", rules) == "<LONG>"


class TestLoadRules:
    def test_loads_a_well_formed_file(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redact": ["a@b.c"],
                    "parametrize": {"U1": "<T>"},
                    "redaction_token": "<GONE>",
                }
            ),
            encoding="utf-8",
        )
        rules = load_rules(path)
        assert rules.redact == ("a@b.c",)
        assert rules.parametrize == {"U1": "<T>"}
        assert rules.redaction_token == "<GONE>"

    def test_absent_sections_default_to_empty(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"redact": ["a@b.c"]}), encoding="utf-8")
        rules = load_rules(path)
        assert rules.parametrize == {}

    def test_rejects_malformed_json(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RulesError):
            load_rules(path)

    def test_rejects_an_empty_literal(self, tmp_path) -> None:
        """An empty rule would match everywhere and corrupt every file."""
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"redact": [""]}), encoding="utf-8")
        with pytest.raises(RulesError) as exc:
            load_rules(path)
        assert "empty" in str(exc.value).lower()

    def test_rejects_a_non_string_token(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"parametrize": {"U1": 5}}), encoding="utf-8")
        with pytest.raises(RulesError):
            load_rules(path)


class TestScrubTree:
    def test_rewrites_text_files_in_place(self, tmp_path) -> None:
        (tmp_path / "a.md").write_text("contact x@y.z", encoding="utf-8")
        changed = scrub_tree(tmp_path, _rules(redact=["x@y.z"]))
        assert changed == 1
        assert (tmp_path / "a.md").read_text(encoding="utf-8") == "contact <REDACTED>"

    def test_never_rewrites_a_binary_file(self, tmp_path) -> None:
        """Only an allowlisted text extension is ever opened for rewriting.

        A blind pass would corrupt images and archives that happen to contain
        the rule bytes.
        """
        blob = bytes(range(256)) * 4
        (tmp_path / "logo.png").write_bytes(blob)
        scrub_tree(tmp_path, _rules(redact=["x@y.z"]))
        assert (tmp_path / "logo.png").read_bytes() == blob

    def test_recurses(self, tmp_path) -> None:
        nested = tmp_path / "skills" / "alpha"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("x@y.z", encoding="utf-8")
        assert scrub_tree(tmp_path, _rules(redact=["x@y.z"])) == 1
        assert (nested / "SKILL.md").read_text(encoding="utf-8") == "<REDACTED>"

    def test_counts_only_files_it_changed(self, tmp_path) -> None:
        (tmp_path / "hit.md").write_text("x@y.z", encoding="utf-8")
        (tmp_path / "miss.md").write_text("nothing", encoding="utf-8")
        assert scrub_tree(tmp_path, _rules(redact=["x@y.z"])) == 1

    def test_tolerates_undecodable_text_extension(self, tmp_path) -> None:
        """A .md holding invalid UTF-8 must be skipped, not crash the run."""
        (tmp_path / "weird.md").write_bytes(b"\xff\xfe not utf8")
        scrub_tree(tmp_path, _rules(redact=["x@y.z"]))  # must not raise

    def test_empty_rules_is_a_no_op(self, tmp_path) -> None:
        (tmp_path / "a.md").write_text("untouched", encoding="utf-8")
        assert scrub_tree(tmp_path, _rules()) == 0
        assert (tmp_path / "a.md").read_text(encoding="utf-8") == "untouched"
