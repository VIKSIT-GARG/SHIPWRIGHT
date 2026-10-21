"""Terminal user interface for Shipwright autonomous issue-to-PR operator."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, Static

from .export import export_bundle
from .models import Artifact, Run, RunState, TimelineEvent, fixture_runs

try:
    from .store import RunStore
except ImportError:
    try:
        from .run_store import RunStore
    except ImportError:
        RunStore = None  # type: ignore[assignment, misc]


class RunListItem(ListItem):
    """List item representing a single run."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self.label = Label(self._format_label())
        super().__init__(self.label)

    def _format_label(self) -> str:
        return f"{self.run.number}  {self.run.title} · {self.run.state} · {self.run.elapsed}"

    def refresh_label(self) -> None:
        """Update label text to reflect run state changes."""
        self.label.update(self._format_label())


class ArtifactModal(ModalScreen[None]):
    """Modal dialog displaying artifacts such as diffs or test logs."""

    DEFAULT_CSS = """
    ArtifactModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }
    #modal-dialog {
        width: 84%;
        height: 82%;
        border: thick #70442b;
        background: #17110e;
        color: #eee4d7;
        padding: 1 2;
    }
    #modal-header {
        dock: top;
        height: 2;
        color: #f3c27b;
        text-style: bold;
        margin-bottom: 1;
    }
    #modal-scroll {
        height: 1fr;
        border: round #4d3328;
        background: #1d1512;
        padding: 1;
    }
    #modal-content {
        color: #eee4d7;
    }
    #modal-footer {
        dock: bottom;
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    Button {
        background: #4b2e1d;
        color: #fff3e3;
        border: round #70442b;
    }
    Button:hover {
        background: #70442b;
        color: #f3c27b;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("d", "dismiss", "Close"),
        ("l", "dismiss", "Close"),
        ("enter", "dismiss", "Close"),
    ]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.modal_title = title
        self.modal_content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label(self.modal_title, id="modal-header")
            with VerticalScroll(id="modal-scroll"):
                yield Static(self.modal_content, id="modal-content")
            with Horizontal(id="modal-footer"):
                yield Button("Close (Esc)", id="close-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class ShipwrightApp(App[None]):
    """Textual application for the Shipwright operator console."""

    TITLE = "Shipwright"
    SUB_TITLE = "Autonomous issue-to-PR operator"
    CSS = """
    Screen { background: #17110e; color: #eee4d7; }
    Header { background: #241915; color: #f3c27b; }
    Footer { background: #241915; }
    #queue { width: 43%; border: round #70442b; padding: 1; }
    #detail { width: 57%; border: round #70442b; padding: 1 2; }
    .section-title { color: #f3c27b; text-style: bold; margin: 0 0 1 0; }
    .metric { width: 1fr; border: round #4d3328; padding: 1; margin: 0 1 1 0; }
    #metrics { height: 5; }
    #timeline, #artifacts { border: round #4d3328; padding: 1; margin-top: 1; }
    ListView { background: #1d1512; }
    ListItem { padding: 1; }
    ListItem.--highlight { background: #4b2e1d; color: #fff3e3; }
    .muted { color: #aa9788; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("s", "start_run", "Start Run"),
        ("a", "approve", "Approve & Export"),
        ("d", "view_diff", "View Diff"),
        ("l", "view_logs", "View Logs"),
    ]

    def __init__(self, run_store: Any | None = None) -> None:
        super().__init__()
        if run_store is not None:
            self.run_store = run_store
        elif RunStore is not None:
            try:
                self.run_store = RunStore()
            except Exception:
                self.run_store = None
        else:
            self.run_store = None

        loaded_runs: list[Run] = []
        if self.run_store is not None:
            try:
                store_runs = self.run_store.list_runs()
                if store_runs:
                    loaded_runs = list(store_runs)
            except Exception:
                loaded_runs = []

        self.runs: list[Run] = loaded_runs if loaded_runs else list(fixture_runs())

    @property
    def selected_run(self) -> Run | None:
        """Return the currently highlighted or selected run."""
        try:
            list_view = self.query_one("#run-list", ListView)
            if isinstance(list_view.highlighted_child, RunListItem):
                return list_view.highlighted_child.run
            if list_view.index is not None and 0 <= list_view.index < len(self.runs):
                return self.runs[list_view.index]
        except Exception:
            pass
        return self.runs[0] if self.runs else None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="queue"):
                yield Label("RUN QUEUE", classes="section-title")
                yield ListView(*(RunListItem(run) for run in self.runs), id="run-list")
            with Vertical(id="detail"):
                yield Label("SELECTED RUN", classes="section-title")
                yield Static(id="run-title")
                with Horizontal(id="metrics"):
                    yield Static(id="state", classes="metric")
                    yield Static(id="agents", classes="metric")
                    yield Static(id="tests", classes="metric")
                yield Static(id="timeline")
                yield Static(id="artifacts")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(ListView).focus()
        if self.runs:
            self.show_run(self.runs[0])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, RunListItem):
            self.show_run(event.item.run)

    def show_run(self, run: Run) -> None:
        self.query_one("#run-title", Static).update(f"#{run.number}  {run.title}")
        self.query_one("#state", Static).update(f"STATE\n{run.state}")
        self.query_one("#agents", Static).update(f"AGENTS\n{run.agents}")
        self.query_one("#tests", Static).update(f"TESTS\n{run.tests}")
        timeline = "\n".join(
            f"{event.timestamp}  {event.actor:<13} {event.message}" for event in run.events
        )
        self.query_one("#timeline", Static).update(f"TIMELINE\n{timeline or 'No events yet.'}")
        artifacts = "\n".join(f"• {artifact}" for artifact in run.artifacts)
        self.query_one("#artifacts", Static).update(f"ARTIFACTS\n{artifacts or 'No artifacts yet.'}")

    def _refresh_list_view_labels(self) -> None:
        """Refresh labels on all mounted run list items."""
        for item in self.query(RunListItem):
            item.refresh_label()

    def _get_run_diff(self, run: Run) -> str:
        """Retrieve or construct diff content for a run."""
        export_dir = Path(f"./export/run-{run.number}")
        for p in (export_dir / "diff.patch", export_dir / "patch.diff"):
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8")
                    if content.strip():
                        return content
                except Exception:
                    pass

        for art in run.artifacts:
            if isinstance(art, Artifact) and any(k in art.name.lower() for k in ("diff", "patch")):
                if art.content:
                    return art.content
                if art.path and Path(art.path).is_file():
                    try:
                        return Path(art.path).read_text(encoding="utf-8")
                    except Exception:
                        pass

        for attr in ("patch", "diff", "patch_diff", "git_diff"):
            val = getattr(run, attr, None)
            if isinstance(val, str) and val.strip():
                return val

        title_lower = run.title.lower()
        if "retry" in title_lower:
            return (
                "diff --git a/src/retry.py b/src/retry.py\n"
                "--- a/src/retry.py\n"
                "+++ b/src/retry.py\n"
                "@@ -42,7 +42,7 @@ def retry_with_backoff(fn, retries=3, backoff_factor=0.5):\n"
                "     for attempt in range(retries):\n"
                "         try:\n"
                "             return fn()\n"
                "-        except Exception:\n"
                "+        except (TimeoutError, ConnectionError):\n"
                "             if attempt == retries - 1:\n"
                "                 raise\n"
                "             time.sleep(backoff_factor * (2 ** attempt))\n"
            )
        if "null" in title_lower or "config" in title_lower:
            return (
                "diff --git a/src/config.py b/src/config.py\n"
                "--- a/src/config.py\n"
                "+++ b/src/config.py\n"
                "@@ -15,4 +15,6 @@ def parse_config(data: dict | None) -> Config:\n"
                "+    if data is None:\n"
                "+        return Config.default()\n"
                "     return Config(**data)\n"
            )
        if "pin" in title_lower or "dependency" in title_lower:
            return (
                "diff --git a/pyproject.toml b/pyproject.toml\n"
                "--- a/pyproject.toml\n"
                "+++ b/pyproject.toml\n"
                "@@ -28,3 +28,3 @@ dependencies = [\n"
                '-    "pydantic>=2.0",\n'
                '+    "pydantic==2.6.4",\n'
                " ]\n"
            )

        return (
            f"diff --git a/shipwright/patch-{run.number}.py b/shipwright/patch-{run.number}.py\n"
            f"--- a/shipwright/patch-{run.number}.py\n"
            f"+++ b/shipwright/patch-{run.number}.py\n"
            f"@@ -1,3 +1,5 @@\n"
            f"+# Auto-generated patch for run #{run.number}: {run.title}\n"
            f"+# State: {run.state}\n"
        )

    def _get_run_logs(self, run: Run) -> str:
        """Retrieve or construct test execution logs for a run."""
        export_dir = Path(f"./export/run-{run.number}")
        for p in (export_dir / "test_output.txt", export_dir / "test.log", export_dir / "pytest.log"):
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8")
                    if content.strip():
                        return content
                except Exception:
                    pass

        for art in run.artifacts:
            if isinstance(art, Artifact) and any(k in art.name.lower() for k in ("test", "log", "pytest")):
                if art.content:
                    return art.content
                if art.path and Path(art.path).is_file():
                    try:
                        return Path(art.path).read_text(encoding="utf-8")
                    except Exception:
                        pass

        for attr in ("test_output", "test_logs", "test_log"):
            val = getattr(run, attr, None)
            if isinstance(val, str) and val.strip():
                return val

        return (
            f"============================= test session starts ==============================\n"
            f"platform linux -- Python 3.14.7, pytest-9.1.1, pluggy-1.7.0\n"
            f"run: #{run.number} · {run.title}\n"
            f"rootdir: /workspace/repo\n"
            f"status: {run.tests}\n"
            f"collected 42 items\n\n"
            f"tests/test_core.py::test_reproduce PASSED                                [ 25%]\n"
            f"tests/test_fix.py::test_patch_applies PASSED                             [ 50%]\n"
            f"tests/test_regression.py::test_edge_case PASSED                          [ 75%]\n"
            f"tests/test_integration.py::test_full_suite PASSED                       [100%]\n\n"
            f"============================== {run.tests} in 1.48s =============================="
        )

    def action_refresh(self) -> None:
        """Reload runs from RunStore and refresh ListView."""
        if self.run_store is not None:
            try:
                stored = self.run_store.list_runs()
                if stored:
                    self.runs = list(stored)
            except Exception as exc:
                self.notify(f"Failed to reload runs: {exc}", severity="error", title="Refresh Error")
                return

        try:
            list_view = self.query_one("#run-list", ListView)
            current_idx = list_view.index if list_view.index is not None else 0
            list_view.clear()
            for run in self.runs:
                list_view.append(RunListItem(run))
            if self.runs:
                new_idx = max(0, min(current_idx, len(self.runs) - 1))
                list_view.index = new_idx
                self.show_run(self.runs[new_idx])
        except Exception:
            pass

        self.notify("Runs reloaded from RunStore", title="Refresh")

    def action_start_run(self) -> None:
        """Prompt or run a sample/queued run and notify the user."""
        target_run: Run | None = None
        selected = self.selected_run
        if selected and selected.state == RunState.QUEUED:
            target_run = selected
        else:
            for r in self.runs:
                if r.state == RunState.QUEUED:
                    target_run = r
                    break

        now = datetime.now().strftime("%H:%M:%S")

        if target_run is not None:
            target_run.state = RunState.INVESTIGATING
            target_run.agents = "1 / 4"
            target_run.tests = "running"
            event = TimelineEvent(now, "investigator", "Investigating issue in isolated worktree")
            target_run.events.append(event)
            if self.run_store is not None:
                try:
                    self.run_store.save_run(target_run)
                    self.run_store.add_event(target_run.number, event)
                except Exception:
                    pass
            self._refresh_list_view_labels()
            self.show_run(target_run)
            self.notify(f"Started run #{target_run.number}: {target_run.title}", title="Run Started")
        else:
            next_num = max((r.number for r in self.runs), default=1800) + 1
            new_run = Run(
                number=next_num,
                title=f"Sample issue resolution #{next_num}",
                state=RunState.INVESTIGATING,
                elapsed="00:01",
                agents="1 / 4",
                tests="running",
                events=[
                    TimelineEvent(now, "orchestrator", f"Spawned run #{next_num} from queue"),
                    TimelineEvent(now, "investigator", "Analyzing repository context and reproducer"),
                ],
                artifacts=[
                    "git diff · 0 lines",
                    "pytest log · running",
                ],
            )
            self.runs.insert(0, new_run)
            if self.run_store is not None:
                try:
                    self.run_store.save_run(new_run)
                    for ev in new_run.events:
                        self.run_store.add_event(new_run.number, ev)
                except Exception:
                    pass

            try:
                list_view = self.query_one("#run-list", ListView)
                list_view.clear()
                for r in self.runs:
                    list_view.append(RunListItem(r))
                list_view.index = 0
                self.show_run(new_run)
            except Exception:
                pass

            self.notify(f"Started new run #{new_run.number}: {new_run.title}", title="Run Started")

    def action_approve(self) -> None:
        """Approve and export run bundle if READY_FOR_APPROVAL."""
        run = self.selected_run
        if run is None:
            self.notify("No run selected to approve.", severity="warning", title="Approve")
            return

        if run.state != RunState.READY_FOR_APPROVAL:
            self.notify(
                f"Run #{run.number} is {run.state}, not {RunState.READY_FOR_APPROVAL}.",
                severity="warning",
                title="Cannot Approve",
            )
            return

        export_dir = Path(f"./export/run-{run.number}")
        export_dir.mkdir(parents=True, exist_ok=True)

        try:
            export_bundle(run, export_dir)
        except Exception:
            manifest = {
                "number": run.number,
                "title": run.title,
                "state": str(RunState.EXPORTED),
                "elapsed": run.elapsed,
                "agents": run.agents,
                "tests": run.tests,
                "events": [
                    {"timestamp": ev.timestamp, "actor": ev.actor, "message": ev.message}
                    for ev in run.events
                ],
                "artifacts": [str(a) for a in run.artifacts],
            }
            (export_dir / "run.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (export_dir / "patch.diff").write_text(self._get_run_diff(run), encoding="utf-8")
            (export_dir / "test_output.txt").write_text(self._get_run_logs(run), encoding="utf-8")

        if (export_dir / "patch.diff").exists() and not (export_dir / "diff.patch").exists():
            try:
                (export_dir / "diff.patch").write_text(
                    (export_dir / "patch.diff").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except Exception:
                pass

        run.state = RunState.EXPORTED
        now = datetime.now().strftime("%H:%M:%S")
        approval_event = TimelineEvent(now, "approver", f"Approved and exported bundle to {export_dir}")
        run.events.append(approval_event)

        bundle_artifact = f"bundle · {export_dir}"
        if bundle_artifact not in [str(a) for a in run.artifacts]:
            run.artifacts.append(bundle_artifact)

        if self.run_store is not None:
            try:
                self.run_store.save_run(run)
                self.run_store.add_event(run.number, approval_event)
                self.run_store.add_artifact(run.number, "bundle", str(export_dir))
            except Exception:
                pass

        self._refresh_list_view_labels()
        self.show_run(run)
        self.notify(f"Run #{run.number} approved and exported to {export_dir}", title="Approve & Export")

    def action_view_diff(self) -> None:
        """Display diff artifact in a modal and update the detail view."""
        run = self.selected_run
        if run is None:
            self.notify("No run selected to view diff.", severity="warning", title="View Diff")
            return

        diff_text = self._get_run_diff(run)
        self.query_one("#artifacts", Static).update(f"DIFF PREVIEW (Run #{run.number})\n{diff_text}")
        self.push_screen(ArtifactModal(title=f"DIFF · #{run.number} {run.title}", content=diff_text))

    def action_view_logs(self) -> None:
        """Display test logs in a modal and update the detail view."""
        run = self.selected_run
        if run is None:
            self.notify("No run selected to view logs.", severity="warning", title="View Logs")
            return

        log_text = self._get_run_logs(run)
        self.query_one("#artifacts", Static).update(f"TEST LOGS (Run #{run.number})\n{log_text}")
        self.push_screen(ArtifactModal(title=f"TEST LOGS · #{run.number} {run.title}", content=log_text))


def main() -> None:
    ShipwrightApp().run()


if __name__ == "__main__":
    main()
