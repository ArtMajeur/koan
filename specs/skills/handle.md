---
type: skill-spec
title: "Skill Spec — handle"
description: "Specifies the `/handle` skill, a resumable state machine that carries a GitHub issue from triage to a review-ready PR by chaining existing skills as separate missions and stopping at human gates."
tags: [skill]
created: 2026-11-09
updated: 2026-11-25
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
`fixing`←`fix`, `qa`←`qa-wait`, `qa_running`←`qa`, `delegated`←`prs`); any other chained tick — e.g. a
re-check that fires after the pipeline gated — is ignored. A poke answers "busy" only
while a re-check event for the current phase is actually scheduled; otherwise (a retry
after an error, a lost event file) it re-evaluates the phase.

| Phase | Entered when | The tick does |
|---|---|---|
| *(none)* | first `/handle` | assign the bot, board → in-progress, run triage (LLM, read-only) |
| `planning` | triage says `go` | after `/plan`: read the plan's *Open Questions*; a single "none" line (optionally "— ready to implement", which may be followed by a note with no question mark on the same line) → `implementing`, else gate |
| `implementing` | plan is clear | after `/implement`: the bot's own open PR on `<prefix>implement-<N>` from the base repository (never a fork's same-named branch), updated since the phase started → `ci` |
| `ci` | PR known | decide from the **PR head's own runs** (below) |
| `reviewing` | head CI green, or no CI | after `/review --force`: the review sidecar's `lgtm` for the **PR head** |
| `fixing` | review blocking, rounds left | after `/rebase --fix` → `ci` |
| `qa` | review lgtm, QA configured for the project | preview deployment for the PR head (or its merge commit) ready, with an `environment_url` → queue the QA command; else re-check later, at most `max_wait_ticks` times per wait |
| `qa_running` | QA queued | hand off, saying whether a QA report (marked comment) was posted |
| `delegated` | the issue's pull requests run as their own pipelines (below) | a roll-up tick: every PR handed off → summary, board, `done`; a poke: status, and the gated PRs are poked |
| `done` | hand-off made | a poke: open PR → new fix round; merged → `stopped`; closed → restart from triage |
| `waiting_human` | any gate or error | a poke resumes the blocked step (`retry` re-runs the phase that errored) |
| `stopped` | PR closed/merged, or issue closed | a poke: merged PR → says so; issue still closed → says so; closed PR → restart from triage; open PR → new fix round |

Every entry into `ci` (after `/implement`, after a fix round, when a PR pipeline starts,
on a resume) first runs the **data-change check** below; a flagged change gates before
CI is read. Queuing a review runs it again: `/ci_check` may have pushed fixes during `ci`.
So does the hand-off: a commit pushed after the review was queued (or while a preview
deployed) is checked before the PR is handed off; a flagged change gates instead.

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

### Issue with pull requests of its own

