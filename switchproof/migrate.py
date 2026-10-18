import importlib.util
import os
import subprocess
import sys
import tempfile

from .compare import compare_rows
from .config import FALKOR_HOST, FALKOR_PORT, NEO4J_URI, PASSWORD, ROOT
from .db import delete_graph, graph_query, neo_query


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "vendor/migration" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export_and_load(graph_name, scratch):
    extractor_type = load_module("sp_extractor", "neo4j_to_csv_extractor.py").Neo4jToCSVExtractor
    loader_type = load_module("sp_loader", "falkordb_csv_loader.py").FalkorDBCSVLoader
    extractor = extractor_type(NEO4J_URI, "neo4j", PASSWORD)
    try:
        extractor.output_dir = scratch
        extractor.extract_all()
    finally:
        extractor.close()
    loader = loader_type(
        host=FALKOR_HOST, port=FALKOR_PORT, password=PASSWORD, graph_name=graph_name, csv_dir=scratch
    )
    try:
        loader.load_all_csvs()
    finally:
        loader.close()


def migrate(graph_name):
    """The unchanged exporter runs in a child process with a temporary working directory."""
    delete_graph(graph_name)
    with tempfile.TemporaryDirectory(prefix="switchproof-export-") as scratch:
        result = subprocess.run(
            [sys.executable, "-m", "switchproof.migrate", graph_name, scratch],
            cwd=scratch,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            timeout=180,
        )
    if result.returncode:
        raise RuntimeError("Migration subprocess failed: " + result.stdout[-1500:] + result.stderr[-1500:])
    checks = []
    for q in [
        "MATCH (n) RETURN count(n) AS count",
        "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY type",
    ]:
        rows, _ = neo_query(q)
        checks.append(compare_rows(rows, graph_query(graph_name, q)))
    if any(c["status"] != "passed" for c in checks):
        raise RuntimeError("Independent topology verification failed after upstream import")
    return {
        "component": "FalkorDB/Neo4j-to-FalkorDB",
        "commit": "6ca9309cbf59690b3588db54b08ad48cad938880",
        "topology_checks": checks,
        "log": result.stdout[-16000:],
    }


if __name__ == "__main__":
    export_and_load(sys.argv[1], sys.argv[2])
