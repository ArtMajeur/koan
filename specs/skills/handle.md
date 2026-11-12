---
type: skill-spec
title: "Skill Spec — handle"
description: "Specifies the `/handle` skill, a resumable state machine that carries a GitHub issue from triage to a review-ready PR by chaining existing skills as separate missions and stopping at human gates."
tags: [skill]
created: 2026-11-09
updated: 2026-11-12
---

# Skill Spec — `handle`

## Command(s)

- **Primary:** `/handle <github-issue-or-pr-url> [context]`
- **Group:** `code`
- **GitHub:** `@<bot> handle` on the issue, or on any pull request

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

A tick whose context starts with `after:<step>` was queued by the pipeline (a chained
follow-up, the CI relay, a scheduled re-check). Any other tick is a **human poke**. Only
pokes change the requester, re-enter a finished pipeline, or get a "busy" reply.

A chained tick runs only if its step is the one the current phase waits for
(`planning`←`plan`, `implementing`←`implement`, `ci`←`ci`/`ci-poll`, `reviewing`←`review`,
`fixing`←`fix`, `qa`←`qa-wait`, `qa_running`←`qa`); any other chained tick — e.g. a
re-check that fires after the pipeline gated — is ignored. A poke answers "busy" only
while a re-check event for the current phase is actually scheduled; otherwise (a retry
after an error, a lost event file) it re-evaluates the phase.

| Phase | Entered when | The tick does |
|---|---|---|
| *(none)* | first `/handle` | assign the bot, board → in-progress, run triage (LLM, read-only) |
| `planning` | triage says `go` | after `/plan`: read the plan's *Open Questions*; a single "none" line (optionally "— ready to implement", nothing else) → `implementing`, else gate |
| `implementing` | plan is clear | after `/implement`: the bot's own open PR on `<prefix>implement-<N>` from the base repository (never a fork's same-named branch), updated since the phase started → `ci` |
| `ci` | PR known | decide from the **PR head's own runs** (below) |
| `reviewing` | head CI green, or no CI | after `/review --force`: the review sidecar's `lgtm` for the **PR head** |
| `fixing` | review blocking, rounds left | after `/rebase --fix` → `ci` |
| `qa` | review lgtm, QA configured for the project | preview deployment for the PR head (or its merge commit) ready, with an `environment_url` → queue the QA command; else re-check later, at most `max_wait_ticks` times per wait |
| `qa_running` | QA queued | hand off, saying whether a QA report (marked comment) was posted |
| `done` | hand-off made | a poke: open PR → new fix round; merged → `stopped`; closed → restart from triage |
| `waiting_human` | any gate or error | a poke resumes the blocked step (`retry` re-runs the phase that errored) |
| `stopped` | PR closed/merged, or issue closed | a poke: merged PR → says so; issue still closed → says so; closed PR → restart from triage; open PR → new fix round |

### Pull-request pipeline

`/handle` on a pull request that no pipeline tracks starts a pipeline **for that PR**:
there is no issue to triage, plan or implement, so the PR itself is the subject. Its
record is keyed by the PR's canonical URL, holds the PR as its `pr_url`, and enters `ci`
directly; from there the phases above apply unchanged (CI → review/fix loop → optional QA
→ hand-off). The requester rule is the same, with the PR author as the fallback; an
`@<bot> handle` written as an inline review comment or in a review counts like one in
the PR's comment thread.

- **Where it differs:** the bot does not assign itself and makes no board moves (the
  board tracks issues, and a PR may not be on it); comments go to the PR thread. The
  hand-off requests no review when the requester is the PR's own author (GitHub refuses
  that request); the author is still pinged.
- **Entry checks:** to the API a PR is an issue, so a closed PR falls under the
  closed-issue rule: it does not start, and reopening it then poking resumes. A merged
  PR is answered as merged. A PR whose
  head lives in a fork is refused with a comment and nothing queued: `/rebase --fix`
  pushes to the head branch and the CI read looks up runs by branch name in the base
  repository, neither of which reaches a fork.
- **Re-entry:** a poke on a finished PR pipeline starts a new fix round while the PR is
  open; a closed PR is never restarted from triage (there is no issue to restart from).
- A PR that a tracked issue's pipeline opened keeps resolving to that issue, whether it
  is addressed as `/pull/<n>` or `/issues/<n>`: the issue's pipeline owns it. That holds
  when a PR pipeline also has a record for the same PR (the PR was handled on its own
  before its issue was): the issue's record wins for pokes and CI relays.

### CI decision (phase `ci`)

`check_existing_ci` reads `none` on a `gh` failure and aggregates the newest SHA among the
runs it finds, which can be the previous commit right after a force-push. The tick
therefore re-reads the branch runs, keeps those of the PR head, and decides:

