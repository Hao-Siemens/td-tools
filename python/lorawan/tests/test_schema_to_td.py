"""Tests for the MultiTech payload schema -> Thing Description generator."""

from __future__ import annotations

import jsonschema
import pytest

from lorawan_wot import vocab
from lorawan_wot.converter import td_to_payload_schema
from lorawan_wot.schema_to_td import (
    SkipReason,
    UnsupportedSchemaError,
    payload_schema_to_td,
)


def test_fixed_schema_assigns_sequential_byte_offsets():
    """Plain sequential fields get cumulative byte offsets and the right types."""
    schema = {
        "name": "demo_fixed",
        "endian": "big",
        "fields": [
            {"name": "count", "type": "u16"},
            {"name": "battery", "type": "u8", "div": 10, "unit": "V"},
            {"name": "temperature", "type": "s16", "div": 100, "unit": "Cel"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    assert td[vocab.PAYLOAD_LAYOUT] == vocab.LAYOUT_FIXED
    events = td[vocab.EVENTS]
    offsets = {name: ev[vocab.FORMS][0][vocab.BYTE_OFFSET] for name, ev in events.items()}
    assert offsets == {"count": 0, "battery": 2, "temperature": 3}
    assert events["battery"][vocab.DATA]["type"] == "number"  # div -> number
    assert events["count"][vocab.DATA]["type"] == "integer"


def test_every_event_form_subscribes_to_the_uplink():
    """Uplinks are pushed, so each form offers only the subscribe operations."""
    schema = {"endian": "big", "fields": [{"name": "count", "type": "u16"}]}
    form = payload_schema_to_td(schema, source="demo.yaml")[vocab.EVENTS]["count"][vocab.FORMS][0]
    assert form["href"] == vocab.UPLINK_HREF
    assert form["op"] == list(vocab.UPLINK_OPS)


def test_fixed_skip_advances_offset_without_an_event():
    """A ``skip`` field advances the cursor but creates no event."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "_hdr", "type": "skip", "length": 2},
            {"name": "value", "type": "u8"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    assert "_hdr" not in td[vocab.EVENTS]
    assert td[vocab.EVENTS]["value"][vocab.FORMS][0][vocab.BYTE_OFFSET] == 2


def test_schema_endian_is_recorded_on_each_form():
    """Byte order is a form-level term, so every value states its own."""
    schema = {
        "endian": "little",
        "fields": [
            {"name": "a", "type": "u16"},
            {"name": "b", "type": "u16"},
            {"name": "c", "type": "be_u16"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    assert vocab.ENDIAN not in td  # never a Thing-level term
    events = td[vocab.EVENTS]
    assert events["a"][vocab.FORMS][0][vocab.ENDIAN] == vocab.ENDIAN_LITTLE
    assert events["c"][vocab.FORMS][0][vocab.ENDIAN] == vocab.ENDIAN_BIG
    # Converting back rebuilds the schema-wide default and the odd-one-out prefix.
    round_tripped = td_to_payload_schema(td)
    assert round_tripped["endian"] == "little"
    assert [f["type"] for f in round_tripped["fields"]] == ["u16", "u16", "be_u16"]


def test_unit_round_trips_through_the_event_data_schema():
    """``unit`` describes the decoded value, so it belongs on ``data``."""
    schema = {
        "endian": "big",
        "fields": [{"name": "temperature", "type": "s16", "div": 10, "unit": "Cel"}],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    assert td[vocab.EVENTS]["temperature"][vocab.DATA]["unit"] == "Cel"
    assert td_to_payload_schema(td)["fields"][0]["unit"] == "Cel"


def test_valid_range_round_trips_as_minimum_and_maximum():
    """A plausibility range is stated with TD core's own data-schema keywords."""
    schema = {
        "endian": "big",
        "fields": [{"name": "temperature", "type": "s16", "valid_range": [-40, 85]}],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    data = td[vocab.EVENTS]["temperature"][vocab.DATA]
    assert (data["minimum"], data["maximum"]) == (-40, 85)
    assert td_to_payload_schema(td)["fields"][0]["valid_range"] == [-40, 85]


def test_tlv_schema_carries_tag_fields_and_tags():
    """A single-field tlv block becomes per-event tags plus tag fields."""
    schema = {
        "endian": "little",
        "fields": [
            {
                "tlv": {
                    "tag_fields": [
                        {"name": "channel_id", "type": "u8"},
                        {"name": "channel_type", "type": "u8"},
                    ],
                    "tag_key": ["channel_id", "channel_type"],
                    "cases": {
                        "[3, 103]": [{"name": "temperature", "type": "s16", "div": 10}],
                    },
                }
            }
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    assert td[vocab.PAYLOAD_LAYOUT] == vocab.LAYOUT_TLV
    assert td[vocab.TAG_FIELDS][0]["name"] == "channel_id"
    assert td[vocab.EVENTS]["temperature"][vocab.FORMS][0][vocab.TAG] == [3, 103]
    # And it round-trips through the forward converter.
    assert td_to_payload_schema(td)["fields"][0]["tlv"]["cases"]["[3, 103]"][0]["name"] == (
        "temperature"
    )


def test_lookup_enum_round_trips_to_strings():
    """A ``lookup`` table splits into a data-schema ``enum`` and a form value map."""
    schema = {
        "endian": "big",
        "fields": [{"name": "hemi", "type": "u8", "lookup": {0: "N", 1: "S"}}],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    event = td[vocab.EVENTS]["hemi"]
    data = event[vocab.DATA]
    assert data["type"] == "string"
    assert data["enum"] == ["N", "S"]
    # The wire integers stay on the form, so the data schema accepts the value
    # the decoder will actually hand the consumer.
    jsonschema.validate(instance="N", schema=data)
    assert event[vocab.FORMS][0][vocab.VALUE_MAP] == [
        {"wireValue": 0, "value": "N"},
        {"wireValue": 1, "value": "S"},
    ]
    assert td_to_payload_schema(td)["fields"][0]["lookup"] == {0: "N", 1: "S"}


def test_lookup_table_with_a_default_label_is_skipped_not_crashed():
    """A non-integer table key must skip its device, not abort the whole run.

    A table may carry a ``default`` label covering every value it does not list
    (PS-269). ``lorav:valueMap`` pairs one wire value with one decoded value and
    cannot say "anything else", so the field is outside the subset.

    The reason it is a test rather than a note: the key was converted with a bare
    ``int()``, so the first schema in the library to use ``default`` raised an
    uncaught ValueError out of the batch script and no catalog was generated at
    all. A skip loses one device; a crash loses all of them.
    """
    schema = {
        "endian": "big",
        "fields": [{"name": "state", "type": "u8", "lookup": {0: "off", 1: "on", "default": "?"}}],
    }
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        payload_schema_to_td(schema, source="demo.yaml")
    assert excinfo.value.reason is SkipReason.ENUM_TABLE


def test_length_remaining_round_trips_through_the_byte_length_sentinel():
    """``length: remaining`` converts, rather than taking its device out.

    The form vocabulary carries a byte count, and ``remaining`` is not one, so
    this used to raise an uncaught ValueError. It is expressible though: the
    reference interpreter resolves the keyword and any negative length the same
    way, and the form schema already documents -1 as that sentinel. Mapping it
    keeps the device; skipping it would not.

    The keyword normalises to the sentinel on the way back, rather than being
    remembered verbatim. Both spellings decode identically, the numeric one is
    what the schema library already writes, and it is the form the vocabulary can
    carry -- so there is one spelling downstream instead of two.
    """
    schema = {
        "endian": "big",
        "fields": [{"name": "payload", "type": "hex", "length": "remaining"}],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    form = td[vocab.EVENTS]["payload"][vocab.FORMS][0]
    assert form[vocab.BYTE_LENGTH] == vocab.BYTE_LENGTH_REMAINING
    assert td_to_payload_schema(td)["fields"][0]["length"] == vocab.BYTE_LENGTH_REMAINING


def test_length_naming_a_variable_is_skipped():
    """A ``$variable`` length has no implementation to round trip to."""
    schema = {
        "endian": "big",
        "fields": [{"name": "payload", "type": "hex", "length": "$len"}],
    }
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        payload_schema_to_td(schema, source="demo.yaml")
    assert excinfo.value.reason is SkipReason.LENGTH_NOT_FIXED


def test_a_wire_field_carrying_a_transform_keeps_its_wire_type_and_width():
    """``transform`` post-processes a value read from the wire; it is not derived.

    Modelled on decentlab's ``air_temperature``: a ``u16`` whose raw count is
    scaled after reading. Treating the transform as proof of derivation replaced
    the wire type with the derived marker, so the field became zero bytes wide,
    ``humidity`` after it read from offset 0 instead of 2, and the temperature
    dropped out of the decode entirely.

    Asserted through the byte offsets rather than on the transform alone, because
    the damage showed up in the *other* fields -- a test that only checked the
    transform survived would have passed throughout.
    """
    schema = {
        "endian": "big",
        "fields": [
            {
                "name": "air_temperature",
                "type": "u16",
                "transform": [{"div": 100}, {"add": -327.68}],
            },
            {"name": "humidity", "type": "u16"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    temp_form = td[vocab.EVENTS]["air_temperature"][vocab.FORMS][0]
    assert temp_form[vocab.WIRE_TYPE] == "u16"
    assert temp_form[vocab.DERIVED] == {"transform": [{"div": 100}, {"add": -327.68}]}
    assert temp_form[vocab.BYTE_OFFSET] == 0
    # The field still occupies its two bytes, so the next one starts after them.
    assert td[vocab.EVENTS]["humidity"][vocab.FORMS][0][vocab.BYTE_OFFSET] == 2

    # And the stages survive the trip back, or the value would decode unscaled.
    rebuilt = td_to_payload_schema(td)["fields"]
    assert rebuilt[0]["type"] == "u16"
    assert rebuilt[0]["transform"] == [{"div": 100}, {"add": -327.68}]


def test_a_value_computed_from_others_is_still_derived():
    """A field with no wire type to read stays on the derived path."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "raw", "type": "u16"},
            {"name": "ratio", "ref": "$raw", "polynomial": [0, 0.5]},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    form = td[vocab.EVENTS]["ratio"][vocab.FORMS][0]
    assert form[vocab.WIRE_TYPE] == vocab.COMPUTED_TYPE
    assert form[vocab.DERIVED]["ref"] == "$raw"


def test_a_sensor_annotation_does_not_take_a_device_out_of_the_catalog():
    """``sensor`` names which sensor a channel belongs to; it does not decode.

    An unrecognised field key is rejected, on the principle that silently
    ignoring one risks dropping something that changes the reading. This one
    cannot: like ``semantic`` and ``ipso`` beside it, it labels the channel's
    origin and the reference interpreter never consults it. Rejecting it cost 42
    devices in the next schema library.
    """
    schema = {
        "endian": "big",
        "fields": [{"name": "battery", "type": "u8", "unit": "%", "sensor": "internal"}],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    event = td[vocab.EVENTS]["battery"]
    assert event[vocab.DATA]["unit"] == "%"
    # Dropped rather than carried: nothing downstream has a use for it.
    assert "sensor" not in str(event[vocab.FORMS][0])


def test_a_downlink_command_byte_becomes_a_const_on_the_data_schema():
    """``value`` fixes a byte when *encoding*; it does not compute anything.

    It had been read as a derived-value descriptor, which took the field's device
    out of the catalog -- 45 of them, since nearly every downlink command in the
    schema library identifies itself with a fixed category and command byte.

    The reference interpreter does not use ``value`` when decoding: it reads the
    byte and reports what the payload actually contained. So the field is an
    ordinary scalar that happens to be constrained, and TD core's ``const``
    already says that. A binding term would have been the wrong place -- the
    constraint is a fact about the value, not about how it is transferred.
    """
    schema = {
        "endian": "big",
        "fields": [
            {"name": "category", "type": "u8", "value": 3},
            {"name": "interval", "type": "u16", "unit": "s"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")

    category = td[vocab.EVENTS]["category"]
    assert category[vocab.DATA]["const"] == 3
    # Still a real byte on the wire, so the field after it is not shifted.
    assert category[vocab.FORMS][0][vocab.WIRE_TYPE] == "u8"
    assert td[vocab.EVENTS]["interval"][vocab.FORMS][0][vocab.BYTE_OFFSET] == 1

    assert td_to_payload_schema(td)["fields"][0]["value"] == 3


def test_a_derived_value_reading_something_the_td_lacks_is_skipped():
    """A wrong Thing Description is worse than a missing one.

    A derived value names its inputs with ``$name``. Rebuilding a payload schema
    from the events alone means an input with no event of its own comes back as
    nothing, and the reference interpreter then evaluates the field against zero.
    qingping decoded a temperature of -50 where its schema says 359.5.

    Unlike a skip, that failure is invisible: the device is in the catalog and
    the number is simply wrong. So it is caught after the events are assembled
    and the device is skipped instead.
    """
    schema = {
        "endian": "big",
        "fields": [
            {
                "byte_group": {
                    "size": 1,
                    "fields": [
                        {"name": "_flag", "type": "u8[0:3]"},
                        {"name": "_count", "type": "u8[4:7]"},
                    ],
                }
            },
            {"name": "reading", "ref": "$_count", "polynomial": [-50, 0.1]},
        ],
    }
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        payload_schema_to_td(schema, source="demo.yaml")
    assert excinfo.value.reason is SkipReason.INTERNAL_REF


def test_an_internal_input_that_does_survive_the_round_trip_is_kept():
    """The guard is on what the Thing Description carries, not on the name.

    Most `_`-prefixed inputs get an event of their own and resolve correctly.
    Rejecting on the leading underscore alone cost 22 devices to prevent the one
    wrong Thing Description above.
    """
    schema = {
        "endian": "big",
        "fields": [
            {"name": "_raw", "type": "u16"},
            {"name": "reading", "ref": "$_raw", "polynomial": [-50, 0.1]},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    assert td[vocab.EVENTS]["reading"][vocab.FORMS][0][vocab.DERIVED]["ref"] == "$_raw"


def test_multi_field_tlv_case_becomes_slotted_events():
    """Several fields under one tag become slot-ordered events sharing the tag."""
    schema = {
        "endian": "little",
        "fields": [
            {
                "tlv": {
                    "tag_fields": [{"name": "ch", "type": "u8"}, {"name": "ty", "type": "u8"}],
                    "tag_key": ["ch", "ty"],
                    "cases": {
                        "[6, 101]": [
                            {"name": "illumination", "type": "u16"},
                            {"name": "infrared", "type": "u16"},
                        ],
                    },
                }
            }
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    illum = td[vocab.EVENTS]["illumination"][vocab.FORMS][0]
    infra = td[vocab.EVENTS]["infrared"][vocab.FORMS][0]
    assert illum[vocab.TAG] == [6, 101] and infra[vocab.TAG] == [6, 101]
    assert illum[vocab.SLOT] == 0 and infra[vocab.SLOT] == 1
    # The forward converter rebuilds the multi-field case in slot order.
    case = td_to_payload_schema(td)["fields"][0]["tlv"]["cases"]["[6, 101]"]
    assert [f["name"] for f in case] == ["illumination", "infrared"]


def test_flagged_groups_become_presence_gated_events():
    """Each flagged group field becomes an event gated by a flags bit."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "flags", "type": "u16"},
            {
                "flagged": {
                    "field": "flags",
                    "groups": [
                        {"bit": 0, "fields": [{"name": "moisture", "type": "u16", "div": 50}]},
                        {"bit": 1, "fields": [{"name": "battery", "type": "u16", "div": 1000}]},
                    ],
                }
            },
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    moisture = td[vocab.EVENTS]["moisture"][vocab.FORMS][0][vocab.PRESENT_WHEN]
    battery = td[vocab.EVENTS]["battery"][vocab.FORMS][0][vocab.PRESENT_WHEN]
    assert moisture == {vocab.PW_FIELD: "flags", vocab.PW_BIT: 0}
    assert battery[vocab.PW_BIT] == 1
    # And it rebuilds into a flagged block referencing the flags field.
    block = td_to_payload_schema(td)["fields"][1]["flagged"]
    assert block["field"] == "flags"
    assert {g["bit"] for g in block["groups"]} == {0, 1}


def test_match_cases_become_value_gated_events():
    """Each match case field becomes an event gated by a discriminator value."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "event", "type": "u8"},
            {
                "match": {
                    "field": "$event",
                    "cases": {
                        "0": [{"name": "reset_reason", "type": "u8"}],
                        "1": [{"name": "alarm_code", "type": "u8"}],
                    },
                }
            },
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    reset = td[vocab.EVENTS]["reset_reason"][vocab.FORMS][0][vocab.PRESENT_WHEN]
    assert reset == {vocab.PW_FIELD: "event", vocab.PW_VALUE: 0}
    block = td_to_payload_schema(td)["fields"][1]["match"]
    assert block["field"] == "$event"
    assert set(block["cases"]) == {0, 1}


def test_match_case_skip_padding_round_trips_via_pad_before():
    """Reserved bytes inside a match case survive as ``lorav:padBefore`` padding."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "event_type", "type": "u8", "var": "evt"},
            {
                "match": {
                    "field": "$evt",
                    "cases": {
                        "1": [
                            {"name": "flags", "type": "u8"},
                            {"name": "_reserved", "type": "skip", "length": 4},
                            {"name": "count", "type": "u16"},
                        ]
                    },
                }
            },
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    # The discriminator alias is preserved so the '$evt' reference still resolves.
    assert td[vocab.EVENTS]["event_type"][vocab.FORMS][0][vocab.ALIAS] == "evt"
    # The reserved bytes are recorded on the field that follows them.
    assert td[vocab.EVENTS]["count"][vocab.FORMS][0][vocab.PAD_BEFORE] == 4
    # Rebuilding restores the exact sequential case layout, padding included.
    rebuilt = td_to_payload_schema(td)
    case = rebuilt["fields"][1]["match"]["cases"][1]
    assert [(f.get("type")) for f in case] == ["u8", "skip", "u16"]
    assert case[1]["length"] == 4
    assert rebuilt["fields"][0]["var"] == "evt"


def test_byte_group_bitfields_become_masked_events():
    """``u8[lo:hi]`` byte_group fields become bitmasked events sharing a byte."""
    schema = {
        "endian": "big",
        "fields": [
            {
                "byte_group": {
                    "size": 1,
                    "fields": [
                        {"name": "lo", "type": "u8[0:3]"},
                        {"name": "hi", "type": "u8[4:7]"},
                    ],
                }
            },
            {"name": "tail", "type": "u8"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    lo = td[vocab.EVENTS]["lo"][vocab.FORMS][0]
    hi = td[vocab.EVENTS]["hi"][vocab.FORMS][0]
    assert lo[vocab.BYTE_OFFSET] == 0 and hi[vocab.BYTE_OFFSET] == 0
    assert lo[vocab.BITMASK] == "0x0F" and hi[vocab.BITMASK] == "0xF0"
    # The byte_group consumes one byte, so the trailing field sits at offset 1.
    assert td[vocab.EVENTS]["tail"][vocab.FORMS][0][vocab.BYTE_OFFSET] == 1


def test_tag_size_tlv_becomes_single_tag_tlv():
    """A ``tag_size`` tlv (no tag_fields, no length prefix) maps to a single-tag tlv."""
    schema = {
        "endian": "big",
        "fields": [
            {
                "tlv": {
                    "tag_size": 1,
                    "length_size": 0,
                    "cases": {
                        0x01: [{"name": "temperature", "type": "s16", "div": 10}],
                        0x03: [
                            {"name": "x", "type": "s8"},
                            {"name": "y", "type": "s8"},
                            {"name": "z", "type": "s8"},
                        ],
                    },
                }
            }
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    assert td[vocab.TAG_FIELDS] == [{"name": "tag", "type": "u8"}]
    assert td[vocab.EVENTS]["temperature"][vocab.FORMS][0][vocab.TAG] == [1]
    # The multi-field case keeps slot order when rebuilt.
    cases = td_to_payload_schema(td)["fields"][0]["tlv"]["cases"]
    assert [f["name"] for f in cases["[3]"]] == ["x", "y", "z"]


def test_computed_field_round_trips_with_ordered_scaling():
    """A derived ``ref`` field round-trips, preserving its mult/div/add order."""
    schema = {
        "endian": "big",
        "fields": [
            {"name": "raw", "type": "u8"},
            {
                "name": "temperature",
                "type": "number",
                "ref": "$raw",
                "add": -28.0,
                "div": 5.0,
                "unit": "Cel",
            },
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    form = td[vocab.EVENTS]["temperature"][vocab.FORMS][0]
    assert form[vocab.WIRE_TYPE] == vocab.COMPUTED_TYPE
    assert form[vocab.DERIVED] == {"ref": "$raw"}
    # The derived value occupies no payload bytes (shares the raw field's offset).
    assert form[vocab.BYTE_OFFSET] == 1
    rebuilt = td_to_payload_schema(td)["fields"][1]
    assert rebuilt["type"] == "number" and rebuilt["ref"] == "$raw"
    # add must precede div so the interpreter computes (raw + add) / div.
    keys = [k for k in rebuilt if k in ("add", "div")]
    assert keys == ["add", "div"]


def test_computed_field_in_tlv_case_round_trips():
    """A derived field sharing a tlv tag round-trips as a computed event."""
    schema = {
        "endian": "big",
        "fields": [
            {
                "tlv": {
                    "tag_fields": [{"name": "ch", "type": "u8"}, {"name": "ty", "type": "u8"}],
                    "tag_key": ["ch", "ty"],
                    "cases": {
                        "[3, 103]": [
                            {"name": "temp_raw", "type": "u16"},
                            {
                                "name": "temperature",
                                "type": "number",
                                "ref": "$temp_raw",
                                "div": 10.0,
                            },
                        ],
                    },
                }
            }
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    computed = td[vocab.EVENTS]["temperature"][vocab.FORMS][0]
    assert computed[vocab.WIRE_TYPE] == vocab.COMPUTED_TYPE
    assert computed[vocab.DERIVED] == {"ref": "$temp_raw"}
    assert computed[vocab.TAG] == [3, 103] and computed[vocab.SLOT] == 1
    # The forward converter rebuilds the raw + derived pair under the shared tag.
    case = td_to_payload_schema(td)["fields"][0]["tlv"]["cases"]["[3, 103]"]
    assert [f["name"] for f in case] == ["temp_raw", "temperature"]
    assert case[1]["type"] == "number" and case[1]["ref"] == "$temp_raw"


def test_multibyte_byte_group_bitfields_round_trip():
    """A 3-byte ``byte_group`` with ``u24[lo:hi]`` ranges round-trips faithfully."""
    schema = {
        "endian": "big",
        "fields": [
            {
                "byte_group": {
                    "size": 3,
                    "fields": [
                        {"name": "temp_raw", "type": "u24[4:23]"},
                        {"name": "humi_raw", "type": "u24[0:11]"},
                    ],
                }
            },
            {"name": "tail", "type": "u8"},
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    temp = td[vocab.EVENTS]["temp_raw"][vocab.FORMS][0]
    humi = td[vocab.EVENTS]["humi_raw"][vocab.FORMS][0]
    assert temp[vocab.WIRE_TYPE] == "u24" and temp[vocab.BITMASK] == "0xFFFFF0"
    assert humi[vocab.BITMASK] == "0x000FFF"
    # The 3-byte group consumes three bytes, so the trailing field sits at offset 3.
    assert td[vocab.EVENTS]["tail"][vocab.FORMS][0][vocab.BYTE_OFFSET] == 3
    # Rebuilding emits multi-byte bit ranges; only the last member consumes the group.
    rebuilt = td_to_payload_schema(td)["fields"]
    bitrange = [f for f in rebuilt if "[" in str(f.get("type", ""))]
    assert {f["type"] for f in bitrange} == {"u24[4:23]", "u24[0:11]"}
    assert sum(f.get("consume", 0) for f in bitrange) == 3


def test_recurring_tlv_name_becomes_multiform_event():
    """A field name reused across tlv cases becomes one event with two forms."""
    schema = {
        "endian": "little",
        "fields": [
            {
                "tlv": {
                    "tag_fields": [
                        {"name": "channel_id", "type": "u8"},
                        {"name": "channel_type", "type": "u8"},
                    ],
                    "cases": {
                        "[3, 103]": [{"name": "temperature", "type": "s16", "div": 10}],
                        "[131, 103]": [
                            {"name": "temperature", "type": "s16", "div": 10},
                            {"name": "temperature_alarm", "type": "u8"},
                        ],
                    },
                }
            }
        ],
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    forms = td[vocab.EVENTS]["temperature"][vocab.FORMS]
    assert len(forms) == 2
    assert {tuple(f[vocab.TAG]) for f in forms} == {(3, 103), (131, 103)}
    # It must rebuild into both original cases, including the paired alarm field.
    cases = td_to_payload_schema(td)["fields"][0]["tlv"]["cases"]
    assert set(cases) == {"[3, 103]", "[131, 103]"}
    assert [f["name"] for f in cases["[131, 103]"]] == ["temperature", "temperature_alarm"]


@pytest.mark.parametrize(
    ("schema", "needle", "reason"),
    [
        (
            {"ports": {1: {"fields": []}}},
            "no 'fields'",
            SkipReason.MALFORMED,
        ),
        (
            {"fields": [{"type": "u8"}]},
            "named scalar fields",
            SkipReason.MIXED_FIELD_SHAPE,
        ),
        (
            {
                "fields": [
                    {
                        "tlv": {
                            "tag_fields": [{"name": "t", "type": "u8"}],
                            "cases": {
                                "1": [
                                    {"name": "dup", "type": "u8"},
                                    {"name": "dup", "type": "u8"},
                                ],
                            },
                        }
                    }
                ]
            },
            "duplicate",
            SkipReason.DUPLICATE_NAME,
        ),
        (
            {"fields": [{"name": "x", "type": "number", "formula": "y * 2"}]},
            "computed",
            SkipReason.COMPUTED,
        ),
        (
            {"fields": [{"name": "x", "type": "number", "value": 42}]},
            "computed",
            SkipReason.COMPUTED,
        ),
        (
            {
                "fields": [
                    {"name": "evt", "type": "u8"},
                    {
                        "match": {
                            "field": "$evt",
                            "cases": {
                                "0": [
                                    {"name": "a", "type": "u8"},
                                    {"name": "_pad", "type": "skip", "length": 2},
                                ]
                            },
                        }
                    },
                ]
            },
            "trailing skip padding",
            SkipReason.SKIP_IN_MATCH,
        ),
    ],
)
def test_unsupported_shapes_raise(schema, needle, reason):
    """Out-of-subset schema shapes raise a descriptive error, not bad output."""
    with pytest.raises(UnsupportedSchemaError) as exc:
        payload_schema_to_td(schema, source="demo.yaml")
    assert needle in str(exc.value)
    assert exc.value.reason is reason


def test_ports_layout_tags_each_form_with_its_fport():
    """A ``ports`` schema yields per-fPort forms, sharing names across ports."""
    schema = {
        "endian": "little",
        "ports": {
            1: {"fields": [{"name": "battery", "type": "u8"}]},
            4: {
                "fields": [
                    {"name": "battery", "type": "u8"},
                    {"name": "speed", "type": "u16"},
                ]
            },
        },
    }
    td = payload_schema_to_td(schema, source="demo.yaml")
    assert td[vocab.PAYLOAD_LAYOUT] == vocab.LAYOUT_PORTS
    # 'battery' is reported on both ports -> one event, two fPort-tagged forms.
    battery_forms = td[vocab.EVENTS]["battery"][vocab.FORMS]
    assert {f[vocab.FPORT] for f in battery_forms} == {1, 4}
    assert td[vocab.EVENTS]["speed"][vocab.FORMS][0][vocab.FPORT] == 4
    # It must rebuild into the original per-port field map.
    rebuilt = td_to_payload_schema(td)
    assert set(rebuilt["ports"]) == {1, 4}
    assert [f["name"] for f in rebuilt["ports"][4]["fields"]] == ["battery", "speed"]
