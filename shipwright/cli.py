"""Command-line interface for Shipwright.

Supports direct model chat, headless issue execution, run inspection,
bundle export, and interactive TUI launching.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence

from .events import (
    AGENT_EVENT,
    ARTIFACT_ADDED,
    RUN_COMPLETED,
    RUN_STATE_CHANGED,
    TEST_OUTPUT,
    Event,
    EventBus,
)
from .export import export_bundle
from .issue import load_issue
from .models import (
    Artifact,
    Issue,
    Review,
    Run,
    RunState,
    TimelineEvent,
    fixture_runs,
)
from .providers import ProviderError, create_provider

try:
    from .orchestrator import Orchestrator
except ImportError:
    Orchestrator = None  # type: ignore[assignment, misc]

try:
    from .store import RunStore
except ImportError:
    try:
        from .run_store import RunStore
    except ImportError:
        RunStore = None  # type: ignore[assignment, misc]

KNOWN_SUBCOMMANDS: frozenset[str] = frozenset({"chat", "run", "list", "status", "export"})


def _normalize_argv(argv: list[str], prog: str = "") -> list[str]:
    """Normalize raw CLI argv to support subcommands and direct prompts.

    - Empty argv under 'shipwright-chat' -> ['chat'] (smoke test with default prompt)
    - Empty argv under 'shipwright' -> [] (launches interactive TUI)
    - Leading arg in KNOWN_SUBCOMMANDS or help flags -> preserved
    - Otherwise (e.g. prompt string or flags) -> prepends 'chat' for backwards compatibility
    """
    prog_base = os.path.basename(prog).lower()

    if not argv:
        if "shipwright-chat" in prog_base:
            return ["chat"]
        return []

    first = argv[0]

    # Standard help or version flags at root
    if first in ("-h", "--help", "-v", "--version"):
        return argv

    # Explicit recognized subcommands
    if first in KNOWN_SUBCOMMANDS:
        return argv

    # Direct prompt string or provider overrides -> forward to chat subcommand
    return ["chat", *argv]


class ShipwrightParser(argparse.ArgumentParser):
    """Custom ArgumentParser with smart argv normalization and prompt fallback."""

    def parse_args(  # type: ignore[override]
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        raw_args = list(sys.argv[1:] if args is None else args)
        prog = getattr(self, "prog", "") or (sys.argv[0] if sys.argv else "")
        if "shipwright-chat" not in prog and sys.argv and "shipwright-chat" in os.path.basename(sys.argv[0]).lower():
            prog = "shipwright-chat"
        normalized = _normalize_argv(raw_args, prog=prog)
        return super().parse_args(normalized, namespace=namespace)


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """Construct the top-level ArgumentParser for Shipwright CLI."""
    if prog is None:
        if sys.argv and "shipwright-chat" in os.path.basename(sys.argv[0]).lower():
            prog = "shipwright-chat"
        else:
            prog = "shipwright"

    parser = ShipwrightParser(
        prog=prog,
        description="Shipwright: Terminal-first autonomous issue-to-PR operator.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", title="Commands")

    # 1. chat subcommand
    chat_p = subparsers.add_parser(
        "chat",
        help="Send one prompt through a Shipwright provider (smoke test / direct query).",
        description="Send one prompt through a Shipwright provider and print the response.",
    )
    chat_p.add_argument(
        "prompt",
        nargs="?",
        default="Reply with exactly: SHIPWRIGHT_OK",
        help="Prompt string to send to the provider",
    )
    chat_p.add_argument("--provider", help="Override SHIPWRIGHT_PROVIDER for this invocation")
    chat_p.add_argument("--model", help="Override SHIPWRIGHT_MODEL for this invocation")

    # 2. run subcommand
    run_p = subparsers.add_parser(
        "run",
        help="Run autonomous issue-to-PR workflow on an issue in headless mode.",
        description="Run headless multi-agent issue resolution with live progress and summary.",
    )
    run_p.add_argument(
        "issue_or_text",
        help="Issue number (e.g. 1842), GitHub URL, local issue file (.md/.txt/.json), or prompt text",
    )
    run_p.add_argument(
        "--repo",
        default=".",
        help="Path to repository root, GitHub shorthand (owner/repo), or Git URL (default: current directory)",
    )
    run_p.add_argument("--provider", help="Override SHIPWRIGHT_PROVIDER for this run")
    run_p.add_argument("--model", help="Override SHIPWRIGHT_MODEL for this run")
    run_p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum verification/repair attempts (default: 3)",
    )
    run_p.add_argument("--test-cmd", help="Custom test command override (e.g. 'pytest -q')")
    run_p.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Automatically approve export if reviewer passes",
    )
    run_p.add_argument("--db", help="Path to SQLite database file")

    # 3. list subcommand
    list_p = subparsers.add_parser(
        "list",
        help="List all past runs from RunStore.",
        description="List tracked runs with optional state filtering and JSON output.",
    )
    list_p.add_argument(
        "--state",
        help="Filter runs by state (e.g. READY_FOR_APPROVAL, VERIFYING, BLOCKED)",
    )
    list_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of runs to display",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Format output as JSON array",
    )
    list_p.add_argument("--db", help="Path to SQLite database file")

    # 4. status subcommand
    status_p = subparsers.add_parser(
        "status",
        help="Display state, events, and artifacts for a run.",
        description="Inspect run state, metrics, timeline events, review findings, and artifacts.",
    )
    status_p.add_argument("run_number", type=int, help="Run number to inspect")
    status_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output full run details as JSON",
    )
    status_p.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help="Watch run events live until completion",
    )
    status_p.add_argument("--db", help="Path to SQLite database file")

    # 5. export subcommand
    export_p = subparsers.add_parser(
        "export",
        help="Export bundle via shipwright.export.export_bundle.",
        description="Export patch bundle (patch.diff, run.json, events.log, test_output.txt, review.md).",
    )
    export_p.add_argument("run_number", type=int, help="Run number to export")
    export_p.add_argument(
        "--out",
        "--output",
        dest="out",
        default=None,
        help="Output directory for exported bundle",
    )
    export_p.add_argument(
        "--format",
        default="bundle",
        choices=["bundle", "patch"],
        help="Export format (bundle directory or unified patch diff)",
    )
    export_p.add_argument(
        "--approve",
        action="store_true",
        default=False,
        help="Transition run state to EXPORTED upon export",
    )
    export_p.add_argument("--db", help="Path to SQLite database file")

    return parser


def _handle_chat(args: argparse.Namespace) -> int:
    """Handle chat subcommand and direct provider smoke tests."""
    env = None
    if getattr(args, "provider", None) or getattr(args, "model", None):
        env = dict(os.environ)
        if args.provider:
            env["SHIPWRIGHT_PROVIDER"] = args.provider
        if args.model:
            env["SHIPWRIGHT_MODEL"] = args.model
    try:
        provider = create_provider(env=env)
        prompt_text = getattr(args, "prompt", "Reply with exactly: SHIPWRIGHT_OK")
        response = provider.complete([{"role": "user", "content": prompt_text}])
    except (ProviderError, ValueError) as exc:
        prog = "shipwright-chat" if "shipwright-chat" in sys.argv[0] else "shipwright"
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        prog = "shipwright-chat" if "shipwright-chat" in sys.argv[0] else "shipwright"
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1

    print(response.content)
    return 0


_GITHUB_SHORTHAND_RE: re.Pattern[str] = re.compile(r"^([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)$")
_GITHUB_ISSUE_URL_RE: re.Pattern[str] = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<number>\d+)"
)
_GITHUB_REMOTE_URL_RE: re.Pattern[str] = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com[:/]|git@github\.com:)(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+?)(?:\.git)?(?:/)?$"
)


def is_git_url(spec: str) -> bool:
    """Check if spec looks like a remote Git URL.

    Matches https://, http://, git://, ssh://, git@, or .git suffix.
    """
    s = spec.strip()
    if (
        s.startswith("https://")
        or s.startswith("http://")
        or s.startswith("git://")
        or s.startswith("ssh://")
        or s.startswith("git@")
    ):
        return True
    if s.endswith(".git"):
        return True
    return False


def is_github_shorthand(spec: str) -> bool:
    """Check if spec is a GitHub shorthand (e.g. 'owner/repo').

    Returns False if spec starts with filesystem prefixes ('.', '/', '~'),
    or if it exists as a local directory on disk.
    """
    s = spec.strip()
    if s.startswith(".") or s.startswith("/") or s.startswith("~"):
        return False
    if len(s) > 1 and s[1] == ":":
        return False
    try:
        p = Path(s)
        if p.is_dir() and (p / ".git").exists():
            return False
    except OSError:
        pass
    return bool(_GITHUB_SHORTHAND_RE.match(s))


def infer_repo_from_issue_url(url: str | None) -> str | None:
    """Extract owner/repo GitHub shorthand from a GitHub issue URL if present.

    Returns 'owner/repo' shorthand string, or None if url is not a GitHub issue URL.
    """
    if not url or not isinstance(url, str):
        return None
    match = _GITHUB_ISSUE_URL_RE.search(url.strip())
    if match:
        owner = match.group("owner")
        repo = match.group("repo").removesuffix(".git")
        return f"{owner}/{repo}"
    return None


def _get_remote_repo_info(repo_spec: str, cache_root: Path) -> tuple[str, Path]:
    """Return (clone_url, target_cache_dir) for a GitHub shorthand or remote Git URL."""
    spec = repo_spec.strip()

    # Case 1: GitHub shorthand "owner/repo"
    shorthand_match = _GITHUB_SHORTHAND_RE.match(spec)
    if shorthand_match and not is_git_url(spec):
        owner = shorthand_match.group(1)
        repo = shorthand_match.group(2).removesuffix(".git")
        clone_url = f"https://github.com/{owner}/{repo}.git"
        target_dir = cache_root / owner / repo
        return clone_url, target_dir

    # Case 2: GitHub URL (https, http, ssh, git@)
    gh_match = _GITHUB_REMOTE_URL_RE.match(spec)
    if gh_match:
        owner = gh_match.group("owner")
        repo = gh_match.group("repo").removesuffix(".git")
        clone_url = spec
        target_dir = cache_root / owner / repo
        return clone_url, target_dir

    # Case 3: Other remote Git URL (e.g. gitlab, bitbucket, custom server)
    clone_url = spec
    cleaned = re.sub(r"^(?:https?://|ssh://|git://|git@)", "", spec)
    cleaned = cleaned.removesuffix(".git").replace(":", "/")
    parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
    if parts:
        target_dir = cache_root.joinpath(*parts)
    else:
        target_dir = cache_root / hashlib.sha256(spec.encode("utf-8")).hexdigest()[:16]
    return clone_url, target_dir


def resolve_repository(
    repo_spec: str,
    *,
    cache_dir: Path | str | None = None,
) -> Path:
    """Resolve a local path, GitHub shorthand, or remote Git URL to a local repository Path.

    Prints progress messages during resolution and cloning.
    """
    spec = repo_spec.strip()
    print(f"Resolving target repository: {spec}...")

    # Remote URL or GitHub shorthand
    if is_git_url(spec) or is_github_shorthand(spec):
        if cache_dir is not None:
            cache_root = Path(cache_dir).resolve()
        else:
            env_cache = os.environ.get("SHIPWRIGHT_REPOS_CACHE")
            if env_cache:
                cache_root = Path(env_cache).resolve()
            else:
                cache_root = (Path.home() / ".shipwright" / "repos").resolve()

        clone_url, target_dir = _get_remote_repo_info(spec, cache_root)

        # Check if already cloned and valid
        if target_dir.is_dir() and (target_dir / ".git").exists():
            print(f"Using cached repository: {target_dir}")
            return target_dir.resolve()

        # Clean up partial directory if .git doesn't exist
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        target_dir.parent.mkdir(parents=True, exist_ok=True)

        # Check for mock / test mode
        mock_mode = (
            os.environ.get("SHIPWRIGHT_MOCK_MODE", "").lower() in ("1", "true", "yes")
            or os.environ.get("SHIPWRIGHT_TEST_MODE", "").lower() in ("1", "true", "yes")
        )
        if mock_mode:
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / ".git").mkdir(parents=True, exist_ok=True)
            print(f"Repository cloned to: {target_dir}")
            return target_dir.resolve()

        print(f"Cloning repository from {clone_url} to {target_dir}...")
        try:
            proc = subprocess.run(
                ["git", "clone", clone_url, str(target_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to execute git clone: {exc}") from exc

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise RuntimeError(f"git clone failed: {err}")

        print(f"Repository cloned to: {target_dir}")
        return target_dir.resolve()

    # Local path
    local_path = Path(spec).expanduser().resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {local_path}")
    print(f"Resolved target repository to: {local_path}")
    return local_path


def _handle_run(args: argparse.Namespace) -> int:
    """Handle headless issue execution via Orchestrator."""
    raw_repo = getattr(args, "repo", ".") or "."
    repo_is_default = raw_repo in (".", "")

    # Auto-infer repository from issue argument if --repo was not specified or default '.'
    target_repo = raw_repo
    if repo_is_default:
        inferred = infer_repo_from_issue_url(getattr(args, "issue_or_text", None))
        if inferred:
            target_repo = inferred

    # Resolve target repository if explicitly specified or inferred from issue argument
    repo_path: str = "."
    if not repo_is_default or target_repo != ".":
        try:
            resolved_path = resolve_repository(target_repo)
            repo_path = str(resolved_path)
        except Exception as exc:
            print(f"shipwright run: Failed to resolve repository '{target_repo}': {exc}", file=sys.stderr)
            return 1

    # Load issue using resolved repo context
    try:
        issue = load_issue(args.issue_or_text, repo_path=repo_path)
    except Exception as exc:
        print(f"shipwright run: Failed to load issue '{args.issue_or_text}': {exc}", file=sys.stderr)
        return 1

    # If --repo was default '.' and target_repo was not yet inferred, check issue.url
    if repo_is_default and target_repo == ".":
        inferred_from_url = infer_repo_from_issue_url(getattr(issue, "url", None))
        if inferred_from_url:
            target_repo = inferred_from_url
            try:
                resolved_path = resolve_repository(target_repo)
                repo_path = str(resolved_path)
            except Exception as exc:
                print(f"shipwright run: Failed to resolve repository '{target_repo}': {exc}", file=sys.stderr)
                return 1
        else:
            try:
                resolved_path = resolve_repository(".")
                repo_path = str(resolved_path)
            except Exception as exc:
                print(f"shipwright run: Failed to resolve repository '.': {exc}", file=sys.stderr)
                return 1

    # Provider override environment
    env = None
    if getattr(args, "provider", None) or getattr(args, "model", None):
        env = dict(os.environ)
        if args.provider:
            env["SHIPWRIGHT_PROVIDER"] = args.provider
        if args.model:
            env["SHIPWRIGHT_MODEL"] = args.model

    provider = None
    try:
        provider = create_provider(env=env)
    except Exception as exc:
        # Provider creation failure will be logged or handled by Orchestrator
        pass

    # Initialize store
    db_path = getattr(args, "db", None)
    if RunStore is not None:
        store = RunStore(db_path=db_path, default_dir=(db_path is None))
    else:
        store = None

    # EventBus setup with live progress logging
    event_bus = EventBus()

    def on_progress_event(event: Event) -> None:
        topic = event.topic
        data = event.data or {}
        ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))

        if topic == RUN_STATE_CHANGED:
            from_st = data.get("from_state", "")
            to_st = data.get("to_state", "")
            if from_st and to_st:
                print(f"[{ts}] State: {from_st} ──> {to_st}")
            else:
                print(f"[{ts}] State: {to_st or from_st}")
        elif topic == AGENT_EVENT:
            actor = data.get("actor", "agent")
            msg = data.get("message", "")
            print(f"[{ts}] [{actor:<12}] {msg}")
        elif topic == TEST_OUTPUT:
            passed = data.get("passed", False)
            output = str(data.get("output", "")).strip()
            summary = output.splitlines()[-1] if output else ("passed" if passed else "failed")
            status = "PASSED" if passed else "FAILED"
            print(f"[{ts}] [test:{status}] {summary}")
        elif topic == ARTIFACT_ADDED:
            name = data.get("name", "")
            print(f"[{ts}] [artifact] Generated {name}")
        elif topic == RUN_COMPLETED:
            state = data.get("state", "")
            success = data.get("success", False)
            print(f"[{ts}] [completed] Finished with state {state} (success={success})")

    event_bus.subscribe("*", on_progress_event)

    print("=" * 60)
    print(f"SHIPWRIGHT HEADLESS RUN: #{issue.number} {issue.title}")
    print(f"Repository Root: {Path(repo_path).resolve()}")
    print("=" * 60)

    if Orchestrator is None:
        print("shipwright run: Orchestrator module is not available.", file=sys.stderr)
        return 1

    orchestrator = Orchestrator(
        repo_root=repo_path,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    max_attempts = getattr(args, "max_attempts", 3)
    try:
        run = orchestrator.run_issue(issue, max_attempts=max_attempts)
    except Exception as exc:
        print(f"shipwright run: Execution failed: {exc}", file=sys.stderr)
        return 1

    # Auto-approve if requested and review passed
    if getattr(args, "auto_approve", False) and run.state == RunState.READY_FOR_APPROVAL:
        out_dir = Path(f"./export/run-{run.number}")
        export_bundle(run, out_dir)
        run.state = RunState.EXPORTED
        if store is not None:
            store.save_run(run)
        print(f"[{time.strftime('%H:%M:%S')}] [auto-approve] Exported run bundle to {out_dir}")

    # Output detailed summary
    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"Run Number:  #{run.number}")
    print(f"Title:       {run.title}")
    state_val = run.state.value if hasattr(run.state, "value") else str(run.state)
    print(f"State:       {state_val}")
    print(f"Elapsed:     {run.elapsed}")
    print(f"Agents:      {run.agents}")
    print(f"Tests:       {run.tests}")
    if run.branch:
        print(f"Branch:      {run.branch}")
    if run.worktree_path:
        print(f"Worktree:    {run.worktree_path}")
    print(f"Attempts:    {run.attempt} / {run.max_attempts}")

    if run.review is not None:
        rev_status = "PASSED" if run.review.passed else "FAILED"
        print(f"Review:      {rev_status} (Confidence: {run.review.confidence:.2f})")
        if run.review.summary:
            print(f"  Summary:   {run.review.summary}")
        if run.review.findings:
            print("  Findings:")
            for item in run.review.findings:
                print(f"    - {item}")

    if run.artifacts:
        print(f"Artifacts ({len(run.artifacts)}):")
        for art in run.artifacts:
            art_name = getattr(art, "name", str(art))
            print(f"  • {art_name}")

    if run.error:
        print(f"Error:       {run.error}")

    print("=" * 60)

    if run.state in (RunState.READY_FOR_APPROVAL, RunState.EXPORTED):
        return 0
    return 1


def _handle_list(args: argparse.Namespace) -> int:
    """Handle list subcommand displaying runs from RunStore."""
    db_path = getattr(args, "db", None)
    if RunStore is None:
        print("shipwright list: RunStore is not available.", file=sys.stderr)
        return 1

    store = RunStore(db_path=db_path, default_dir=(db_path is None))
    runs = store.list_runs()

    # Apply state filter
    state_filter = getattr(args, "state", None)
    if state_filter:
        filter_str = str(state_filter).strip().upper()
        runs = [
            r for r in runs
            if (r.state.value if hasattr(r.state, "value") else str(r.state)).upper() == filter_str
        ]

    # Apply limit
    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        runs = runs[:limit]

    # JSON formatted output
    if getattr(args, "json", False):
        records = []
        for r in runs:
            records.append(
                {
                    "number": r.number,
                    "title": r.title,
                    "state": r.state.value if hasattr(r.state, "value") else str(r.state),
                    "elapsed": r.elapsed,
                    "agents": r.agents,
                    "tests": r.tests,
                    "branch": r.branch,
                    "attempt": r.attempt,
                    "max_attempts": r.max_attempts,
                }
            )
        print(json.dumps(records, indent=2))
        return 0

    if not runs:
        print("No runs found.")
        return 0

    # Human-readable table
    header = f"{'RUN':<8} {'STATE':<20} {'TESTS':<16} {'ELAPSED':<9} {'TITLE'}"
    print(header)
    print("-" * len(header))
    for r in runs:
        state_str = r.state.value if hasattr(r.state, "value") else str(r.state)
        print(f"#{r.number:<7} {state_str:<20} {r.tests:<16} {r.elapsed:<9} {r.title}")

    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Handle status subcommand displaying state, events, and artifacts for a run."""
    db_path = getattr(args, "db", None)
    if RunStore is None:
        print("shipwright status: RunStore is not available.", file=sys.stderr)
        return 1

    store = RunStore(db_path=db_path, default_dir=(db_path is None))
    run_num = args.run_number
    watch = getattr(args, "watch", False)

    while True:
        run = store.get_run(run_num)

        # Fallback to fixture runs if not in store
        if run is None:
            for f in fixture_runs():
                if f.number == run_num:
                    run = f
                    break

        if run is None:
            print(f"Run #{run_num} not found.", file=sys.stderr)
            return 1

        state_str = run.state.value if hasattr(run.state, "value") else str(run.state)

        if getattr(args, "json", False):
            payload = {
                "number": run.number,
                "title": run.title,
                "state": state_str,
                "elapsed": run.elapsed,
                "agents": run.agents,
                "tests": run.tests,
                "branch": run.branch,
                "repo_path": run.repo_path,
                "worktree_path": run.worktree_path,
                "attempt": run.attempt,
                "max_attempts": run.max_attempts,
                "error": run.error,
                "events": [
                    {
                        "timestamp": ev.timestamp,
                        "actor": ev.actor,
                        "message": ev.message,
                    }
                    for ev in run.events
                ],
                "artifacts": [getattr(a, "name", str(a)) for a in run.artifacts],
                "review": {
                    "passed": run.review.passed,
                    "confidence": run.review.confidence,
                    "summary": run.review.summary,
                    "findings": run.review.findings,
                }
                if run.review
                else None,
            }
            print(json.dumps(payload, indent=2))
            return 0

        # Formatted console output
        print("=" * 60)
        print(f"RUN #{run.number}: {run.title}")
        print("=" * 60)
        print(f"State:       {state_str}")
        print(f"Elapsed:     {run.elapsed}")
        print(f"Agents:      {run.agents}")
        print(f"Tests:       {run.tests}")
        print(f"Branch:      {run.branch or 'N/A'}")
        print(f"Attempts:    {run.attempt} / {run.max_attempts}")
        if run.error:
            print(f"Error:       {run.error}")

        print("\nTIMELINE EVENTS:")
        print("-" * 60)
        if run.events:
            for ev in run.events:
                print(f"{ev.timestamp}  [{ev.actor:<12}]  {ev.message}")
        else:
            print("No events recorded.")

        print("\nARTIFACTS:")
        print("-" * 60)
        if run.artifacts:
            for art in run.artifacts:
                art_name = getattr(art, "name", str(art))
                print(f"• {art_name}")
        else:
            print("No artifacts recorded.")

        if run.review is not None:
            rev_status = "PASSED" if run.review.passed else "FAILED"
            print("\nREVIEW:")
            print("-" * 60)
            print(f"Status:      {rev_status}")
            print(f"Confidence:  {run.review.confidence:.2f}")
            if run.review.summary:
                print(f"Summary:     {run.review.summary}")
            if run.review.findings:
                print("Findings:")
                for item in run.review.findings:
                    print(f"  - {item}")

        print("=" * 60)

        terminal_states = {
            RunState.EXPORTED,
            RunState.BLOCKED,
            RunState.CANCELLED,
            RunState.READY_FOR_APPROVAL,
        }
        if not watch or run.state in terminal_states:
            break
        time.sleep(1.0)

    return 0


