"""Silver schema contracts: load contracts/<table>.yaml into typed objects.

The format is documented in contracts/README.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

import yaml

KINDS = {"fact", "dimension", "reference"}
ON_ORPHAN = {"quarantine", "nullify"}


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    allowed: tuple[str, ...] | None = None
    range: tuple[float | None, float | None] | None = None
    split: str | None = None
    # Each condition maps column -> accepted values (None matches NULL); conditions are OR-ed.
    null_when: tuple[dict[str, tuple[Any, ...]], ...] = ()

    @property
    def is_list(self) -> bool:
        return self.type.endswith("[]")

    @property
    def base_type(self) -> str:
        return self.type.removesuffix("[]")


@dataclass(frozen=True)
class ForeignKey:
    column: str
    table: str
    ref_column: str
    on_orphan: str = "quarantine"


@dataclass(frozen=True)
class ProcessDay:
    timestamp: str | None = None
    cutoff: str | None = None
    tolerance_minutes: int = 0
    inherits_table: str | None = None
    inherits_via: str | None = None


@dataclass(frozen=True)
class Contract:
    table: str
    kind: str
    primary_key: tuple[str, ...]
    dedupe_order: tuple[str, ...]
    columns: dict[str, Column]
    partition_column: str | None = None
    process_day: ProcessDay | None = None
    value_map: dict[str, dict[str, str | None]] = field(default_factory=dict)
    foreign_keys: tuple[ForeignKey, ...] = ()
    pii_hash: tuple[str, ...] = ()
    pii_age_band: tuple[str, ...] = ()
    pii_free_text: tuple[str, ...] = ()
    # Columns computed in silver (not present in bronze), e.g. amount_usd_source.
    derived: dict[str, Column] = field(default_factory=dict)


def _column(name: str, spec: dict[str, Any]) -> Column:
    rng = spec.get("range")
    return Column(
        name=name,
        type=spec["type"],
        nullable=spec.get("nullable", True),
        allowed=tuple(spec["allowed"]) if "allowed" in spec else None,
        range=(rng[0], rng[1]) if rng else None,
        split=spec.get("split"),
        null_when=tuple(
            {col: tuple(vals) for col, vals in cond.items()} for cond in spec.get("null_when", [])
        ),
    )


def parse_contract(raw: dict[str, Any]) -> Contract:
    pdr = raw.get("process_day")
    process_day = None
    if pdr and "inherits" in pdr:
        process_day = ProcessDay(
            inherits_table=pdr["inherits"]["table"], inherits_via=pdr["inherits"]["via"]
        )
    elif pdr:
        process_day = ProcessDay(
            timestamp=pdr["timestamp"],
            cutoff=pdr["cutoff"],
            tolerance_minutes=pdr.get("tolerance_minutes", 0),
        )
    fks = []
    for fk in raw.get("foreign_keys", []):
        table, ref_column = fk["references"].split(".")
        fks.append(ForeignKey(fk["column"], table, ref_column, fk.get("on_orphan", "quarantine")))
    pii = raw.get("pii", {})
    return Contract(
        table=raw["table"],
        kind=raw["kind"],
        primary_key=tuple(raw["primary_key"]),
        dedupe_order=tuple(raw["dedupe_order"]),
        columns={name: _column(name, spec) for name, spec in raw["columns"].items()},
        partition_column=raw.get("partition_column"),
        process_day=process_day,
        value_map=raw.get("value_map", {}),
        foreign_keys=tuple(fks),
        pii_hash=tuple(pii.get("hash", [])),
        pii_age_band=tuple(pii.get("age_band", [])),
        pii_free_text=tuple(pii.get("free_text", [])),
        derived={name: _column(name, spec) for name, spec in raw.get("derived", {}).items()},
    )


def validate(contracts: dict[str, Contract]) -> list[str]:
    """Internal consistency checks that need no data. Returns a list of errors."""
    errors = []
    for c in contracts.values():
        cols = set(c.columns)

        def check(names, what, c=c, cols=cols):
            for n in names:
                if n not in cols:
                    errors.append(f"{c.table}: {what} references unknown column {n!r}")

        for name in set(c.derived) & cols:
            errors.append(f"{c.table}: derived column {name!r} is also a source column")
        if c.kind not in KINDS:
            errors.append(f"{c.table}: unknown kind {c.kind!r}")
        check(c.primary_key, "primary_key")
        check([o.split()[0] for o in c.dedupe_order if not o.startswith("_")], "dedupe_order")
        check(c.value_map, "value_map")
        check(c.pii_hash + c.pii_age_band + c.pii_free_text, "pii")
        if c.kind == "fact" and c.partition_column not in cols:
            errors.append(f"{c.table}: facts need a partition_column from its columns")
        if c.process_day and c.process_day.timestamp:
            check([c.process_day.timestamp], "process_day")
        if c.process_day and c.process_day.inherits_table:
            check([c.process_day.inherits_via], "process_day")
            if c.process_day.inherits_table not in contracts:
                errors.append(f"{c.table}: process_day inherits unknown table")
        for col in c.columns.values():
            for cond in col.null_when:
                check(cond, f"{col.name}.null_when")
            if col.split and not col.is_list:
                errors.append(f"{c.table}.{col.name}: split requires a list type")
        for fk in c.foreign_keys:
            check([fk.column], "foreign_keys")
            if fk.on_orphan not in ON_ORPHAN:
                errors.append(f"{c.table}.{fk.column}: unknown on_orphan {fk.on_orphan!r}")
            parent = contracts.get(fk.table)
            if parent is None:
                errors.append(f"{c.table}.{fk.column}: references unknown table {fk.table!r}")
            elif parent.primary_key != (fk.ref_column,):
                errors.append(f"{c.table}.{fk.column}: must reference {fk.table}'s primary key")
    return errors


def load_contracts(directory: Path) -> dict[str, Contract]:
    contracts = {}
    for path in sorted(directory.glob("*.yaml")):
        contract = parse_contract(yaml.safe_load(path.read_text()))
        if contract.table != path.stem:
            raise ValueError(f"{path}: table {contract.table!r} must match the file name")
        contracts[contract.table] = contract
    errors = validate(contracts)
    if errors:
        raise ValueError("Invalid contracts:\n  " + "\n  ".join(errors))
    return contracts


def build_order(contracts: dict[str, Contract]) -> list[str]:
    """Tables ordered so every parent (FK target or process_day source) comes first."""
    graph: dict[str, set[str]] = {}
    for c in contracts.values():
        deps = {fk.table for fk in c.foreign_keys if fk.table != c.table}
        if c.process_day and c.process_day.inherits_table:
            deps.add(c.process_day.inherits_table)
        graph[c.table] = deps
    return list(TopologicalSorter(graph).static_order())
