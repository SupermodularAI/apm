"""Redact or parametrize identifiers in a staged tree.

Rule **data** is always supplied by the caller (``--rules``); APM bundles
none.  What ships here is the *mechanism*:

* **Literal replacement, never regex.** Rule data is user-supplied, and a
  stray ``.`` or ``+`` in an email would silently over-match if treated as a
  pattern.
* **Allowlisted text extensions only.** A blind pass would corrupt images and
  archives that happen to contain the rule bytes.
* **Longest literal first**, so overlapping rules cannot leave a partially
  substituted remainder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Extensions eligible for rewriting.  Anything else is copied untouched.
TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".mdx",
        ".txt",
        ".json",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".yaml",
        ".yml",
        ".sh",
        ".bash",
        ".zsh",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".html",
        ".css",
        ".scss",
        ".svg",
        ".toml",
        ".ini",
        ".cfg",
        ".env",
        ".sql",
        ".xml",
    }
)

# S105 is suppressed below: this constant is the placeholder that *replaces*
# sensitive values. It is the opposite of a secret.
DEFAULT_REDACTION_TOKEN = "<REDACTED>"  # noqa: S105


class RulesError(Exception):
    """Raised when a rules file cannot be loaded or is unsafe to apply."""


@dataclass(frozen=True)
class ScrubRules:
    """Literals to remove or rename.

    ``redact`` literals become :attr:`redaction_token`; ``parametrize`` maps a
    literal to a specific placeholder so a consumer knows what to substitute.
    """

    redact: tuple[str, ...] = ()
    parametrize: dict[str, str] = None  # type: ignore[assignment]
    redaction_token: str = DEFAULT_REDACTION_TOKEN

    def __post_init__(self) -> None:
        if self.parametrize is None:
            object.__setattr__(self, "parametrize", {})

    @property
    def is_empty(self) -> bool:
        return not self.redact and not self.parametrize


def load_rules(path: Path) -> ScrubRules:
    """Load rules from *path*.

    Schema (all sections optional)::

        {
          "redact": ["someone@example.com"],
          "parametrize": {"U0123": "<SLACK_USER_ID>"},
          "redaction_token": "<REDACTED>"
        }
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RulesError(f"cannot read rules from {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RulesError(f"{path}: expected a JSON object at the top level")

    redact_raw = payload.get("redact", [])
    if not isinstance(redact_raw, list):
        raise RulesError(f"{path}: 'redact' must be a list")
    redact: list[str] = []
    for item in redact_raw:
        if not isinstance(item, str):
            raise RulesError(f"{path}: 'redact' entries must be strings")
        if not item:
            # An empty literal matches at every position and would shred the file.
            raise RulesError(f"{path}: 'redact' contains an empty literal")
        redact.append(item)

    param_raw = payload.get("parametrize", {})
    if not isinstance(param_raw, dict):
        raise RulesError(f"{path}: 'parametrize' must be an object")
    parametrize: dict[str, str] = {}
    for key, value in param_raw.items():
        if not isinstance(key, str) or not key:
            raise RulesError(f"{path}: 'parametrize' keys must be non-empty strings")
        if not isinstance(value, str) or not value:
            raise RulesError(f"{path}: 'parametrize[{key}]' must be a non-empty string token")
        parametrize[key] = value

    token = payload.get("redaction_token", DEFAULT_REDACTION_TOKEN)
    if not isinstance(token, str) or not token:
        raise RulesError(f"{path}: 'redaction_token' must be a non-empty string")

    return ScrubRules(redact=tuple(redact), parametrize=parametrize, redaction_token=token)


def _ordered_replacements(rules: ScrubRules) -> list[tuple[str, str]]:
    """Literal -> replacement, longest literal first.

    Ordering matters when one literal contains another: replacing the short one
    first would leave the long one unmatched and partially substituted.
    """
    pairs = [(lit, rules.redaction_token) for lit in rules.redact]
    pairs.extend(rules.parametrize.items())
    return sorted(pairs, key=lambda kv: len(kv[0]), reverse=True)


def scrub_text(text: str, rules: ScrubRules) -> str:
    """Apply *rules* to *text*. Pure; no I/O."""
    for literal, replacement in _ordered_replacements(rules):
        text = text.replace(literal, replacement)
    return text


def scrub_tree(root: Path, rules: ScrubRules) -> int:
    """Rewrite eligible files beneath *root* in place.

    Returns the number of files actually changed.  Files whose extension is not
    allowlisted, and files that are not valid UTF-8, are left untouched.
    """
    if rules.is_empty:
        return 0

    changed = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A text-suffixed file that is not decodable text: leave it alone
            # rather than fail the whole run.
            continue
        scrubbed = scrub_text(original, rules)
        if scrubbed != original:
            path.write_text(scrubbed, encoding="utf-8")
            changed += 1
    return changed
