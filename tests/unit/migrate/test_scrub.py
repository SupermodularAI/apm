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
                    "parametrize": {"U0BA46PD": "<T>"},
                    "redaction_token": "<GONE>",
                }
            ),
            encoding="utf-8",
        )
        rules = load_rules(path)
        assert rules.redact == ("a@b.c",)
        assert rules.parametrize == {"U0BA46PD": "<T>"}
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
        path.write_text(json.dumps({"parametrize": {"U0BA46PD": 5}}), encoding="utf-8")
        with pytest.raises(RulesError):
            load_rules(path)

    @pytest.mark.parametrize("literal", ["i", "ab", "xyz"])
    def test_rejects_a_dangerously_short_literal(self, tmp_path, literal) -> None:
        """A short literal matches inside ordinary words and shreds every file.

        Observed for real: a generator bug emitted the single character "i" as a
        rule, which rewrote every 'i' in every staged file to a placeholder. The
        output still looked plausible in aggregate -- tens of thousands of
        "substitutions" -- so this must fail at load time, not review time.
        """
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"parametrize": {literal: "<TOKEN>"}}), encoding="utf-8")
        with pytest.raises(RulesError) as exc:
            load_rules(path)
        assert "short" in str(exc.value).lower()

    def test_rejects_a_short_redact_literal(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"redact": ["ab"]}), encoding="utf-8")
        with pytest.raises(RulesError):
            load_rules(path)

    def test_accepts_a_short_literal_when_explicitly_allowed(self, tmp_path) -> None:
        """An escape hatch, because some real identifiers are genuinely short."""
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps({"parametrize": {"ab": "<TOKEN>"}, "allow_short_literals": True}),
            encoding="utf-8",
        )
        rules = load_rules(path)
        assert rules.parametrize == {"ab": "<TOKEN>"}


class TestPerPrimitiveRules:
    """Rules scoped to one primitive, overriding the global set.

    Real case that motivated this: one address is *redacted* in one skill and
    *parametrized* to a meaningful token in two others. A flat rule set cannot
    express one literal having two treatments, so the more aggressive rule wins
    everywhere and the token is lost.
    """

    def test_loads_a_primitives_block(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redact": ["shared@example.com"],
                    "primitives": {"invoice": {"parametrize": {"shared@example.com": "<FINANCE>"}}},
                }
            ),
            encoding="utf-8",
        )
        rules = load_rules(path)
        assert rules.redact == ("shared@example.com",)
        assert "invoice" in rules.primitives

    def test_scoped_rule_overrides_the_global_one(self, tmp_path) -> None:
        """Scoped beats global -- otherwise the specific intent is unreachable."""
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redact": ["shared@example.com"],
                    "primitives": {"invoice": {"parametrize": {"shared@example.com": "<FINANCE>"}}},
                }
            ),
            encoding="utf-8",
        )
        rules = load_rules(path)

        # Unscoped primitive: the global redact applies.
        assert scrub_text("mail shared@example.com", rules.for_primitive("other")) == (
            "mail <REDACTED>"
        )
        # Scoped primitive: the parametrize token wins.
        assert scrub_text("mail shared@example.com", rules.for_primitive("invoice")) == (
            "mail <FINANCE>"
        )

    def test_scoped_rules_are_additive_to_the_global_set(self, tmp_path) -> None:
        """A scope adds its rules; it does not discard the global ones."""
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redact": ["global@example.com"],
                    "primitives": {"invoice": {"redact": ["local@example.com"]}},
                }
            ),
            encoding="utf-8",
        )
        scoped = load_rules(path).for_primitive("invoice")
        assert scrub_text("global@example.com", scoped) == "<REDACTED>"
        assert scrub_text("local@example.com", scoped) == "<REDACTED>"

    def test_scope_redact_beats_a_global_parametrize(self, tmp_path) -> None:
        """An explicit local redact must not be overridden by a global token.

        Real case: one address is parametrized in two skills and redacted in a
        third. The third's redaction is deliberate -- deferring to the global
        token there would leak a name the author chose to remove.
        """
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "parametrize": {"shared@example.com": "<FINANCE>"},
                    "primitives": {"onepager": {"redact": ["shared@example.com"]}},
                }
            ),
            encoding="utf-8",
        )
        rules = load_rules(path)
        assert scrub_text("mail shared@example.com", rules.for_primitive("onepager")) == (
            "mail <REDACTED>"
        )
        # Everywhere else, the global token still applies.
        assert scrub_text("mail shared@example.com", rules.for_primitive("other")) == (
            "mail <FINANCE>"
        )

    def test_scope_inherits_the_global_redaction_token(self, tmp_path) -> None:
        """A scope that does not set its own token uses the global one.

        The dataclass default is non-empty, so a naive `scope.token or global`
        silently substitutes the default and every scoped redaction differs
        from every unscoped one.
        """
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redaction_token": "<REDACTED-EMAIL>",
                    "primitives": {"onepager": {"redact": ["someone@example.com"]}},
                }
            ),
            encoding="utf-8",
        )
        scoped = load_rules(path).for_primitive("onepager")
        assert scrub_text("mail someone@example.com", scoped) == "mail <REDACTED-EMAIL>"

    def test_scope_may_override_the_redaction_token(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "redaction_token": "<REDACTED-EMAIL>",
                    "primitives": {
                        "onepager": {
                            "redact": ["someone@example.com"],
                            "redaction_token": "<GONE>",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        scoped = load_rules(path).for_primitive("onepager")
        assert scrub_text("mail someone@example.com", scoped) == "mail <GONE>"

    def test_for_primitive_is_identity_without_a_scope(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"redact": ["a@b.com"]}), encoding="utf-8")
        rules = load_rules(path)
        assert rules.for_primitive("anything").redact == rules.redact

    def test_short_literals_are_rejected_inside_a_scope_too(self, tmp_path) -> None:
        """The guard must not have a hole in the nested block."""
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps({"primitives": {"invoice": {"redact": ["ab"]}}}), encoding="utf-8"
        )
        with pytest.raises(RulesError) as exc:
            load_rules(path)
        assert "short" in str(exc.value).lower()

    def test_rejects_a_non_object_primitives_block(self, tmp_path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"primitives": ["invoice"]}), encoding="utf-8")
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
