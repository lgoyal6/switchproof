import json
import subprocess
import sys

import pytest

from switchproof import context, engine
from switchproof.compare import digest
from switchproof.db import delete_graph, graph_query, write_graph

pytestmark = pytest.mark.integration


@pytest.fixture
def investigation(monkeypatch, tmp_path):
    control = "sp_test_control"
    delete_graph(control)
    monkeypatch.setattr(context, "CONTROL", control)
    monkeypatch.setattr(engine, "RUNS", tmp_path)

    # This is a verifier integration test, not evidence of a live model call.
    def test_decision(evidence, provider):
        assert len(evidence["failures"]) == 4
        assert any("Movie.source_element_id" in r["dependencies"] for r in evidence["failures"])
        return {
            "provider": "integration test decision, no live model",
            "decision": {
                "repair_ids": [
                    "restore_role_arrays",
                    "preserve_parallel_edges",
                    "count_relationships",
                    "preserve_source_id",
                ],
                "reasoning": "Explicit test decision exercises real database validation, not live inference.",
            },
        }

    monkeypatch.setattr(engine, "choose_repair", test_decision)
    reports = []
    yield reports
    for report in reports:
        if "graph" in report:
            delete_graph(report["graph"])
    delete_graph(control)


def test_full_pipeline_memory_and_stale_precondition(investigation, monkeypatch):
    report = engine.run_investigation(engine.new_report(), movies=240, people=120)
    investigation.append(report)
    assert report["status"] == "verified", report.get("error") or report.get("application")
    assert len([r for r in report["baseline"] if r["status"] in {"error", "mismatch"}]) == 4
    assert len([r for r in report["final"] if r["status"] == "passed"]) == 11
    assert report["final"][-1]["status"] == "inconclusive"
    assert report["application"]["status"] == "passed"
    assert len(report["application"]["checks"]) == 7
    assert report["candidates"][0]["status"] == "rejected"
    assert len([c for c in report["candidates"] if c["status"] == "accepted"]) == 4
    assert report["benchmark"]["status"] == "passed"
    assert all(q["neo4j"]["count"] == q["falkordb"]["count"] == 20 for q in report["benchmark"]["queries"])
    assert report["receipt_hash"] == digest({k: v for k, v in report.items() if k != "receipt_hash"})
    code = """
import json,sys
from pathlib import Path
from switchproof import context, engine
context.CONTROL = 'sp_test_control'
engine.RUNS = Path(sys.argv[1])
report = engine.run_investigation(engine.new_report(), movies=60, people=30, measure=False)
print(json.dumps(report))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(engine.RUNS)], capture_output=True, text=True, check=True, timeout=90
    )
    second = json.loads(process.stdout)
    investigation.append(second)
    assert second["status"] == "verified", second.get("error") or second.get("application")
    assert second["planner"]["provider"] == "verified graph memory"
    assert second["planner"]["revalidated"] is True
    assert second["planner"]["origin_run"] == report["id"]
    assert context.get_recipe(report["recipe_key"])[0]["origin_run"] == report["id"]
    # A real graph mutation invalidates the ID precondition, rather than accepting cached success.
    write_graph(second["graph"], "MATCH (m:Movie {id:'m0000'}) SET m.source_element_id='wrong'")
    assert engine.preconditions(second["graph"], second["source_manifest"])["preserve_source_id"] is False
    assert graph_query(context.CONTROL, "MATCH (r:Recipe) RETURN count(r) AS count")[0]["count"] == 1
    # Poisoning a recipe with an unallowlisted action must fall back to a fresh decision.
    write_graph(
        context.CONTROL, "MATCH (r:Recipe) SET r.repair=$repair", {"repair": json.dumps(["run_shell"])}
    )
    third = engine.run_investigation(engine.new_report(), movies=60, people=30, measure=False)
    investigation.append(third)
    assert third["status"] == "verified", third.get("error")
    assert third["memory_rejected"] == "Recipe contains an unsupported repair"
    assert third["planner"]["provider"] == "integration test decision, no live model"
