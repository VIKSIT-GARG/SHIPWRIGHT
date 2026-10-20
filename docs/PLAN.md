# Shipwright

Shipwright is a terminal-first autonomous issue-to-PR operator. It accepts a reproducible GitHub issue, runs a bounded multi-agent coding workflow in an isolated worktree, and produces a reviewable patch with test evidence. Publishing a branch or pull request always requires an explicit human action.

## Product boundary

### MVP

- Python repositories only.
- One repository checked out locally or cloned from GitHub.
- One issue per run.
- Isolated Git worktree per run.
- Three agent roles: investigator, implementer, reviewer.
- Verifier runs the repository's declared test command.
- Self-correction loop with a configurable attempt limit.
- TUI operator console with live event stream, agent status, test output, diff summary, and approval gate.
- Exportable run bundle containing the issue, plan, patch, logs, and review report.

### Explicitly out of scope for the first slice

- Automatic merge.
- Arbitrary language/toolchain support.
- Long-lived background service or hosted multi-tenant mode.
- Secret management beyond reading an already-configured local GitHub credential.
- Unbounded autonomous retries.

## Architecture

```text
TUI (Textual)
    │ commands/events
Run service (async Python)
    ├── Run state store (SQLite)
    ├── Event bus (in-process now, Redis-compatible later)
    ├── Agent orchestrator
    │     ├── investigator
    │     ├── implementer
    │     ├── verifier
    │     └── reviewer
    ├── Workspace manager (Git worktrees)
    ├── Command runner (timeouts, output capture, cancellation)
    └── Delivery adapter (local patch first, GitHub draft PR later)
```

The TUI is the first client, not the workflow engine. Keeping the orchestration layer independent makes it possible to add a FastAPI service or browser UI later without rewriting the run lifecycle.

## Run state machine

```text
QUEUED → INVESTIGATING → PLANNED → IMPLEMENTING → VERIFYING
                                      ↑              │
                                      └── REPAIRING ←┘ (failed checks, attempts remaining)
VERIFYING → REVIEWING → READY_FOR_APPROVAL → EXPORTED
                  └────→ BLOCKED
```

Terminal states: `EXPORTED`, `BLOCKED`, `CANCELLED`.

Every transition is persisted with a timestamp, actor, reason, and related artifact IDs. The UI reconstructs the run from events rather than relying on transient agent output.

## Core domain objects

- `Repository`: source URL, local root, default branch, test command.
- `Issue`: provider, number, title, body, labels, source URL.
- `Run`: repository + issue + state + attempt count + worktree path.
- `AgentTask`: role, prompt/input artifact, status, start/end timestamps, output artifact.
- `CommandExecution`: argv, cwd, exit code, timeout, stdout/stderr artifact.
- `Artifact`: typed immutable file or text output, checksum, size, created-at.
- `Review`: findings, confidence, approval recommendation, blocking status.

SQLite is enough for the MVP. Filesystem artifacts remain outside the database and are content-addressed by SHA-256 so logs and diffs are reproducible.

## Safety model

1. Never edit the user's primary checkout directly; all mutations happen inside a disposable worktree.
2. Every subprocess has an allowlisted working directory, timeout, output cap, and cancellation path.
3. Agent prompts receive only the issue, repository snapshot metadata, relevant files, and prior failed-check evidence.
4. Tool output is treated as untrusted data, including issue text and generated patches.
5. No push, PR creation, or merge occurs without a visible approval action in the TUI.
6. Each run has a hard attempt limit and a final reason when it stops.

## TUI layout

The console is keyboard-first and split into:

- Run queue: active, blocked, and completed runs.
- Run detail: state, agent cards, elapsed time, attempt count, and test summary.
- Event log: timestamped investigator/implementer/verifier/reviewer events.
- Artifacts: diff, test log, review report, and exported bundle.
- Command bar: start, cancel, retry, approve export, and open repository.

Initial shortcuts: `j/k` select, `Enter` open, `s` start run, `c` cancel, `r` retry, `a` approve export, `d` diff, `l` logs, `q` quit.

## Delivery milestones

1. **Foundation:** package layout, domain models, SQLite store, event bus, and a mocked run fixture.
2. **TUI:** queue/detail layout, keyboard navigation, live event rendering, artifact viewer.
3. **Local execution:** Git worktree manager, subprocess runner, test command detection, captured logs.
4. **Agent loop:** OmniRush adapter boundary plus investigator → implementer → verifier → reviewer flow.
5. **Hardening:** cancellation, timeouts, attempt limits, malformed output handling, and regression tests.
6. **GitHub delivery:** issue import, branch metadata, draft PR preparation, and explicit approval gate.

## First implementation slice

Start with milestone 1 and the TUI against deterministic fixture data. This gives us a judgeable product surface immediately and lets the workflow engine evolve behind stable interfaces. The first vertical demo should show:

1. select a queued issue;
2. start a run;
3. watch agent events appear;
4. inspect a test log and diff summary;
5. approve export of a review bundle.
