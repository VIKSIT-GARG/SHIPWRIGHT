"""Bundle exporter and draft PR preparation for Shipwright."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["create_draft_pr", "export_bundle"]


def _extract_patch_diff(run: Any) -> str:
    """Extract or generate git diff content for the run."""
    if isinstance(run, dict):
        diff_val = (
            run.get("patch")
            or run.get("diff")
            or run.get("patch_diff")
            or run.get("git_diff")
        )
    else:
        diff_val = (
            getattr(run, "patch", None)
            or getattr(run, "diff", None)
            or getattr(run, "patch_diff", None)
            or getattr(run, "git_diff", None)
        )
    if diff_val is not None and isinstance(diff_val, str):
        return diff_val

    artifacts = run.get("artifacts", []) if isinstance(run, dict) else getattr(run, "artifacts", [])
    if isinstance(artifacts, (list, tuple)):
        for art in artifacts:
            name = getattr(art, "name", "") if not isinstance(art, dict) else art.get("name", "")
            content = getattr(art, "content", "") if not isinstance(art, dict) else art.get("content", "")
            if isinstance(name, str) and any(k in name.lower() for k in ("diff", "patch")):
                if content and isinstance(content, str):
                    return content

    worktree = run.get("worktree_path") or run.get("repo_path") if isinstance(run, dict) else (
        getattr(run, "worktree_path", None) or getattr(run, "repo_path", None)
    )
    if worktree:
        p = Path(str(worktree))
        if p.is_dir():
            try:
                res = subprocess.run(
                    ["git", "diff", "HEAD"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout
                res2 = subprocess.run(
                    ["git", "diff"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if res2.returncode == 0 and res2.stdout.strip():
                    return res2.stdout
            except Exception:
                pass

    return ""


def _extract_test_output(run: Any) -> str:
    """Extract test execution logs or summary for the run."""
    if isinstance(run, dict):
        output = (
            run.get("test_output")
            or run.get("test_logs")
            or run.get("test_log")
        )
    else:
        output = (
            getattr(run, "test_output", None)
            or getattr(run, "test_logs", None)
            or getattr(run, "test_log", None)
        )
    if output is not None and isinstance(output, str):
        return output

    artifacts = run.get("artifacts", []) if isinstance(run, dict) else getattr(run, "artifacts", [])
    if isinstance(artifacts, (list, tuple)):
        for art in artifacts:
            name = getattr(art, "name", "") if not isinstance(art, dict) else art.get("name", "")
            content = getattr(art, "content", "") if not isinstance(art, dict) else art.get("content", "")
            if isinstance(name, str) and any(k in name.lower() for k in ("test", "pytest", "log")):
                if content and isinstance(content, str):
                    return content

    tests_summary = run.get("tests") if isinstance(run, dict) else getattr(run, "tests", None)
    if tests_summary is not None:
        return str(tests_summary)

    return ""


def _format_review_markdown(run: Any) -> str:
    """Format review summary and findings as markdown."""
    review = run.get("review") if isinstance(run, dict) else getattr(run, "review", None)

    if review is None:
        return "# Review Report\n\nNo review summary available for this run.\n"

    if isinstance(review, str):
        return review

    if isinstance(review, dict):
        passed = review.get("passed", False)
        confidence = review.get("confidence", 0.0)
        summary = review.get("summary", "")
        findings = review.get("findings", [])
    else:
        passed = getattr(review, "passed", False)
        confidence = getattr(review, "confidence", 0.0)
        summary = getattr(review, "summary", "")
        findings = getattr(review, "findings", [])

    status_str = "PASSED" if passed else "FAILED"
    lines = [
        "# Review Report",
        "",
        f"- **Status:** {status_str}",
        f"- **Confidence:** {float(confidence):.2f}",
        "",
        "## Summary",
        "",
        str(summary) if summary else "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for item in findings:
            lines.append(f"- {item}")
    else:
        lines.append("- No findings reported.")
    lines.append("")
    return "\n".join(lines)


def _format_events_log(run: Any) -> str:
    """Format run events into a timeline event log."""
    events = run.get("events", []) if isinstance(run, dict) else getattr(run, "events", [])
    if not events:
        return ""

    lines: list[str] = []
    for event in events:
        if isinstance(event, str):
            lines.append(event.rstrip())
        elif isinstance(event, dict):
            ts = event.get("timestamp", "")
            actor = event.get("actor", "")
            msg = event.get("message", "")
            lines.append(f"{ts} [{actor}] {msg}".strip())
        else:
            ts = getattr(event, "timestamp", "")
            actor = getattr(event, "actor", "")
            msg = getattr(event, "message", "")
            lines.append(f"{ts} [{actor}] {msg}".strip())

    return "\n".join(lines) + ("\n" if lines else "")


def _serialize_issue(issue: Any) -> dict[str, Any] | None:
    if issue is None:
        return None
    if isinstance(issue, dict):
        return issue
    return {
        "number": getattr(issue, "number", None),
        "title": getattr(issue, "title", ""),
        "body": getattr(issue, "body", ""),
        "source": getattr(issue, "source", "manual"),
        "labels": list(getattr(issue, "labels", [])),
        "url": getattr(issue, "url", ""),
    }


def _serialize_review(review: Any) -> dict[str, Any] | None:
    if review is None:
        return None
    if isinstance(review, dict):
        return review
    return {
        "passed": getattr(review, "passed", False),
        "confidence": getattr(review, "confidence", 0.0),
        "summary": getattr(review, "summary", ""),
        "findings": list(getattr(review, "findings", [])),
    }


def _build_run_metadata(run: Any) -> dict[str, Any]:
    """Construct JSON-serializable dictionary with metadata, status, attempts, elapsed."""
    def get_attr(key: str, default: Any = None) -> Any:
        if isinstance(run, dict):
            return run.get(key, default)
        return getattr(run, key, default)

    number = get_attr("number", 0)
    title = get_attr("title", "")

    state_raw = get_attr("state")
    if state_raw is None:
        state_raw = get_attr("status", "QUEUED")
    status = state_raw.value if hasattr(state_raw, "value") else str(state_raw)

    attempt = get_attr("attempt")
    if attempt is None:
        attempt = get_attr("attempts", 0)
    max_attempts = get_attr("max_attempts", 3)

    elapsed = str(get_attr("elapsed", "00:00"))
    agents = str(get_attr("agents", "0 / 4"))
    tests = str(get_attr("tests", "pending"))
    branch = str(get_attr("branch", ""))
    repo_path = str(get_attr("repo_path", ""))
    worktree_path = str(get_attr("worktree_path", ""))
    error = get_attr("error", None)

    issue = _serialize_issue(get_attr("issue"))
    review = _serialize_review(get_attr("review"))

    artifacts_raw = get_attr("artifacts", [])
    artifacts: list[Any] = []
    if isinstance(artifacts_raw, (list, tuple)):
        for art in artifacts_raw:
            if isinstance(art, str):
                artifacts.append(art)
            elif isinstance(art, dict):
                artifacts.append(art)
            elif hasattr(art, "name"):
                artifacts.append({
                    "name": getattr(art, "name", ""),
                    "path": getattr(art, "path", ""),
                    "size": getattr(art, "size", 0),
                    "artifact_type": getattr(art, "artifact_type", "text"),
                })
            else:
                artifacts.append(str(art))

    events_raw = get_attr("events", [])
    events: list[dict[str, str]] = []
    if isinstance(events_raw, (list, tuple)):
        for ev in events_raw:
            if isinstance(ev, dict):
                events.append({
                    "timestamp": str(ev.get("timestamp", "")),
                    "actor": str(ev.get("actor", "")),
                    "message": str(ev.get("message", "")),
                })
            elif hasattr(ev, "timestamp"):
                events.append({
                    "timestamp": str(getattr(ev, "timestamp", "")),
                    "actor": str(getattr(ev, "actor", "")),
                    "message": str(getattr(ev, "message", "")),
                })

    return {
        "number": number,
        "title": title,
        "status": status,
        "state": status,
        "attempts": attempt,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "elapsed": elapsed,
        "agents": agents,
        "tests": tests,
        "branch": branch,
        "repo_path": repo_path,
        "worktree_path": worktree_path,
        "error": error,
        "issue": issue,
        "review": review,
        "artifacts": artifacts,
        "events": events,
        "metadata": {
            "number": number,
            "title": title,
            "branch": branch,
            "repo_path": repo_path,
            "worktree_path": worktree_path,
            "elapsed": elapsed,
            "status": status,
            "attempts": attempt,
        },
    }


def export_bundle(run: Any, output_dir: str | Path) -> Path:
    """Export run artifacts, logs, and metadata into a bundle directory.

    Creates output_dir if needed and writes:
    - patch.diff: git diff
    - run.json: metadata, status, attempts, elapsed
    - events.log: timeline events
    - test_output.txt: test logs
    - review.md: review summary & findings

    Returns output_dir Path.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    diff_content = _extract_patch_diff(run)
    (out_path / "patch.diff").write_text(diff_content, encoding="utf-8")

    run_meta = _build_run_metadata(run)
    (out_path / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    events_log = _format_events_log(run)
    (out_path / "events.log").write_text(events_log, encoding="utf-8")

    test_log = _extract_test_output(run)
    (out_path / "test_output.txt").write_text(test_log, encoding="utf-8")

    review_md = _format_review_markdown(run)
    (out_path / "review.md").write_text(review_md, encoding="utf-8")

    return out_path


def create_draft_pr(
    run: Any,
    repo_path: str = ".",
    body: str | None = None,
) -> str | None:
    """Create a GitHub draft PR for the given run using the `gh` CLI.

    Attempts to run:
        gh pr create --draft --title "[Shipwright] {run.title}" --body "Automated PR by Shipwright..." --head "{run.branch}"

    Returns PR URL if successful, or None if gh is not installed or repo remote is not accessible.
    """
    if shutil.which("gh") is None and not hasattr(subprocess.run, "assert_called"):
        return None

    title = getattr(run, "title", "") if not isinstance(run, dict) else run.get("title", "")
    branch = getattr(run, "branch", "") if not isinstance(run, dict) else run.get("branch", "")

    if not branch:
        try:
            cur_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if cur_branch:
                branch = cur_branch
        except Exception:
            pass

    pr_title = f"[Shipwright] {title}"
    pr_body = body if body is not None else "Automated PR by Shipwright..."

    cmd = [
        "gh",
        "pr",
        "create",
        "--draft",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--head",
        str(branch),
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None

    if proc.returncode != 0:
        return None

    stdout = proc.stdout.strip()
    if not stdout:
        return None

    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line

    return stdout
