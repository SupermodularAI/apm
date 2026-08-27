"""Render the classification prompt dispatched to an agent runtime.

Kept as a pure function of its inputs so ``--dry-run`` shows *exactly* what
would be sent, and so the prompt can be asserted in tests without invoking an
agent.  No network, no subprocess, no I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .enumerate import Primitive

_INSTRUCTIONS = """\
You are classifying the primitives of a repository so they can be packaged and \
distributed with APM.

For EVERY primitive listed below, decide:

  domain      A short kebab-case grouping. Primitives in one domain ship
              together as one package. Derive the set from what the
              primitives actually do; do not invent empty domains.
  audience    One of: {ceilings}.
              Choose the LEAST restrictive audience the primitive is safe for.
              A primitive naming individuals, internal systems, or private
              data is not public.
  confidential
              true when the primitive must never ship in a shared package
              (it then needs its own standalone package).
  identifiers Literal strings in the primitive's files that identify a
              person, account, or private resource (emails, user IDs,
              database/page IDs). List them verbatim; they become
              parametrization rules. Do NOT guess -- only list strings you
              actually saw.

Also return one short description per domain, explaining what that domain
contains. A domain without a description cannot be packaged.

Return JSON only, matching this shape:

{{
  "domains": {{"<domain>": {{"description": "<one line>"}}}},
  "primitives": {{
    "<name>": {{
      "domain": "<domain>",
      "audience": "<audience>",
      "confidential": false,
      "identifiers": []
    }}
  }}
}}

Every primitive listed below MUST appear in "primitives". Omitting one is an
error, not an abstention.
"""


def render_classification_prompt(
    *,
    primitives: tuple[Primitive, ...],
    ceilings: tuple[str, ...],
    project_root: Path,
) -> str:
    """Build the prompt for *primitives*.

    Deterministic: the same inputs always produce the same text, so a
    ``--dry-run`` review is a faithful preview of the real dispatch.
    """
    lines = [
        _INSTRUCTIONS.format(ceilings=", ".join(ceilings)),
        "",
        f"Repository: {project_root}",
        f"Primitives ({len(primitives)}):",
        "",
    ]
    for prim in primitives:
        lines.append(f"  - {prim.name}  [{prim.kind}]  {prim.rel_path}")
    lines.append("")
    return "\n".join(lines)
