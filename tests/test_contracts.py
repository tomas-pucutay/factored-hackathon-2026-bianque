from pathlib import Path

import pytest

from bianque.pipeline.contracts import build_order, load_contracts, parse_contract, validate

CONTRACTS = load_contracts(Path("contracts"))


def test_all_13_tables_have_a_contract():
    assert len(CONTRACTS) == 13


def test_build_order_puts_parents_first():
    order = build_order(CONTRACTS)
    for c in CONTRACTS.values():
        for fk in c.foreign_keys:
            assert order.index(fk.table) < order.index(c.table)
    assert order.index("call_center_interactions") < order.index("satisfaction_surveys")


def test_orphaned_branch_keys_are_nullified():
    nullified = {
        (c.table, fk.column)
        for c in CONTRACTS.values()
        for fk in c.foreign_keys
        if fk.on_orphan == "nullify"
    }
    assert nullified == {
        ("customers", "registration_branch_id"),
        ("service_agents", "assigned_branch_id"),
    }


def test_nullified_keys_are_nullable():
    for c in CONTRACTS.values():
        for fk in c.foreign_keys:
            if fk.on_orphan == "nullify":
                assert c.columns[fk.column].nullable


def _minimal(**overrides):
    raw = {
        "table": "t",
        "kind": "dimension",
        "primary_key": ["id"],
        "dedupe_order": ["_ingested_at DESC"],
        "columns": {"id": {"type": "VARCHAR", "nullable": False}},
    }
    return {**raw, **overrides}


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"primary_key": ["missing"]}, "primary_key references unknown column"),
        ({"value_map": {"missing": {"a": "b"}}}, "value_map references unknown column"),
        (
            {"foreign_keys": [{"column": "id", "references": "nope.id"}]},
            "references unknown table",
        ),
        (
            {"columns": {"id": {"type": "VARCHAR", "null_when": [{"missing": ["x"]}]}}},
            "null_when references unknown column",
        ),
        ({"columns": {"id": {"type": "VARCHAR", "split": ","}}}, "split requires a list type"),
    ],
)
def test_validate_catches_broken_references(overrides, error):
    contract = parse_contract(_minimal(**overrides))
    errors = validate({"t": contract})
    assert any(error in e for e in errors), errors


def test_derived_column_cannot_shadow_a_source_column():
    contract = parse_contract(_minimal(derived={"id": {"type": "VARCHAR"}}))
    assert any("also a source column" in e for e in validate({"t": contract}))


def test_age_band_requires_a_derived_column():
    raw = _minimal(columns={"id": {"type": "VARCHAR"}, "dob": {"type": "DATE"}})
    contract = parse_contract({**raw, "pii": {"age_band": ["dob"]}})
    assert any("derived age_band" in e for e in validate({"t": contract}))
