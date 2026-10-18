import fcntl
import json
import os
import uuid
from dataclasses import asdict

from . import context
from .benchmark import benchmark, lifecycle
from .compare import compare_rows, digest
from .config import STATE
from .db import copy_graph, delete_graph, falkor, graph_query, neo_query, write_graph
from .fixtures import seed_source
from .migrate import migrate
from .movies import verify_movies
from .planner import choose_repair
from .workload import CASES, REPAIR_CASE, REPAIRS

RUNS = STATE / "runs"
RUNS.mkdir(parents=True, exist_ok=True)
ALLOWED = set(REPAIRS) - {"drop_identity"}


def save(report):
    destination = RUNS / (report["id"] + ".json")
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, destination)


def evaluate(graph, overrides=None, cases=None):
    overrides = overrides or {}
    output = []
    for case in cases if cases is not None else CASES:
        row = {
            "case_id": case.id,
            "title": case.title,
            "source_query": case.query,
            "target_query": overrides.get(case.id, case.query),
            "params": case.params,
            "classification": case.classification,
            "dependencies": list(case.dependencies),
        }
        if case.nondeterministic:
            row.update(status="inconclusive", reason="LIMIT without ORDER BY has no stable subset contract")
        else:
            try:
                expected, _ = neo_query(case.query, case.params)
                row["expected"] = expected
                actual = graph_query(graph, row["target_query"], case.params)
                row.update(compare_rows(expected, actual, case.ordered), actual=actual)
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:900]}")
        output.append(row)
    return output


def source_manifest():
    ids, _ = neo_query(
        "MATCH (m:Movie) RETURN m.id AS id, elementId(m) AS source_id, "
        "m.source_element_id AS exported_id ORDER BY id"
    )
    edges, _ = neo_query(
        "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) "
        "RETURN r.source_edge_id AS edge_id, r.roles AS roles, "
        "p.id AS person, m.id AS movie ORDER BY edge_id"
    )
    return {"movie_ids": ids, "role_arrays": edges}


def preconditions(graph, manifest):
    target_ids = graph_query(
        graph, "MATCH (m:Movie) RETURN m.id AS id, m.source_element_id AS exported_id ORDER BY id"
    )
    source_ids = [{"id": m["id"], "exported_id": m["source_id"]} for m in manifest["movie_ids"]]
    target_edges = graph_query(
        graph,
        "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) "
        "RETURN r.source_edge_id AS edge_id, p.id AS person, "
        "m.id AS movie ORDER BY edge_id",
    )
    source_edges = [{k: v for k, v in e.items() if k != "roles"} for e in manifest["role_arrays"]]
    unique = len({e["edge_id"] for e in source_edges}) == len(source_edges)
    typed = all(
        isinstance(e["roles"], list) and all(isinstance(v, str) for v in e["roles"])
        for e in manifest["role_arrays"]
    )
    ids_ok = (
        bool(source_ids)
        and all(m["source_id"] == m["exported_id"] for m in manifest["movie_ids"])
        and compare_rows(source_ids, target_ids)["status"] == "passed"
    )
    edges_ok = (
        bool(source_edges)
        and unique
        and typed
        and compare_rows(source_edges, target_edges)["status"] == "passed"
    )
    return {
        "preserve_source_id": ids_ok,
        "restore_role_arrays": edges_ok,
        "preserve_parallel_edges": edges_ok,
        "count_relationships": edges_ok,
        "source_manifest_hash": digest(manifest),
    }


def apply_repair(graph, repair, overrides, manifest):
    if repair == "restore_role_arrays":
        write_graph(
            graph,
            "UNWIND $rows AS row MATCH ()-[r:ACTED_IN {source_edge_id:row.edge_id}]->() "
            "SET r.roles=row.roles",
            {"rows": manifest["role_arrays"]},
        )
    else:
        overrides[REPAIR_CASE[repair]] = REPAIRS[repair]["query"]


def green(rows):
    return (
        bool(rows)
        and any(r["status"] == "passed" for r in rows)
        and all(r["status"] in {"passed", "inconclusive"} for r in rows)
    )


