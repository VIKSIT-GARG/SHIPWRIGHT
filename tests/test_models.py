from shipwright.models import RunState, fixture_runs


def test_fixture_runs_have_stable_demo_data() -> None:
    runs = list(fixture_runs())

    assert [run.number for run in runs] == [1842, 1839, 1828]
    assert runs[0].state is RunState.VERIFYING
    assert runs[0].tests == "42 passed"
    assert len(runs[0].events) == 4
