import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from falkordb import FalkorDB
from neo4j import GraphDatabase, Query
from neo4j.graph import Node, Path, Relationship

from .config import FALKOR_HOST, FALKOR_PORT, NEO4J_URI, PASSWORD


def falkor():
    return FalkorDB(
        host=FALKOR_HOST, port=FALKOR_PORT, password=PASSWORD, socket_timeout=30, socket_connect_timeout=5
    )


def normalize(value):
    """Retain types, ordered lists, duplicate rows, and stable entity identity."""
    if isinstance(value, Node):
        return {"$node": {"labels": sorted(value.labels), "properties": normalize(dict(value))}}
    if isinstance(value, Relationship):
        return {"$relationship": {"type": value.type, "properties": normalize(dict(value))}}
    if isinstance(value, Path):
        raise TypeError("Native path values are unsupported; project stable scalar properties instead")
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, (datetime, date, time, timedelta, Decimal)):
        return {"$type": type(value).__name__, "value": str(value)}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("Nonfinite floating point values require an explicit comparison contract")
        return value
    raise TypeError(f"Unsupported result type: {type(value).__name__}")


def neo_query(text, params=None):
    with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", PASSWORD)) as driver:
        with driver.session(database="neo4j") as session:
            result = session.run(Query(text, timeout=15), params or {})
            rows = [normalize(dict(r)) for r in result]
            summary = result.consume()
            return rows, summary.counters.properties_set


def graph_query(name, text, params=None, readonly=True):
    client = falkor()
    try:
        graph = client.select_graph(name)
        result = (graph.ro_query if readonly else graph.query)(text, params or {})
        names = [h[1] for h in result.header]
        return [normalize(dict(zip(names, row))) for row in result.result_set]
    finally:
        client.connection.close()


def write_graph(name, text, params=None):
    return graph_query(name, text, params, readonly=False)


def delete_graph(name):
    client = falkor()
    try:
        if name in client.list_graphs():
            client.select_graph(name).delete()
    finally:
        client.connection.close()


def copy_graph(source, destination):
    client = falkor()
    try:
        client.connection.execute_command("GRAPH.COPY", source, destination)
    finally:
        client.connection.close()
