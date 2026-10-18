"""Execute the pinned Movies Flask handlers with a small, explicit driver adapter."""

import importlib.util
from types import SimpleNamespace
from unittest.mock import patch

from falkordb.node import Node

from .compare import canonical, compare_rows
from .config import NEO4J_URI, PASSWORD, ROOT
from .db import copy_graph, delete_graph, falkor, neo_query

GRAPH_REWRITE = """MATCH (m:Movie)<-[r:ACTED_IN]-(a:Person)
WITH m, a, count(r) AS n UNWIND range(1,n) AS occurrence
WITH m, collect(a.name) AS cast RETURN m.title AS movie, cast LIMIT $limit"""


class FalkorMovieDriver:
    def __init__(self, graph):
        self.client = falkor()
        self.graph = self.client.select_graph(graph)

    def execute_query(self, query, database_=None, routing_=None, result_transformer_=None, **params):
        # The upstream graph projection omits relationship identity. Bind/count it explicitly
        # to preserve parallel edges on this engine. No output field or duplicate is discarded.
        if "AS movie, collect(a.name) AS cast" in query:
            query = GRAPH_REWRITE
        result = self.graph.query(query, params)
        names = [h[1] for h in result.header]
        records = [
            {key: (value.properties if isinstance(value, Node) else value) for key, value in zip(names, row)}
            for row in result.result_set
        ]
        # The Python client exposes query statistics as floats. This API count is integral.
        updates = result.properties_set
        if int(updates) != updates:
            raise ValueError("Driver returned a fractional property update counter")
        summary = SimpleNamespace(counters=SimpleNamespace(properties_set=int(updates)))
        if result_transformer_:
            if result_transformer_.__name__ == "single":
                return records[0] if records else None
            if result_transformer_.__name__ == "consume":
                return summary
            raise ValueError("Unsupported upstream result transformer")
        return records, summary, names

    def close(self):
        self.client.connection.close()


class MovieParameterDriver:
    """Share one documented input fix: Flask query arguments are strings, Cypher LIMIT is integer."""

    def __init__(self, driver):
        self.driver = driver

    def execute_query(self, query, **kwargs):
        if "limit" in kwargs:
            kwargs["limit"] = int(kwargs["limit"])
        return self.driver.execute_query(query, **kwargs)

    def close(self):
        self.driver.close()


def app_for(graph=None):
    spec = importlib.util.spec_from_file_location("sp_movies", ROOT / "vendor/movies/movies_sync.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        "os.environ",
        {
            "NEO4J_URI": NEO4J_URI,
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": PASSWORD,
            "NEO4J_DATABASE": "neo4j",
        },
    ):
        spec.loader.exec_module(module)
    if graph:
        module.driver.close()
        module.driver = FalkorMovieDriver(graph)
    module.driver = MovieParameterDriver(module.driver)
    return module


def endpoint_contract(path, payload):
    # Node positions are presentation details. Reconstruct graph links by semantic node identity.
    if path.startswith("/graph") and isinstance(payload, dict):
        nodes = payload["nodes"]
        return {
            "nodes": sorted(nodes, key=canonical),
            "links": sorted(
                [{"source": nodes[e["source"]], "target": nodes[e["target"]]} for e in payload["links"]],
                key=canonical,
            ),
        }
    if isinstance(payload, list):
        return sorted(payload, key=canonical)
    if isinstance(payload, dict) and "cast" in payload:
        return {**payload, "cast": sorted(payload["cast"], key=canonical)}
    return payload


def verify_movies(graph):
    sandbox = graph + "_app"
    copy_graph(graph, sandbox)
    source, target = app_for(), app_for(sandbox)
    source.app.testing = target.app.testing = True
    checks = []
    try:
        left, right = source.app.test_client(), target.app.test_client()
        # limit exceeds the entire frozen fixture, avoiding the app's nondeterministic subset.
        for path in [
            "/search?q=North",
            "/search",
            "/movie/North%200000",
            "/movie/unknown",
            "/graph?limit=10000",
        ]:
            a, b = left.get(path), right.get(path)
            expected = {"http": a.status_code, "body": endpoint_contract(path, a.get_json())}
            actual = {"http": b.status_code, "body": endpoint_contract(path, b.get_json())}
            checks.append({"endpoint": path, **compare_rows([expected], [actual])})
        before, _ = neo_query("MATCH (m:Movie {id:'m0000'}) RETURN m.votes AS votes")
        try:
            a = left.post("/movie/North%200000/vote")
            b = right.post("/movie/North%200000/vote")
            checks.append(
                {
                    "endpoint": "POST /movie/North 0000/vote",
                    **compare_rows(
                        [{"http": a.status_code, "body": a.get_json()}],
                        [{"http": b.status_code, "body": b.get_json()}],
                    ),
                }
            )
            after, _ = neo_query("MATCH (m:Movie {id:'m0000'}) RETURN m.votes AS votes")
            r = target.driver.driver.graph.ro_query("MATCH (m:Movie {id:'m0000'}) RETURN m.votes AS votes")
            checks.append(
                {
                    "endpoint": "vote persisted state",
                    **compare_rows([{"votes": before[0]["votes"] + 1}], [{"votes": r.result_set[0][0]}]),
                }
            )
            if after[0]["votes"] != before[0]["votes"] + 1:
                raise RuntimeError("Upstream vote failed to update the source")
        finally:
            neo_query("MATCH (m:Movie {id:'m0000'}) SET m.votes=$votes", before[0])
    finally:
        source.driver.close()
        target.driver.close()
        delete_graph(sandbox)
    return {
        "status": "passed" if all(c["status"] == "passed" for c in checks) else "mismatch",
        "component": "neo4j-examples/movies-python-bolt",
        "checks": checks,
        "adapter": "Shared LIMIT parameter conversion; Falkor adapter counts parallel edges and exposes integer update counters",
        "contract": "Unordered search/cast and graph node identities; role ordering and duplicates retained. "
        "Graph limit covers the full synthetic fixture. Arbitrary limited subsets unverified.",
    }