def _handle_export(args: argparse.Namespace) -> int:
    """Handle export subcommand via shipwright.export.export_bundle."""
    db_path = getattr(args, "db", None)
    store = RunStore(db_path=db_path, default_dir=(db_path is None)) if RunStore is not None else None
    run_num = args.run_number
    run = store.get_run(run_num) if store is not None else None

    # Fallback to fixture runs if not in store
    if run is None:
        for f in fixture_runs():
            if f.number == run_num:
                run = f
                break

    if run is None:
        print(f"Run #{run_num} not found.", file=sys.stderr)
        return 1

    if getattr(args, "approve", False):
        run.state = RunState.EXPORTED
        if store is not None:
            store.save_run(run)

    out_dir = getattr(args, "out", None) or Path(f"./export/run-{run.number}")
    export_path = export_bundle(run, out_dir)

    # Format check if raw patch diff requested
    if getattr(args, "format", "bundle") == "patch":
        diff_file = Path(export_path) / "patch.diff"
        if diff_file.is_file():
            print(diff_file.read_text(encoding="utf-8"))
            return 0

    print(f"Exported run #{run.number} bundle to: {export_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for Shipwright."""
    prog = None
    if sys.argv and "shipwright-chat" in os.path.basename(sys.argv[0]).lower():
        prog = "shipwright-chat"
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)

    if args.subcommand is None:
        from .tui import main as tui_main

        tui_main()
        return 0

    if args.subcommand == "chat":
        return _handle_chat(args)
    if args.subcommand == "run":
        return _handle_run(args)
    if args.subcommand == "list":
        return _handle_list(args)
    if args.subcommand == "status":
        return _handle_status(args)
    if args.subcommand == "export":
        return _handle_export(args)

    print(f"shipwright: Unknown subcommand '{args.subcommand}'", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
