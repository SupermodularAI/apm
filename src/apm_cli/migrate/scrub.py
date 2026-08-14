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

#: Shortest literal accepted without an explicit opt-in.  A rule shorter than
#: this matches inside ordinary words: a generator bug once emitted the single
#: character "i", which rewrote every 'i' in every staged file to a placeholder
#: while still *looking* like tens of thousands of successful substitutions.
MIN_LITERAL_LENGTH = 4


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
    #: ``None`` means "not explicitly set" -- a scope then inherits the global
    #: token rather than silently reverting to the module default.
    redaction_token: str | None = DEFAULT_REDACTION_TOKEN
    #: Rules scoped to a single primitive, overriding the global set for it.
    primitives: dict[str, ScrubRules] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.parametrize is None:
            object.__setattr__(self, "parametrize", {})
        if self.primitives is None:
            object.__setattr__(self, "primitives", {})

    @property
    def is_empty(self) -> bool:
        return not self.redact and not self.parametrize and not self.primitives

    def for_primitive(self, name: str) -> ScrubRules:
        """Return the rules that apply to *name*.

        A scope is **additive** to the global set -- it adds its own literals
        rather than discarding the shared ones -- and **wins on conflict**, so
        a literal the global set redacts can be parametrized for one primitive.
        Without that precedence the specific intent would be unreachable.
        """
        scope = self.primitives.get(name)
        if scope is None:
            return self

        # Precedence, strongest first:
        #   scope.parametrize > scope.redact > global.parametrize > global.redact
        # Only the scope's OWN rules override the global ones. Letting a global
        # parametrize suppress a scope's explicit redact would invert the point
        # of scoping -- an author who redacted a name locally would silently get
        # the shared token instead.
        parametrize = {
            lit: token for lit, token in self.parametrize.items() if lit not in scope.redact
        }
        parametrize.update(scope.parametrize)

        # A literal this scope parametrizes must not also be redacted, or the
        # two replacements race within one pass.
        redact = tuple(
            dict.fromkeys(lit for lit in (*scope.redact, *self.redact) if lit not in parametrize)
        )
        return ScrubRules(
            redact=redact,
            parametrize=parametrize,
            redaction_token=(
                scope.redaction_token if scope.redaction_token is not None else self.redaction_token
            ),
        )


def load_rules(path: Path) -> ScrubRules:
    """Load rules from *path*.

    Schema (all sections optional)::

        {
          "redact": ["someone@example.com"],
          "parametrize": {"U0123": "<SLACK_USER_ID>"},
          "redaction_token": "<REDACTED>",
          "allow_short_literals": false,
          "primitives": {
            "invoice": {"parametrize": {"someone@example.com": "<FINANCE>"}}
          }
        }

    A ``primitives`` scope is additive to the global set and wins on conflict,
    so one literal can be redacted generally and parametrized for a named
    primitive.

    Literals shorter than :data:`MIN_LITERAL_LENGTH` are rejected unless
    ``allow_short_literals`` is set: they match inside ordinary words and
    corrupt every file they touch.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RulesError(f"cannot read rules from {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RulesError(f"{path}: expected a JSON object at the top level")

    allow_short = bool(payload.get("allow_short_literals", False))

    def _check_length(literal: str, where: str) -> None:
        if not allow_short and len(literal) < MIN_LITERAL_LENGTH:
            raise RulesError(
                f"{path}: {where} literal {literal!r} is dangerously short "
                f"(< {MIN_LITERAL_LENGTH} chars). It would match inside ordinary words "
                "and corrupt every file it touches. Set 'allow_short_literals': true "
                "only if you are certain."
            )

    def _parse_block(block: dict, where: str) -> ScrubRules:
        """Parse one rules block. Shared by the global set and each scope, so
        validation can never differ between them."""
        redact_raw = block.get("redact", [])
        if not isinstance(redact_raw, list):
            raise RulesError(f"{path}: {where}'redact' must be a list")
        redact: list[str] = []
        for item in redact_raw:
            if not isinstance(item, str):
                raise RulesError(f"{path}: {where}'redact' entries must be strings")
            if not item:
                # An empty literal matches at every position and would shred the file.
                raise RulesError(f"{path}: {where}'redact' contains an empty literal")
            _check_length(item, f"{where}'redact'")
            redact.append(item)

        param_raw = block.get("parametrize", {})
        if not isinstance(param_raw, dict):
            raise RulesError(f"{path}: {where}'parametrize' must be an object")
        parametrize: dict[str, str] = {}
        for key, value in param_raw.items():
            if not isinstance(key, str) or not key:
                raise RulesError(f"{path}: {where}'parametrize' keys must be non-empty strings")
            if not isinstance(value, str) or not value:
                raise RulesError(
                    f"{path}: {where}'parametrize[{key}]' must be a non-empty string token"
                )
            _check_length(key, f"{where}'parametrize'")
            parametrize[key] = value

        # Absent inside a scope means "inherit"; absent at the top level means
        # the module default.
        default_token = DEFAULT_REDACTION_TOKEN if not where else None
        token = block.get("redaction_token", default_token)
        if token is not None and (not isinstance(token, str) or not token):
            raise RulesError(f"{path}: {where}'redaction_token' must be a non-empty string")

        return ScrubRules(redact=tuple(redact), parametrize=parametrize, redaction_token=token)

    globals_ = _parse_block(payload, "")

    scopes_raw = payload.get("primitives", {})
    if not isinstance(scopes_raw, dict):
        raise RulesError(f"{path}: 'primitives' must be an object keyed by primitive name")
    primitives: dict[str, ScrubRules] = {}
    for name, block in scopes_raw.items():
        if not isinstance(name, str) or not name:
            raise RulesError(f"{path}: 'primitives' keys must be non-empty strings")
        if not isinstance(block, dict):
            raise RulesError(f"{path}: 'primitives[{name}]' must be an object")
        primitives[name] = _parse_block(block, f"primitives[{name}] ")

    return ScrubRules(
        redact=globals_.redact,
        parametrize=globals_.parametrize,
        redaction_token=globals_.redaction_token,
        primitives=primitives,
    )


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
