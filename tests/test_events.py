import json
from pathlib import Path

from qbx.events import EventBus


def test_event_bus_retains_a_larger_recent_history_window():
    bus = EventBus()
    for i in range(201):
        bus.emit("tick", f"event {i}", index=i)

    history = bus.history
    assert len(history) == 200
    assert history[0]["index"] == 1
    assert history[-1]["index"] == 200


def test_event_bus_history_since_filters_old_events():
    bus = EventBus()
    bus.emit("tick", "first")
    bus.emit("tick", "second")
    assert [event["id"] for event in bus.history_since(0)] == [1, 2]
    assert [event["id"] for event in bus.history_since(1)] == [2]
    assert bus.last_event_id == 2


def test_event_bus_snapshot_and_subscribe_returns_atomic_history():
    bus = EventBus()
    bus.emit("tick", "first")
    bus.emit("tick", "second")

    history, queue = bus.snapshot_and_subscribe(1)
    bus.emit("tick", "third")

    assert [event["id"] for event in history] == [2]
    assert queue.get_nowait()["id"] == 3


def test_event_bus_sse_format_includes_event_id():
    bus = EventBus()
    bus.emit("tick", "first")

    payload = bus.sse_format(bus.history[0])

    assert payload.startswith("id: 1\n")
    assert "\ndata: {" in payload


def test_event_bus_persists_ids_across_restart(tmp_path):
    state_path = Path(tmp_path) / "events.json"
    first = EventBus(state_path=state_path)
    first.emit("tick", "first")
    first.emit("tick", "second")
    # Ids are batched to disk; flush (or 25 emits) before restart.
    first.flush()

    second = EventBus(state_path=state_path)
    assert second.last_event_id == 2
    second.emit("tick", "third")

    assert second.history[-1]["id"] == 3


def test_event_bus_batches_disk_persists(tmp_path):
    state_path = Path(tmp_path) / "events.json"
    bus = EventBus(state_path=state_path)
    for i in range(24):
        bus.emit("tick", f"e{i}")
    # Under the batch threshold — counter not on disk yet.
    assert not state_path.exists() or json.loads(state_path.read_text()).get("next_id", 1) <= 1
    bus.emit("tick", "e24")  # 25th → persist
    assert json.loads(state_path.read_text())["next_id"] == 26
