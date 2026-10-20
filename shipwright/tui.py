from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from .models import Run, RunState, fixture_runs


class RunListItem(ListItem):
    def __init__(self, run: Run) -> None:
        super().__init__(Label(f"{run.number}  {run.title} · {run.state} · {run.elapsed}"))
        self.run = run


class ShipwrightApp(App[None]):
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
    ]

    def __init__(self) -> None:
        super().__init__()
        self.runs = list(fixture_runs())

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
        self.show_run(self.runs[0])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, RunListItem):
            self.show_run(event.item.run)

    def show_run(self, run: Run) -> None:
        self.query_one("#run-title", Static).update(f"#{run.number}  {run.title}")
        self.query_one("#state", Static).update(f"STATE\n{run.state}")
        self.query_one("#agents", Static).update(f"AGENTS\n{run.agents}")
        self.query_one("#tests", Static).update(f"TESTS\n{run.tests}")
        timeline = "\n".join(f"{event.timestamp}  {event.actor:<13} {event.message}" for event in run.events)
        self.query_one("#timeline", Static).update(f"TIMELINE\n{timeline or 'No events yet.'}")
        artifacts = "\n".join(f"• {artifact}" for artifact in run.artifacts)
        self.query_one("#artifacts", Static).update(f"ARTIFACTS\n{artifacts or 'No artifacts yet.'}")

    def action_refresh(self) -> None:
        self.notify("Fixture data refreshed")


def main() -> None:
    ShipwrightApp().run()


if __name__ == "__main__":
    main()