An issue can be closed by pull requests that already exist, that live in another
repository than the issue (an issue tracked in a parent repository, its code in the
repositories it groups), or by several of them. The **linked pull requests** of an issue
are the open PRs GitHub lists as closing it (`closedByPullRequestsReferences`: a closing
keyword in the PR body, or the issue's *Development* links), whoever authored them. A PR
from a fork, or in a repository no project is configured for, cannot be handled: it is
named in the status comment and otherwise ignored. So is a PR that another issue's own
pipeline tracks (it closes both issues; that pipeline owns it), and a PR this issue does
not track yet in a repository where the requester may not command the bot. Rights decide
what a poke may **start**, never which PRs the issue **waits for**: a PR already in the
issue's list stays in it, with its real state, whoever pokes. When none of the linked
PRs can be the issue's, the issue goes on as it does without any (triage). When
GitHub cannot list the linked PRs (a linked repository the token cannot read), triage and
the PR an `/implement` opened go on as if there were none; a poke on a `delegated` issue
fails instead (error gate), since "none" would there mean "start over".

- **When they are looked up.** Before every triage (first `/handle`, a poke at a triage
  gate): an issue that already has linked PRs is not triaged, planned or implemented
  again. After `/implement`, when the `<prefix>implement-<N>` branch has no fresh PR in
  the issue's own repository: the bot's own linked PRs updated since the phase started.
  And on every poke of a `delegated` issue.
- **One PR, in the issue's own repository:** the issue's record adopts it (`pr_url`) and
  enters `ci`: the single-PR pipeline above, unchanged.
- **Anything else** (a PR in another repository, or several PRs): the issue **delegates**.
  Its record lists the PRs (`prs`) and enters `delegated`. Each PR gets a pull-request
  pipeline of its own, keyed by its URL, under the project of **its** repository, so CI,
  the data-change check and QA follow that repository's `handle:` settings. The PR's
  record names the issue as its `parent` and starts with the issue's requester.
- **Starting a PR pipeline** is a `/handle <pr-url>` mission. Delegation queues it for a PR
  with no pipeline yet, and for one parked at a gate; a PR already moving is left alone,
  and one already handed off is only reported: a new fix round is asked on the PR itself.
  A PR whose pipeline stopped (it was closed) and that is open again starts afresh.
- **A data change is never approved from the issue.** A PR parked at a `data_change` gate
  is reported, not poked, and a reply on the issue does not reopen that gate: the
  approval is a `@<bot> handle` on the PR, by someone who looked at its files.
- **Roll-up.** A PR pipeline with a `parent` queues `/handle <issue> after:prs` whenever
  it stops moving (gate, hand-off, PR closed or merged). That tick reads the PRs' records:
  once every PR is handed off or stopped, and at least one is handed off, the issue gets
  one summary comment, moves to the review column (same rule as a hand-off) and enters
  `done`. Until then it only logs: each PR reports on its own thread.
- **Pokes.** On the issue: the linked PRs are looked up again (a new one is adopted), the
  reply gives one status line per PR, and gated PRs are poked. A poke on an issue already
  handed off, with nothing new to start, gets the status only: no second summary or
  board move. With no open linked PR left: if one of the issue's PRs is merged, the
  reply says so and the pipeline stops (a PR merged into a branch that does not close
  the issue must not be implemented again; later pokes repeat it, and closing the issue
  is how to end it); otherwise, or when its PRs can no longer be read, the issue starts
  over from triage.
  On a PR: that PR's pipeline, as for any PR.
- **Gates.** A delegated PR's gate, other than a data change, also reopens on a human
  reply written on the parent issue after the gate.
- **Closing the issue** stops the issue's own roll-up, not its PRs' pipelines: merging one
  PR can close the issue while another is still moving. Close a PR to stop its pipeline.
- **Not unified:** an issue that adopted a single PR of its own repository keeps the
  single-PR record; a second linked PR appearing later is not picked up by that record.

### Transferred issue

GitHub gives a transferred issue a new URL, and the record is keyed by the URL. The
pipeline follows the issue rather than start over (a fresh triage would discard the gate
the issue was parked at):

- Every comment the pipeline posts ends with a hidden marker naming its record key
  (`<!-- koan-handle: <key> -->`). Comments move with the issue.
- A tick that finds no record for its issue reads the marker of the bot's latest marked
  comment. When it names another key that has a record, and GitHub resolves that old URL
  to this issue, the record is renamed to the new URL (one locked write) and the tick
  goes on from the stored phase.
- **The project follows the issue.** The record, and the tick that renames it, switch to
  the project configured for the issue's new repository; with no project for that
  repository, the tick's. Every later tick on a moved record does the same switch, so
  one queued before the transfer never plans or implements in the old checkout.
- **The pull request does not move.** A PR stays in the repository it was opened in:
  every read and write of the pipeline's PR uses the repository of the stored `pr_url`,
  never the issue's new one. The steps that work on the PR (`/review`, `/rebase --fix`,
  the `## CI` entry, the QA command) are queued under the project of the PR's
  repository, whose checkout holds its branch, and `ready_before_ci`, `data_guard` and
  `qa` are read from that project. When no project is configured for that repository,
  the tick fails before doing anything (error gate, `retry`): the issue's checkout is
  never used in its place. The ticks, `/plan` and `/implement` stay with the
  issue's project.
