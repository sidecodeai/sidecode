"""A62: every board update writes one log line, with ids and counts and never anything written."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.board.llm import BoardModel
from rc_gateway.board.policy import BoardRefusal

from .fakes import FakeModel, Rig, answer, make_rig, person, reply, settle, until

SECRET_ASK = "SECRET-REQUEST rotate the prod database password"
SECRET_REPLY = "SECRET-REPLY the password is hunter2"
SECRET_ITEM = "SECRET-ITEM rotate the password"

FIELDS = {
    "session_id",
    "device_id",
    "username",
    "trigger",
    "watched",
    "outcome",
    "events",
    "rounds_new",
    "rounds_context",
    "window_chars",
    "items_before",
    "items_after",
    "ms",
}


def runs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        dict(record.fields)  # type: ignore[attr-defined]
        for record in caplog.records
        if record.getMessage() == "board run"
    ]


def everything(caplog: pytest.LogCaptureFixture) -> str:
    return json.dumps(
        [[record.getMessage(), getattr(record, "fields", {})] for record in caplog.records],
        default=str,
    )


async def opened_behind(rig: Rig) -> None:
    rig.host.history["ad2c9abb-4a1e-470a-835c-228778fc17f0"] = [
        person(1, SECRET_ASK),
        reply(2, SECRET_REPLY),
    ]
    rig.sessions.put(last_seq=2)
    await rig.get(rig.app())


async def test_an_update_is_one_line_of_ids_and_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig = await make_rig(tmp_path)
    rig.model.script = [answer((None, "todo", SECRET_ITEM))]
    await opened_behind(rig)
    await until(lambda: bool(runs(caplog)))
    await settle()
    (line,) = runs(caplog)
    assert set(line) == FIELDS | {"input_tokens", "output_tokens"}
    assert line["trigger"] == "open" and line["watched"] is True
    assert line["outcome"] == "updated"
    assert (line["events"], line["rounds_new"], line["rounds_context"]) == (2, 1, 0)
    assert (line["items_before"], line["items_after"]) == (0, 1)
    assert (line["input_tokens"], line["output_tokens"]) == (1200, 40)
    assert line["window_chars"] > 0 and line["username"] == "ada"
    assert "SECRET" not in everything(caplog)


async def test_a_failed_update_is_one_line_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig = await make_rig(tmp_path)
    rig.model.script = ["SECRET-ANSWER that is no board"]
    await opened_behind(rig)
    await until(lambda: bool(runs(caplog)))
    await settle()
    (line,) = runs(caplog)
    assert line["outcome"] == "invalid_output"
    # The model answered, so what it reported of its usage is there.
    assert set(line) == FIELDS | {"input_tokens", "output_tokens"}
    assert "SECRET" not in everything(caplog)


class Refusing:
    async def quick(self, username: str) -> str | None:
        return None

    async def admit(self, username: str) -> BoardModel:
        raise BoardRefusal("subscription_required")


async def test_a_refused_update_names_the_reason_and_reads_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    def refusing(model: FakeModel) -> Refusing:
        return Refusing()

    rig = await make_rig(tmp_path, policy=refusing)
    await opened_behind(rig)
    await until(lambda: bool(runs(caplog)))
    await settle()
    (line,) = runs(caplog)
    assert line["outcome"] == "refused:subscription_required"
    assert line["events"] == 0 and rig.host.asks == []
