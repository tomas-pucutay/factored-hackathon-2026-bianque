from datetime import date

import duckdb
import pandas as pd

from bianque.pipeline.watermark import (
    SilverState,
    fingerprint,
    load_state,
    plan,
    save_state,
    window_months,
)


def write_file(path, dates):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"process_date": dates}, dtype="string").to_parquet(path, index=False)
    return str(path)


def state_for(files, watermark, contract_hash="h"):
    return SilverState(watermark, {f: fingerprint(f) for f in files}, contract_hash)


def run_plan(files, state, **kw):
    return plan(duckdb.connect(), files, state, "h", "process_date", 7, **kw)


def test_window_months_spans_month_boundary():
    assert window_months(date(2026, 6, 3), 7) == {"2026-05", "2026-06"}
    assert window_months(date(2026, 6, 17), 7) == {"2026-06"}


def test_full_rebuild_triggers(tmp_path):
    f = write_file(tmp_path / "a.parquet", ["2026-06-17"])
    assert run_plan([f], None).reason == "no previous state"
    assert run_plan([f], state_for([f], date(2026, 6, 17), "old")).full
    assert run_plan([f], state_for([f], date(2026, 6, 17)), full=True).reason == "requested"
    gone = state_for([f], date(2026, 6, 17))
    gone.fingerprints["deleted.parquet"] = "x"
    assert run_plan([f], gone).reason == "bronze files were removed"


def test_no_changes_reprocesses_only_the_trailing_window(tmp_path):
    f = write_file(tmp_path / "a.parquet", ["2026-06-17"])
    p = run_plan([f], state_for([f], date(2026, 6, 17)))
    assert (p.full, p.months, p.late_rows, p.watermark) == (
        False,
        {"2026-06"},
        0,
        date(2026, 6, 17),
    )


def test_new_day_moves_the_watermark(tmp_path):
    old = write_file(tmp_path / "a.parquet", ["2026-06-17"])
    state = state_for([old], date(2026, 6, 17))
    new = write_file(tmp_path / "b.parquet", ["2026-07-02"])
    p = run_plan([old, new], state)
    assert p.watermark == date(2026, 7, 2)
    assert p.months == {"2026-06", "2026-07"}  # window 06-25..07-02


def test_late_partition_beyond_window_is_reprocessed_and_counted(tmp_path):
    old = write_file(tmp_path / "a.parquet", ["2026-06-17"])
    state = state_for([old], date(2026, 6, 17))
    late = write_file(tmp_path / "late.parquet", ["2024-01-10", "2024-01-10"])
    p = run_plan([old, late], state)
    assert p.months == {"2024-01", "2026-06"}
    assert p.late_rows == 2
    assert p.watermark == date(2026, 6, 17)  # late data never moves the watermark back


def test_state_round_trip(tmp_path):
    path = tmp_path / "s" / "t.json"
    state = SilverState(date(2026, 6, 17), {"a": "1-2"}, "h")
    save_state(path, state)
    assert load_state(path) == state
    assert load_state(tmp_path / "missing.json") is None