| Head state | Action |
|---|---|
| success | `/review` |
| no run for the head (none on the branch, or only older commits') | re-check once after `poll_minutes`, then `/review` as "no CI" — a path-filtered or docs-only commit has none. Only that re-check (or a poke, if it was lost) concludes: a CI relay that finds no run for the head waits for it, scheduling one if none is left |
| pending, `ci_check` enabled | register in `## CI` (without resetting an existing entry); the relay wakes the tick |
| pending without `ci_check` | re-check after `poll_minutes`, at most `max_polls` times, then gate |
| failure still owned by `/ci_check` (in `## CI`, or no exhausted outcome relayed) | register and wait |
| failure after the queue gave up, or blocked on approval | gate |

Entering `ci` with `ready_before_ci: true` first takes a draft PR out of draft (a failure
gates): for a CI that skips drafts, a draft would otherwise read as "no CI" and the
review/fix loop would run without it. The default keeps the PR a draft until hand-off.

The hand-off states the head's CI as read at that moment ("green", "no CI ran", or the
state) — it never asserts green from an earlier signal.

## Inputs

| Input | Source | Required | Notes |
|---|---|---|---|
| issue or PR URL | command arg | yes | a PR URL tracked by an issue's pipeline resolves to that issue; any other PR URL is its own pipeline (see *Pull-request pipeline*); the API's `html_url` is the canonical key |
| `after:<step>` | command arg | no | set by the pipeline only; marks a chained tick |
| `handle:` | `config.yaml` | no | `max_review_rounds`, `poll_minutes`, `max_polls`, `ready_before_ci`, `board`, `product_team`, `qa` |
| `handle.qa` | `projects.yaml` | no | per project (or under `defaults`); overrides the global `handle.qa` key by key |

## Outputs / side effects

- **State:** `instance/.handle-state.json`, one record per canonical issue URL (phase,
  requester, PR, branch, review round, phase start, resume target, CI counters). Every
  read-modify-write holds a file lock; the CI relay checks and updates under one lock.
- **Missions:** each queued step is followed by `/handle <issue> after:<step>`; re-checks
  are one-shot events (`instance/events/`) queuing `/handle <issue> after:ci-poll|qa-wait`.
- **GitHub:** issue comments (triage, gates, errors, hand-off), self-assignment, PR marked
  ready, review requested from the requester, optional board column moves.
- **Chat:** at the normal messaging level a chained tick that succeeds is logged only (no
  `Done:` line per tick); a failed tick, a human `/handle` and every step mission still
  report as usual. See `docs/messaging/messaging-level.md`.

## Requester

- Set by a **human poke** only: the author of the latest `@<bot> handle` (or
  `@<bot> /handle`) comment **newer than the previous tick**, if authorized by the same
  rule as GitHub @mention commands (`github.authorized_users`, write access for `*`).
  The comment is parsed like any @mention command (`parse_mention_command`), so the
  instruction quoted from a gate comment in a code span is not a request.
  Persisted in the record. A poke from chat finds no newer mention and keeps the stored
  requester rather than handing the pipeline to an earlier commenter.
- Chained ticks never change it, so a drive-by mention mid-pipeline cannot take over the
  pings, the review request or the board column.
- With no authorized mention (started from chat), the issue author if authorized;
  otherwise nobody is mentioned and no review is requested.

## Chaining contract

- **Continuation without self-mention.** The follow-up tick is inserted right after the
  step it waits for, in one locked write. The queue is FIFO with one slot, so the tick runs
  after the step finished (success or failure).
- **A chained tick and a human poke are different requests.** The mission dedup
  signature includes a continuation tag (`after:<step>`) placed right after the URL, for
  any command. The running `/handle` is In Progress while it queues its own follow-up; the
  follow-up's tag gives it a different signature, so the regular dedup (Pending + In
  Progress) never drops it, and a pending leftover tick never swallows a human
  `@bot handle`. A poke in a step phase (`planning`, `implementing`, `reviewing`,
  `fixing`, `qa_running`) answers "busy" while that step's follow-up is still queued, and
  re-evaluates only once it is gone (a lost step).
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

A gate other than an error reopens only once a human (not a bot account) commented after
it was posted (`gated_at`) — on the issue or on its PR (comment, inline review comment or
review), since CI and review gates are often answered there — with the answer, or the
`@<bot> handle` itself. A poke queued while the
tick that opened the gate was still running predates the question: it does not redo the
step, and the bot replies that it is still waiting for an answer. A poke from chat
resumes once someone has replied in either thread.

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
  best-effort: a failure is logged and the pipeline continues. A failed board move is
  also reported on the issue (redacted, naming the column to set by hand): a stale board
  is otherwise invisible to the team. No board configured stays silent.
