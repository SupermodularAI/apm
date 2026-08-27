"""Parse and validate an agent's classification response.

The dispatch that produces the response is non-deterministic; everything here
is not.  A response is either accepted whole, or rejected with a list of
*specific* defects the caller can re-prompt against -- never partially
accepted, because a half-classified repo silently drops primitives from every
package that follows.

The completeness check is deliberately stricter than os-dist's
``assertAllSkillsClassified``, which fails *open* when the target is not a git
repository: here, every enumerated primitive must be accounted for regardless
of how it was discovered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .enumerate import Primitive

#: ```json ... ``` or ``` ... ``` -- agents wrap JSON in fences more often than not.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class ClassifiedPrimitive:
    """One primitive's classification."""

    name: str
    domain: str
    audience: str
    #: Primitive kind ("skill", "agent", ...).  A name is unique only WITHIN a
    #: kind -- ``resolve-vm-id`` is a real skill and a real agent in the same
    #: repository -- so identity is (kind, name), never name alone.
    kind: str = ""
    #: Source path this classification resolved to, carried through so staging
    #: never has to re-resolve a name that maps to more than one file.
    rel_path: str = ""
    confidential: bool = False
    #: Literal strings the agent reported as identifying. Proposals for a human
    #: to confirm -- never applied automatically.
    identifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Domain:
    """A package grouping."""

    name: str
    description: str


@dataclass(frozen=True)
class Classification:
    """A validated, complete classification."""

    domains: dict[str, Domain]
    #: Keyed by ``(kind, name)`` -- see :class:`ClassifiedPrimitive`.
    primitives: dict[tuple[str, str], ClassifiedPrimitive]


@dataclass
class ClassificationError(Exception):
    """Raised when a response cannot be accepted.

    Carries every defect found, not just the first, so one repair round can
    address them all.
    """

    defects: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - exercised via str(exc)
        return summarise_defects(self.defects)


def summarise_defects(defects: list[str]) -> str:
    """Render *defects* as re-promptable text."""
    if not defects:
        return "classification rejected (no detail recorded)"
    head = f"classification rejected ({len(defects)} defect(s)):"
    return "\n".join([head, *(f"  - {d}" for d in defects)])


def _coerce_payload(raw: Any, defects: list[str]) -> dict | None:
    """Turn agent output into a mapping, or record why that failed."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        defects.append(f"expected JSON object or string, got {type(raw).__name__}")
        return None

    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    else:
        # Unfenced prose around a JSON object is common; take the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except ValueError as exc:
        defects.append(f"response is not valid JSON: {exc}")
        return None

    if not isinstance(parsed, dict):
        defects.append(f"expected a JSON object at the top level, got {type(parsed).__name__}")
        return None
    return parsed


def _parse_domains(payload: dict, defects: list[str]) -> dict[str, Domain]:
    raw = payload.get("domains")
    if not isinstance(raw, dict) or not raw:
        defects.append("'domains' must be a non-empty object")
        return {}

    domains: dict[str, Domain] = {}
    for name, spec in raw.items():
        description = spec.get("description") if isinstance(spec, dict) else None
        if not isinstance(description, str) or not description.strip():
            # writeApmCatalog throws rather than emit `description: undefined`;
            # catching it here means the defect is repairable by re-prompting.
            defects.append(f"domain '{name}' has no description")
            continue
        domains[name] = Domain(name=name, description=description.strip())
    return domains


def _parse_one(
    name: str,
    spec: Any,
    *,
    domains: dict[str, Domain],
    ceilings: tuple[str, ...],
    defects: list[str],
) -> ClassifiedPrimitive | None:
    if not isinstance(spec, dict):
        defects.append(f"primitive '{name}': expected an object")
        return None

    domain = spec.get("domain")
    if not isinstance(domain, str) or not domain:
        defects.append(f"primitive '{name}': missing 'domain'")
        return None
    if domain not in domains:
        defects.append(f"primitive '{name}': domain '{domain}' is not declared in 'domains'")
        return None

    audience = spec.get("audience")
    if audience not in ceilings:
        defects.append(
            f"primitive '{name}': audience '{audience}' is not one of {', '.join(ceilings)}"
        )
        return None

    identifiers = spec.get("identifiers") or ()
    if not isinstance(identifiers, (list, tuple)):
        defects.append(f"primitive '{name}': 'identifiers' must be a list")
        return None

    return ClassifiedPrimitive(
        name=name,
        domain=domain,
        audience=audience,
        confidential=bool(spec.get("confidential", False)),
        identifiers=tuple(str(i) for i in identifiers),
    )


def parse_classification(
    raw: Any,
    *,
    primitives: tuple[Primitive, ...],
    ceilings: tuple[str, ...],
) -> Classification:
    """Validate *raw* against the primitives that actually exist.

    Raises :class:`ClassificationError` carrying every defect found.
    """
    defects: list[str] = []

    payload = _coerce_payload(raw, defects)
    if payload is None:
        raise ClassificationError(defects)

    domains = _parse_domains(payload, defects)

    raw_primitives = payload.get("primitives")
    if not isinstance(raw_primitives, dict):
        defects.append("'primitives' must be an object")
        raise ClassificationError(defects)

    expected = {p.name for p in primitives}
    returned = set(raw_primitives)

    for missing in sorted(expected - returned):
        defects.append(f"primitive '{missing}' was not classified")
    for invented in sorted(returned - expected):
        defects.append(f"primitive '{invented}' does not exist in the repository")

    # The response is keyed by NAME (the shape a human or a runtime writes), but
    # identity is (kind, name).  One entry therefore classifies every primitive
    # sharing that name -- otherwise the second one is silently discarded.
    classified: dict[tuple[str, str], ClassifiedPrimitive] = {}
    for name in sorted(expected & returned):
        for prim in sorted(
            (p for p in primitives if p.name == name), key=lambda p: p.kind
        ):
            parsed = _parse_one(
                name,
                raw_primitives[name],
                domains=domains,
                ceilings=ceilings,
                defects=defects,
            )
            if parsed is not None:
                classified[(prim.kind, name)] = replace(
                    parsed, kind=prim.kind, rel_path=prim.rel_path
                )

    if defects:
        raise ClassificationError(defects)

    return Classification(domains=domains, primitives=classified)
