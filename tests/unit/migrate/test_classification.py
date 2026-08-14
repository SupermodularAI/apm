"""Validation and repair of an agent's classification response.

The dispatch itself is non-deterministic; everything downstream of it is not.
These tests pin the deterministic half: a response is either accepted, or
rejected with defects specific enough to re-prompt against.

Nothing here invokes an agent.
"""

from __future__ import annotations

import pytest

from apm_cli.migrate.classification import (
    ClassificationError,
    parse_classification,
    summarise_defects,
)
from apm_cli.migrate.enumerate import Primitive

CEILINGS = ("public", "company", "personal")


def _primitives(*names: str) -> tuple[Primitive, ...]:
    return tuple(Primitive(name=n, kind="skill", rel_path=f".apm/skills/{n}") for n in names)


def _response(**overrides) -> dict:
    """A minimal well-formed response for two primitives."""
    base = {
        "domains": {"tools": {"description": "Developer tooling."}},
        "primitives": {
            "alpha": {
                "domain": "tools",
                "audience": "public",
                "confidential": False,
                "identifiers": [],
            },
            "beta": {
                "domain": "tools",
                "audience": "company",
                "confidential": False,
                "identifiers": [],
            },
        },
    }
    base.update(overrides)
    return base


class TestAcceptsValidResponses:
    def test_round_trips_a_well_formed_response(self) -> None:
        result = parse_classification(
            _response(), primitives=_primitives("alpha", "beta"), ceilings=CEILINGS
        )
        assert set(result.primitives) == {"alpha", "beta"}
        assert result.primitives["alpha"].audience == "public"
        assert result.domains["tools"].description == "Developer tooling."

    def test_accepts_a_json_string(self) -> None:
        """Agents return text; the parser takes the raw stdout."""
        import json

        result = parse_classification(
            json.dumps(_response()),
            primitives=_primitives("alpha", "beta"),
            ceilings=CEILINGS,
        )
        assert set(result.primitives) == {"alpha", "beta"}

    def test_tolerates_a_fenced_code_block(self) -> None:
        """Agents wrap JSON in ``` fences more often than not."""
        import json

        fenced = f"Here you go:\n```json\n{json.dumps(_response())}\n```\n"
        result = parse_classification(
            fenced, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS
        )
        assert set(result.primitives) == {"alpha", "beta"}

    def test_confidential_primitive_is_preserved(self) -> None:
        resp = _response()
        resp["primitives"]["beta"]["confidential"] = True
        result = parse_classification(
            resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS
        )
        assert result.primitives["beta"].confidential is True


class TestRejectsMalformedResponses:
    def test_rejects_non_json(self) -> None:
        with pytest.raises(ClassificationError) as exc:
            parse_classification(
                "I couldn't do that.", primitives=_primitives("alpha"), ceilings=CEILINGS
            )
        assert "json" in str(exc.value).lower()

    def test_rejects_a_non_object_payload(self) -> None:
        with pytest.raises(ClassificationError):
            parse_classification("[1, 2, 3]", primitives=_primitives("alpha"), ceilings=CEILINGS)


class TestCompletenessAssertion:
    """The gate os-dist's assertAllSkillsClassified fails OPEN on."""

    def test_omitted_primitive_is_a_defect(self) -> None:
        resp = _response()
        del resp["primitives"]["beta"]
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        assert "beta" in str(exc.value)

    def test_invented_primitive_is_a_defect(self) -> None:
        """A name that is not in the repo means the agent hallucinated."""
        resp = _response()
        resp["primitives"]["ghost"] = {
            "domain": "tools",
            "audience": "public",
            "confidential": False,
            "identifiers": [],
        }
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        assert "ghost" in str(exc.value)

    def test_every_missing_primitive_is_reported_at_once(self) -> None:
        """Report all defects, so one repair round can fix them all."""
        resp = _response()
        del resp["primitives"]["alpha"]
        del resp["primitives"]["beta"]
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        message = str(exc.value)
        assert "alpha" in message and "beta" in message


class TestFieldValidation:
    def test_rejects_an_unknown_audience(self) -> None:
        resp = _response()
        resp["primitives"]["alpha"]["audience"] = "everyone"
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        assert "everyone" in str(exc.value)

    def test_rejects_a_domain_with_no_description(self) -> None:
        """writeApmCatalog throws rather than emit description: undefined."""
        resp = _response()
        resp["domains"]["tools"]["description"] = ""
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        assert "description" in str(exc.value).lower()

    def test_rejects_a_primitive_in_an_undeclared_domain(self) -> None:
        resp = _response()
        resp["primitives"]["beta"]["domain"] = "nowhere"
        with pytest.raises(ClassificationError) as exc:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        assert "nowhere" in str(exc.value)

    def test_rejects_a_missing_domain_field(self) -> None:
        resp = _response()
        del resp["primitives"]["alpha"]["domain"]
        with pytest.raises(ClassificationError):
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)

    def test_defaults_optional_fields(self) -> None:
        """confidential/identifiers are optional; absence is not a defect."""
        resp = _response()
        del resp["primitives"]["alpha"]["confidential"]
        del resp["primitives"]["alpha"]["identifiers"]
        result = parse_classification(
            resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS
        )
        assert result.primitives["alpha"].confidential is False
        assert result.primitives["alpha"].identifiers == ()


class TestDefectSummary:
    def test_summary_is_repromptable(self) -> None:
        """The repair prompt must name what to fix, not just say 'invalid'."""
        resp = _response()
        del resp["primitives"]["beta"]
        resp["primitives"]["alpha"]["audience"] = "everyone"
        try:
            parse_classification(resp, primitives=_primitives("alpha", "beta"), ceilings=CEILINGS)
        except ClassificationError as exc:
            text = summarise_defects(exc.defects)
        assert "beta" in text
        assert "everyone" in text
