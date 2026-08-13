"""Adoption of an existing repository of primitives into APM.

Supports ``apm migrate init`` / ``apm migrate check``.  Kept separate from
``apm_cli.commands.migrate`` so the pipeline stages are unit-testable without
going through Click.
"""

from __future__ import annotations
