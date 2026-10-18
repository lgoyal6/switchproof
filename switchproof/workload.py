from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    query: str
    params: dict = field(default_factory=dict)
    dependencies: tuple = ()
    ordered: bool = True
    classification: str = "application"
    nondeterministic: bool = False


CASES = [
    Case(
        "search",
        "Search movie titles",
        "MATCH (m:Movie) WHERE toLower(m.title) CONTAINS toLower($title) RETURN m.id AS id, m.title AS title, m.released AS released ORDER BY id",
        {"title": "North"},
        ("Movie.title", "Movie.id", "Movie.released"),
    ),
    Case(
        "detail",
        "Movie details",
        "MATCH (m:Movie {id:$id}) RETURN m.title AS title, m.summary AS summary, m.duration AS duration, m.votes AS votes",
        {"id": "m0000"},
        ("Movie.id", "Movie.title", "Movie.summary", "Movie.duration", "Movie.votes"),
    ),
    Case(
        "cast",
        "Cast and ordered roles",
        "MATCH (m:Movie {id:$id})<-[r:ACTED_IN]-(p:Person) RETURN p.name AS name, r.roles AS roles ORDER BY name",
        {"id": "m0000"},
        ("Movie.id", "Person.name", "ACTED_IN.roles"),
    ),
    Case(
        "counts",
        "Movie and actor counts",
        "MATCH (m:Movie) RETURN count(m) AS count",
        dependencies=("Movie",),
    ),
    Case(
        "empty",
        "Missing movie",
        "MATCH (m:Movie {id:$id}) RETURN m.title AS title",
        {"id": "does-not-exist"},
        ("Movie.id", "Movie.title"),
    ),
    Case(
        "null",
        "Missing properties stay null",
        "MATCH (m:Movie {id:$id}) RETURN m.nonexistent AS missing, m.votes AS votes",
        {"id": "m0001"},
        ("Movie.id", "Movie.votes"),
    ),
    Case(
        "duplicates",
        "Duplicate relationships retain multiplicity",
        "MATCH (m:Movie {id:$id})<-[:ACTED_IN]-(p:Person) RETURN p.name AS name ORDER BY name",
        {"id": "m0000"},
        ("Movie.id", "ACTED_IN", "Person.name"),
    ),
    Case(
        "two_hop",
        "Find movies through a shared actor",
        "MATCH (m:Movie {id:$id})<-[:ACTED_IN]-(p:Person)-[:ACTED_IN]->(other:Movie) WHERE other.id <> m.id RETURN DISTINCT other.id AS id, other.title AS title ORDER BY id",
        {"id": "m0000"},
        ("Movie.id", "Movie.title", "ACTED_IN"),
    ),
    Case(
        "aggregate",
        "Count appearances per actor",
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.id AS id, count(m) AS appearances ORDER BY appearances DESC, id LIMIT 10",
        dependencies=("Person.id", "ACTED_IN"),
    ),
    Case(
        "identity",
        "Preserve source-facing entity IDs",
        "MATCH (m:Movie {id:$id}) RETURN elementId(m) AS external_id, m.title AS title",
        {"id": "m0000"},
        ("Movie.id", "Movie.source_element_id", "Movie.title"),
        classification="explicit compatibility probe",
    ),
    Case(
        "unordered",
        "Unordered result contract",
        "MATCH (m:Movie) WHERE m.released = $year RETURN m.id AS id",
        {"year": 2000},
        ("Movie.id", "Movie.released"),
        ordered=False,
    ),
    Case(
        "nondeterministic",
        "Limit without ordering needs a contract",
        "MATCH (m:Movie) RETURN m.title AS title LIMIT 3",
        dependencies=("Movie.title",),
        nondeterministic=True,
    ),
]

REPAIRS = {
    "preserve_source_id": {
        "title": "Use the explicitly preserved source ID",
        "query": "MATCH (m:Movie {id:$id}) RETURN m.source_element_id AS external_id, m.title AS title",
        "changes": ["Movie.source_element_id"],
        "precondition": "Movie.source_element_id was captured from Neo4j elementId before export",
    },
    "drop_identity": {
        "title": "Discard the unsupported field (negative-control candidate)",
        "query": "MATCH (m:Movie {id:$id}) RETURN m.title AS title",
        "changes": ["Movie.source_element_id"],
        "precondition": "Deliberately incorrect: removing a field violates the output contract",
    },
}


REPAIRS.update(
    {
        "restore_role_arrays": {
            "title": "Restore typed role arrays from the source manifest",
            "changes": ["ACTED_IN.roles"],
            "precondition": "Every source edge has a unique exported source_edge_id and a typed role list",
        },
        "preserve_parallel_edges": {
            "title": "Count relationships before projecting duplicate names",
            "query": "MATCH (m:Movie {id:$id})<-[r:ACTED_IN]-(p:Person) WITH p.name AS name, count(r) AS n UNWIND range(1,n) AS occurrence RETURN name ORDER BY name",
            "changes": ["ACTED_IN"],
            "precondition": "The declared contract preserves duplicate relationship occurrences",
        },
        "count_relationships": {
            "title": "Count relationship identities in the aggregate",
            "query": "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) RETURN p.id AS id, count(r) AS appearances ORDER BY appearances DESC, id LIMIT 10",
            "changes": ["ACTED_IN"],
            "precondition": "Appearances counts edges including parallel edges, not distinct movies",
        },
    }
)
REPAIR_CASE = {
    "preserve_source_id": "identity",
    "drop_identity": "identity",
    "preserve_parallel_edges": "duplicates",
    "count_relationships": "aggregate",
}
