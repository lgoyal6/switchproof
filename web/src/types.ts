export type CheckResult = {
  status: string;
  expected_rows?: number;
  actual_rows?: number;
  expected_hash?: string;
  actual_hash?: string;
  expected_sample?: unknown[];
  actual_sample?: unknown[];
  diff?: Record<string, unknown>;
};
export type QueryCheck = CheckResult & {
  classification: string;
  case_id: string;
  title: string;
  source_query: string;
  target_query: string;
  error?: string;
  reason?: string;
  params: Record<string, unknown>;
  dependencies: string[];
  expected?: unknown[];
  actual?: unknown[];
};
export type Candidate = {
  repair_id: string;
  title: string;
  status: string;
  affected_cases: string[];
  checks: QueryCheck[];
};
export type DependencyGraph = {
  nodes: { id: string; label: string; type: string; status?: string }[];
  edges: { source: string; target: string; label?: string }[];
};
export type Timing = {
  case_id: string;
  title: string;
  neo4j: { p50_ms: number | null; p95_ms: number | null; count: number };
  falkordb: { p50_ms: number | null; p95_ms: number | null; count: number };
  ratio_neo4j_over_falkor: number | null;
};
export type Report = {
  id: string;
  status: string;
  created_at: string;
  stage: string;
  error?: string;
  fixture?: { movies: number; people: number; relationships: number };
  baseline?: QueryCheck[];
  final?: QueryCheck[];
  candidates?: Candidate[];
  events?: { time: string; stage: string; message: string }[];
  planner?: {
    provider: string;
    model?: string;
    origin_run?: string;
    revalidated?: boolean;
    decision: { reasoning: string };
  };
  application?: {
    status: string;
    checks: (CheckResult & { endpoint: string })[];
    adapter: string;
    contract: string;
  };
  lifecycle?: { status: string; copy_ms: number };
  benchmark?: {
    status: string;
    scope: string;
    runner: string;
    queries: Timing[];
    repeats: number;
    warmups: number;
  };
  dependency_graph?: DependencyGraph;
  receipt_hash?: string;
  scope?: string;
};
