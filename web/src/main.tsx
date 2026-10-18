import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowRight,
  Check,
  ChevronRight,
  Download,
  FlaskConical,
  GitBranch,
  Layers,
  LoaderCircle,
  Network,
  Play,
  ShieldCheck,
  X,
} from "lucide-react";
import "./style.css";

type Row = {
  classification: string;
  case_id: string;
  title: string;
  status: string;
  source_query: string;
  target_query: string;
  error?: string;
  reason?: string;
  diff?: Record<string, unknown>;
  expected_hash?: string;
  actual_hash?: string;
  expected_rows?: number;
  actual_rows?: number;
  dependencies: string[];
};
type Candidate = {
  repair_id: string;
  title: string;
  status: string;
  affected_cases: string[];
  checks: Row[];
};
type Timing = {
  case_id: string;
  title: string;
  neo4j: { p50_ms: number };
  falkordb: { p50_ms: number };
  ratio_neo4j_over_falkor: number | null;
};
type Graph = {
  nodes: { id: string; label: string; type: string; status?: string }[];
  edges: { source: string; target: string }[];
};
type Report = {
  id: string;
  status: string;
  created_at: string;
  stage: string;
  error?: string;
  fixture?: { movies: number; people: number; relationships: number };
  baseline?: Row[];
  final?: Row[];
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
    checks: { endpoint: string; status: string }[];
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
  dependency_graph?: Graph;
  receipt_hash?: string;
  scope?: string;
};
const isActive = (s: string) => ["queued", "running"].includes(s);
const stages = [
  "fixture",
  "migration",
  "baseline",
  "planning",
  "candidates",
  "verification",
  "benchmark",
  "complete",
];
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const r = await fetch(path, options);
  if (!r.ok) {
    const b = await r.json();
    throw Error(b.detail || "Request failed");
  }
  return r.json();
}
function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`}>
      {["verified", "passed", "accepted"].includes(status) ? (
        <Check size={12} />
      ) : ["rejected", "error", "mismatch"].includes(status) ? (
        <X size={12} />
      ) : null}
      {status.replaceAll("_", " ")}
    </span>
  );
}
function GraphView({
  graph,
  selected,
  onSelect,
}: {
  graph: Graph;
  selected: string;
  onSelect: (s: string) => void;
}) {
  const positions = new Map<string, { x: number; y: number }>(),
    h = Math.max(
      480,
      graph.nodes.filter((n) => n.type === "schema").length * 38 + 50,
    );
  for (const [type, x] of [
    ["query", 18],
    ["schema", 340],
    ["candidate", 660],
  ] as const) {
    const list = graph.nodes.filter((n) => n.type === type);
    list.forEach((n, i) =>
      positions.set(n.id, {
        x,
        y: 28 + (i * (h - 56)) / Math.max(list.length - 1, 1),
      }),
    );
  }
  const relevant = new Set(
    graph.edges
      .filter((e) => e.source === "query:" + selected)
      .map((e) => e.target),
  );
  return (
    <div className="graph-wrap">
      <div className="graph-columns">
        <span>Workload queries</span>
        <span>Shared schema</span>
        <span>Tested repairs</span>
      </div>
      <svg
        viewBox={`0 0 920 ${h}`}
        role="img"
        aria-label="Query dependencies and repair evidence"
      >
        {graph.edges.map((e, i) => {
          const a = positions.get(e.source),
            b = positions.get(e.target);
          if (!a || !b) return null;
          const startX = a.x < b.x ? a.x + 220 : a.x;
          const endX = a.x < b.x ? b.x : b.x + 220;
          const middle = (startX + endX) / 2;
          return (
            <path
              key={i}
              d={`M${startX},${a.y + 13} C${middle},${a.y + 13} ${middle},${b.y + 13} ${endX},${b.y + 13}`}
              className={
                e.source === "query:" + selected ||
                (e.source.startsWith("repair:") && relevant.has(e.target))
                  ? "edge lit"
                  : "edge"
              }
            />
          );
        })}
        {graph.nodes.map((n) => {
          const p = positions.get(n.id);
          if (!p) return null;
          const selectedNode =
            n.id === "query:" + selected || relevant.has(n.id);
          return (
            <g
              key={n.id}
              transform={`translate(${p.x},${p.y})`}
              className={`graph-node ${n.type} ${n.status || ""} ${selectedNode ? "chosen" : ""}`}
              onClick={() => n.type === "query" && onSelect(n.id.slice(6))}
              role={n.type === "query" ? "button" : undefined}
              tabIndex={n.type === "query" ? 0 : undefined}
              onKeyDown={(e) => {
                if (n.type === "query" && ["Enter", " "].includes(e.key)) {
                  e.preventDefault();
                  onSelect(n.id.slice(6));
                }
              }}
              aria-label={n.label}
            >
              <title>
                {n.label}
                {n.status ? `, ${n.status}` : ""}
              </title>
              <rect width="220" height="27" rx="5" />
              <text x="10" y="18">
                {n.label.length > 30 ? n.label.slice(0, 28) + "…" : n.label}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="muted">
        Select a query to trace the schema dependencies used by the planner and
        verifier.
      </p>
    </div>
  );
}
function App() {
  const [runs, setRuns] = useState<Report[]>([]),
    [id, setId] = useState(""),
    [report, setReport] = useState<Report | null>(null),
    [error, setError] = useState(""),
    [selected, setSelected] = useState("identity"),
    [tab, setTab] = useState("Investigation"),
    [starting, setStarting] = useState(false),
    [memory, setMemory] = useState(true),
    [provider, setProvider] = useState("anthropic");
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const data = await api<Report[]>("/api/runs");
        if (!cancelled) {
          setRuns(data);
          setId((current) => current || data[0]?.id || "");
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void refresh();
    const timer = setInterval(refresh, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const data = await api<Report>("/api/runs/" + id);
        if (!cancelled) setReport(data);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void refresh();
    const timer = setInterval(refresh, 1500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [id]);
  async function start() {
    setStarting(true);
    setError("");
    try {
      const data = await api<Report>("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, use_memory: memory }),
      });
      setId(data.id);
      setReport(data);
      setTab("Investigation");
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  }
  const baseline = report?.baseline || [],
    final = report?.final || [],
    before = baseline.find((r) => r.case_id === selected),
    after = final.find((r) => r.case_id === selected),
    passed = final.filter((r) => r.status === "passed").length,
    failures = baseline.filter((r) =>
      ["error", "mismatch"].includes(r.status),
    ).length;
  const working =
    starting ||
    (!!report && isActive(report.status)) ||
    runs.some((r) => isActive(r.status));
  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="/">
          <span className="brand-symbol">
            <GitBranch size={22} />
          </span>
          SwitchProof
        </a>
        <div className="workspace">
          <span className="tiny-dot" />
          Local migration lab
        </div>
        <div className="nav-label">Workspace</div>
        <div className="nav-item current">
          <FlaskConical size={17} />
          Investigations<span>{runs.length}</span>
        </div>
        <div className="sidebar-heading">Recent investigations</div>
        <div className="run-list">
          {runs.map((r) => (
            <button
              key={r.id}
              className={`run ${id === r.id ? "selected" : ""}`}
              onClick={() => {
                setId(r.id);
                setReport(null);
              }}
            >
              <span className={`run-dot ${r.status}`} />
              <span>
                <strong>Movies migration</strong>
                <small>
                  {new Date(r.created_at).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}{" "}
                  / {r.id.slice(0, 6)}
                </small>
              </span>
              <ChevronRight size={14} />
            </button>
          ))}
        </div>
        <div className="sidebar-footer">
          <Network size={19} />
          <div>
            Powered by FalkorDB
            <small>Dependencies, trials, verified memory</small>
          </div>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            Workspace <ChevronRight size={13} /> Investigations{" "}
            <ChevronRight size={13} />
            <b>{id ? id.slice(0, 6) : "New investigation"}</b>
          </div>
          <span className="local-label">
            <span className="tiny-dot" />
            Local environment
          </span>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <div className="route-label">
                Neo4j <ArrowRight size={15} /> FalkorDB
              </div>
              <h1>
                Make the switch.
                <br />
                Keep the behavior.
              </h1>
              <p>
                Find what breaks, test the repair, and take the evidence with
                you.
              </p>
            </div>
            <div className="run-controls">
              <button className="primary" onClick={start} disabled={working}>
                {working ? (
                  <LoaderCircle size={16} className="spin" />
                ) : (
                  <Play size={15} />
                )}{" "}
                {working ? "Investigation running" : "Run investigation"}
              </button>
              <label>
                <input
                  type="checkbox"
                  checked={memory}
                  onChange={(e) => setMemory(e.target.checked)}
                />{" "}
                Reuse verified graph memory
              </label>
              <label className="provider-label">
                Live planner
                <select
                  aria-label="Live planner provider"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                >
                  <option value="zero">Zero / Kimi</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </label>
            </div>
          </div>
          {error && (
            <div className="error-panel" role="alert">
              {error}
              <button onClick={() => setError("")} aria-label="Dismiss error">
                <X size={16} />
              </button>
            </div>
          )}
          <div className="fixture-bar">
            <Layers size={17} />
            <b>Neo4j Movies</b>
            <span>Open-source application</span>
            <i />
            {report?.fixture ? (
              <span>
                {report.fixture.movies} movies / {report.fixture.people} people
                / {report.fixture.relationships} edges
              </span>
            ) : (
              <span>Reproducible synthetic fixture</span>
            )}
            <span className="fixture-note">Disposable local databases</span>
          </div>
          {!report ? (
            <section className="empty">
              <GitBranch size={40} />
              <h2>
                {id
                  ? "Loading evidence"
                  : "Your migration deserves a test run."}
              </h2>
              <p>
                Run the Movies workload on both engines, investigate differences
                through the dependency graph, and verify repairs in disposable
                graph copies.
              </p>
              <p className="muted">
                A new investigation recreates only the local SwitchProof source
                fixture.
              </p>
            </section>
          ) : (
            <>
              <div className="report-line">
                <div>
                  <b>Investigation {report.id.slice(0, 6)}</b>
                  <Badge status={report.status} />
                </div>
                <a
                  className="text-button"
                  href={`/api/runs/${report.id}/receipt`}
                  download
                >
                  <Download size={15} /> Export evidence
                </a>
              </div>
              {isActive(report.status) && (
                <div className="progress" role="status">
                  <LoaderCircle className="spin" size={17} />
                  <div>
                    <b>{report.stage}</b>
                    <span>
                      {report.events?.at(-1)?.message ||
                        "Preparing local investigation"}
                    </span>
                  </div>
                  <div className="steps">
                    {stages.map((s, i) => (
                      <span
                        key={s}
                        className={
                          i <= stages.indexOf(report.stage) ? "filled" : ""
                        }
                      />
                    ))}
                  </div>
                </div>
              )}
              {report.error && (
                <div className="error-panel" role="alert">
                  <div>
                    <b>Investigation stopped</b>
                    <p>{report.error}</p>
                    <span>
                      Check the databases or selected model provider, then start
                      a new investigation.
                    </span>
                  </div>
                </div>
              )}
              <div className="findings-strip">
                <div>
                  <span>Before the switch</span>
                  <strong>
                    {baseline.length ? failures : "-"}{" "}
                    <small>differences found</small>
                  </strong>
                </div>
                <ArrowRight size={24} />
                <div>
                  <span>After verified repairs</span>
                  <strong>
                    {final.length ? passed : "-"} <small>checks passed</small>
                  </strong>
                </div>
                <div className="boundary">
                  <ShieldCheck size={21} />
                  <span>
                    Evidence has boundaries.
                    <small>
                      {final.length
                        ? "1 nondeterministic check stays inconclusive."
                        : "Correctness gates run before performance tests."}
                    </small>
                  </span>
                </div>
              </div>
              <nav className="tabs" aria-label="Evidence sections">
                {[
                  "Investigation",
                  "Dependency graph",
                  "Application",
                  "Performance",
                  "Activity",
                ].map((t) => (
                  <button
                    key={t}
                    aria-current={tab === t ? "page" : undefined}
                    className={tab === t ? "active" : ""}
                    onClick={() => setTab(t)}
                  >
                    {t}
                  </button>
                ))}
              </nav>
              {tab === "Investigation" && (
                <>
                  <div className="investigation-grid">
                    <section className="workload-panel">
                      <div className="section-heading">
                        <h2>Workload checks</h2>
                        <span>{baseline.length} cases</span>
                      </div>
                      <div className="table-head">
                        <span>Query</span>
                        <span>Baseline / final</span>
                      </div>
                      {baseline.map((row) => (
                        <button
                          className={`case-row ${selected === row.case_id ? "chosen" : ""}`}
                          key={row.case_id}
                          aria-label={`${row.title}, baseline ${row.status}, final ${final.find((r) => r.case_id === row.case_id)?.status || "pending"}`}
                          onClick={() => setSelected(row.case_id)}
                        >
                          <div>
                            <span className={`case-icon ${row.status}`}>
                              {row.status === "passed" ? (
                                <Check size={13} />
                              ) : row.status === "inconclusive" ? (
                                "?"
                              ) : (
                                <X size={13} />
                              )}
                            </span>
                            <span>
                              {row.title}
                              <small>{row.classification}</small>
                            </span>
                          </div>
                          <div>
                            <span
                              className={`status-dot ${row.status}`}
                              title={row.status}
                            />
                            <ArrowRight size={12} />
                            <span
                              className={`status-dot ${final.find((r) => r.case_id === row.case_id)?.status || "pending"}`}
                              title={
                                final.find((r) => r.case_id === row.case_id)
                                  ?.status || "pending"
                              }
                            />
                          </div>
                        </button>
                      ))}
                    </section>
                    <section className="detail-panel">
                      <div className="section-heading">
                        <div>
                          <span className="detail-kicker">Selected query</span>
                          <h2>
                            {before?.title || "Waiting for workload results"}
                          </h2>
                        </div>
                        {after && <Badge status={after.status} />}
                      </div>
                      {before && (
                        <>
                          <div className="query-label">Neo4j source</div>
                          <pre className="query-code">
                            {before.source_query}
                          </pre>
                          <div className="query-label target">
                            FalkorDB{" "}
                            {after && after.target_query !== before.source_query
                              ? "verified candidate"
                              : "query"}
                          </div>
                          <pre
                            className={`query-code ${after && after.target_query !== before.source_query ? "repaired" : ""}`}
                          >
                            {after?.target_query || before.target_query}
                          </pre>
                          <div className="evidence-callout">
                            <GitBranch size={16} />
                            <div>
                              <b>Graph-selected dependencies</b>
                              <p>{before.dependencies.join(", ")}</p>
                              <button
                                onClick={() => setTab("Dependency graph")}
                              >
                                Trace in dependency graph{" "}
                                <ArrowRight size={13} />
                              </button>
                            </div>
                          </div>
                          <div className="result-detail">
                            <b>
                              {before.error
                                ? "Baseline error"
                                : before.reason
                                  ? "Contract boundary"
                                  : "Baseline comparison"}
                            </b>
                            <p>
                              {before.error ||
                                before.reason ||
                                (before.status === "passed"
                                  ? "Source and target agree for this case."
                                  : "Source and target results differ. Inspect the typed comparison below.")}
                            </p>
                            {before.diff &&
                              Object.keys(before.diff).length > 0 && (
                                <details>
                                  <summary>Inspect baseline difference</summary>
                                  <pre>
                                    {JSON.stringify(before.diff, null, 2)}
                                  </pre>
                                </details>
                              )}
                            {after?.expected_hash && (
                              <div className="hash-result">
                                <span>
                                  Source SHA-256{" "}
                                  <code>
                                    {after.expected_hash.slice(0, 18)}
                                  </code>
                                </span>
                                <span>
                                  Target SHA-256{" "}
                                  <code>{after.actual_hash?.slice(0, 18)}</code>
                                </span>
                                <span>
                                  {after.expected_rows} expected /{" "}
                                  {after.actual_rows} actual rows
                                </span>
                              </div>
                            )}
                          </div>
                        </>
                      )}
                    </section>
                  </div>
                  <section className="candidate-section">
                    <div className="section-heading">
                      <h2>Repairs must earn a pass</h2>
                      <span>Disposable graph trials</span>
                    </div>
                    {report.candidates?.map((c) => (
                      <div className="candidate-row" key={c.repair_id}>
                        <span className={`candidate-icon ${c.status}`}>
                          {c.status === "accepted" ? (
                            <Check size={16} />
                          ) : (
                            <X size={16} />
                          )}
                        </span>
                        <div>
                          <b>{c.title}</b>
                          <p>
                            {c.affected_cases.length} graph-linked readers /{" "}
                            {c.checks.length} candidate checks
                          </p>
                        </div>
                        <Badge status={c.status} />
                      </div>
                    )) || (
                      <p className="muted">
                        Candidates appear after the live planner selects
                        repairs.
                      </p>
                    )}
                  </section>
                  {report.planner && (
                    <section className="planner-note">
                      <Network size={24} />
                      <div>
                        <h3>
                          {report.planner.provider === "verified graph memory"
                            ? "Retrieved from verified graph memory"
                            : report.planner.provider.includes("test")
                              ? "Integration test decision, no live model"
                              : "A live model proposes. The verifier decides."}
                        </h3>
                        <p>{report.planner.decision.reasoning}</p>
                        <small>
                          {report.planner.provider} {report.planner.model || ""}
                          {report.planner.origin_run
                            ? ` / Origin ${report.planner.origin_run} / ${report.planner.revalidated ? "Revalidated on this run" : "Awaiting revalidation"}`
                            : ""}
                        </small>
                      </div>
                    </section>
                  )}
                </>
              )}
              {tab === "Dependency graph" &&
                (report.dependency_graph ? (
                  <section className="section-card">
                    <div className="section-heading">
                      <h2>Why this query needs this repair</h2>
                      <span>Live FalkorDB investigation graph</span>
                    </div>
                    <GraphView
                      graph={report.dependency_graph}
                      selected={selected}
                      onSelect={setSelected}
                    />
                  </section>
                ) : (
                  <div className="empty">
                    <Network size={35} />
                    <h2>Graph evidence is being collected</h2>
                    <p>
                      The traversal supplies planner evidence and selects
                      affected readers.
                    </p>
                  </div>
                ))}
              {tab === "Application" &&
                (report.application ? (
                  <section className="section-card">
                    <div className="section-heading">
                      <h2>The original Movies HTTP handlers</h2>
                      <Badge status={report.application.status} />
                    </div>
                    <p className="intro">{report.application.adapter}</p>
                    {report.application.checks.map((c, i) => (
                      <div className="endpoint-row" key={i}>
                        <code>{c.endpoint}</code>
                        <Badge status={c.status} />
                      </div>
                    ))}
                    <p className="scope">{report.application.contract}</p>
                    <div className="lifecycle">
                      <Layers size={23} />
                      <div>
                        <b>Disposable graph copies</b>
                        <p>
                          {report.lifecycle?.status === "passed"
                            ? "Changing one copy left the second copy and original unchanged."
                            : "Isolation check not verified."}
                        </p>
                        <small>
                          {report.lifecycle?.copy_ms?.toFixed(2)} ms for two
                          graph copies in one process. Not a security isolation
                          claim.
                        </small>
                      </div>
                    </div>
                  </section>
                ) : (
                  <div className="empty">
                    <FlaskConical size={35} />
                    <h2>Application checks follow candidate validation</h2>
                    <p>
                      Search, cast, graph and vote run against the pinned
                      upstream handlers.
                    </p>
                  </div>
                ))}
              {tab === "Performance" &&
                (report.benchmark ? (
                  <section className="section-card">
                    <div className="section-heading">
                      <h2>Measure the workload that passed</h2>
                      <Badge status={report.benchmark.status} />
                    </div>
                    <p className="intro">{report.benchmark.scope}</p>
                    <div className="benchmark-legend">
                      <span>
                        <i className="neo-color" />
                        Neo4j
                      </span>
                      <span>
                        <i className="falkor-color" />
                        FalkorDB
                      </span>
                      <span>Median client latency in milliseconds</span>
                    </div>
                    {report.benchmark.queries.map((q) => {
                      const max = Math.max(
                        q.neo4j.p50_ms,
                        q.falkordb.p50_ms,
                        0.001,
                      );
                      return (
                        <div className="benchmark-row" key={q.case_id}>
                          <div>
                            <b>{q.title}</b>
                            <small>
                              {q.ratio_neo4j_over_falkor
                                ? `${q.ratio_neo4j_over_falkor.toFixed(2)}x Neo4j / FalkorDB median`
                                : "No valid ratio"}
                            </small>
                          </div>
                          <div className="bars">
                            <div>
                              <span
                                className="neo-bar"
                                style={{
                                  width: `${(q.neo4j.p50_ms / max) * 80}%`,
                                }}
                              />
                              <code>{q.neo4j.p50_ms?.toFixed(3)}</code>
                            </div>
                            <div>
                              <span
                                className="falkor-bar"
                                style={{
                                  width: `${(q.falkordb.p50_ms / max) * 80}%`,
                                }}
                              />
                              <code>{q.falkordb.p50_ms?.toFixed(3)}</code>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                    <p className="scope">
                      {report.benchmark.runner}. {report.benchmark.warmups}{" "}
                      warmups and {report.benchmark.repeats} measured samples
                      per query and engine. Alternating order, same Docker
                      resource limits. Raw samples, p95, errors and memory
                      snapshot are in the receipt.
                    </p>
                  </section>
                ) : (
                  <div className="empty">
                    <ShieldCheck size={35} />
                    <h2>Correctness comes first</h2>
                    <p>
                      Measurements begin only after the workload and application
                      checks pass.
                    </p>
                  </div>
                ))}
              {tab === "Activity" && (
                <section className="section-card">
                  <h2>Investigation activity</h2>
                  {report.events?.map((e, i) => (
                    <div className="activity-row" key={i}>
                      <span>{new Date(e.time).toLocaleTimeString()}</span>
                      <b>{e.stage}</b>
                      <p>{e.message}</p>
                    </div>
                  ))}
                  {report.receipt_hash && (
                    <p className="scope">
                      Receipt SHA-256: <code>{report.receipt_hash}</code>
                    </p>
                  )}
                </section>
              )}
              <footer className="report-footer">
                <ShieldCheck size={15} />
                {report.scope ||
                  "Finite checks on a declared workload. Unverified behavior is never marked as passing."}
              </footer>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
