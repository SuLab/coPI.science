"""Spec §7.4: run_profile_pipeline is split into step functions in the same module,
and job_progress lives in a module grant_enrichment can import without a cycle."""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_pipeline_records_progress_through_job_progress_record():
    """The pipeline reaches `record` through the module (not a copied name), so
    `job_progress.configure` redirects it."""
    from src.services import job_progress, profile_pipeline

    assert profile_pipeline.job_progress is job_progress
    assert not hasattr(job_progress, "append_job_progress")


def test_enrichment_modules_do_not_import_the_pipeline():
    for rel in ("src/services/grant_enrichment.py", "src/services/industry_evidence.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "src.services.profile_pipeline" not in mods, rel


def test_no_pipeline_function_exceeds_the_size_gate():
    tree = ast.parse((ROOT / "src/services/profile_pipeline.py").read_text(encoding="utf-8"))
    long = [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.end_lineno - n.lineno + 1 > 200]
    assert long == []
