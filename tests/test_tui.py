import asyncio

from shipwright.tui import ShipwrightApp


def test_tui_renders_and_selects_runs() -> None:
    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            assert str(app.query_one("#run-title").renderable) == "#1842  Fix flaky retry test"
            await pilot.press("down")
            assert str(app.query_one("#run-title").renderable) == "#1839  Handle null config"

    asyncio.run(run())
