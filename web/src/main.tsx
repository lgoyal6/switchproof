import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowRight,
  Check,
  CheckCheck,
  ChevronDown,
  CircleHelp,
  Clock3,
  Copy,
  Database,
  Download,
  ExternalLink,
  FileCode2,
  GitBranch,
  History,
  Info,
  LoaderCircle,
  Network,
  Play,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { GraphView } from "./GraphView";
import type { QueryCheck, Report } from "./types";
import "./style.css";

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
const tabs = [
  "Query review",
  "Application",
  "Dependency graph",
  "Performance",
  "Run log",
] as const;
type Tab = (typeof tabs)[number];
type Filter = "all" | "changed" | "unverified";
const failed = (r: QueryCheck) => ["error", "mismatch"].includes(r.status);
const date = (value: string) =>
  new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const r = await fetch(path, options);
  if (!r.ok) {
    const body = await r.json();
    throw Error(body.detail || "Request failed");
  }
  return r.json();
}
function explainFailure(error: string) {
  if (/ValidationError|structured|reasoning/i.test(error))
    return "The planner returned an invalid repair decision. This run stopped before accepting repairs. Start a new run to try again.";
  if (/auth|api.key|401/i.test(error))
    return "The repair planner could not authenticate. Check the configured provider credentials and start a new run.";
  if (/Another process|already using/i.test(error))
    return "Another investigation is using the source database. Wait for it to finish, then run the demo again.";
  return "An investigation step failed. Review the technical details below before starting another run.";
}
function Status({ status, label }: { status: string; label?: string }) {
  const good = ["passed", "verified", "accepted"].includes(status);
  return (
    <span className={`status ${status}`}>
      {good ? (
        <Check size={13} />
      ) : ["error", "mismatch", "rejected"].includes(status) ? (
        <X size={13} />
      ) : (
        <span className="status-dot" />
      )}
      {label || status.replaceAll("_", " ")}
    </span>
  );
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <FileCode2 size={28} />
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
function QueryCode({
  query,
  kind,
  label,
}: {
  query: string;
  kind: string;
  label: string;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const formatted = query.replace(
    /\b(WHERE|RETURN|ORDER BY|LIMIT|WITH|UNWIND|SET)\b/g,
    "\n$1",
  );
  async function copy() {
    try {
      await navigator.clipboard.writeText(query);
      setCopied(true);
      setCopyError(false);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopyError(true);
    }
  }
  return (
    <div className={`code-pane ${kind}`}>
      <div className="code-heading">
        <span>
          <span className="engine-dot" />
          {label}
        </span>
        <button
          className="icon-button"
          onClick={copy}
          aria-label={`Copy ${label} query`}
          title={copied ? "Copied" : "Copy query"}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
      <div className="code-lines">
        {formatted.split("\n").map((line, i) => (
          <div className="code-line" key={i}>
            <span className="line-number" aria-hidden="true">
              {i + 1}
            </span>
            <code>
              {line
                .split(
                  /(\b(?:MATCH|WHERE|RETURN|AS|ORDER BY|LIMIT|WITH|UNWIND|SET|DISTINCT|DESC|IN)\b|\$\w+|'[^']*')/g,
                )
                .map((token, j) => (
                  <span
                    key={j}
                    className={
                      token.startsWith("$")
                        ? "code-param"
                        : token.startsWith("'")
                          ? "code-string"
                          : /^(MATCH|WHERE|RETURN|AS|ORDER BY|LIMIT|WITH|UNWIND|SET|DISTINCT|DESC|IN)$/.test(
                                token,
                              )
                            ? "code-keyword"
                            : undefined
                    }
                  >
                    {token}
                  </span>
                ))}
            </code>
          </div>
        ))}
      </div>
      {(copied || copyError) && (
        <div className="copy-feedback" role="status">
          {copied
            ? "Query copied"
            : "Could not copy. Select the query text to copy it."}
        </div>
      )}
    </div>
  );
}
const explanations: Record<string, { problem: string; fix: string }> = {
  identity: {
    problem:
      "FalkorDB cannot execute elementId(). The application still needs the original external ID.",
    fix: "Read the source ID captured before export. The query keeps the same fields and values.",
  },
  cast: {
    problem:
      "The CSV import changed ordered role arrays into strings. Splitting on semicolons would corrupt role names.",
    fix: "Restore exact role arrays from the typed source manifest, matched by unique edge ID.",
  },
  duplicates: {
    problem:
      "The imported query omitted a repeated appearance from a parallel relationship.",
    fix: "Count relationship occurrences before projecting names, preserving duplicate results.",
  },
  aggregate: {
    problem:
      "The aggregate undercounted appearances when an actor had parallel relationships to a movie.",
    fix: "Count relationship identities so every appearance is included.",
  },
};
function QueryInspector({
  before,
  after,
  report,
  onGraph,
}: {
  before: QueryCheck;
  after?: QueryCheck;
  report: Report;
  onGraph: () => void;
}) {
  const [view, setView] = useState<"query" | "results">("query");
  const affected =
    report.candidates?.filter(
      (c) =>
        c.status === "accepted" &&
        c.checks.some((check) => check.case_id === before.case_id),
    ) || [];
  const explanation = explanations[before.case_id];
  const changed = failed(before);
  const hashMatch =
    !!after?.expected_hash && after.expected_hash === after.actual_hash;
  const params = Object.entries(before.params || {});
  return (
    <section className="inspector" aria-label="Selected query">
      <div className="inspector-heading">
        <div>
          <span className="query-id">{before.case_id}.cypher</span>
          <h2>{before.title}</h2>
        </div>
        {after && (
          <Status
            status={after.status}
            label={
              after.status === "passed"
                ? "Verified"
                : after.status === "inconclusive"
                  ? "Not verified"
                  : undefined
            }
          />
        )}
      </div>
      <div className="inspector-subnav">
        <div role="group" aria-label="Query evidence view">
          <button
            aria-pressed={view === "query"}
            className={view === "query" ? "selected" : ""}
            onClick={() => setView("query")}
          >
            Query comparison
          </button>
          <button
            aria-pressed={view === "results"}
            className={view === "results" ? "selected" : ""}
            onClick={() => setView("results")}
          >
            Result evidence
          </button>
        </div>
        <span>
          {before.classification === "explicit compatibility probe"
            ? "Added compatibility probe"
            : "Movies workload"}
        </span>
      </div>
      {before.reason ? (
        <div className="contract-note">
          <CircleHelp size={20} />
          <div>
            <h3>This query needs a result contract</h3>
            <p>
              {before.reason}. Different engines may return different valid
              subsets. This check cannot establish equivalence.
            </p>
          </div>
        </div>
      ) : (
        <div className={`change-summary ${changed ? "changed" : ""}`}>
          <span className="change-mark">
            {changed ? <GitBranch size={17} /> : <Check size={17} />}
          </span>
          <div>
            <h3>{changed ? "Why a repair was needed" : "No repair needed"}</h3>
            <p>
              {changed
                ? explanation?.problem ||
                  before.error ||
                  "The source and imported results differed."
                : "This query already returned matching results after import."}
            </p>
          </div>
        </div>
      )}
      {view === "query" ? (
        <>
          <div className="query-comparison">
            <QueryCode
              query={before.source_query}
              kind={changed ? "source changed" : "source"}
              label="Neo4j source"
            />
            <QueryCode
              query={after?.target_query || before.target_query}
              kind={after?.status === "passed" ? "target verified" : "target"}
              label="FalkorDB target"
            />
          </div>
          {params.length > 0 && (
            <div className="parameter-line">
              <span>Parameters</span>
              {params.map(([key, value]) => (
                <code key={key}>
                  ${key} = {JSON.stringify(value)}
                </code>
              ))}
            </div>
          )}
          {changed && after?.status === "passed" && (
            <div className="repair-explanation">
              <CheckCheck size={18} />
              <div>
                <b>
                  {before.source_query === after.target_query
                    ? "Data repaired, query unchanged"
                    : "Query repair verified"}
                </b>
                <p>
                  {explanation?.fix || affected.map((c) => c.title).join(". ")}
                </p>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="result-evidence">
          <div className="result-columns">
            <div>
              <h3>Expected from Neo4j</h3>
              <pre>
                {JSON.stringify(
                  before.expected_sample || before.expected?.slice(0, 4) || [],
                  null,
                  2,
                )}
              </pre>
            </div>
            <div>
              <h3>Imported result, before repair</h3>
              {before.error ? (
                <div className="query-error">{before.error}</div>
              ) : (
                <pre>
                  {JSON.stringify(
                    before.actual_sample || before.actual?.slice(0, 4) || [],
                    null,
                    2,
                  )}
                </pre>
              )}
            </div>
          </div>
          {before.diff && Object.keys(before.diff).length > 0 && (
            <details>
              <summary>Typed differences from the baseline</summary>
              <pre>{JSON.stringify(before.diff, null, 2)}</pre>
            </details>
          )}
          <p className="small-note">
            Samples are shown here. The receipt contains the complete query
            results and comparison evidence.
          </p>
        </div>
      )}
      {after?.expected_hash && (
        <div className="verification-line">
          <div>
            <span className={`verification-icon ${hashMatch ? "matched" : ""}`}>
              {hashMatch ? <Check size={14} /> : <X size={14} />}
            </span>
            <b>{hashMatch ? "Result hashes match" : "Result hashes differ"}</b>
            <span>
              {after.expected_rows} expected / {after.actual_rows} actual rows
            </span>
          </div>
          <code title={after.expected_hash}>
            {after.expected_hash.slice(0, 16)}
          </code>
        </div>
      )}
      <div className="dependency-line">
        <Network size={15} />
        <span>Reads {before.dependencies.join(", ")}</span>
        <button onClick={onGraph}>
          Trace dependencies <ArrowRight size={13} />
        </button>
      </div>
    </section>
  );
}
function RunDialog({
  busy,
  provider,
  setProvider,
  memory,
  setMemory,
  onStart,
  onClose,
}: {
  busy: boolean;
  provider: string;
  setProvider: (v: string) => void;
  memory: boolean;
  setMemory: (v: boolean) => void;
  onStart: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="run-dialog"
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="dialog-title">
        <h2>Run the Movies demo</h2>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="Close run settings"
        >
          <X size={19} />
        </button>
      </div>
      <p>
        This recreates the disposable Movies fixture, migrates it to FalkorDB,
        and verifies the workload. Existing reports are kept.
      </p>
      <label className="field-label">
        Repair planner
        <select value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="anthropic">Anthropic / Claude</option>
          <option value="zero">Zero / Kimi</option>
        </select>
      </label>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={memory}
          onChange={(e) => setMemory(e.target.checked)}
        />
        <span>
          Reuse verified repairs
          <small>
            Saved recipes must pass fresh validation. A new model call is used
            when no valid recipe exists.
          </small>
        </span>
      </label>
      <div className="dialog-foot">
        <button className="button secondary" onClick={onClose}>
          Cancel
        </button>
        <button className="button primary" onClick={onStart} disabled={busy}>
          {busy ? (
            <LoaderCircle size={16} className="spin" />
          ) : (
            <Play size={14} />
          )}
          Run investigation
        </button>
      </div>
    </dialog>
  );
}
function App() {
  const [runs, setRuns] = useState<Report[]>([]),
    [id, setId] = useState(""),
    [report, setReport] = useState<Report | null>(null),
    [error, setError] = useState(""),
    [selected, setSelected] = useState("identity"),
    [tab, setTab] = useState<Tab>("Query review"),
    [filter, setFilter] = useState<Filter>("changed"),
    [search, setSearch] = useState(""),
    [starting, setStarting] = useState(false),
    [memory, setMemory] = useState(true),
    [provider, setProvider] = useState("anthropic"),
    [showDialog, setShowDialog] = useState(false),
    [showHelp, setShowHelp] = useState(false);
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
    const interval = setInterval(refresh, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
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
    const interval = setInterval(refresh, 2000);
    return () => {
      cancelled = true;
      clearInterval(interval);
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
      setTab("Query review");
      setShowDialog(false);
      setSelected("identity");
      setFilter("changed");
      setSearch("");
    } catch (e) {
      setError(String(e));
      setShowDialog(false);
    } finally {
      setStarting(false);
    }
  }
  const baseline = report?.baseline || [],
    final = report?.final || [],
    changes = baseline.filter(failed),
    passed = final.filter((r) => r.status === "passed").length,
    inconclusive = baseline.filter((r) => r.status === "inconclusive").length,
    repairs = report?.candidates?.filter((c) => c.status === "accepted") || [],
    appPassed =
      report?.application?.checks.filter((c) => c.status === "passed").length ||
      0;
  const visible = baseline.filter(
    (r) =>
      (filter === "all" ||
        (filter === "changed" && failed(r)) ||
        (filter === "unverified" && r.status === "inconclusive")) &&
      r.title.toLowerCase().includes(search.toLowerCase()),
  );
  const before = visible.find((r) => r.case_id === selected) || visible[0],
    after = final.find((r) => r.case_id === before?.case_id);
  const working =
    starting ||
    runs.some((r) => isActive(r.status)) ||
    (!!report && isActive(report.status));
  const verdict =
    report?.status === "verified"
      ? "Ready for the tested workload"
      : report?.status === "error"
        ? "Investigation stopped"
        : report?.status === "needs_attention"
          ? "This migration needs attention"
          : report?.status === "abstained"
            ? "No supported repair selected"
            : report?.status === "interrupted"
              ? "Investigation interrupted"
              : "Checking compatibility";
  const planner = report?.planner;
  return (
    <>
      <header className="app-header">
        <a className="brand" href="/" aria-label="SwitchProof home">
          <span className="brand-mark">
            <GitBranch size={21} strokeWidth={2} />
          </span>
          SwitchProof
        </a>
        <span className="header-divider" />
        <span className="product-name">Migration workbench</span>
        <div className="header-links">
          <span className="local-indicator">
            <i />
            Demo environment
          </span>
          <button
            className={showHelp ? "help-button active" : "help-button"}
            onClick={() => setShowHelp(!showHelp)}
            aria-expanded={showHelp}
          >
            <CircleHelp size={16} />
            What this does
          </button>
          <a
            href="https://github.com/lgoyal6/switchproof"
            target="_blank"
            rel="noreferrer"
            className="repo-link"
          >
            Source <ExternalLink size={13} />
          </a>
        </div>
      </header>
      {showHelp && (
        <section className="purpose-panel">
          <div>
            <h2>Can this app move to FalkorDB without breaking?</h2>
            <p>
              SwitchProof runs the same workload on Neo4j and FalkorDB, finds
              differences, and tests repairs in disposable graph copies. A graph
              connects each failing query to the data it depends on. The final
              report shows what passed and what remains unverified.
            </p>
          </div>
          <div className="purpose-scope">
            <b>Current scope: the Movies demo</b>
            <p>
              This version uses a frozen synthetic dataset and four supported
              repairs. It does not import arbitrary repositories.
            </p>
          </div>
        </section>
      )}
      <main className="workbench">
        <div className="page-toolbar">
          <div className="breadcrumbs">
            <span>Movies demo</span>
            <span>/</span>
            <span>Compatibility report</span>
          </div>
          <div className="toolbar-actions">
            {runs.length > 0 && (
              <label className="history-picker">
                <History size={14} />
                <select
                  aria-label="Investigation history"
                  value={id}
                  onChange={(e) => {
                    setId(e.target.value);
                    setReport(null);
                    setSelected("identity");
                    setSearch("");
                  }}
                >
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {date(r.created_at)} / {r.id.slice(0, 6)} /{" "}
                      {r.status.replaceAll("_", " ")}
                    </option>
                  ))}
                </select>
                <ChevronDown size={13} />
              </label>
            )}
            {report && (
              <a
                className="button secondary"
                href={`/api/runs/${report.id}/receipt`}
                download
              >
                <Download size={14} />
                Export report
              </a>
            )}
            <button
              className="button primary"
              disabled={working}
              onClick={() => setShowDialog(true)}
            >
              {working ? (
                <LoaderCircle size={15} className="spin" />
              ) : (
                <Play size={13} />
              )}{" "}
              {working ? "Running investigation" : "Run demo"}
            </button>
          </div>
        </div>
        <div className="page-title">
          <div>
            <h1>
              Neo4j <ArrowRight aria-label="to" /> FalkorDB
            </h1>
            <p>Will the Movies app behave the same after migration?</p>
          </div>
          <div className="report-meta">
            {report && (
              <>
                <span>
                  Run <code>{report.id.slice(0, 6)}</code>
                </span>
                <span>{date(report.created_at)}</span>
              </>
            )}
          </div>
        </div>
        <div className="migration-route">
          <div className="database-label">
            <Database size={18} />
            <span>
              <b>Neo4j</b>
              <small>Source</small>
            </span>
          </div>
          <div className="route-connection">
            <span>Export, migrate, compare</span>
            <div>
              <i />
              <ArrowRight size={16} />
            </div>
          </div>
          <div className="database-label target-db">
            <Database size={18} />
            <span>
              <b>FalkorDB</b>
              <small>Target</small>
            </span>
          </div>
          <div className="fixture-details">
            {report?.fixture ? (
              <>
                <b>
                  {report.fixture.movies} movies <span>/</span>{" "}
                  {report.fixture.people} people <span>/</span>{" "}
                  {report.fixture.relationships} edges
                </b>
                <span>Synthetic fixture for the open-source Movies app</span>
              </>
            ) : (
              <>
                <b>Reproducible Movies fixture</b>
                <span>
                  240 movies, 120 people, and a deliberate duplicate edge
                </span>
              </>
            )}
          </div>
          <span className="demo-tag">Demo workload</span>
        </div>
        {error && (
          <div className="error-banner" role="alert">
            <Info size={17} />
            <span>{error}</span>
            <button
              className="icon-button"
              onClick={() => setError("")}
              aria-label="Dismiss error"
            >
              <X size={17} />
            </button>
          </div>
        )}
        {!report ? (
          <Empty
            title={
              id
                ? "Loading the migration report"
                : "Test a migration before making the switch"
            }
          >
            {id
              ? "Reading saved results."
              : "Run the Movies demo to see compatibility differences, verified repairs, application checks, and measured performance."}
          </Empty>
        ) : (
          <>
            <section
              className={`verdict ${report.status}`}
              aria-label="Migration verdict"
            >
              <span className="verdict-icon">
                {report.status === "verified" ? (
                  <CheckCheck size={25} />
                ) : isActive(report.status) ? (
                  <LoaderCircle size={24} className="spin" />
                ) : report.status === "error" ? (
                  <X size={25} />
                ) : (
                  <Info size={25} />
                )}
              </span>
              <div className="verdict-copy">
                <h2>{verdict}</h2>
                <p>
                  {report.status === "verified"
                    ? `${repairs.length} repairs applied. All ${passed} deterministic queries match the source.`
                    : report.error
                      ? explainFailure(report.error)
                      : report.events?.at(-1)?.message ||
                        "Preparing the disposable databases."}
                </p>
                {report.error && (
                  <details className="technical-error">
                    <summary>Technical details</summary>
                    <pre>{report.error}</pre>
                  </details>
                )}
                {report.status === "verified" && (
                  <span>
                    {inconclusive} query{" "}
                    {inconclusive === 1 ? "remains" : "remain"} unverified.
                    Results apply to this fixture and declared workload.
                  </span>
                )}
              </div>
              {final.length > 0 && (
                <div className="verdict-facts">
                  <div>
                    <b>
                      {passed}
                      <span>
                        /
                        {
                          final.filter((r) => r.status !== "inconclusive")
                            .length
                        }
                      </span>
                    </b>
                    <small>Query checks</small>
                  </div>
                  <div>
                    <b>
                      {appPassed}
                      <span>/{report.application?.checks.length || 0}</span>
                    </b>
                    <small>App checks</small>
                  </div>
                  <div className="unverified-fact">
                    <b>{inconclusive}</b>
                    <small>Unverified</small>
                  </div>
                </div>
              )}
            </section>
            {isActive(report.status) && (
              <div className="run-progress" role="status">
                {stages.map((stage, i) => (
                  <span
                    key={stage}
                    className={
                      i < stages.indexOf(report.stage)
                        ? "done"
                        : stage === report.stage
                          ? "current"
                          : ""
                    }
                  >
                    {i < stages.indexOf(report.stage) ? (
                      <Check size={12} />
                    ) : (
                      <i />
                    )}
                    {stage}
                  </span>
                ))}
              </div>
            )}
            <nav className="report-tabs" aria-label="Report sections">
              {tabs.map((t) => (
                <button
                  key={t}
                  className={tab === t ? "active" : ""}
                  aria-current={tab === t ? "page" : undefined}
                  onClick={() => setTab(t)}
                >
                  {t}
                  {t === "Query review" && baseline.length > 0 && (
                    <span>{baseline.length}</span>
                  )}
                  {t === "Application" && report.application && (
                    <span>{report.application.checks.length}</span>
                  )}
                </button>
              ))}
              <span className="tab-note">
                {report.status === "verified" ? (
                  <>
                    <ShieldCheck size={13} />
                    Fresh comparisons required for every repair
                  </>
                ) : (
                  <>
                    <Clock3 size={13} />
                    {report.stage}
                  </>
                )}
              </span>
            </nav>
            {tab === "Query review" && (
              <>
                <div className="query-workspace">
                  <aside className="query-list">
                    <div className="query-list-heading">
                      <h2>Workload queries</h2>
                      <span>{baseline.length} total</span>
                    </div>
                    <div
                      className="query-filters"
                      role="group"
                      aria-label="Filter queries"
                    >
                      {(
                        [
                          {
                            id: "changed",
                            label: "Changed",
                            count: changes.length,
                          },
                          { id: "all", label: "All", count: baseline.length },
                          {
                            id: "unverified",
                            label: "Unverified",
                            count: inconclusive,
                          },
                        ] as const
                      ).map((f) => (
                        <button
                          key={f.id}
                          aria-pressed={filter === f.id}
                          className={filter === f.id ? "active" : ""}
                          onClick={() => setFilter(f.id)}
                        >
                          {f.label} <span>{f.count}</span>
                        </button>
                      ))}
                    </div>
                    <label className="query-search">
                      <Search size={14} />
                      <input
                        aria-label="Search queries"
                        placeholder="Find a query…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                      />
                      {search && (
                        <button
                          className="icon-button"
                          aria-label="Clear query search"
                          onClick={() => setSearch("")}
                        >
                          <X size={13} />
                        </button>
                      )}
                    </label>
                    <div className="query-items">
                      {visible.map((row) => {
                        const result = final.find(
                          (r) => r.case_id === row.case_id,
                        );
                        return (
                          <button
                            className={`query-item ${before?.case_id === row.case_id ? "selected" : ""}`}
                            key={row.case_id}
                            onClick={() => setSelected(row.case_id)}
                            aria-pressed={before?.case_id === row.case_id}
                            aria-label={`${row.title}, baseline ${row.status}, final ${result?.status || "pending"}`}
                          >
                            <FileCode2 className="query-file-icon" size={15} />
                            <span className="query-item-copy">
                              <b>{row.title}</b>
                              <small>
                                {row.status === "inconclusive"
                                  ? "Result contract required"
                                  : failed(row)
                                    ? result?.status === "passed"
                                      ? "Repaired and verified"
                                      : "Repair required"
                                    : "Matched after import"}
                              </small>
                            </span>
                            <span
                              className={`query-state ${result?.status || row.status}`}
                            >
                              {(result?.status || row.status) === "passed" ? (
                                <Check size={13} />
                              ) : row.status === "inconclusive" ? (
                                <CircleHelp size={13} />
                              ) : (
                                <X size={13} />
                              )}
                            </span>
                          </button>
                        );
                      })}
                      {visible.length === 0 && (
                        <p className="no-results">
                          {search
                            ? "No queries match your search."
                            : baseline.length
                              ? "No queries in this filter."
                              : "Queries appear after import."}
                        </p>
                      )}
                    </div>
                    <div className="query-list-footer">
                      <span>
                        <span className="legend-dot green" />
                        Verified
                      </span>
                      <span>
                        <span className="legend-dot amber" />
                        Unverified
                      </span>
                    </div>
                  </aside>
                  {before ? (
                    <QueryInspector
                      key={before.case_id}
                      before={before}
                      after={after}
                      report={report}
                      onGraph={() => setTab("Dependency graph")}
                    />
                  ) : (
                    <Empty title="No query selected">
                      Choose a different filter or clear your search to inspect
                      a query.
                    </Empty>
                  )}
                </div>
                <div className="lower-grid">
                  <section className="repair-ledger">
                    <div className="section-heading">
                      <div>
                        <h2>Repair trials</h2>
                        <p>
                          Every candidate is checked in a disposable graph copy.
                        </p>
                      </div>
                      <span>{report.candidates?.length || 0} tested</span>
                    </div>
                    {report.candidates?.map((c) => (
                      <details
                        className={`repair-trial ${c.status}`}
                        key={c.repair_id}
                      >
                        <summary>
                          <span className="trial-status-icon">
                            {c.status === "accepted" ? (
                              <Check size={15} />
                            ) : (
                              <X size={15} />
                            )}
                          </span>
                          <span>
                            {c.title}
                            <small>
                              {c.checks.length} candidate checks /{" "}
                              {c.affected_cases.length} linked queries
                            </small>
                          </span>
                          <Status status={c.status} />
                          <ChevronDown size={14} />
                        </summary>
                        <div className="trial-body">
                          <p>
                            {c.status === "rejected"
                              ? "This candidate changed the output contract and was refused, even though its query ran."
                              : "The candidate passed its checks, then the complete workload was checked again."}
                          </p>
                          {c.checks.map((c) => (
                            <div key={c.case_id}>
                              <span>{c.title}</span>
                              <Status status={c.status} />
                            </div>
                          ))}
                        </div>
                      </details>
                    ))}
                    {!report.candidates?.length && (
                      <p className="section-placeholder">
                        Repair trials appear after the planner selects a
                        supported action.
                      </p>
                    )}
                  </section>
                  <section className="planner-panel">
                    <div className="section-heading">
                      <div>
                        <h2>
                          {planner?.provider === "verified graph memory"
                            ? "Verified repair memory"
                            : "Repair planner"}
                        </h2>
                        <p>
                          {planner?.provider === "verified graph memory"
                            ? "Retrieved from FalkorDB, checked again on this run."
                            : "The model proposes a repair. Executable checks decide acceptance."}
                        </p>
                      </div>
                      <Network size={19} />
                    </div>
                    {planner ? (
                      <>
                        <div className="planner-provenance">
                          <span className="planner-dot" />
                          <b>
                            {planner.provider === "verified graph memory"
                              ? "Saved recipe"
                              : planner.provider.includes("test")
                                ? "Test decision"
                                : planner.model || planner.provider}
                          </b>
                          <span>
                            {planner.revalidated
                              ? "Revalidated"
                              : planner.provider === "verified graph memory"
                                ? "Pending validation"
                                : planner.provider.includes("test")
                                  ? "Test fixture"
                                  : "Live decision"}
                          </span>
                        </div>
                        <p className="planner-reasoning">
                          {planner.decision.reasoning}
                        </p>
                        {planner.origin_run && (
                          <button
                            className="origin-link"
                            onClick={() => {
                              setId(planner.origin_run!);
                              setReport(null);
                            }}
                          >
                            View original run{" "}
                            <code>{planner.origin_run.slice(0, 6)}</code>
                            <ArrowRight size={13} />
                          </button>
                        )}
                      </>
                    ) : (
                      <p className="section-placeholder">
                        The planner receives failed checks and graph
                        dependencies after baseline comparison.
                      </p>
                    )}
                  </section>
                </div>
              </>
            )}
            {tab === "Dependency graph" && (
              <section className="evidence-section">
                <div className="section-heading">
                  <div>
                    <h2>Follow a query to the repair it needs</h2>
                    <p>
                      Query readers and repair changes meet at shared schema
                      elements in FalkorDB.
                    </p>
                  </div>
                  <span className="section-tag">
                    <Network size={14} />
                    Investigation graph
                  </span>
                </div>
                {report.dependency_graph ? (
                  <GraphView
                    graph={report.dependency_graph}
                    selected={selected}
                    onSelect={(queryId) => {
                      setSelected(queryId);
                      setFilter("all");
                      setSearch("");
                    }}
                  />
                ) : (
                  <Empty title="Dependency evidence is not available yet">
                    The graph supplies planner input and selects affected
                    queries after the baseline run.
                  </Empty>
                )}
              </section>
            )}
            {tab === "Application" && (
              <section className="evidence-section">
                <div className="section-heading">
                  <div>
                    <h2>Does the Movies app still behave the same?</h2>
                    <p>
                      Comparisons run the pinned open-source Flask handlers
                      against both databases.
                    </p>
                  </div>
                  {report.application && (
                    <Status status={report.application.status} />
                  )}
                </div>
                {report.application ? (
                  <>
                    <div className="endpoint-table">
                      <div className="endpoint-table-head">
                        <span>Application behavior</span>
                        <span>Verified contract</span>
                        <span>Result</span>
                      </div>
                      {report.application.checks.map((c, i) => (
                        <div className="endpoint-row" key={i}>
                          <code>{c.endpoint}</code>
                          <span>
                            {c.endpoint === "vote persisted state"
                              ? "Saved vote value"
                              : "HTTP status and response body"}
                          </span>
                          <Status status={c.status} />
                        </div>
                      ))}
                    </div>
                    <div className="application-notes">
                      <div>
                        <h3>What was adapted</h3>
                        <p>{report.application.adapter}.</p>
                        <details>
                          <summary>Comparison contract</summary>
                          <p>{report.application.contract}</p>
                        </details>
                      </div>
                      <div>
                        <h3>Graph copy isolation</h3>
                        <p>
                          {report.lifecycle?.status === "passed"
                            ? "Changing one copy left the other copy and the original unchanged."
                            : "Copy isolation has not been verified."}
                        </p>
                        <small>
                          {report.lifecycle?.copy_ms?.toFixed(2)} ms to create
                          two copies in one FalkorDB process. This is not a
                          security boundary.
                        </small>
                      </div>
                    </div>
                  </>
                ) : (
                  <Empty title="Application checks follow repair validation">
                    Search, cast, graph output, and voting must match before the
                    migration can pass.
                  </Empty>
                )}
              </section>
            )}
            {tab === "Performance" && (
              <section className="evidence-section">
                <div className="section-heading">
                  <div>
                    <h2>How does the verified workload perform?</h2>
                    <p>
                      Measured after correctness passed, using the repaired
                      target queries.
                    </p>
                  </div>
                  {report.benchmark && (
                    <Status status={report.benchmark.status} />
                  )}
                </div>
                {report.benchmark ? (
                  <>
                    <div className="measurement-note">
                      <Info size={16} />
                      <p>{report.benchmark.scope}</p>
                    </div>
                    <div className="performance-scroll">
                      <table className="performance-table">
                        <thead>
                          <tr>
                            <th>Query</th>
                            <th>
                              <span className="legend-dot gray" />
                              Neo4j median
                            </th>
                            <th>
                              <span className="legend-dot blue" />
                              FalkorDB median
                            </th>
                            <th>Neo4j / FalkorDB</th>
                            <th>p95, source / target</th>
                          </tr>
                        </thead>
                        <tbody>
                          {report.benchmark.queries.map((q) => (
                            <tr key={q.case_id}>
                              <td>
                                <b>{q.title}</b>
                                <div className="timing-bars">
                                  <span
                                    className="source-bar"
                                    style={{
                                      width: `${((q.neo4j.p50_ms || 0) / Math.max(q.neo4j.p50_ms || 0, q.falkordb.p50_ms || 0, 0.001)) * 100}%`,
                                    }}
                                  />
                                  <span
                                    className="target-bar"
                                    style={{
                                      width: `${((q.falkordb.p50_ms || 0) / Math.max(q.neo4j.p50_ms || 0, q.falkordb.p50_ms || 0, 0.001)) * 100}%`,
                                    }}
                                  />
                                </div>
                              </td>
                              <td>
                                {q.neo4j.p50_ms?.toFixed(3) ?? "N/A"}{" "}
                                <small>ms</small>
                              </td>
                              <td>
                                {q.falkordb.p50_ms?.toFixed(3) ?? "N/A"}{" "}
                                <small>ms</small>
                              </td>
                              <td>
                                {q.ratio_neo4j_over_falkor?.toFixed(2) ?? "N/A"}
                                {q.ratio_neo4j_over_falkor ? "×" : ""}
                              </td>
                              <td>
                                {q.neo4j.p95_ms?.toFixed(3) ?? "N/A"} /{" "}
                                {q.falkordb.p95_ms?.toFixed(3) ?? "N/A"}{" "}
                                <small>ms</small>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="method-note">
                      {report.benchmark.warmups} warmups,{" "}
                      {report.benchmark.repeats} measured samples per query per
                      engine. Alternating engine order, persistent client
                      connections, and resource limits recorded in the report. A ratio
                      above 1 means a lower FalkorDB median in this recorded run.
                      Raw samples and errors are in the exported report.
                    </p>
                  </>
                ) : (
                  <Empty title="No performance results yet">
                    The complete deterministic workload and application checks
                    must pass before timing begins.
                  </Empty>
                )}
              </section>
            )}
            {tab === "Run log" && (
              <section className="evidence-section">
                <div className="section-heading">
                  <div>
                    <h2>Investigation log</h2>
                    <p>From source fixture to verified evidence.</p>
                  </div>
                  <code className="run-code">{report.id}</code>
                </div>
                <ol className="activity-list">
                  {report.events?.map((e, i) => (
                    <li key={i}>
                      <span className="activity-index">{i + 1}</span>
                      <time dateTime={e.time}>
                        {new Date(e.time).toLocaleTimeString()}
                      </time>
                      <b>{e.stage}</b>
                      <p>{e.message}</p>
                    </li>
                  ))}
                </ol>
                {report.receipt_hash && (
                  <div className="receipt-hash">
                    <span>Report SHA-256</span>
                    <code>{report.receipt_hash}</code>
                  </div>
                )}
              </section>
            )}
            <footer className="report-footer">
              <div>
                <ShieldCheck size={14} />
                <span>
                  {report.status === "verified"
                    ? "Verified for this workload. Not a guarantee for every query or dataset."
                    : "Only completed comparisons can establish compatibility for this workload."}
                </span>
              </div>
              <span>
                FalkorDB stores dependencies, trials, and repair memory.
              </span>
            </footer>
          </>
        )}
      </main>
      {showDialog && (
        <RunDialog
          busy={working}
          provider={provider}
          setProvider={setProvider}
          memory={memory}
          setMemory={setMemory}
          onStart={start}
          onClose={() => setShowDialog(false)}
        />
      )}
    </>
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
