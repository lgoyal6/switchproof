import json
import uuid
from datetime import datetime, timezone

from .compare import digest
from .db import graph_query, write_graph
from .workload import CASES

CONTROL = "sp_investigations"


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(run_id, graph_name, engine_version):
    write_graph(
        CONTROL,
        "MERGE (r:Run {id:$id}) SET r.graph=$graph, r.created=$created, r.engine=$engine, r.status='running'",
        {"id": run_id, "graph": graph_name, "created": now(), "engine": engine_version},
    )
    for case in CASES:
        qid = f"{run_id}:{case.id}"
        write_graph(
            CONTROL,
            "MATCH (r:Run {id:$run}) MERGE (q:Query {id:$id}) SET q.case_id=$case, q.title=$title, q.text=$text MERGE (r)-[:HAS_QUERY]->(q)",
            {"run": run_id, "id": qid, "case": case.id, "title": case.title, "text": case.query},
        )
        for dep in case.dependencies:
            write_graph(
                CONTROL,
                "MATCH (q:Query {id:$id}) MERGE (s:SchemaElement {name:$name}) MERGE (q)-[:READS]->(s)",
                {"id": qid, "name": dep},
            )


def record_result(run_id, case_id, result, stage="baseline"):
    write_graph(
        CONTROL,
        "MATCH (q:Query {id:$qid}) CREATE (v:Validation {id:$id, stage:$stage, status:$status, evidence:$evidence}) CREATE (q)-[:HAS_RESULT]->(v)",
        {
            "qid": f"{run_id}:{case_id}",
            "id": uuid.uuid4().hex,
            "stage": stage,
            "status": result["status"],
            "evidence": json.dumps(result),
        },
    )


def investigation_context(run_id):
    # This traversal supplies the planner's allowed evidence, rather than a parallel Python list.
    return graph_query(
        CONTROL,
        "MATCH (r:Run {id:$run})-[:HAS_QUERY]->(q:Query)-[:HAS_RESULT]->(v:Validation) WHERE v.stage='baseline' AND v.status <> 'passed' AND v.status <> 'inconclusive' OPTIONAL MATCH (q)-[:READS]->(s:SchemaElement) RETURN q.case_id AS case_id, q.text AS query, v.evidence AS evidence, collect(s.name) AS dependencies ORDER BY case_id",
        {"run": run_id},
    )


def affected_cases(run_id, dependencies):
    rows = graph_query(
        CONTROL,
        "MATCH (r:Run {id:$run})-[:HAS_QUERY]->(q:Query)-[:READS]->(s:SchemaElement) WHERE s.name IN $names RETURN DISTINCT q.case_id AS case_id ORDER BY case_id",
        {"run": run_id, "names": dependencies},
    )
    return [r["case_id"] for r in rows]


def record_candidate(run_id, repair_id, changes, result):
    cid = f"{run_id}:candidate:{repair_id}"
    write_graph(
        CONTROL,
        "MATCH (r:Run {id:$run}) CREATE (c:Candidate {id:$id, repair:$repair, status:$status}) CREATE (r)-[:TRIED]->(c)",
        {"run": run_id, "id": cid, "repair": repair_id, "status": result["status"]},
    )
    for dep in changes:
        write_graph(
            CONTROL,
            "MATCH (c:Candidate {id:$id}) MERGE (s:SchemaElement {name:$name}) MERGE (c)-[:CHANGES]->(s)",
            {"id": cid, "name": dep},
        )


def recipe_key(engine, schema_hash, query):
    return digest({"engine": engine, "schema": schema_hash, "query": query})


def get_recipe(key):
    return graph_query(
        CONTROL,
        "MATCH (p:Recipe {key:$key, status:'verified'}) RETURN p.repair AS repair, p.origin_run AS origin_run, p.precondition AS precondition",
        {"key": key},
    )


def remember(key, run_id, repair_id, precondition):
    write_graph(
        CONTROL,
        "MATCH (r:Run {id:$run}) MERGE (p:Recipe {key:$key}) ON CREATE SET p.origin_run=$run SET p.repair=$repair, p.status='verified', p.last_verified_run=$run, p.precondition=$condition MERGE (r)-[:ESTABLISHED]->(p)",
        {"run": run_id, "key": key, "repair": repair_id, "condition": precondition},
    )


def finish(run_id, status):
    write_graph(CONTROL, "MATCH (r:Run {id:$id}) SET r.status=$status", {"id": run_id, "status": status})


def visualization(run_id):
    nodes, edges = {}, []
    rows = graph_query(
        CONTROL,
        "MATCH (r:Run {id:$run})-[:HAS_QUERY]->(q:Query)-[:READS]->(s:SchemaElement) RETURN q.case_id AS query, q.title AS title, s.name AS schema",
        {"run": run_id},
    )
    for row in rows:
        a, b = "query:" + row["query"], "schema:" + row["schema"]
        nodes[a] = {"id": a, "label": row["title"], "type": "query"}
        nodes[b] = {"id": b, "label": row["schema"], "type": "schema"}
        edges.append({"source": a, "target": b, "label": "reads"})
    for row in graph_query(
        CONTROL,
        "MATCH (r:Run {id:$run})-[:TRIED]->(c:Candidate)-[:CHANGES]->(s:SchemaElement) RETURN c.repair AS repair, c.status AS status, s.name AS schema",
        {"run": run_id},
    ):
        a, b = "repair:" + row["repair"], "schema:" + row["schema"]
        nodes[a] = {
            "id": a,
            "label": row["repair"].replace("_", " "),
            "type": "candidate",
            "status": row["status"],
        }
        edges.append({"source": a, "target": b, "label": "changes"})
    return {"nodes": list(nodes.values()), "edges": edges}
