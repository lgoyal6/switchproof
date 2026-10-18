// Planner evidence comes from failed validations, not a parallel in-memory failure list.
MATCH (r:Run {id:$run})-[:HAS_QUERY]->(q:Query)-[:HAS_RESULT]->(v:Validation)
WHERE v.stage='baseline' AND v.status <> 'passed' AND v.status <> 'inconclusive'
OPTIONAL MATCH (q)-[:READS]->(s:SchemaElement)
RETURN q.case_id AS case_id, q.text AS query, v.evidence AS evidence,
       collect(s.name) AS dependencies ORDER BY case_id;

// Affected readers for a candidate's changed schema elements.
MATCH (r:Run {id:$run})-[:HAS_QUERY]->(q:Query)-[:READS]->(s:SchemaElement)
WHERE s.name IN $names
RETURN DISTINCT q.case_id AS case_id ORDER BY case_id;

// Memory lookup. Its output still requires current preconditions and executable checks.
MATCH (p:Recipe {key:$key, status:'verified'})
RETURN p.repair AS repair, p.origin_run AS origin_run, p.precondition AS precondition;
