import asyncio
import shutil
from pathlib import Path

from shipwright.models import Run, RunState, TimelineEvent
from shipwright.store import RunStore
from shipwright.tui import ArtifactModal, ShipwrightApp


def _get_text(widget: object) -> str:
    value = getattr(widget, "renderable", None)
    if value is None:
        value = getattr(widget, "content", None)
    if value is None:
        value = getattr(widget, "_content", "")
    return str(value)


def test_tui_renders_and_selects_runs() -> None:
    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            assert _get_text(app.query_one("#run-title")) == "#1842  Fix flaky retry test"
            await pilot.press("down")
            assert _get_text(app.query_one("#run-title")) == "#1839  Handle null config"

    asyncio.run(run())


def test_tui_bindings_definition() -> None:
    expected_bindings = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("s", "start_run", "Start Run"),
        ("a", "approve", "Approve & Export"),
        ("d", "view_diff", "View Diff"),
        ("l", "view_logs", "View Logs"),
    ]
    for b in expected_bindings:
        assert b in ShipwrightApp.BINDINGS


def test_tui_loads_from_run_store() -> None:
    store = RunStore()
    custom_run = Run(
        number=9999,
        title="Custom Store Run",
        state=RunState.QUEUED,
        elapsed="01:00",
        agents="1 / 4",
        tests="1 passed",
    )
    store.save_run(custom_run)

    async def run() -> None:
        app = ShipwrightApp(run_store=store)
        async with app.run_test():
            assert len(app.runs) == 1
            assert app.runs[0].number == 9999
            assert _get_text(app.query_one("#run-title")) == "#9999  Custom Store Run"

    asyncio.run(run())


def test_tui_action_refresh() -> None:
    store = RunStore()
    r1 = Run(number=501, title="Initial Run", state=RunState.INVESTIGATING)
    store.save_run(r1)

    async def run() -> None:
        app = ShipwrightApp(run_store=store)
        async with app.run_test() as pilot:
            assert len(app.runs) == 1
            assert app.runs[0].number == 501

            # Add another run to store
            r2 = Run(number=502, title="Second Run", state=RunState.VERIFYING)
            store.save_run(r2)

            # Trigger refresh via pilot keybinding
            await pilot.press("r")
            assert len(app.runs) == 2
            assert {r.number for r in app.runs} == {501, 502}

    asyncio.run(run())


def test_tui_action_start_run() -> None:
    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            initial_count = len(app.runs)
            # Press 's' to start run
            await pilot.press("s")
            # Should have added a new run or started a queued run
            assert len(app.runs) >= initial_count
            assert app.runs[0].state == RunState.INVESTIGATING

    asyncio.run(run())


def test_tui_action_approve_and_export(tmp_path: Path) -> None:
    export_dir = Path("./export/run-1828")
    if export_dir.exists():
        shutil.rmtree(export_dir)

    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            # Navigate to Run #1828 which is READY_FOR_APPROVAL
            await pilot.press("down")
            await pilot.press("down")
            assert _get_text(app.query_one("#run-title")) == "#1828  Pin dependency"
            assert app.selected_run is not None
            assert app.selected_run.state == RunState.READY_FOR_APPROVAL

            # Approve via 'a'
            await pilot.press("a")
            assert app.selected_run.state == RunState.EXPORTED
            assert export_dir.exists()
            assert (export_dir / "run.json").exists()

    try:
        asyncio.run(run())
    finally:
        if export_dir.exists():
            shutil.rmtree(export_dir)


def test_tui_action_view_diff() -> None:
    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            await pilot.press("d")
            # Modal screen should be pushed
            assert isinstance(app.screen, ArtifactModal)
            assert "DIFF" in app.screen.modal_title
            assert "diff --git" in app.screen.modal_content

            # Dismiss modal
            await pilot.press("escape")
            assert not isinstance(app.screen, ArtifactModal)

    asyncio.run(run())


def test_tui_action_view_logs() -> None:
    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            await pilot.press("l")
            # Modal screen should be pushed
            assert isinstance(app.screen, ArtifactModal)
            assert "TEST LOGS" in app.screen.modal_title
            assert "test session starts" in app.screen.modal_content

            # Dismiss modal
            await pilot.press("escape")
            assert not isinstance(app.screen, ArtifactModal)

    asyncio.run(run())
