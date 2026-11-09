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

| Phase | Entered when | The tick does |
|---|---|---|
| *(none)* | first `/handle` | assign the bot, board → in-progress, run triage (LLM, read-only) |
| `planning` | triage says `go` | after `/plan`: read the plan's *Open Questions*; empty → `implementing`, else gate |
| `implementing` | plan is clear | after `/implement`: find the open PR on `<prefix>implement-<N>`, add it to `## CI` → `ci` |
| `ci` | PR registered | wait; the `## CI` queue relays its terminal outcome back |
| `reviewing` | CI green (or no CI) | after `/review --force`: read the review sidecar's `lgtm` |
| `fixing` | review blocking, rounds left | after `/rebase --fix`: re-register the PR in `## CI` → `ci` |
| `qa` | review lgtm, QA configured | find the PR's preview deployment for the PR head; queue the QA command, or schedule a re-check (`instance/events/`, every `wait_minutes`, at most `max_wait_ticks`, then hand off without QA) |
| `done` | QA ran or is off | PR ready for review, requester asked to review, board → review column, ping |
| `waiting_human` | any gate | ping the requester; the next human `/handle` re-runs the blocked step |
| `stopped` | PR closed/merged | nothing |

## Inputs

| Input | Source | Required | Notes |
|---|---|---|---|
| issue URL | command arg | yes | GitHub issues only; a PR URL tracked by `/handle` resolves to its issue |
| trailing context | command arg | no | ignored by the tick (the triage reads the issue thread) |
| `handle:` | `config.yaml` | no | `max_review_rounds`, `board`, `product_team`, `qa` — all optional |

## Outputs / side effects

- **State:** `instance/.handle-state.json`, one record per issue URL (phase, PR URL,
  review round, phase start time, resume phase, last CI outcome). Written under a lock.
- **Missions:** each queued step is followed by `/handle <issue> after:<step>`.
- **GitHub:** issue comments (triage result, gates, hand-off), self-assignment, PR marked
  ready for review, review requested from the requester, optional board column moves.
- **Requester:** the author of the latest `@<bot> handle` comment on the issue (not the
  bot); falls back to the issue author. Every gate and the hand-off mention them.

## Chaining contract

- **Continuation without self-mention.** The follow-up tick is a queued mission
  `/handle <issue> after:<step>`, inserted right after the step it waits for. The queue is
  FIFO with one slot, so the tick runs after the step finished (success or failure).
- **Dedup against Pending only.** The running `/handle` mission is In Progress while it
  queues its own follow-up; the regular dedup (Pending + In Progress) would drop it. The
  follow-up is deduplicated against Pending only, and its text differs from the running
  one (`after:<step>`) so the two entries are never confused.
- **CI relay.** `ci_queue_runner.drain_one()` relays every *terminal* outcome
  (`success`, `failure` after the last attempt, `none`, `blocked_approval`, PR
  closed/merged) for a PR whose `/handle` record is in phase `ci`: it stores the outcome
  and queues `/handle <issue>`. Non-terminal outcomes (pending, failure with attempts
  left) are not relayed — `/ci_check` keeps fixing.
- **Freshness.** A step's output counts only if newer than the phase start: the plan
  comment's `created_at`, the review sidecar's `timestamp`. Stale or missing output means
  the step failed → gate.

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
  `first_output_timeout - 60` seconds. A triage that fails or stalls gates on the requester
  instead of leaving the issue parked in phase `triage`.

## Gates (stop and ping the requester)

| Gate | Resume (next human `/handle`) |
|---|---|
| triage `needs_human` or `not_a_bug` | re-run triage (the thread now holds the answers) |
| plan has open questions / no *Open Questions* section | re-run `/plan` (iterates on the thread) |
| no PR after `/implement` | re-run `/implement` |
| CI failed after the last `/ci_check`, or blocked on approval | re-register the PR in `## CI` |
| review still blocking after `max_review_rounds` (default 3) | one more review/fix round |
| review produced no fresh verdict | re-run `/review` |

## Invariants

- The tick itself never edits code; code changes come only from `/implement` and
  `/rebase --fix`.
- Blocking = the review's `lgtm` is false, i.e. at least one `critical` or `warning`
  finding (the review schema's own rule). Suggestions never block.
- Review rounds are bounded by `max_review_rounds`; CI fixes stay bounded by
  `ci_fix_max_attempts` (the `## CI` contract is unchanged).
- Board moves, team lookup, assignment and QA are best-effort: a failure is logged and
  the pipeline continues. Comments and state writes are not best-effort.
- Nothing instance-specific lives in code: board, team, QA command and preview
  environment pattern come from `handle:` in `config.yaml`.
- The PR stays a draft until the hand-off; `/handle` never merges.
- The triage's free text (summary, questions) goes through `leak_detector` before it
  is posted: its prompt carries text any issue participant wrote, and the model can read files.

## Error cases

| Condition | Behavior |
|---|---|
| URL is not a GitHub issue | usage reply, nothing queued |
| PR closed or merged mid-pipeline | phase `stopped`, comment once |
| triage output unparseable | gate (`needs_human`) with the raw summary |
| `gh` failure on a required read | gate with the error; state kept for resume |

## Integration hooks

- **Handler:** `handler.py` queues `/handle <url>` (Telegram, dashboard).
- **Runner:** `skills.core.handle.handle_runner`, registered in `_SKILL_RUNNERS`.
- **GitHub/Jira:** `github_enabled: true`, `github_context_aware: true`; GitHub only.
- **CI queue:** `ci_queue_runner.drain_one()` calls `handle_state.relay_ci_outcome()`.
- **Review:** reads `instance/.review-findings/<owner>_<repo>_<pr>.json`.
- **QA:** queues the configured QA command with the preview URL; the QA skill owns its
  own credentials and reporting.

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
