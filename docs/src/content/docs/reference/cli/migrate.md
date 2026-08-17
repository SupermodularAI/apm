---
title: apm migrate
description: Bring an existing repository of skills and agents into APM. The brownfield counterpart to `apm init`.
sidebar:
  order: 1
---

## Synopsis

```bash
apm migrate init [PATH] [--ceiling CEILING]... [--agent RUNTIME] [--max-repairs N]
                        [--out DIR] [--rules FILE] [--classification FILE]
                        [--require-rules|--allow-unscrubbed] [--skip-missing] [--dry-run] [-y] [-v]
apm migrate check [PATH] [--manifest FILE]

# Example
apm migrate init ./my-skills-repo --dry-run
```

## Description

`apm migrate` adopts a repository that **already contains** skills and agents into APM: it classifies each primitive, stages a copy, and writes the `apm.yml` manifest(s) that [`apm pack`](../pack/) consumes.

Use [`apm init`](../init/) for a new, empty project. Use `apm migrate init` when the primitives already exist and the repository simply is not an APM project yet.

:::note
"Migrate" here means **adoption**, not format upgrade. APM upgrades its own legacy formats automatically (lockfiles, `marketplace.yml`); that happens during `apm install` and needs no command.
:::

### Why a manifest producer

`apm pack` is already declarative -- it reads `apm.yml` to decide what to produce. What it has no concept of is *which* primitives belong in a given package. `apm migrate init` produces that decision as an [`includes:`](../../apm-yml/#includes) list and leaves emission untouched, so there is no second packer to keep in step.

## Subcommands

### `apm migrate init`

Classify a repository's primitives and write the manifests.

```bash
apm migrate init                          # current directory
apm migrate init ./repo --dry-run         # review the prompt, write nothing
apm migrate init ./repo --ceiling public  # one audience only
```

| Flag | Description |
|---|---|
| `PATH` | Optional positional; the repository to migrate. Defaults to the current directory. |
| `--ceiling` | Audience ceiling to emit; repeatable. One of `public`, `company`, `personal`. Defaults to all three. |
| `--agent` | APM runtime used to classify. Resolved from APM's runtime registry (today: `copilot`, `codex`, `llm`). |
| `--max-repairs` | How many times to re-prompt with the specific defects before giving up. Default `2`, range 0–5. |
| `--classification` | Consume a classification response from a file instead of dispatching. Reproducible and costs no tokens. |
| `--skip-missing` | Stage without primitives that are classified but absent from the working tree, instead of failing. |
| `--restricted-hook` | Hook script that must not ship below the top ceiling; repeatable. Use for scripts holding a credential. |
| `--out` | Staging directory for the copied tree and manifests. Default: `./.apm-migrate`. |
| `--rules` | Identifier/PII rules applied while staging. Never bundled with APM -- always supplied by you. |
| `--dry-run` | Print the classification prompt and exit without writing anything. |
| `--yes`, `-y` | Skip interactive confirmation. |
| `--verbose`, `-v` | Show detailed output. |

### `apm migrate check`

Validate a classification produced by `apm migrate init`: every primitive in the repository is classified exactly once, and every emitted manifest is well-formed.

```bash
apm migrate check ./repo
```

| Flag | Description |
|---|---|
| `PATH` | Optional positional; the repository to validate. Defaults to the current directory. |
| `--manifest` | Manifest to validate. Default: discovered under `./.apm-migrate`. |

## Classification

`apm migrate init` asks a runtime from APM's own registry to classify each primitive, then **validates the answer against the primitives that actually exist**. A response that omits a primitive, invents one, uses an unknown audience, or declares a domain without a description is rejected and re-prompted with those specific defects quoted back (`--max-repairs`, default 2).

The result is written to `<out>/classification.json` **as a proposal, not an authority**. Classification carries confidentiality and PII consequences, so review it before relying on the staged output. Re-run with `--classification <out>/classification.json` to reproduce a staging run exactly, without dispatching again.

Candidate identifiers the runtime reports are printed for review and are **never applied automatically** — add the ones you confirm to a `--rules` file.

## Hooks

Hooks are staged alongside skills and agents: `.claude/hooks/<file>` packs to `hooks/<file>` with its executable bit intact, and `.claude/hooks.json` packs to `hooks.json`. A hook script that loses `+x` is delivered but never fires, so the bit is preserved deliberately.

:::caution
A hook script frequently holds a **credential** for the service it talks to, and scrubbing cannot help — a token is not a known literal in any rules file. Mark such scripts with `--restricted-hook <name>` and they are withheld from every ceiling below the top one.

A descriptor that wires a withheld script is withheld too: shipping the wiring without its target leaves a hook command pointing at a file that was never staged.
:::

## How primitives are discovered

When the target is a **git repository**, discovery uses `git ls-files`, so untracked work-in-progress content is excluded -- a committed skill must be classified, but a scratch skill in your working tree is not part of any release.

When the target is **not** a git repository (a tarball, an export), that distinction cannot be made. `apm migrate init` falls back to a filesystem walk and says so, rather than implying a guarantee that is not in force.

## Staging and audiences

Output is written to a staging directory, never in place. Each ceiling gets its own `apm.yml` whose `includes:` enumerates exactly the primitives at or below that audience, so `apm pack` run against a staged tree produces an audience-appropriate package with no further filtering.

:::caution
`includes:` governs **packing**, not **installing**. `apm install <git-url>` against the source repository integrates everything present on disk, regardless of audience. Publish the packed artifact if audience separation must hold for consumers.
:::

:::caution[A directory holding only dotfiles does not survive install]
`apm pack` preserves a directory whose only entries are dotfiles (verified: both
`state/.gitignore` and `template/data/.gitkeep` reach the bundle), but
`apm install` does not recreate it on the consumer side. A directory containing
any non-dotfile comes through normally.

This matters when a primitive relies on an empty directory existing — a
placeholder the user is told to fill, or a path a script writes into. Either have
the script `mkdir -p` before writing, or ship a non-dotfile alongside. This is
**not** a general "dotfiles are dropped" rule: individual dotfiles inside a
directory that also holds regular files are unaffected.
:::

The staging directory must not be a harness directory (`.claude`, `.cursor`, `.codex`, `.opencode`, `.github`). APM's target auto-detection scans for those names, so writing a staged tree into one would change how the repository itself resolves targets. `apm migrate init` refuses rather than corrupt the repo it was pointed at.

## See also

- [`apm init`](../init/) -- scaffold a new, empty APM project.
- [`apm pack`](../pack/) -- produce distributable artifacts from the manifests this command writes.