- **Former URLs are kept** in the record (`moved_from`, with the project of the time): a
  marker naming one of them still finds the record after a second transfer, and a
  follow-up tick or re-check queued under one of them still counts as pending.
- Only a marker that closes a comment the bot itself wrote counts, and the redirect is
  checked with GitHub: text an issue participant got the bot to quote cannot move a record.
- *Known limits:* comments posted before the marker existed carry none, so an issue
  transferred with only such comments starts over. A step queued before the transfer
  still names the old URL and the old issue number (the `<prefix>implement-<N>` branch):
  it gates as "no PR" and a poke re-runs it; autoreview may run on what it produced,
  since that mission no longer names the record's key.

### Data-change check (entering `ci`)

The pipeline never ships a change to data without a human. Two layers:

- **Triage** answers `needs_human` when the fix needs a database migration, a new or
  changed command or script, a change to existing translations, or any other change that
  modifies stored data (backfill, cleanup, data fix). Adding new translation keys alone
  does not need a human.
- **The PR's files**, read from the API (`pulls/<n>/files`), against the project's
  `handle.data_guard` path patterns (`fnmatch` on the repository path; a renamed file
  is matched on its old path too):

  | Category | Flagged when |
  |---|---|
  | `migrations` | any file added, changed, renamed or removed |
  | `scripts` (commands, scripts) | any file added, changed, renamed or removed |
  | `translations` | a file removed or renamed, or changed with deleted lines or with no added line (a binary catalog, a mode change); a new file or a change that only adds lines is tolerated (new keys) |

  In a `.json` catalog, a deleted line that comes back unchanged but for a trailing comma
  does not count as deleted: appending a key after the last entry rewrites that entry's
  line. This is read from the file's patch; without one (GitHub omits large patches) the
  file is flagged.

  A flagged file gates (`data_change`, the files listed with their category). The next
  human poke approves exactly the listed files **at their current content** (path and
  blob SHA) and moves on to `ci`; a later commit that touches an approved file, or flags
  a new one, gates again. The record holds the files a gate listed
  (`data_changes_pending`) until the poke moves them to `data_changes_approved`.
  A PR pipeline is checked the same way: the gate applies to a human's PR too.
  When GitHub lists fewer files than the PR's `changed_files` (it caps the listing), the
  unlisted files cannot be checked: the gate names them as unchecked, and the poke
  approves the PR head. The count and the head are read in one call after the listing;
  a head that moved during the check fails the tick (error gate, `retry`).
- *Known limit:* "only adds lines" is a line count, not a parse of the catalog; an
  added entry that redefines an existing key passes.

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
| `handle:` | `config.yaml` | no | `max_review_rounds`, `poll_minutes`, `max_polls`, `ready_before_ci`, `board`, `product_team`, `qa`, `data_guard` |
| `handle.qa` | `projects.yaml` | no | per project (or under `defaults`); overrides the global `handle.qa` key by key |
| `handle.data_guard` | `config.yaml`, `projects.yaml` | no | `migrations`, `scripts`, `translations`: lists of path patterns; a project's list replaces the global one, which replaces the generic default |

## Outputs / side effects

- **State:** `instance/.handle-state.json`, one record per canonical issue URL (phase,
  requester, PR, branch, review round, phase start, resume target, CI counters, the
  queued `step` command). Every
  read-modify-write holds a file lock; the CI relay checks and updates under one lock.
- **Missions:** each queued step is followed by `/handle <issue> after:<step>`; re-checks
  are one-shot events (`instance/events/`) queuing `/handle <issue> after:ci-poll|qa-wait`.
