import type { DependencyGraph } from "./types";

export function GraphView({
  graph,
  selected,
  onSelect,
}: {
  graph: DependencyGraph;
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
              <rect width="220" height="27" rx="4" />
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
