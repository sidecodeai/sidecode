# protocol/

The wire contract shared by the gateway, the device daemon, the web app and the SideCode native
apps, which are distributed separately and speak the same protocol.

| Path | What it holds |
| --- | --- |
| `PROTOCOL.md` | The normative specification: transport, HTTP API, objects, events, frames, UI semantics, and a conformance checklist per role |
| `schema/` | JSON Schema draft 2020-12 for every object, event, frame and HTTP body |
| `fixtures/` | A valid example of every frame type, event kind and HTTP body, plus bare shared objects and two full session timelines |
| `fixtures_invalid/` | Frames that must be rejected; they self-test the validator |
| `scripts/validate_fixtures.py` | Validates the fixtures, enforces the rules JSON Schema cannot express, and reports coverage |

## Changing the contract

**Only a deliberate protocol change touches this directory.** A component that finds the contract
ambiguous, contradictory or impossible reports the problem with proposed wording; it does not invent
a field, rename one, or work around it locally. Every change lands as `PROTOCOL.md` plus the matching
schema, fixtures and validator inventory in the same commit.

## Validating

```
cd protocol
uv run --with jsonschema python scripts/validate_fixtures.py
```

Python 3.12. The command prints a `PASS` / `FAIL` line per file and exits non-zero on any problem.
It checks three things:

1. **Schema conformance** — every file under `fixtures/` against the schema its directory and name
   select.
2. **Rules JSON Schema cannot express** — strictly increasing `seq` inside a timeline, the
   `session.history` rules of PROTOCOL.md section 6.4, the 64 KiB event bound and the 8 / 16 / 32 KiB
   tool payload bounds, and UUID v4 identifiers.
3. **Coverage and self-test** — every frame type, event kind, tool kind and HTTP body named in the
   protocol has both a schema entry and a fixture, and every file under `fixtures_invalid/` is
   rejected.

The inventory the coverage check works from lives at the top of the script. Adding a frame type or
event kind to the protocol means adding it there too, or the check fails.

## Schema layout

| File | Contents |
| --- | --- |
| `objects.json` | `Device`, `AgentInfo`, `Session`, `Usage`, `Git`, `Attachment`, `AttachmentUpload`, `LabeledId`, `Error`, and the scalar types |
| `events.json` | `Event` as a `oneOf` discriminated by `kind`, one definition per kind |
| `app_frames.json` | Every frame on `WS /ws/app`, plus the typed `result` shape per request |
| `device_frames.json` | Every frame on `WS /ws/device`, including forwarded requests |
| `http.json` | Request and response bodies for `/api/*`, and the push payload |
| `stt_frames.json` | The text frames on `WS /ws/stt` |
| `timeline.json` | The test-harness shape of a `fixtures/timelines/*.json` file. Not a wire format. |

Files compose through `$id` and relative `$ref`, so a loader must register all of them together
rather than resolving one file at a time. Every object leaves `additionalProperties` open on purpose:
PROTOCOL.md requires unknown fields to be ignored, and a strict validator would break on the next
revision.

## Using the fixtures in component tests

Every component's test suite should decode every fixture. That is the cheapest way to catch a
diverged model before integration.

### Python (gateway, client)

```python
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA = Path(__file__).parents[2] / "protocol" / "schema"
REGISTRY = Registry().with_resources(
    (json.loads(p.read_text())["$id"], Resource.from_contents(json.loads(p.read_text()), DRAFT202012))
    for p in SCHEMA.glob("*.json")
)


def validator(schema_file: str, definition: str | None = None) -> Draft202012Validator:
    uri = f"https://remote-control.dev/schema/v1/{schema_file}"
    if definition:
        uri = f"{uri}#/$defs/{definition}"
    return Draft202012Validator({"$ref": uri}, registry=REGISTRY)
```

Validate what your own code emits, not only what the fixtures contain: build a `Session`, dump it,
and run it through `objects.json#/$defs/Session`.

### TypeScript (web)

Either decode the fixtures into your own types and assert on the fields you rely on, or validate with
ajv. ajv needs the draft 2020-12 entry point and all schemas registered together:

```ts
import Ajv2020 from "ajv/dist/2020"
import objects from "../../protocol/schema/objects.json"
import events from "../../protocol/schema/events.json"
import appFrames from "../../protocol/schema/app_frames.json"

const ajv = new Ajv2020({ strict: false, allErrors: true })
ajv.addSchema([objects, events, appFrames])
const validate = ajv.getSchema("https://remote-control.dev/schema/v1/app_frames.json")!
```

Then iterate `protocol/fixtures/app/*.json` and `protocol/fixtures/events/*.json` with
`import.meta.glob` and assert `validate(fixture)` for each. The two timelines are the useful
end-to-end case: feed `frames` into the timeline reducer and compare the result with
`session_final`.

### Any other language (native clients)

Decode, do not validate. A client that has no JSON Schema validator should round-trip every
fixture through its own decoder into its model types, and fail loudly on any file it cannot
decode:

```text
for each file under protocol/fixtures/**/*.json:
    pick the model by directory: app frame, device frame, event, timeline, …
    decode the file into that model, and fail the run on any error
```

Decoding must tolerate unknown fields and unknown enum values. Model unknown `kind` and unknown
`agent` values as a passthrough case rather than throwing, or the app will break on the first
protocol addition.

## Fixture map

| Directory | Schema it validates against |
| --- | --- |
| `fixtures/app/` | `app_frames.json` root, and the typed `Reply*` definitions for `reply.*.json` |
| `fixtures/device/` | `device_frames.json` root |
| `fixtures/device/forwarded/` | `device_frames.json#/$defs/ForwardedRequest` |
| `fixtures/events/` | `events.json` root |
| `fixtures/objects/` | `objects.json`, one definition per file, mapped in the validator |
| `fixtures/http/` | `http.json`, one definition per file, mapped in the validator |
| `fixtures/stt/` | `stt_frames.json` root |
| `fixtures/timelines/` | `timeline.json` |
| `fixtures/history/page.json` | `app_frames.json#/$defs/ReplySessionHistory` |
| `fixtures/replay/subscribe.reply.json` | `app_frames.json#/$defs/ReplySessionSubscribe` |

Fixture identifiers are UUID v4, timestamps are September 2026, and `seq` increases strictly within a
timeline. Model ids, versions and paths in the fixtures are illustrative; nothing should hard-code
them.
