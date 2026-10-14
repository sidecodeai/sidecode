"""Helpers shared by the test modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"
SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "protocol" / "schema"


def load_fixture(relative: str) -> Any:
    path = FIXTURE_ROOT / relative
    if not path.exists():
        pytest.skip(f"protocol fixture {relative} is not present yet")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_schema(name: str) -> Any:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def object_validator(name: str) -> Any:
    """A jsonschema validator for one `$defs` entry of `objects.json`, or None."""
    path = SCHEMA_ROOT / "objects.json"
    if not path.exists():
        return None
    import jsonschema

    schema = _load_schema("objects.json")
    return jsonschema.Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"})


def device_frame_validator(name: str) -> Any:
    """A jsonschema validator for one `$defs` entry of `device_frames.json`, or None."""
    path = SCHEMA_ROOT / "device_frames.json"
    if not path.exists():
        return None
    import jsonschema
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012 as DRAFT

    def retrieve(uri: str) -> Any:
        return Resource.from_contents(
            _load_schema(uri.rsplit("/", 1)[-1]), default_specification=DRAFT
        )

    schema = _load_schema("device_frames.json")
    registry = Registry(retrieve=retrieve)  # type: ignore[call-arg]
    return jsonschema.Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}, registry=registry)


def event_validator() -> Any:
    """A jsonschema validator for `Event`, or None when the schema is missing."""
    path = SCHEMA_ROOT / "events.json"
    if not path.exists():
        return None
    import jsonschema
    from referencing import Registry, Resource

    def retrieve(uri: str) -> Any:
        name = uri.rsplit("/", 1)[-1]
        return Resource.from_contents(_load_schema(name), default_specification=DRAFT)

    from referencing.jsonschema import DRAFT202012 as DRAFT

    schema = _load_schema("events.json")
    registry = Registry(retrieve=retrieve)  # type: ignore[call-arg]
    return jsonschema.Draft202012Validator(schema, registry=registry)
