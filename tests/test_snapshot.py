"""Snapshot storage: immutability, atomic swaps, and JSON round-tripping."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

import pytest

from cru_flags import Client
from cru_flags._client import _freeze, _freeze_document, _thaw, _thaw_document

from .conftest import FlagService

POLL = 0.02
WAIT = 5.0

ClientFactory = Callable[..., Client]


def test_freeze_makes_the_whole_tree_immutable() -> None:
    frozen = _freeze_document(
        {"Flags": {"a": {"Enabled": True}}, "Regions": ["us-east-1", ["nested"]]},
    )

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["Flags"], MappingProxyType)
    assert isinstance(frozen["Flags"]["a"], MappingProxyType)
    assert frozen["Regions"] == ("us-east-1", ("nested",))

    with pytest.raises(TypeError):
        frozen["Flags"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["Flags"]["a"]["Enabled"] = False  # type: ignore[index]


def test_freeze_leaves_scalars_alone() -> None:
    for value in (True, False, None, 0, 1.5, "text"):
        assert _freeze(value) is value


def test_thaw_undoes_freeze() -> None:
    document = {"Flags": {"a": {"Enabled": True}}, "Regions": ["us-east-1"]}
    assert _thaw_document(_freeze_document(document)) == document
    assert _thaw(_freeze(["a", {"b": 1}])) == ["a", {"b": 1}]


def test_the_stored_snapshot_is_frozen(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True

    stored = client._snapshot
    assert isinstance(stored, MappingProxyType)
    with pytest.raises(TypeError):
        stored["Flags"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        stored["Flags"]["pilot_banner"]["Enabled"] = False


def test_snapshot_json_round_trips_the_served_document(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True

    snapshot = client.snapshot()
    assert snapshot == document
    assert json.loads(json.dumps(snapshot)) == document


def test_snapshot_is_a_detached_copy(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True

    snapshot = client.snapshot()
    snapshot["Flags"].clear()
    snapshot["Version"] = 999

    assert client.enabled("pilot_banner") is True
    assert client.snapshot()["Version"] == 3


def test_snapshot_is_empty_before_the_first_success(
    make_client: ClientFactory,
) -> None:
    assert make_client().snapshot() == {}


def test_enabled_is_safe_while_snapshots_are_swapped(
    make_client: ClientFactory,
) -> None:
    client = make_client()
    first = _freeze_document(
        {"Version": 1, "Flags": {"pilot_banner": {"Enabled": True}}},
    )
    second = _freeze_document(
        {
            "Version": 2,
            "Flags": {
                "pilot_banner": {"Enabled": True, "UpdatedBy": "someone"},
                "another": {"Enabled": False},
            },
        },
    )

    stop = threading.Event()
    failures: list[BaseException] = []
    observations: list[bool] = []

    def writer() -> None:
        while not stop.is_set():
            client._publish(first, etag='"1"')
            client._publish(second, etag='"2"')

    def reader() -> None:
        try:
            while not stop.is_set():
                observations.append(client.enabled("pilot_banner"))
        except BaseException as error:  # noqa: BLE001 - the point is to record it
            failures.append(error)

    threads = [threading.Thread(target=writer, name="writer")]
    threads += [threading.Thread(target=reader, name=f"reader-{i}") for i in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(0.25)
    stop.set()
    for thread in threads:
        thread.join(WAIT)

    assert failures == []
    assert len(observations) > 1000, len(observations)
    # pilot_banner is True in both documents, so no reader may ever see a
    # half-applied swap.
    assert all(observations)