- **GitHub:** issue comments (triage, gates, errors, hand-off), self-assignment, PR marked
  ready, review requested from the requester, optional board column moves.
- **Chat:** the pipeline reports its **milestones**, not its steps. Every tick hands the
  agent loop one outcome (stdout markers, `outcome_line`), and at the normal messaging
  level the loop sends:

  | Tick | Chat |
  |---|---|
  | human poke that moved the pipeline on (or answered "busy") | `✅ [project] 🧭 Handling <issue-url>` + what it did (`/implement queued`) |
  | stops at a gate — poke or chained — or a poke finds the gate unanswered | `⏸ [project] 🧭 Needs you: <issue-url>` + what blocks |
  | hand-off | `✅ [project] 🧭 Ready for your review: <pr-url>` + the head's CI as read |
  | human poke that the pipeline declines or that ends it (fork PR, closed issue, closed or merged PR) | `⏹ [project] 🧭 Not handling <issue-url>` + why |
  | chained tick that only advances, waits, is stale, or finds the issue or PR closed | log only |
  | failed tick | `❌ [project] Failed: /handle <url>` + the reason |

  What the pipeline queued for itself is logged only when it succeeds: the step its phase
  waits for (`/plan`, `/implement`, `/review --force`, `/rebase --fix`, the QA command —
  recorded as `step` when queued), a `/ci_check` on the PR, and the `## CI` queue's own
  lines about that PR ("CI passed — ready for review" between two fix rounds is not
  true). A step that **fails** always reports. This holds while the pipeline moves on its
  own (any phase but a gate, `done`, `stopped`); a pipeline parked at a gate owns nothing,
  and the same command queued by a human (another URL or other arguments) reports as
  usual. An unreadable state file sends the message rather than hide it. The `debug`
  level sends every mission's line (the `## CI` lines stay quiet). See `docs/messaging/messaging-level.md`.

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
| the PR touches migrations, commands/scripts, or existing translations | approve the listed files at their current content, then `ci` |
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
- No change to data ships without a human: triage gates work that needs one, and the
  data-change check gates every PR head before CI, before a review and before the hand-off.
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
  The fallback also never queues `/implement` when the bot's latest `assigned` event on
  the issue was made by the bot itself: a self-assignment is not a request. This does not
  depend on the record, so it holds for an issue whose URL changed (a transfer re-sends
  the assignment under the new thread). An unreadable timeline is logged and the fallback
  proceeds. Only issues are checked (the pipeline assigns the bot to nothing else); the
  read is one paginated call per `assign` notification that reaches it.
- **CI queue:** `ci_queue_runner.drain_one()` calls `handle_state.relay_ci_outcome()`, and
  sends its chat line about a PR only when no pipeline drives it (`handle_state.drives_pr`).
- **Agent loop:** `run._notify_mission_end()` reads the tick's milestone
  (`KOAN_OUTCOME_MILESTONE:`) and skips the success line of a mission the pipeline queued
  for itself (`handle_state.owns_mission`).
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
  repository, or to link it as closing the issue (a closing keyword naming the issue in
  the PR body). A PR opened elsewhere without that link gates as "no PR".
- Linked PRs include a human's: after a poke on the issue, `/rebase --fix` pushes fix
  commits to that person's branch, as it does when the PR itself is poked.
- At most 20 linked PRs are read per issue.
- A preview deployment is matched on the PR head or its merge commit; a deployment keyed
  on another SHA never reads as ready and the hand-off says the preview was not ready.
- Kōan must run as a **user account**: mention commands (`parse_mention_command`,
  `latest_handle_mention`) match `@<github.nickname>`, which an App identity — login
  `<slug>[bot]`, mentioned as `@<slug>` — never matches. Login comparisons fold only gh's
  `app/<slug>` spelling to REST's `<slug>[bot]`, so a user and an App of the same name
  never match.
