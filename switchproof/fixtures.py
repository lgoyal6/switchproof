from .db import neo_query


def seed_source(movies=240, people=120):
    """Deterministic synthetic data for the real open-source application's schema."""
    neo_query("MATCH (n) DETACH DELETE n")
    movie_rows = [
        {
            "id": f"m{i:04d}",
            "title": f"{'North' if i % 3 == 0 else 'Atlas'} {i:04d}",
            "released": 2000 + i % 24,
            "summary": f"Synthetic migration fixture {i}.",
            "duration": 90 + i % 40,
            "rated": "PG",
            "tagline": "A reproducible journey.",
            "votes": i % 7,
        }
        for i in range(movies)
    ]
    people_rows = [{"id": f"p{i:04d}", "name": f"Performer {i:04d}"} for i in range(people)]
    neo_query("UNWIND $rows AS row CREATE (m:Movie) SET m = row", {"rows": movie_rows})
    neo_query("UNWIND $rows AS row CREATE (p:Person) SET p = row", {"rows": people_rows})
    edges = [
        {
            "movie": f"m{i:04d}",
            "person": f"p{(i + j * 7) % people:04d}",
            "roles": [f"Role {j}", "Ensemble; featured"],
        }
        for i in range(movies)
        for j in range(4)
    ]
    # An intentional duplicate is essential to test multiset semantics.
    edges.append(dict(edges[0]))
    for i, edge in enumerate(edges):
        edge["source_edge_id"] = f"edge-{i}"
    neo_query(
        "UNWIND $rows AS row MATCH (m:Movie {id:row.movie}), (p:Person {id:row.person}) CREATE (p)-[:ACTED_IN {roles:row.roles, source_edge_id:row.source_edge_id}]->(m)",
        {"rows": edges},
    )
    neo_query("MATCH (m:Movie) SET m.source_element_id = elementId(m)")
    neo_query("CREATE INDEX movie_id IF NOT EXISTS FOR (m:Movie) ON (m.id)")
    neo_query("CREATE INDEX person_id IF NOT EXISTS FOR (p:Person) ON (p.id)")
    neo_query("CALL db.awaitIndexes(30)")
    return {"movies": movies, "people": people, "relationships": len(edges), "synthetic": True}
