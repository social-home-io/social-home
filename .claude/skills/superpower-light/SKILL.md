---
name: superpower-light
description: A lean plan-then-subagent-driven delivery workflow for any non-trivial coding task — a feature, refactor, multi-file change, bug fix with real risk, or anything you'd open a PR for. Plan first (explore, ask only on genuine forks), decompose into bite-sized TDD tasks, implement each with a fresh subagent, review every stage in two passes (spec-compliance then code-quality; adversarial for security-sensitive code), do one final whole-change review, and ship behind green CI with docs updated. Use when work is more than a trivial edit, when correctness/security matters, or when the user wants careful, reviewed, high-quality delivery. Skip for one-line fixes, pure Q&A, or read-only exploration.
---

# superpower-light

Plan → decompose → implement (fresh subagent + TDD) → review each stage twice → final review → ship. The contract: **nothing reaches main unreviewed**, you (the orchestrator) delegate the *reading and writing* so your context stays clean for *judgment*, and **security + quality are gates, not afterthoughts**. Scale the ceremony to the risk.

**Announce each stage** as you enter it ("Planning…", "Task 3: implement", "Spec review", "Final review", "Shipping") so the user always knows where you are. **Track multi-task work** in a todo/task list and keep it current.

## The loop

```
Understand → Plan (get sign-off for big work)
  → for each task:  implement(subagent, TDD)  →  spec-review  →  quality-review  →  mark done
  → final whole-change review
  → ship (branch off main · green CI · PR + docs · merge on green)
```

### 1 · Understand & plan
- **Explore read-only first** — fan out parallel search/read agents over unfamiliar areas; keep conclusions, not file dumps. Find the existing pattern to mirror; don't reinvent what's there.
- **Ask only on genuine forks** — a decision that materially changes what you build and that you can't resolve from code/spec/sensible defaults. Offer 2–4 concrete options *before* finalizing the plan. Never ask about conventional defaults.
- **Write a concise plan**: Context (why) · recommended Approach (one, not a menu) · exact files to create/modify · task list · how to verify end-to-end. Get sign-off before large/expensive work.

### 2 · Decompose
- **Bite-sized, independently-shippable tasks** — each a vertical slice that compiles and tests green on its own; order so earlier tasks set up later ones; one commit per task.
- **Map the file layout up front** — one clear responsibility per file; follow the codebase's conventions, not your preferences.

### 3 · Implement — one fresh subagent per task
- **Hand it everything**: full task text, scene-setting context, the exact pattern/files to follow, and the verify command. Never say "read the plan" — construct precisely what it needs.
- **TDD**: write the failing test → run it and watch it fail *for the right reason* (a test you never saw fail proves nothing) → minimal implementation → run (green) → commit. Wrote implementation before its test? Delete it and rebuild from the test — don't keep it "for reference."
- **Pick the model tier by task**: fast/cheap for mechanical work with a complete spec; capable for integration/judgment or anything security-sensitive; strongest for design and review.
- **Isolate parallel work** — if you fan subagents out on *disjoint* files, give each its own git worktree so they can't collide. Prefer the harness's native worktree tool over raw `git worktree` (don't create phantom state the harness can't manage); detect existing isolation before making more.
- **Run continuously** — don't pause to ask "should I continue?" between tasks. Only stop for a real blocker or genuine ambiguity.
- Subagent ends with a status — DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT. On a block: add context, escalate the model, or split the task — never silently re-run the same thing.

### 4 · Review every stage — two passes, in order
After each task, before the next:
1. **Spec-compliance** (fresh subagent): *"Don't trust the implementer's report — read the actual code. Did it build exactly what was asked: nothing missing, nothing extra, no misread requirement?"* Fix gaps → re-review until ✅.
2. **Code-quality** (only once spec passes): correctness, clarity, reuse/DRY, tests that assert real behaviour (not mocks), one-responsibility files, no dead/over-built code. Fix → re-review.
- **Security-sensitive code earns an adversarial pass** — tell the reviewer to *break it*: auth bypass, injection, path traversal, secret/token leakage, crypto/encryption-first violations, unsafe deserialization, missing fail-closed, SSRF. Default verdict: unsafe until proven otherwise.
- **You verify independently too** — run the tests yourself and read the diff. Subagent review and your own eyes are both required; neither replaces the other. Never accept "close enough" on spec.
- **Acting on review feedback** (a subagent's *or* the human's): restate the point, verify it against the codebase, and apply it only if it's right *for this code* — push back with reasoning when it's wrong, ask when it's unclear (don't partial-implement a related set). No performative "you're absolutely right!"; fix one item at a time and re-test each.

