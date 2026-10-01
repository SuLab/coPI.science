"""MD-6: every table with simulation_run_id has an FK to simulation_runs.id with an
explicit ondelete, and an index led by simulation_run_id. Two listed exceptions."""
import src.models  # noqa: F401
from src.database import Base

#: table -> reason. Nothing else may be exempt.
EXCEPTIONS = {
    "simulation_process_status": "the one-row engine status table (id = 1): its run FK is "
                                 "ondelete SET NULL so the status survives run deletion, and a "
                                 "one-row table needs no run-led index (src/models/simulation_control.py)",
    "cohort_audit_events": "no FK: topology snapshots record a run id for display and "
                           "must survive run deletion (src/models/cohort.py)",
}


def _run_scoped():
    return [t for t in Base.metadata.sorted_tables if "simulation_run_id" in t.c]


def test_every_run_scoped_table_has_an_explicit_ondelete_fk():
    for table in _run_scoped():
        if table.name in EXCEPTIONS:
            continue
        fks = [fk for fk in table.c.simulation_run_id.foreign_keys
               if fk.column.table.name == "simulation_runs"]
        assert fks, f"{table.name}.simulation_run_id has no FK to simulation_runs"
        assert fks[0].ondelete, f"{table.name}: FK has no explicit ondelete"


def test_every_run_scoped_table_has_a_run_led_index():
    for table in _run_scoped():
        if table.name in EXCEPTIONS:
            continue
        led = [ix for ix in table.indexes if list(ix.columns)[0].name == "simulation_run_id"]
        led += [c for c in table.constraints
                if hasattr(c, "columns") and c.__class__.__name__ == "UniqueConstraint"
                and list(c.columns)[0].name == "simulation_run_id"]
        assert led, f"{table.name}: no index led by simulation_run_id"


def test_exceptions_are_real_run_scoped_tables():
    names = {t.name for t in _run_scoped()}
    assert set(EXCEPTIONS) <= names
