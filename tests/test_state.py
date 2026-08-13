"""State durability: atomic writes, corruption recovery, restart survival."""

from __future__ import annotations

import json
from pathlib import Path

from core.state import StateStore, atomic_write_json, write_heartbeat


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_atomic_write_overwrites_in_place(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}


def test_state_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path=path)
    store.record_attempt("heartbeat")
    store.record_failure("heartbeat", "boom")
    store.bump("alerts_sent", 3)
    store.save()

    reloaded = StateStore.load(path)
    assert reloaded.task("heartbeat").consecutive_failures == 1
    assert reloaded.task("heartbeat").last_error == "boom"
    assert reloaded.counters["alerts_sent"] == 3
    assert reloaded.recovered_from_corruption is False


def test_corrupt_state_recovers_and_quarantines(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")

    store = StateStore.load(path)

    assert store.recovered_from_corruption is True
    assert store.tasks == {}
    quarantined = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert len(quarantined) == 1, "corrupt file must be preserved, not deleted"


def test_structurally_wrong_state_recovers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    # Valid JSON, wrong shape — tasks must be a mapping of task payloads.
    path.write_text(json.dumps({"tasks": ["not", "a", "mapping"]}), encoding="utf-8")

    store = StateStore.load(path)

    assert store.recovered_from_corruption is True
    assert store.tasks == {}


def test_json_array_state_recovers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert StateStore.load(path).recovered_from_corruption is True


def test_success_clears_failure_streak(tmp_path: Path) -> None:
    store = StateStore(path=tmp_path / "s.json")
    store.record_failure("vision", "a")
    store.record_failure("vision", "b")
    assert store.task("vision").consecutive_failures == 2
    store.record_success("vision")
    assert store.task("vision").consecutive_failures == 0
    assert store.task("vision").healthy is True


def test_error_message_is_bounded(tmp_path: Path) -> None:
    store = StateStore(path=tmp_path / "s.json")
    store.record_failure("crawl", "x" * 5000)
    assert len(store.task("crawl").last_error) == 500


def test_heartbeat_file_written(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 1234})
    payload = json.loads(path.read_text())
    assert payload["pid"] == 1234
    assert "written_at" in payload
