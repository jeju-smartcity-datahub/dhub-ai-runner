"""Runner state in SQLite (one host) and Postgres (shared by pods)."""

import threading
import time

import pytest
from commons.state import PostgresState, State


@pytest.fixture(params=["sqlite", "postgres"])
def state(request, tmp_path):
    if request.param == "sqlite":
        return State(tmp_path)
    url = request.getfixturevalue("postgres_url")
    PostgresState(url, "other").save_session("a", "s", {"other": True})
    return PostgresState(url, f"c{time.monotonic_ns()}")


def test_state_round_trip(state):
    state.set_checkpoint("p", "s1", state="ready", offsets={"in": 3})
    assert state.get_checkpoint("p", "s1")["offsets"] == {"in": 3}
    assert [c["step"] for c in state.list_checkpoints("p")] == ["s1"]
    state.reset_checkpoints("p")
    assert state.get_checkpoint("p", "s1") == {"state": "new", "offsets": {}}

    state.queue_run("r1", "p")
    state.start_run("r1", "p")
    assert state.get_run("r1")["state"] == "running"
    state.end_run("r1", "error", "boom")
    assert state.list_runs("p")[0] == {**state.get_run("r1"), "state": "error", "error": "boom"}
    state.queue_run("r2", "p")
    assert state.interrupt_runs() == 1 and state.get_run("r2")["state"] == "interrupted"

    state.save_session("a", "s", {"messages": [1]})
    assert state.get_session("a", "s") == {"messages": [1]}
    assert state.delete_session("a", "s") and state.get_session("a", "s") is None


def test_sessions_and_stopped(state):
    hello = {"type": "human", "data": {"content": "hello"}}
    state.save_session("a", "s1", {"messages": [hello, {"type": "ai", "data": {}}]})
    assert state.list_sessions("a") == [
        {**state.list_sessions("a")[0], "session_id": "s1", "messages": 2, "title": "hello"}
    ]
    assert state.list_sessions("b") == []

    state.set_stopped("agent", "a", True)
    state.set_stopped("agent", "a", True)  # again: no error
    state.set_stopped("pipeline", "p", True)
    assert list(state.stopped("agent")) == ["a"] and list(state.stopped("pipeline")) == ["p"]
    state.set_stopped("agent", "a", False)
    assert state.stopped("agent") == {}


def test_dataset_lock_is_exclusive(state):
    order = []

    def hold():
        with state.dataset_lock("d"):
            order.append("b")

    with state.dataset_lock("d"):
        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(0.3)
        order.append("a")
    thread.join(5)
    assert order == ["a", "b"]
