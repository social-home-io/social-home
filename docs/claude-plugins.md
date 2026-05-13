# Recommended Claude Code plugins

This repo ships a project-level `.claude/settings.json` that declares
the [Anthropic plugins marketplace][marketplace] and enables the eight
plugins below. Open the repo in Claude Code and the harness installs
them on first launch — no manual `/plugin` dance required.

[marketplace]: https://github.com/anthropics/claude-plugins-official

If a plugin is missing from your session, run
`/plugin marketplace add anthropics/claude-plugins-official` once, then
`/reload-plugins`.

## The stack

| Plugin | What it adds | Why we want it for socialhome |
|---|---|---|
| `superpowers` | Brainstorming, TDD, plans, parallel agents, code-review checklists | The §2 invariants and §24.11 pipeline make "think before you patch" the right default. The TDD and verification-before-completion skills keep the 90 % branch-coverage gate green. |
| `code-simplifier` | A specialised reviewer agent that suggests collapsing duplication | "Never add `*Addendum` / `*Extension` subclasses" lives in CLAUDE.md — this agent helps catch them before a PR lands. |
| `frontend-design` | A skill that drafts polished UI in the project's idiom | The Preact SPA in `client/` is the user-facing surface; this skill produces design-coherent components instead of generic chrome. |
| `github` | `gh`-aware review / PR / issue helpers | Day-to-day workflow is on GitHub. PR creation, code review, and issue triage all flow through these helpers. |
| `claude-code-setup` | The recommender that proposes hooks, subagents, and skills | Used when bootstrapping a new contributor's Claude config. |
| `chrome-devtools-mcp` | DevTools MCP — drive a real Chromium for UI verification | The devcontainer bakes in Chromium + Xvfb; this plugin is what makes "verify the change in Chrome" possible. See [`.claude/skills/chrome-devtools-setup/SKILL.md`](../.claude/skills/chrome-devtools-setup/SKILL.md). |
| `pyright-lsp` | Pyright over LSP for the backend | Catches type errors in `socialhome/` without leaving the editor. |
| `typescript-lsp` | TypeScript LSP for the SPA | Same, for `client/`. Pairs with the `pnpm run typecheck` pre-commit hook. |

## Project-local skills

In addition to the marketplace plugins, this repo carries two
project-local skills under `.claude/skills/`:

- **[`federation-demo`](../.claude/skills/federation-demo/SKILL.md)** —
  boots four households (a / b / c / d) on adjacent ports and walks
  the full §11 pairing + cross-household federation surface under the
  real WebRTC transport. Use when validating a federation change or
  smoke-testing an `aiolibdatachannel` bump.
- **[`chrome-devtools-setup`](../.claude/skills/chrome-devtools-setup/SKILL.md)** —
  troubleshoots the chrome-devtools-mcp plugin in this Debian-trixie
  devcontainer (Xvfb, `PUPPETEER_EXECUTABLE_PATH`, the `--no-sandbox`
  flags config).

## Adding or removing a plugin

Edit `.claude/settings.json` in the same commit. The file lives at
the project root so it travels with the branch — there's no separate
"recommended plugins" document to keep in sync.