def _run_investigation(report, provider="anthropic", use_memory=True, movies=240, people=120, measure=True):
    run_id = report["id"]
    graph = "sp_" + run_id
    report.update(status="running", graph=graph, events=[], candidates=[], baseline=[], final=[])

    def event(stage, message):
        report["stage"] = stage
        report["events"].append({"time": context.now(), "stage": stage, "message": message})
        save(report)

    try:
        event("fixture", "Create the disposable synthetic Movies dataset")
        report["fixture"] = seed_source(movies, people)
        manifest = source_manifest()
        report["source_manifest"] = manifest
        modules = falkor().connection.execute_command("MODULE", "LIST")
        report["versions"] = {
            "falkordb_modules": str(modules),
            "neo4j": neo_query("CALL dbms.components() YIELD name, versions RETURN name, versions")[0],
        }
        engine = digest(report["versions"])
        context.initialize(run_id, graph, engine)
        event("migration", "Run pinned upstream CSV export and import")
        report["migration"] = migrate(graph)
        event("baseline", "Compare the declared workload, including duplicates and scalar types")
        report["baseline"] = evaluate(graph)
        for row in report["baseline"]:
            context.record_result(run_id, row["case_id"], row)
        evidence = context.investigation_context(run_id)
        report["graph_evidence"] = evidence
        report["preconditions"] = preconditions(graph, manifest)
        schema = digest(
            {
                "contract_version": 2,
                "cases": [asdict(c) for c in CASES],
                "manifest_shape": ["movie_ids", "source_edge_id", "ordered_string_roles"],
            }
        )
        key = context.recipe_key(engine, schema, json.dumps([c.query for c in CASES]))
        report["recipe_key"] = key
        recipes = context.get_recipe(key) if use_memory else []
        event("planning", "Traverse failures through schema dependencies and select an allowlisted repair")
        report["planner_input"] = {
            "failures": evidence,
            "preconditions": report["preconditions"],
            "catalog": {k: v for k, v in REPAIRS.items() if k in ALLOWED},
        }
        if recipes:
            try:
                repairs = json.loads(recipes[0]["repair"])
                if not isinstance(repairs, list) or not set(repairs) <= ALLOWED:
                    raise ValueError("Recipe contains an unsupported repair")
                if not all(report["preconditions"][r] for r in repairs):
                    raise ValueError("Current source preconditions do not hold")
                report["planner"] = {
                    "provider": "verified graph memory",
                    "origin_run": recipes[0]["origin_run"],
                    "revalidated": False,
                    "decision": {
                        "repair_ids": repairs,
                        "reasoning": "Retrieved a previously verified recipe. "
                        "Every current precondition and result must pass again.",
                    },
                }
            except (ValueError, TypeError, KeyError) as exc:
                report["memory_rejected"] = str(exc)
                report["planner"] = choose_repair(report["planner_input"], provider)
        else:
            report["planner"] = choose_repair(report["planner_input"], provider)
        repairs = list(dict.fromkeys(report["planner"]["decision"]["repair_ids"]))
        if not repairs:
            report["status"] = "abstained"
            event("abstained", "Planner abstained; no repair or benchmark accepted")
            context.finish(run_id, report["status"])
            return report
        if not set(repairs) <= ALLOWED or not all(report["preconditions"].get(r) for r in repairs):
            raise ValueError("Selected repairs failed the deterministic precondition gate")
        event("candidates", "Try each repair in an independent disposable graph copy")
        # Negative control demonstrates that a runnable query can still violate the contract.
        trial = graph + "_negative"
        copy_graph(graph, trial)
        try:
            bad = evaluate(
                trial,
                {"identity": REPAIRS["drop_identity"]["query"]},
                [c for c in CASES if c.id == "identity"],
            )
            outcome = {
                "repair_id": "drop_identity",
                "title": REPAIRS["drop_identity"]["title"],
                "status": "rejected" if not green(bad) else "unexpected_pass",
                "checks": bad,
                "affected_cases": context.affected_cases(run_id, REPAIRS["drop_identity"]["changes"]),
            }
            report["candidates"].append(outcome)
            context.record_candidate(run_id, "drop_identity", REPAIRS["drop_identity"]["changes"], outcome)
            if green(bad):
                raise RuntimeError("Verifier accepted the deliberately lossy negative control")
        finally:
            delete_graph(trial)
        overrides = {}
        for index, repair in enumerate(repairs):
            affected = context.affected_cases(run_id, REPAIRS[repair]["changes"])
            trial = graph + "_candidate_" + str(index)
            copy_graph(graph, trial)
            candidate_overrides = dict(overrides)
            try:
                apply_repair(trial, repair, candidate_overrides, manifest)
                # A query-only candidate must pass its own query. The data repair must pass
                # every graph-selected reader. Other known failures wait for their repair.
                tested = (
                    [c for c in CASES if c.id == REPAIR_CASE[repair]]
                    if repair in REPAIR_CASE
                    else [c for c in CASES if c.id in affected]
                )
                checks = evaluate(trial, candidate_overrides, tested)
                outcome = {
                    "repair_id": repair,
                    "title": REPAIRS[repair]["title"],
                    "status": "accepted" if green(checks) else "rejected",
                    "affected_cases": affected,
                    "checks": checks,
                }
                context.record_candidate(run_id, repair, REPAIRS[repair]["changes"], outcome)
                report["candidates"].append(outcome)
                if green(checks):
                    apply_repair(graph, repair, overrides, manifest)
            finally:
                delete_graph(trial)
            save(report)
        event("verification", "Recheck the full workload and the original Movies HTTP handlers")
        report["overrides"] = overrides
        report["final"] = evaluate(graph, overrides)
        for row in report["final"]:
            context.record_result(run_id, row["case_id"], row, "final")
        report["application"] = verify_movies(graph)
        report["lifecycle"] = lifecycle(graph)
        correct = (
            green(report["final"])
            and report["application"]["status"] == "passed"
            and report["lifecycle"]["status"] == "passed"
        )
        report["dependency_graph"] = context.visualization(run_id)
        if correct:
            if report["planner"]["provider"] == "verified graph memory":
                report["planner"]["revalidated"] = True
            context.remember(key, run_id, json.dumps(repairs), json.dumps(report["preconditions"]))
            if measure:
                event("benchmark", "Measure only after correctness passes")
                report["benchmark"] = benchmark(graph, overrides)
                report["post_benchmark"] = evaluate(graph, overrides)
                correct = green(report["post_benchmark"]) and report["benchmark"]["status"] == "passed"
        report["status"] = "verified" if correct else "needs_attention"
        report["scope"] = (
            "Finite workload comparisons on a synthetic fixture using the pinned Movies handlers. "
            "One nondeterministic case remains inconclusive. Not universal migration proof."
        )
        context.finish(run_id, report["status"])
        event("complete", "Evidence receipt saved" if correct else "Unresolved checks require attention")
    except Exception as exc:
        report.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:1500]}")
        try:
            context.finish(run_id, "error")
            report["dependency_graph"] = context.visualization(run_id)
        except Exception:
            pass
        event("error", report["error"])
    finally:
        report["finished_at"] = context.now()
        report["receipt_hash"] = digest({k: v for k, v in report.items() if k != "receipt_hash"})
        save(report)
    return report


def run_investigation(report, provider="anthropic", use_memory=True, movies=240, people=120, measure=True):
    # Source seeding is destructive only within the dedicated lab. Serialize CLI and API processes too.
    with (STATE / "source.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report.update(
                status="error", stage="error", error="Another process is using the disposable source"
            )
            save(report)
            return report
        try:
            return _run_investigation(report, provider, use_memory, movies, people, measure)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def new_report():
    return {"id": uuid.uuid4().hex[:12], "created_at": context.now(), "status": "queued", "stage": "queued"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["zero", "anthropic"], default="anthropic")
    parser.add_argument("--fresh-planner", action="store_true")
    parser.add_argument("--no-benchmark", action="store_true")
    args = parser.parse_args()
    report = run_investigation(
        new_report(), args.provider, not args.fresh_planner, measure=not args.no_benchmark
    )
    print(
        json.dumps(
            {
                "id": report["id"],
                "status": report["status"],
                "error": report.get("error"),
                "receipt": str(RUNS / (report["id"] + ".json")),
            }
        )
    )
    raise SystemExit(0 if report["status"] == "verified" else 1)
