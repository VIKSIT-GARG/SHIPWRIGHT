"""Core multi-agent Orchestrator for Shipwright.

Coordinates investigator, implementer, verifier, and reviewer agents
through the bounded autonomous state machine from issue to reviewable PR.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import shlex
import time
from typing import Any

from shipwright.models import Run, RunState, TimelineEvent, Issue, Review, Artifact, Repository
from shipwright.store import RunStore
from shipwright.events import EventBus, RUN_STATE_CHANGED, AGENT_EVENT, ARTIFACT_ADDED, RUN_COMPLETED, TEST_OUTPUT
from shipwright.workspace import WorkspaceManager
from shipwright.runner import CommandRunner
from shipwright.repo import resolve_repo, infer_repo_from_issue_url
from shipwright.agents.investigator import InvestigatorAgent
from shipwright.agents.implementer import ImplementerAgent
from shipwright.agents.verifier import VerifierAgent
from shipwright.agents.reviewer import ReviewerAgent
from shipwright.providers import create_provider

logger = logging.getLogger(__name__)

__all__ = ["Orchestrator"]


class Orchestrator:
    """Core multi-agent orchestrator managing the autonomous issue-to-PR workflow."""

    def __init__(
        self,
        repo_root: str = ".",
        provider: Any = None,
        store: RunStore | None = None,
        event_bus: EventBus | None = None,
        *,
        workspace_mgr: WorkspaceManager | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        """Initialize the Orchestrator with workspace, store, event bus, and agents.

        Args:
            repo_root: Path to the target git repository root, shorthand 'owner/repo',
                or remote git URL. Defaults to ".".
            provider: LLM provider instance. If None, created via create_provider().
            store: RunStore instance for run persistence. If None, a new RunStore is created.
            event_bus: EventBus instance for publishing lifecycle events. If None, created.
            workspace_mgr: Optional WorkspaceManager override for isolated worktrees.
            runner: Optional CommandRunner override for command and test execution.
        """
        self.repo_source = str(repo_root) if repo_root is not None else "."
        self.repository: Repository | None = None
        try:
            self.repository = resolve_repo(self.repo_source)
            self.repo_root = Path(self.repository.root).resolve()
        except Exception as exc:
            logger.debug("Failed immediate repository resolution for '%s': %s", self.repo_source, exc)
            self.repository = None
            try:
                self.repo_root = Path(repo_root).resolve()
            except Exception:
                self.repo_root = Path(".").resolve()

        if provider is None:
            try:
                self.provider = create_provider()
            except Exception as exc:
                logger.warning("Failed to initialize default provider: %s", exc)
                self.provider = None
        else:
            self.provider = provider

        self.store = store if store is not None else RunStore()
        self.event_bus = event_bus if event_bus is not None else EventBus()
        self.workspace_mgr = workspace_mgr if workspace_mgr is not None else WorkspaceManager(self.repo_root)
        self.runner = runner if runner is not None else CommandRunner()

        self.investigator = InvestigatorAgent(self.provider)
        self.implementer = ImplementerAgent(self.provider)
        self.verifier = VerifierAgent(self.runner)
        self.reviewer = ReviewerAgent(self.provider)

        self._cancelled_runs: set[int] = set()
        self._active_runs: dict[int, Run] = {}

    def get_repository(self) -> Repository:
        """Return the resolved repository instance, resolving lazily if needed."""
        if self.repository is None:
            self.repository = resolve_repo(self.repo_source)
            self.repo_root = Path(self.repository.root).resolve()
        return self.repository

    def _format_elapsed(self, seconds: float) -> str:
        """Format elapsed seconds into MM:SS format."""
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins:02d}:{secs:02d}"

    def _update_elapsed(self, run: Run, start_time: float) -> None:
        """Update elapsed duration string on the run."""
        elapsed_sec = max(0.0, time.monotonic() - start_time)
        run.elapsed = self._format_elapsed(elapsed_sec)

    def _summarize_test_output(self, passed: bool, output: str) -> str:
        """Extract a concise test summary string from output."""
        if not output:
            return "passed" if passed else "failed"

        patterns = [
            r"(\d+\s+passed(?:,\s*\d+\s+failed)?)",
            r"(\d+\s+failed(?:,\s*\d+\s+passed)?)",
            r"(OK(?:\s*\(.*?\))?)",
            r"(FAILED(?:\s*\(.*?\))?)",
            r"(PASSED)",
        ]
        for pat in patterns:
            m = re.search(pat, output, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        return "passed" if passed else "failed"

    def _record_event(self, run: Run, actor: str, message: str) -> TimelineEvent:
        """Record and broadcast a timeline event."""
        timestamp = time.strftime("%H:%M:%S")
        event = TimelineEvent(timestamp=timestamp, actor=actor, message=message)
        run.events.append(event)
        self.store.add_event(run.number, event)
        self.event_bus.publish(
            AGENT_EVENT,
            {
                "run_number": run.number,
                "actor": actor,
                "message": message,
                "state": run.state.value if hasattr(run.state, "value") else str(run.state),
            },
        )
        return event

    def _transition(self, run: Run, new_state: RunState, actor: str, message: str) -> None:
        """Transition the run to a new state, emitting events and saving state."""
        old_state = run.state
        run.state = new_state
        self._record_event(run, actor=actor, message=message)
        self.event_bus.publish(
            RUN_STATE_CHANGED,
            {
                "run_number": run.number,
                "state": new_state.value if hasattr(new_state, "value") else str(new_state),
                "from_state": old_state.value if hasattr(old_state, "value") else str(old_state),
                "to_state": new_state.value if hasattr(new_state, "value") else str(new_state),
            },
        )
        self.store.save_run(run)

    def _add_artifact(
        self,
        run: Run,
        name: str,
        content: str,
        artifact_type: str = "text",
    ) -> Artifact:
        """Record an artifact in memory, in storage, and over the event bus."""
        content_bytes = content.encode("utf-8")
        checksum = hashlib.sha256(content_bytes).hexdigest()
        artifact = Artifact(
            name=name,
            path=name,
            content=content,
            checksum=checksum,
            size=len(content_bytes),
            artifact_type=artifact_type,
        )

        run.artifacts = [
            a for a in run.artifacts
            if getattr(a, "name", str(a)) != name
        ]
        run.artifacts.append(artifact)
        self.store.add_artifact(run.number, name, content)
        self.event_bus.publish(
            ARTIFACT_ADDED,
            {
                "run_number": run.number,
                "name": name,
                "content": content,
            },
        )
        return artifact

    def _is_cancelled(self, run_number: int) -> bool:
        """Check whether a run has been marked for cancellation."""
        return run_number in self._cancelled_runs

    def _handle_cancellation(self, run: Run, start_time: float) -> Run:
        """Transition run to CANCELLED state, emit events, and persist."""
        self._update_elapsed(run, start_time)
        if run.state != RunState.CANCELLED:
            self._transition(
                run,
                RunState.CANCELLED,
                actor="orchestrator",
                message=f"Run #{run.number} cancelled by operator",
            )
            self.event_bus.publish(
                RUN_COMPLETED,
                {
                    "run_number": run.number,
                    "state": RunState.CANCELLED.value,
                    "success": False,
                },
            )
        self.store.save_run(run)
        return run

    def cancel_run(self, run_number: int) -> None:
        """Cancel an active or queued run.

        Args:
            run_number: Identifier of the run to cancel.
        """
        self._cancelled_runs.add(run_number)

        run = self._active_runs.get(run_number)
        if run is None:
            run = self.store.get_run(run_number)

        if run is not None and run.state not in (
            RunState.EXPORTED,
            RunState.BLOCKED,
            RunState.CANCELLED,
        ):
            from_state = run.state
            run.state = RunState.CANCELLED
            self._record_event(
                run,
                actor="orchestrator",
                message=f"Run #{run_number} cancelled by operator",
            )
            self.event_bus.publish(
                RUN_STATE_CHANGED,
                {
                    "run_number": run_number,
                    "state": RunState.CANCELLED.value,
                    "from_state": from_state.value if hasattr(from_state, "value") else str(from_state),
                    "to_state": RunState.CANCELLED.value,
                },
            )
            self.event_bus.publish(
                RUN_COMPLETED,
                {
                    "run_number": run_number,
                    "state": RunState.CANCELLED.value,
                    "success": False,
                },
            )
            self.store.save_run(run)

    def run_issue(
        self,
        issue: Issue,
        max_attempts: int = 3,
        repo_override: str | None = None,
    ) -> Run:
        """Execute the autonomous multi-agent state machine for an issue.

        State machine execution:
        1. QUEUED: create run record, worktree via WorkspaceManager.
        2. INVESTIGATING: InvestigatorAgent analyzes repo files and issue, produces plan.
        3. PLANNED: record plan artifact and event.
        4. Loop (attempt 1 to max_attempts):
           - IMPLEMENTING: ImplementerAgent applies changes to worktree files.
           - VERIFYING: VerifierAgent runs tests in worktree.
           - If tests pass: break to REVIEWING.
           - If tests fail and attempts remain: state = REPAIRING, loop back to IMPLEMENTING with test failure output.
           - If attempts exhausted: state = BLOCKED, record event, return.
        5. REVIEWING: ReviewerAgent reviews diff and test output.
        6. If review passes: state = READY_FOR_APPROVAL.
        7. Record final artifacts (git diff, test output, review report) and store run.
        8. Return run.

        Args:
            issue: Target Issue instance describing title, body, and issue number.
            max_attempts: Maximum number of implement-verify repair attempts.
            repo_override: Optional repository source override (local path, shorthand, URL).

        Returns:
            The finished Run record.
        """
        start_time = time.monotonic()

        # Determine run number and title
        run_number = getattr(issue, "number", None)
        if run_number is None and isinstance(issue, dict):
            run_number = issue.get("number")
        if not isinstance(run_number, int) or run_number <= 0:
            existing_runs = self.store.list_runs()
            run_number = max([r.number for r in existing_runs] or [0]) + 1

        title = getattr(issue, "title", None)
        if title is None and isinstance(issue, dict):
            title = issue.get("title")
        if not title:
            title = f"Issue #{run_number}"

        branch_name = f"shipwright/run-{run_number}"

        # 1. QUEUED: create run record, worktree via WorkspaceManager.
        run = Run(
            number=run_number,
            title=title,
            state=RunState.QUEUED,
            elapsed="00:00",
            agents="0 / 4",
            tests="pending",
            events=[],
            artifacts=[],
            issue=issue,
            repo_path=str(self.repo_root),
            worktree_path="",
            branch=branch_name,
            attempt=0,
            max_attempts=max_attempts,
        )

        self._active_runs[run.number] = run

        try:
            # Resolve target repository:
            # 1. If repo_override is provided, resolve repo_override.
            # 2. If self.repo_source is default '.' and issue.url is a GitHub issue URL,
            #    infer repository URL via infer_repo_from_issue_url(issue.url) and resolve it!
            # 3. Otherwise use resolved self.repository or resolve self.repo_source.
            target_repo_source: str | None = None
            if repo_override:
                target_repo_source = repo_override
            elif self.repo_source.strip() in (".", "./", ""):
                issue_url = getattr(issue, "url", None)
                if issue_url is None and isinstance(issue, dict):
                    issue_url = issue.get("url")
                if issue_url and isinstance(issue_url, str):
                    inferred = infer_repo_from_issue_url(issue_url)
                    if inferred:
                        target_repo_source = inferred

            if target_repo_source is not None:
                active_repository = resolve_repo(target_repo_source)
            elif self.repository is not None:
                active_repository = self.repository
            else:
                active_repository = self.get_repository()

            resolved_repo_root = Path(active_repository.root).resolve()
            run.repo_path = str(resolved_repo_root)

            # Ensure workspace manager targets the resolved repository root
            if (
                isinstance(self.workspace_mgr, WorkspaceManager)
                and Path(self.workspace_mgr.repo_root).resolve() != resolved_repo_root
            ):
                workspace_mgr = WorkspaceManager(
                    resolved_repo_root,
                    mock_mode=getattr(self.workspace_mgr, "mock_mode", False),
                )
            else:
                workspace_mgr = self.workspace_mgr

            self._transition(
                run,
                RunState.QUEUED,
                actor="orchestrator",
                message=f"Run queued for issue #{run.number}: {run.title}",
            )

            # Create isolated worktree
            worktree_path = workspace_mgr.create_worktree(run.number, branch_name=branch_name)
            run.worktree_path = str(worktree_path)
            self._record_event(
                run,
                actor="orchestrator",
                message=f"Prepared isolated workspace at {worktree_path}",
            )
            self.store.save_run(run)

            if self._is_cancelled(run.number):
                return self._handle_cancellation(run, start_time)

            # 2. INVESTIGATING: InvestigatorAgent analyzes repo files and issue, produces plan.
            run.agents = "1 / 4"
            self._update_elapsed(run, start_time)
            self._transition(
                run,
                RunState.INVESTIGATING,
                actor="investigator",
                message="Analyzing repository files and issue requirements",
            )

            repo_files = workspace_mgr.list_files(run.worktree_path)
            plan = self.investigator.investigate(
                repo_files=repo_files,
                issue=issue,
                worktree_path=run.worktree_path,
            )

            if not isinstance(plan, dict):
                plan = {
                    "summary": f"Investigation for issue #{run.number}",
                    "relevant_files": repo_files[:5] if repo_files else [],
                    "suggested_approach": "Review repository files and apply changes.",
                }

            if self._is_cancelled(run.number):
                return self._handle_cancellation(run, start_time)

            # 3. PLANNED: record plan artifact and event.
            plan_summary = plan.get("summary", "Plan generated")
            self._transition(
                run,
                RunState.PLANNED,
                actor="investigator",
                message=f"Plan generated: {plan_summary}",
            )

            plan_json = json.dumps(plan, indent=2)
            self._add_artifact(run, name="plan.json", content=plan_json)

            if self._is_cancelled(run.number):
                return self._handle_cancellation(run, start_time)

            # 4. Loop (attempt 1 to max_attempts)
            last_test_output = ""
            tests_passed = False

            for attempt in range(1, max_attempts + 1):
                run.attempt = attempt
                self._update_elapsed(run, start_time)

                if self._is_cancelled(run.number):
                    return self._handle_cancellation(run, start_time)

                # IMPLEMENTING: ImplementerAgent applies changes to worktree files.
                run.agents = "2 / 4"
                self._transition(
                    run,
                    RunState.IMPLEMENTING,
                    actor="implementer",
                    message=f"Applying code modifications (attempt {attempt}/{max_attempts})",
                )

                modified_files = self.implementer.implement(
                    investigation=plan,
                    issue=issue,
                    worktree_path=run.worktree_path,
                    previous_error=last_test_output if attempt > 1 else None,
                )

                if modified_files:
                    files_str = ", ".join(modified_files)
                    msg = f"Applied modifications to {len(modified_files)} file(s): {files_str}"
                else:
                    msg = "Implementer proposed no file modifications"
                self._record_event(run, actor="implementer", message=msg)

                if self._is_cancelled(run.number):
                    return self._handle_cancellation(run, start_time)

                # VERIFYING: VerifierAgent runs tests in worktree.
                run.agents = "3 / 4"
                self._transition(
                    run,
                    RunState.VERIFYING,
                    actor="verifier",
                    message=f"Running repository tests (attempt {attempt}/{max_attempts})",
                )

                test_cmd = (
                    shlex.split(active_repository.test_cmd)
                    if getattr(active_repository, "test_cmd", None)
                    else None
                )
                passed, test_output = self.verifier.verify(
                    worktree_path=run.worktree_path,
                    test_cmd=test_cmd,
                )
                last_test_output = test_output
                run.tests = self._summarize_test_output(passed, test_output)

                self.event_bus.publish(
                    TEST_OUTPUT,
                    {
                        "run_number": run.number,
                        "passed": passed,
                        "output": test_output,
                        "attempt": attempt,
                    },
                )
                self._add_artifact(run, name="test_output.txt", content=test_output)

                if passed:
                    self._record_event(
                        run,
                        actor="verifier",
                        message=f"Tests passed on attempt {attempt}: {run.tests}",
                    )
                    tests_passed = True
                    break

                # Tests failed
                self._record_event(
                    run,
                    actor="verifier",
                    message=f"Tests failed on attempt {attempt}: {run.tests}",
                )

                if attempt < max_attempts:
                    self._transition(
                        run,
                        RunState.REPAIRING,
                        actor="orchestrator",
                        message=f"Tests failed on attempt {attempt}; entering repair mode ({max_attempts - attempt} attempt(s) remaining)",
                    )
                else:
                    run.error = f"Tests failed after {max_attempts} attempts"
                    self._transition(
                        run,
                        RunState.BLOCKED,
                        actor="orchestrator",
                        message=f"Run blocked: tests failed after {max_attempts} attempts",
                    )
                    self.event_bus.publish(
                        RUN_COMPLETED,
                        {
                            "run_number": run.number,
                            "state": RunState.BLOCKED.value,
                            "success": False,
                        },
                    )
                    self._update_elapsed(run, start_time)
                    self.store.save_run(run)
                    return run

            if not tests_passed:
                run.error = f"Tests failed after {max_attempts} attempts"
                self._transition(
                    run,
                    RunState.BLOCKED,
                    actor="orchestrator",
                    message=f"Run blocked: tests failed after {max_attempts} attempts",
                )
                self.event_bus.publish(
                    RUN_COMPLETED,
                    {
                        "run_number": run.number,
                        "state": RunState.BLOCKED.value,
                        "success": False,
                    },
                )
                self._update_elapsed(run, start_time)
                self.store.save_run(run)
                return run

            if self._is_cancelled(run.number):
                return self._handle_cancellation(run, start_time)

            # 5. REVIEWING: ReviewerAgent reviews diff and test output.
            run.agents = "4 / 4"
            self._transition(
                run,
                RunState.REVIEWING,
                actor="reviewer",
                message="Reviewing proposed diff and test results",
            )

            diff = workspace_mgr.get_diff(run.worktree_path)
            review = self.reviewer.review(
                issue=issue,
                diff=diff,
                test_output=last_test_output,
            )
            run.review = review

            self._record_event(
                run,
                actor="reviewer",
                message=f"Review completed: passed={review.passed}, confidence={review.confidence:.2f} - {review.summary}",
            )

            # 6. If review passes: state = READY_FOR_APPROVAL.
            if review.passed:
                self._transition(
                    run,
                    RunState.READY_FOR_APPROVAL,
                    actor="reviewer",
                    message="Review passed: patch is ready for approval",
                )
                try:
                    commit_title = getattr(issue, "title", run.title)
                    workspace_mgr.commit_changes(
                        run.worktree_path,
                        f"Fix #{run.number}: {commit_title}",
                    )
                except Exception as exc:
                    logger.debug("Failed to commit worktree changes: %s", exc)
            else:
                run.error = f"Review rejected: {review.summary}"
                self._transition(
                    run,
                    RunState.BLOCKED,
                    actor="reviewer",
                    message=f"Review rejected changes: {review.summary}",
                )

            # 7. Record final artifacts (git diff, test output, review report) and store run.
            self._add_artifact(run, name="patch.diff", content=diff)
            self._add_artifact(run, name="diff.patch", content=diff)
            self._add_artifact(run, name="test_output.txt", content=last_test_output)

            findings_text = (
                "\n".join(f"- {f}" for f in review.findings)
                if review.findings
                else "- No issues found."
            )
            review_report = (
                f"# Review Report\n\n"
                f"**Verdict:** {'PASSED' if review.passed else 'FAILED'}\n"
                f"**Confidence:** {review.confidence:.2f}\n\n"
                f"## Summary\n"
                f"{review.summary}\n\n"
                f"## Findings\n"
                f"{findings_text}\n"
            )
            self._add_artifact(run, name="review.md", content=review_report)

            self._update_elapsed(run, start_time)
            self.store.save_review(run.number, review)

            success = (run.state == RunState.READY_FOR_APPROVAL)
            self.event_bus.publish(
                RUN_COMPLETED,
                {
                    "run_number": run.number,
                    "state": run.state.value if hasattr(run.state, "value") else str(run.state),
                    "success": success,
                },
            )

            self.store.save_run(run)

            # 8. Return run.
            return run

        except Exception as exc:
            logger.exception("Run #%s failed with unexpected exception: %s", run.number, exc)
            run.error = str(exc)
            self._transition(
                run,
                RunState.BLOCKED,
                actor="orchestrator",
                message=f"Run blocked by exception: {exc}",
            )
            self.event_bus.publish(
                RUN_COMPLETED,
                {
                    "run_number": run.number,
                    "state": RunState.BLOCKED.value,
                    "success": False,
                    "error": str(exc),
                },
            )
            self._update_elapsed(run, start_time)
            self.store.save_run(run)
            return run

        finally:
            self._active_runs.pop(run.number, None)
