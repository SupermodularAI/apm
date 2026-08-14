"""Dispatching the classification prompt to an APM runtime.

Dispatch goes through APM's own ``RuntimeFactory`` / ``RuntimeAdapter``
rather than a bespoke subprocess, so it inherits upstream's process-group
termination, child TLS trust and timeout handling.

**No test here invokes a real runtime.** The adapter is the seam: a fake
implementing ``execute_prompt`` stands in, which is also what makes the
repair loop testable at all -- a live model would give a different answer
every run.
"""

from __future__ import annotations

import json

import pytest

from apm_cli.migrate.classification import ClassificationError
from apm_cli.migrate.dispatch import (
    DispatchError,
    classify_with_runtime,
    resolve_runtime,
)
from apm_cli.migrate.enumerate import Primitive

CEILINGS = ("public", "company", "personal")


def _primitives(*names: str) -> tuple[Primitive, ...]:
    return tuple(Primitive(name=n, kind="skill", rel_path=f".apm/skills/{n}") for n in names)


def _valid_response(*names: str) -> str:
    return json.dumps(
        {
            "domains": {"tools": {"description": "Tools."}},
            "primitives": {
                n: {"domain": "tools", "audience": "public", "confidential": False} for n in names
            },
        }
    )


class FakeRuntime:
    """Stands in for a ``RuntimeAdapter``.

    Returns each queued reply in turn, so a test can script "malformed first,
    correct on retry" and assert the repair loop actually re-prompts.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def execute_prompt(self, prompt_content: str, **kwargs) -> str:
        self.prompts.append(prompt_content)
        if not self.replies:
            raise AssertionError("runtime called more times than the test scripted")
        return self.replies.pop(0)


class TestResolveRuntime:
    def test_unknown_runtime_is_rejected_with_the_known_list(self) -> None:
        """Naming a runtime APM cannot dispatch to must fail loudly."""
        with pytest.raises(DispatchError) as exc:
            resolve_runtime("definitely-not-a-runtime")
        message = str(exc.value)
        assert "definitely-not-a-runtime" in message
        assert "codex" in message or "copilot" in message or "llm" in message


class TestClassifyWithRuntime:
    def test_returns_a_parsed_classification(self) -> None:
        runtime = FakeRuntime(_valid_response("alpha", "beta"))
        result = classify_with_runtime(
            runtime,
            primitives=_primitives("alpha", "beta"),
            ceilings=CEILINGS,
            project_root="/repo",
        )
        assert set(result.primitives) == {"alpha", "beta"}

    def test_sends_the_rendered_prompt(self) -> None:
        """What --dry-run shows must be what dispatch actually sends."""
        runtime = FakeRuntime(_valid_response("alpha"))
        classify_with_runtime(
            runtime,
            primitives=_primitives("alpha"),
            ceilings=CEILINGS,
            project_root="/repo",
        )
        assert "alpha" in runtime.prompts[0]
        assert "public, company, personal" in runtime.prompts[0]


class TestRepairLoop:
    def test_reprompts_once_after_a_malformed_reply(self) -> None:
        runtime = FakeRuntime("not json at all", _valid_response("alpha"))
        result = classify_with_runtime(
            runtime,
            primitives=_primitives("alpha"),
            ceilings=CEILINGS,
            project_root="/repo",
            max_repairs=2,
        )
        assert set(result.primitives) == {"alpha"}
        assert len(runtime.prompts) == 2

    def test_repair_prompt_names_the_defects(self) -> None:
        """A bare 'try again' wastes the retry; the defects must be quoted."""
        incomplete = json.dumps(
            {
                "domains": {"tools": {"description": "Tools."}},
                "primitives": {"alpha": {"domain": "tools", "audience": "public"}},
            }
        )
        runtime = FakeRuntime(incomplete, _valid_response("alpha", "beta"))
        classify_with_runtime(
            runtime,
            primitives=_primitives("alpha", "beta"),
            ceilings=CEILINGS,
            project_root="/repo",
            max_repairs=2,
        )
        assert "beta" in runtime.prompts[1], "repair prompt must name the missing primitive"

    def test_gives_up_after_the_retry_budget(self) -> None:
        runtime = FakeRuntime("bad", "still bad", "yet again bad")
        with pytest.raises(ClassificationError):
            classify_with_runtime(
                runtime,
                primitives=_primitives("alpha"),
                ceilings=CEILINGS,
                project_root="/repo",
                max_repairs=2,
            )
        assert len(runtime.prompts) == 3, "initial attempt + 2 repairs"

    def test_zero_budget_means_one_attempt(self) -> None:
        runtime = FakeRuntime("bad")
        with pytest.raises(ClassificationError):
            classify_with_runtime(
                runtime,
                primitives=_primitives("alpha"),
                ceilings=CEILINGS,
                project_root="/repo",
                max_repairs=0,
            )
        assert len(runtime.prompts) == 1


class TestRuntimeFailures:
    def test_a_runtime_exception_becomes_a_dispatch_error(self) -> None:
        """A crashed runtime must not surface as an opaque traceback."""

        class Exploding:
            def execute_prompt(self, prompt_content: str, **kwargs) -> str:
                raise RuntimeError("binary not found")

        with pytest.raises(DispatchError) as exc:
            classify_with_runtime(
                Exploding(),
                primitives=_primitives("alpha"),
                ceilings=CEILINGS,
                project_root="/repo",
            )
        assert "binary not found" in str(exc.value)

    def test_an_empty_reply_is_a_defect_not_a_crash(self) -> None:
        runtime = FakeRuntime("", "")
        with pytest.raises(ClassificationError):
            classify_with_runtime(
                runtime,
                primitives=_primitives("alpha"),
                ceilings=CEILINGS,
                project_root="/repo",
                max_repairs=1,
            )
