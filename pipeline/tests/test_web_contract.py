"""The web app's runtime guard must stay in step with the pydantic contract.

`web/src/lib/factsheet.ts` validates a payload before the page renders it, because
`payload` is jsonb and the declared TypeScript type is an assertion the compiler cannot
check. The guard names the sections it requires and the schema version it renders.

Those names are a hand-maintained copy of what `factsheet/schema.py` declares, in another
language, and a copy is the thing that drifts. Rename a section in the pydantic model and
the TypeScript guard keeps checking a key that no longer exists - passing every payload,
guarding nothing, and saying nothing about it.

So this test reads the guard as text and checks it against the generated JSON Schema. It
lives in the Python suite rather than in `web/` deliberately: the contract is owned here,
`web/` has no test runner, and a drift test belongs on the side that defines the thing.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "web" / "src" / "lib" / "factsheet.ts"
CONTRACT = REPO / "contracts" / "factsheet.v1.schema.json"

# Required by the contract but not asserted by the guard, with the reason. Narrow on
# purpose: anything not listed here must be checked, so adding a section to the contract
# forces a decision rather than passing silently.
NOT_GUARDED = {
    # Timestamps the page prints but does not branch on; a missing one renders as empty
    # text rather than a broken section.
    "generated_at",
    "as_of_date",
}


@pytest.fixture(scope="module")
def guard_source() -> str:
    assert GUARD.exists(), f"the web guard is missing: {GUARD}"
    return GUARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contract() -> dict:
    assert CONTRACT.exists(), "run: qe factsheet export-schema"
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _listed(source: str, const_name: str) -> set[str]:
    """The string literals of a `const X = [...] as const` declaration."""
    match = re.search(rf"const {const_name} = \[(.*?)\] as const", source, re.S)
    assert match, f"{const_name} not found in the guard - was it renamed?"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


class TestTheGuardMatchesTheContract:
    def test_the_schema_version_is_the_one_the_contract_declares(
        self, guard_source: str, contract: dict
    ) -> None:
        """If these disagree the guard rejects every payload, and the site goes dark on the
        next build - loudly, which is the intended direction of failure, but still worth
        catching here first."""
        expected = contract["properties"]["schema_version"]["const"]
        match = re.search(r'SUPPORTED_SCHEMA_VERSION = "([^"]+)"', guard_source)
        assert match, "SUPPORTED_SCHEMA_VERSION not found in the guard"
        assert match.group(1) == expected

    def test_every_required_section_is_guarded(self, guard_source: str, contract: dict) -> None:
        guarded = _listed(guard_source, "REQUIRED_ARRAYS") | _listed(
            guard_source, "REQUIRED_OBJECTS"
        )
        required = set(contract["required"]) - NOT_GUARDED

        missing = sorted(required - guarded)
        assert not missing, (
            f"required by the contract but not checked by the web guard: {missing}. "
            "Add them to REQUIRED_ARRAYS or REQUIRED_OBJECTS in web/src/lib/factsheet.ts, "
            "or to NOT_GUARDED here with a reason."
        )

    def test_the_guard_does_not_check_sections_that_no_longer_exist(
        self, guard_source: str, contract: dict
    ) -> None:
        """The quieter half of drift. A guard checking a renamed key passes everything and
        reports nothing wrong."""
        guarded = _listed(guard_source, "REQUIRED_ARRAYS") | _listed(
            guard_source, "REQUIRED_OBJECTS"
        )
        known = set(contract["properties"])

        stale = sorted(guarded - known)
        assert not stale, (
            f"the web guard requires sections the contract does not define: {stale}. "
            "Every payload would fail, or the check is dead."
        )

    def test_arrays_are_guarded_as_arrays_and_objects_as_objects(
        self, guard_source: str, contract: dict
    ) -> None:
        """A section checked with the wrong kind either rejects valid payloads or accepts
        malformed ones."""
        properties = contract["properties"]

        for key in _listed(guard_source, "REQUIRED_ARRAYS"):
            schema = properties.get(key, {})
            kind = _kind_of(schema, contract)
            assert kind == "array", f'"{key}" is guarded as an array but the contract says {kind}'

        for key in _listed(guard_source, "REQUIRED_OBJECTS"):
            schema = properties.get(key, {})
            kind = _kind_of(schema, contract)
            assert kind == "object", f'"{key}" is guarded as an object but the contract says {kind}'


def _kind_of(schema: dict, contract: dict) -> str:
    """Resolve a property's JSON type, following one `$ref` hop.

    json-schema-to-typescript flattens these, so the guard sees a concrete shape while the
    contract still expresses it as a reference.
    """
    if "type" in schema:
        return str(schema["type"])
    ref = schema.get("$ref") or schema.get("allOf", [{}])[0].get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        target = contract.get("$defs", {}).get(name, {})
        return str(target.get("type", "object"))
    return "unknown"


class TestTheGuardIsActuallyWiredIn:
    def test_both_queries_validate_their_payload(self) -> None:
        """A guard nothing calls is worse than no guard: it reads as protection."""
        queries = (REPO / "web" / "src" / "lib" / "queries" / "published.ts").read_text(
            encoding="utf-8"
        )
        assert "assertFactSheet" in queries, "the published queries do not call the guard"
        # Both the list page and the ticker page read payloads.
        assert queries.count("assertFactSheet(") >= 2, (
            "only one query validates its payload; both listPublished and getPublished "
            "hand a payload to the page"
        )