- Nothing instance-specific lives in code: board, team, QA command and preview
  environment pattern (`{pr}` is the only placeholder) come from `handle:` in `config.yaml`.
- QA is a per-project capability: a preview deployment belongs to one repository. A
  project's `handle.qa` in `projects.yaml` overrides the global one key by key, so QA can
  be configured for the projects that have previews only. A project left without a
  command or a preview environment never enters `qa`: an lgtm review hands off at once,
  with no wait for a preview that cannot come.
- The PR stays a draft until the hand-off (or until CI, with `ready_before_ci`);
  `/handle` never merges.
- The triage's free text (summary, questions) goes through `leak_detector` before it
  is posted: its prompt carries text any issue participant wrote, and the model can read files.

## Error cases

| Condition | Behavior |
|---|---|
| not a GitHub URL | failure, nothing queued |
| `github.nickname` not configured | fail fast, nothing posted |
| PR URL with no pipeline | start a pull-request pipeline at `ci` |
| PR from a fork, no pipeline yet | reply on the PR, nothing queued |
| PR closed or merged mid-pipeline | phase `stopped`, comment once |
| issue closed (a merged or closed PR is reported first) | closing the issue calls the pipeline off: the next tick that would act comments once and sets `stopped`; a `/handle` on a closed issue does not start. A step already queued still runs. Reopen the issue and poke to resume |
| any exception during a tick | error comment + `waiting_human` with `resume: retry` |

## Integration hooks

- **Handler:** `handler.py` queues `/handle <url>` for an issue or PR URL (Telegram, dashboard).
- **Runner:** `skills.core.handle.handle_runner`, registered in `_SKILL_RUNNERS`.
- **GitHub/Jira:** `github_enabled: true`, GitHub only (a Jira mention queues a mission
  that fails URL validation). `github_context_aware: false`: the tick reads the thread
  itself, so the mention's text stays out of the queue and a comment cannot carry an
  `after:` tag.
- **Autoreview guard:** best-effort — an unreadable state file is logged and autoreview
  proceeds, like the CI relay.
- **Assignment fallback:** `github_command_handler._try_assignment_notification` never
  queues `/implement` for an `assign` notification on an issue that has a `/handle` record,
  whatever its phase (`handle_state.tracks_issue`). The pipeline assigns the bot itself, and
  GitHub then reports later activity on the thread — any comment — under the `assign`
  reason: without the guard, the first comment on a gated issue starts `/implement` behind
  the gate. Only `@<bot> handle` moves such an issue. Best-effort like the autoreview guard:
  an unreadable state file is logged and the fallback proceeds.
- **CI queue:** `ci_queue_runner.drain_one()` calls `handle_state.relay_ci_outcome()`.
- **Autoreview:** `mission_runner._maybe_queue_autoreview()` skips a mission whose issue
  (or pull request, for a pull-request pipeline) an active `/handle` pipeline is keyed by
  (`handle_state.tracks_mission`): a second review/fix
  loop would push new heads under the pipeline and race its `/review` steps.
- **Plan:** `plan_runner.extract_open_questions()` — the same section boundaries the
  assumptions audit writes into.
- **Review:** reads `instance/.review-findings/<owner>_<repo>_<pr>.json`.
- **QA:** queues the configured QA command with the preview URL and the report marker; the
  QA skill owns its credentials.

## Known debt / watch-outs

- Triage emits a JSON verdict but has no golden-dataset eval yet.
- Triage's Read/Glob/Grep are not path-scoped to the checkout; redaction covers known
  token formats only, not arbitrary file contents. The tool list is the `allowed_tools`
  convention `/ask`, `/explain` and `/plan` share: a pre-approval, not a denial — with
  `skip_permissions: true` the provider does not restrict the session to it. The triage
  is read-only by instruction, not by enforcement.
- Comments go through `post_reply`, so the reply circuit breaker applies
  (`github.max_replies_per_thread_per_hour`, default 10): past it a tick fails, cannot
  post its error either, and the phase stays put until a poke. The operator gets the
  breaker's chat warning.
- Freshness and gate checks compare this host's clock with GitHub timestamps; a clock
  off by minutes misjudges them.
- A mention's text can redirect the command (shared @mention behavior): a GitHub issue or
  PR URL in the same comment as `@<bot> handle` becomes the target. Put a linked answer
  and the `@<bot> handle` in separate comments.
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
- Kōan must run as a **user account**: mention commands (`parse_mention_command`,
  `latest_handle_mention`) match `@<github.nickname>`, which an App identity — login
  `<slug>[bot]`, mentioned as `@<slug>` — never matches. Login comparisons fold only gh's
  `app/<slug>` spelling to REST's `<slug>[bot]`, so a user and an App of the same name
  never match.
