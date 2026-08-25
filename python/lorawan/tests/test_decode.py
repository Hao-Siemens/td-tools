"""End-to-end decode tests driven by the example test-vector files.

Each ``examples/*.vectors.json`` file pairs a Thing Description with known
payloads and their expected decoded values. These tests convert the TD, decode
every payload through the MultiTech reference interpreter, and assert the result
matches -- exercising the whole binding pipeline.
"""

from __future__ import annotations

import jsonschema
import pytest

from lorawan_wot import vocab
from lorawan_wot.decode import decode_uplink

from .conftest import EXAMPLES_DIR, load_json, vector_files


def _iter_cases():
    """Yield ``(id, td, payload, fport, expected)`` for every test vector."""
    for vectors_path in vector_files():
        spec = load_json(vectors_path)
        td = load_json(EXAMPLES_DIR / spec["td"])
        for vector in spec["vectors"]:
            case_id = f"{vectors_path.stem}-{vector['name']}"
            yield pytest.param(
                td, vector["payload"], vector.get("fport"), vector["expected"], id=case_id
            )


@pytest.mark.parametrize(("td", "payload", "fport", "expected"), list(_iter_cases()))
def test_decode_matches_expected(td, payload, fport, expected):
    data = decode_uplink(td, payload, fport=fport)
    for key, want in expected.items():
        assert key in data, f"missing field {key!r} in {data}"
        if isinstance(want, float):
            assert data[key] == pytest.approx(want), f"{key}: {data[key]} != {want}"
        else:
            assert data[key] == want, f"{key}: {data[key]} != {want}"


def test_decoded_values_satisfy_td_types():
    """Decoded numbers must be consistent with each event's declared data type."""
    spec = load_json(EXAMPLES_DIR / "dragino-lht65n.vectors.json")
    td = load_json(EXAMPLES_DIR / spec["td"])
    first = spec["vectors"][0]
    data = decode_uplink(td, first["payload"], fport=first.get("fport"))
    for name, value in data.items():
        declared = td[vocab.EVENTS][name][vocab.DATA]["type"]
        if declared == "integer":
            assert isinstance(value, int)
        elif declared == "number":
            assert isinstance(value, (int, float))


@pytest.mark.parametrize(("td", "payload", "fport", "expected"), list(_iter_cases()))
def test_decoded_value_validates_against_its_data_schema(td, payload, fport, expected):
    """Every decoded value must validate against its own event's ``data`` schema.

    A Thing Description promises that an event's ``data`` describes what the
    consumer will receive, so the decoder's output is the one instance that schema
    must accept. Nothing checked that until 0.3.0, and the gap was not theoretical:
    categorical readings were emitted as ``{"type": "string", "oneOf": [{"const":
    0, "title": "dry"}]}``, which no instance can satisfy -- it rejects the decoded
    ``"dry"`` for not matching ``const: 0`` and the raw ``0`` for not being a
    string. All 21 such schemas in the examples were unsatisfiable, and every
    decode test still passed, because they only ever compared decoded values to
    the vectors and never to the TD.
    """
    decoded = decode_uplink(td, payload, fport=fport)
    for name in expected:
        schema = td[vocab.EVENTS][name][vocab.DATA]
        jsonschema.validate(instance=decoded[name], schema=schema)
