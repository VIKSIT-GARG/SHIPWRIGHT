import asyncio

from shipwright.tui import ShipwrightApp


def test_tui_renders_and_selects_runs() -> None:
    def text(widget: object) -> str:
        value = getattr(widget, "renderable", None)
        if value is None:
            value = getattr(widget, "content", None)
        if value is None:
            value = getattr(widget, "_content")
        return str(value)

    async def run() -> None:
        app = ShipwrightApp()
        async with app.run_test() as pilot:
            assert text(app.query_one("#run-title")) == "#1842  Fix flaky retry test"
            await pilot.press("down")
            assert text(app.query_one("#run-title")) == "#1839  Handle null config"

    asyncio.run(run())