### 5 · Final whole-change review
One reviewer over the entire diff for what per-task reviews can't see: cross-task integration, end-to-end coherence, and a last security sweep. Fix Critical/Important; record deferred items in the PR.

### 6 · Ship
- **Always ship via a PR — never commit or push to the default branch (main/master) directly.** Branch first, push the branch, open a PR, and let it merge through the PR. This holds even for a one-line fix and even when you have direct push rights. The only commits that touch main are PR merges.
- **Green CI, then hand off — don't merge your own PR.** Run the full gate (tests, lint, type-check, build) and watch CI to green, then leave the PR for the human to review and merge. Self-merge **only** when the user explicitly authorizes it for this session (e.g. "merge it once green") — that authorization is per-session and does not carry over to the next one.
- **PR hygiene** — tight user-facing title, correct label, and **update the matching docs in the same change** (API/protocol/schema/architecture as the repo expects). Stacked work: rebase onto the merged base.

## When something breaks (debug to root cause)
A failing test, bug, or unexpected behaviour is a **gate, not a detour** — no fix until you've found the cause.
1. **Read the actual error** — full message, stack, exit code. Don't skim past it.
2. **Reproduce** it reliably, in the smallest case.
3. **Find the root cause** — trace to the real source and confirm your hypothesis *before* changing code. (E.g. a blank sandboxed iframe → not "add a retry," but trace it: CORS block → CSP `'self'` mismatch → opaque origin → the real fix was inlining the bundle.)
4. **Fix the cause, not the symptom** — no band-aids, retries, or `sleep`s that just mask it.
5. **Add a regression test** that fails on the old behaviour and passes on the fix.
Under time pressure this is *faster* than guess-and-check thrashing.

## Non-negotiables (the gates)
- **PR-only — never commit/push to main directly.** Every change, however small, lands through a pull request. No direct commits to the default branch, ever.
- **Don't merge your own PRs.** Open it, drive CI to green, and hand off — the human merges. Merge yourself only on explicit, per-session authorization; never assume standing permission.
- **Verify before you claim — evidence, not adjectives.** Before any "done / fixed / passing / green," run the actual command *in this turn* and quote the real output (exit code, failure count). Ban "should / probably / seems to." A subagent's "success" report is not evidence — the diff and the test output are. If a check genuinely couldn't run (no dev server, offline, no browser), say so plainly; never claim a pass you didn't observe.
- **UI changes are visually verified** — desktop and mobile, plus a short UX pass (defaults, empty/error states, recovery, keyboard/a11y). Can't open a browser? Say so and tell the reviewer exactly what to check.
- **Security is a gate** — no secrets/credentials in code or payloads; fail closed; validate at trust boundaries; least privilege; verify external API/contract assumptions against real docs instead of guessing.
- **DRY + small** — reuse over new code; the smallest change that's correct; no speculative abstraction (YAGNI).
- **Match the codebase** — its patterns, naming, test layout, and idioms win over personal style.

## Scale the ceremony to the risk
- **Trivial** (typo, one-liner) → skip subagents/heavy review: branch, make the change, run the test, open a PR. (Still a PR — never a direct commit to main.)
- **Small** (1–3 task feature/fix) → a few-line plan, light review (your diff read + tests; add a quality subagent if it touches shared/critical code).
- **Substantial / security-critical / multi-PR** → the full loop, adversarial review on the risky parts, a final review, and stacked PRs that each ship working software.

## Anti-patterns
- Parallel implementer subagents on overlapping files (race/conflict) — parallelize only disjoint work; serialize commits.
- Letting the implementer's self-review replace an independent review.
- Starting code-quality review before spec-compliance is ✅.
- Committing build artifacts, committing to main, or bypassing a failing hook (`--no-verify`).
- Bloating your own context by reading everything yourself — delegate the reading, keep the conclusions.

## Pre-ship checklist
- [ ] Spec + quality review passed for every task (adversarial where security-sensitive)
- [ ] Final whole-change review done; Critical/Important addressed
- [ ] Any bug fixed at its root cause (+ a regression test), not patched at the symptom
- [ ] Full CI gate green (tests · lint · type-check · build), confirmed — not assumed
- [ ] UI verified on desktop + mobile (or explicitly noted as not runnable)
- [ ] No secrets; trust-boundary checks fail closed
- [ ] Change is on a branch + opened as a PR — never a direct commit/push to main
- [ ] CI green, then handed off for the human to merge (self-merge only if explicitly authorized this session)
- [ ] Docs updated in the same change; PR titled + labeled
