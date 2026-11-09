---
type: skill-spec
title: "Skill Spec — handle"
description: "Specifies the `/handle` skill, a resumable state machine that carries a GitHub issue from triage to a review-ready PR by chaining existing skills as separate missions and stopping at human gates."
tags: [skill]
created: 2026-11-09
updated: 2026-11-09
---

# Skill Spec — `handle`

## Command(s)

- **Primary:** `/handle <github-issue-url> [context]`
- **Group:** `code`
- **GitHub:** `@<bot> handle` on the issue (or on the PR it opened)

## Purpose

Carry one GitHub issue end to end — triage, plan, implementation, CI, review/fix loop,
optional browser QA, hand-off — without a human re-invoking each skill. It exists because
every step already has a skill, but nothing chains them, and the bot cannot wake itself:
GitHub never notifies an account of its own @mention, and Kōan ignores its own comments.

## Model

`/handle` is a **tick**. Each run reads the issue's stored phase, does the cheap work of
that phase, and either queues the next step or stops at a gate. Heavy work always runs
as its **own mission** (`/plan`, `/implement`, `/review`, `/rebase --fix`, the QA
command), so every step gets a fresh model context and its own timeout and budget.

A tick whose context starts with `after:` was queued by the pipeline (a chained
follow-up, the CI relay, a scheduled re-check). Any other tick is a **human poke**. Only
pokes change the requester, re-enter a finished pipeline, or get a "busy" reply.

| Phase | Entered when | The tick does |
|---|---|---|
| *(none)* | first `/handle` | assign the bot, board → in-progress, run triage (LLM, read-only) |
| `planning` | triage says `go` | after `/plan`: read the plan's *Open Questions*; a single "none" line → `implementing`, else gate |
| `implementing` | plan is clear | after `/implement`: the open PR on `<prefix>implement-<N>`, updated since the phase started → `ci` |
| `ci` | PR known | decide from the **PR head's own runs** (below) |
| `reviewing` | head CI green, or no CI | after `/review --force`: the review sidecar's `lgtm` for the **PR head** |
| `fixing` | review blocking, rounds left | after `/rebase --fix` → `ci` |
| `qa` | review lgtm, QA configured | preview deployment for the PR head (or its merge commit) ready → queue the QA command; else re-check later |
| `qa_running` | QA queued | hand off, saying whether a QA report (marked comment) was posted |
| `done` | hand-off made | a poke: open PR → new fix round; merged → `stopped`; closed → restart from triage |
| `waiting_human` | any gate or error | a poke resumes the blocked step (`retry` re-runs the phase that errored) |
| `stopped` | PR closed/merged | a poke on a closed PR restarts from triage |

### CI decision (phase `ci`)

`check_existing_ci` reads `none` on a `gh` failure and aggregates the newest SHA among the
runs it finds, which can be the previous commit right after a force-push. The tick
therefore re-reads the branch runs, keeps those of the PR head, and decides:

| Head state | Action |
|---|---|
| success | `/review` |
| no runs on the branch at all | re-check once after `poll_minutes`, then `/review` (hand-off says no CI ran) |
| pending, `ci_check` enabled | register in `## CI` (without resetting an existing entry); the relay wakes the tick |
| pending without `ci_check`, or no run for the head yet | re-check after `poll_minutes`, at most `max_polls` times, then gate |
| failure still owned by `/ci_check` (in `## CI`, or no exhausted outcome relayed) | register and wait |
| failure after the queue gave up, or blocked on approval | gate |

The hand-off states the head's CI as read at that moment ("green", "no CI ran", or the
state) — it never asserts green from an earlier signal.

## Inputs

| Input | Source | Required | Notes |
|---|---|---|---|
| issue URL | command arg | yes | a PR URL tracked by `/handle` resolves to its issue; the API's `html_url` is the canonical key |
| `after:<step>` | command arg | no | set by the pipeline only; marks a chained tick |
| `handle:` | `config.yaml` | no | `max_review_rounds`, `poll_minutes`, `max_polls`, `board`, `product_team`, `qa` |

## Outputs / side effects

- **State:** `instance/.handle-state.json`, one record per canonical issue URL (phase,
  requester, PR, branch, review round, phase start, resume target, CI counters). Every
  read-modify-write holds a file lock; the CI relay checks and updates under one lock.
- **Missions:** each queued step is followed by `/handle <issue> after:<step>`; re-checks
  are one-shot events (`instance/events/`) queuing `/handle <issue> after:ci-poll|qa-wait`.
- **GitHub:** issue comments (triage, gates, errors, hand-off), self-assignment, PR marked
  ready, review requested from the requester, optional board column moves.

## Requester

- Set by a **human poke** only: the author of the latest `@<bot> handle` (or
  `@<bot> /handle`) comment, if authorized by the same rule as GitHub @mention commands
  (`github.authorized_users`, write access for `*`). Persisted in the record.
- Chained ticks never change it, so a drive-by mention mid-pipeline cannot take over the
  pings, the review request or the board column.
- With no authorized mention (started from chat), the issue author if authorized;
  otherwise nobody is mentioned and no review is requested.

## Chaining contract

- **Continuation without self-mention.** The follow-up tick is inserted right after the
  step it waits for, in one locked write. The queue is FIFO with one slot, so the tick runs
  after the step finished (success or failure).
