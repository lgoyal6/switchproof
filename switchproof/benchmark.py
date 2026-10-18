"""Small sequential client runner. Correctness gates are enforced by the orchestrator."""

import math
import os
import statistics
import subprocess
import time

from neo4j import GraphDatabase, Query

from .compare import compare_rows
from .config import NEO4J_URI, PASSWORD
from .db import copy_graph, delete_graph, falkor, graph_query, write_graph
from .workload import CASES


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def benchmark(graph, overrides, repeats=20, warmups=3):
    client = falkor()
    target = client.select_graph(graph)
    output = []
    with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", PASSWORD)) as driver:
        with driver.session(database="neo4j") as source:
            for case in CASES:
                if case.nondeterministic:
                    continue
                timings = {"neo4j": [], "falkordb": []}
                errors = []
                for i in range(warmups + repeats):
                    # Alternate order to reduce a fixed first-engine bias.
                    order = ["neo4j", "falkordb"] if i % 2 else ["falkordb", "neo4j"]
                    for engine in order:
                        start = time.perf_counter_ns()
                        try:
                            if engine == "neo4j":
                                result = source.run(Query(case.query, timeout=15), case.params)
                                list(result)
                                result.consume()
                            else:
                                target.ro_query(overrides.get(case.id, case.query), case.params)
                            if i >= warmups:
                                timings[engine].append((time.perf_counter_ns() - start) / 1e6)
                        except Exception as exc:
                            errors.append({"engine": engine, "iteration": i, "error": str(exc)[:500]})
                row = {"case_id": case.id, "title": case.title, "errors": errors}
                for engine, values in timings.items():
                    row[engine] = {
                        "samples_ms": values,
                        "count": len(values),
                        "p50_ms": statistics.median(values) if values else None,
                        "p95_ms": percentile(values, 0.95) if values else None,
                    }
                row["ratio_neo4j_over_falkor"] = (
                    row["neo4j"]["p50_ms"] / row["falkordb"]["p50_ms"]
                    if all(timings.values()) and not errors
                    else None
                )
                output.append(row)
    client.connection.close()
    memory_snapshot = []
    memory_note = "Docker cgroup snapshot"
    try:
        memory = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                "switchproof-neo4j-1",
                "switchproof-falkor-1",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if memory.returncode == 0:
            memory_snapshot = memory.stdout.splitlines()
        else:
            memory_note = "Memory snapshot unavailable on this host"
    except (OSError, subprocess.TimeoutExpired):
        memory_note = "Docker memory telemetry unavailable in hosted runtime"
    return {
        "runner": "SwitchProof Python client fallback",
        "repeats": repeats,
        "warmups": warmups,
        "concurrency": 1,
        "resource_caps": os.environ.get("SWITCHPROOF_RESOURCE_CAPS", "2 CPUs and 1 GiB per Docker service"),
        "scope": "Synthetic fixture, warm connections, end-to-end client latency on the recorded runtime. "
        "Not a production benchmark or a universal engine comparison.",
        "upstream_runner_note": "Pinned FalkorDB/benchmark build blocked by missing protoc; "
        "this run uses the documented Python fallback.",
        "memory_snapshot": memory_snapshot,
        "memory_note": memory_note,
        "queries": output,
        "status": "passed" if all(not r["errors"] for r in output) else "errors",
    }


def lifecycle(graph):
    a, b = graph + "_tenant_a", graph + "_tenant_b"
    created = []
    try:
        start = time.perf_counter_ns()
        for name in [a, b]:
            copy_graph(graph, name)
            created.append(name)
        copy_ms = (time.perf_counter_ns() - start) / 1e6
        q = "MATCH (m:Movie {id:'m0000'}) RETURN m.votes AS votes"
        before = graph_query(b, q)
        write_graph(a, "MATCH (m:Movie {id:'m0000'}) SET m.votes=99999")
        check = compare_rows(before, graph_query(b, q))
        source_check = compare_rows(before, graph_query(graph, q))
        return {
            "status": "passed" if check["status"] == source_check["status"] == "passed" else "mismatch",
            "copies": 2,
            "copy_ms": copy_ms,
            "isolation": check,
            "original_unchanged": source_check,
            "scope": "Two disposable graph copies in one FalkorDB process; not a security isolation claim",
        }
    finally:
        for name in created:
            delete_graph(name)
