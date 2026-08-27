"""Ask a runtime to classify a repository's primitives.

Dispatch goes through APM's own :class:`~apm_cli.runtime.base.RuntimeAdapter`
rather than a bespoke subprocess, so it inherits the process-group
termination, child TLS trust and timeout handling that already live there --
and a new runtime registered upstream works here without changes.

The model's answer is never trusted. It is validated by
:func:`~apm_cli.migrate.classification.parse_classification`, and on failure
re-prompted with the *specific* defects quoted back. That loop is what keeps a
non-deterministic step inside a deterministic pipeline: dispatch may vary, but
what leaves this module has been checked against the primitives that actually
exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from .classification import ClassificationError, parse_classification, summarise_defects
from .prompt import render_classification_prompt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from .classification import Classification
    from .enumerate import Primitive

#: How many times to re-prompt with the defects before giving up.  Two is
#: enough for a transient formatting slip without burning tokens on a runtime
#: that cannot do the task.
DEFAULT_MAX_REPAIRS = 2


class DispatchError(Exception):
    """Raised when the runtime itself cannot be used or fails to answer."""


class PromptRuntime(Protocol):
    """The slice of ``RuntimeAdapter`` this module needs.

    Declared structurally so tests can substitute a fake without importing
    APM's runtime machinery -- and so a real adapter satisfies it for free.
    """

    def execute_prompt(self, prompt_content: str, **kwargs: Any) -> str: ...


def available_runtime_names() -> tuple[str, ...]:
    """Runtime names APM can dispatch to, whether or not installed here."""
    from ..runtime.registry import adapter_descriptors

    return tuple(d.name for d in adapter_descriptors())


def resolve_runtime(name: str, *, model: str | None = None) -> PromptRuntime:
    """Return the adapter registered as *name*.

    Raises :class:`DispatchError` naming the known runtimes, rather than
    letting an unknown name fail somewhere deeper.
    """
    from ..runtime.factory import RuntimeFactory

    known = available_runtime_names()
    if name not in known:
        raise DispatchError(
            f"unknown runtime '{name}'. APM can dispatch to: {', '.join(known)}. "
            "Install one, or pass --classification with a response from elsewhere."
        )
    try:
        return RuntimeFactory.get_runtime_by_name(name, model)
    except Exception as exc:  # adapters raise varied exception types
        raise DispatchError(f"cannot initialise runtime '{name}': {exc}") from exc


def _repair_prompt(base_prompt: str, defects: list[str]) -> str:
    """Re-ask, quoting what was wrong with the previous answer."""
    return (
        f"{base_prompt}\n\n"
        "Your previous answer was rejected. Fix exactly these problems and "
        "return the corrected JSON only:\n\n"
        f"{summarise_defects(defects)}\n"
    )


def classify_with_runtime(
    runtime: PromptRuntime,
    *,
    primitives: tuple[Primitive, ...],
    ceilings: tuple[str, ...],
    project_root: Path | str,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    on_attempt: Any = None,
) -> Classification:
    """Classify *primitives* by asking *runtime*, repairing on rejection.

    Returns a validated :class:`Classification`.  Raises
    :class:`ClassificationError` if the retry budget is exhausted, or
    :class:`DispatchError` if the runtime itself fails.
    """
    from pathlib import Path as _Path

    base_prompt = render_classification_prompt(
        primitives=primitives,
        ceilings=ceilings,
        project_root=_Path(project_root),
    )

    prompt = base_prompt
    last_error: ClassificationError | None = None

    for attempt in range(max_repairs + 1):
        if on_attempt is not None:
            on_attempt(attempt, max_repairs)

        try:
            reply = runtime.execute_prompt(prompt)
        except Exception as exc:  # adapters raise varied exception types
            raise DispatchError(f"runtime failed to answer: {exc}") from exc

        try:
            return parse_classification(reply, primitives=primitives, ceilings=ceilings)
        except ClassificationError as exc:
            last_error = exc
            prompt = _repair_prompt(base_prompt, exc.defects)

    assert last_error is not None  # noqa: S101 - loop always runs at least once
    raise last_error