- **Dedup against Pending only.** The running `/handle` is In Progress while it queues its
  own follow-up; the regular dedup (Pending + In Progress) would drop it. The follow-up is
  deduplicated against Pending only. Human and relay `/handle` lines keep the regular dedup.
- **CI relay.** `ci_queue_runner.drain_one()` relays every *terminal* outcome (`success`,
  `failure` after the last attempt, `none`, `blocked_approval`, PR closed/merged) for a PR
  whose record is in phase `ci`. It only wakes the tick; the tick re-reads the head's runs.
- **Freshness.** A step's output counts only if it belongs to this attempt: the plan
  comment is newer than the phase start; the PR was updated since the phase start; the
  review sidecar's `head_sha` is the PR head and its `timestamp` follows the phase start;
  the QA report is a bot comment on the PR carrying the `<!-- koan-handle-qa -->` marker.

## Timeouts

- **Step missions** run under their own budgets (`skill_timeout`, `first_output_timeout`,
  `ci_check.timeout`, `mission_timeout` for a prompt-only QA skill). A step killed by a
  timeout is marked Failed and never retried, so the queued tick that follows it runs next
  and finds no fresh output → gate (or, after QA, a hand-off that says QA left no report).
- **Requeued or recovered steps** (quota, auth, stagnation, crash recovery) go back to the
  **top** of Pending, ahead of their follow-up tick, so a tick never judges a step that
  has not run yet.
- **The tick itself** is short except for the triage, which streams provider events (one
  stdout line each, keeping the skill liveness watchdog alive) under an `idle_timeout` of
  `first_output_timeout - 60` seconds. A triage that fails or stalls gates on the requester.

## Gates (stop and ping the requester)

| Gate | Resume (next human `/handle`) |
|---|---|
| triage `needs_human` or `not_a_bug`, or no usable verdict | re-run triage (the thread now holds the answers) |
| plan has open questions (incl. a folded assumptions audit) / no fresh plan | re-run `/plan` |
| no PR, or no PR updated by this `/implement` | re-run `/implement` |
| CI failed after the queue gave up, blocked on approval, or no head run after `max_polls` | back to `ci` with fresh counters |
| review still blocking after `max_review_rounds` fix rounds | one more fix round |
| no review verdict for the PR head | re-run `/review` |
| unexpected error (any exception in a tick) | `retry`: re-run the phase that errored |

## Invariants

- The tick itself never edits code; code changes come only from `/implement` and
  `/rebase --fix`.
- Blocking = the review's `lgtm` is false, i.e. at least one `critical` or `warning`
  finding (the review schema's own rule). Suggestions never block.
- `max_review_rounds` (default 3) bounds **fix** rounds, so a PR gets at most N+1 reviews.
  CI fixes stay bounded by `ci_fix_max_attempts` (the `## CI` contract is unchanged).
- A tick never ends silently: every exception is caught at the runner boundary, reported
  on the issue (redacted, mentioning the stored requester), and leaves a resumable gate.
- Board moves, team lookup, assignment, the review request and marking the PR ready are
  best-effort: a failure is logged and the pipeline continues.
- Nothing instance-specific lives in code: board, team, QA command and preview
  environment pattern (`{pr}` is the only placeholder) come from `handle:` in `config.yaml`.
- The PR stays a draft until the hand-off; `/handle` never merges.
- The triage's free text (summary, questions) goes through `leak_detector` before it
  is posted: its prompt carries text any issue participant wrote, and the model can read files.

## Error cases

| Condition | Behavior |
|---|---|
| not a GitHub URL | failure, nothing queued |
| `github.nickname` not configured | fail fast, nothing posted |
| PR URL with no pipeline | reply on the PR, nothing queued |
| PR closed or merged mid-pipeline | phase `stopped`, comment once |
| any exception during a tick | error comment + `waiting_human` with `resume: retry` |

## Integration hooks

- **Handler:** `handler.py` queues `/handle <url>` for an issue or PR URL (Telegram, dashboard).
- **Runner:** `skills.core.handle.handle_runner`, registered in `_SKILL_RUNNERS`.
- **GitHub/Jira:** `github_enabled: true`, `github_context_aware: true`; GitHub only.
- **CI queue:** `ci_queue_runner.drain_one()` calls `handle_state.relay_ci_outcome()`.
- **Plan:** `plan_runner.extract_open_questions()` — the same section boundaries the
  assumptions audit writes into.
- **Review:** reads `instance/.review-findings/<owner>_<repo>_<pr>.json`.
- **QA:** queues the configured QA command with the preview URL and the report marker; the
  QA skill owns its credentials.

## Known debt / watch-outs

- Triage emits a JSON verdict but has no golden-dataset eval yet.
- Triage's Read/Glob/Grep are not path-scoped to the checkout; redaction covers known
  token formats only, not arbitrary file contents.
- Board support targets organization-owned Projects (v2); the token needs `project`
  scope, and the team lookup needs `read:org`.
- A plan whose *Open Questions* heading is translated or renamed is treated as unclear
  (conservative gate).
- QA output is not parsed: QA is informational and its comment is linked in the hand-off.
- Triage reads the project checkout as it stands; it does not switch to the default
  branch first, so a checkout left on another branch skews the assessment.
- `/implement` is assumed to open its PR on `<prefix>implement-<N>` in the issue's own
  repository; an issue tracked in another repo than the code gates as "no PR".
- A preview deployment is matched on the PR head or its merge commit; a deployment keyed
  on another SHA never reads as ready and the hand-off says the preview was not ready.
