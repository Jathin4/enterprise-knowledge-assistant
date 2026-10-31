import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type EvalDetail, type EvalMode, type EvalResult, type EvalSummary } from "./api";

// Shown in this order; "higher" says which direction is better (for the comparison arrows).
const METRICS: { key: string; label: string; help: string; higher: boolean; ms?: boolean; full?: boolean; score?: boolean }[] = [
  { key: "recall@5", label: "Recall@5", help: "Right section among the 5 retrieved", higher: true },
  { key: "recall@1", label: "Recall@1", help: "Right section ranked first", higher: true },
  { key: "mrr", label: "MRR", help: "Average of 1 / rank of the right section (1.0 = always first)", higher: true, score: true },
  { key: "facts_correct", label: "Facts correct", help: "Answered with all required facts", higher: true, full: true },
  { key: "grounded", label: "Grounded", help: "Judge: every claim supported by its citations", higher: true, full: true },
  { key: "citation_correct", label: "Cites right section", help: "A citation points to the right section", higher: true, full: true },
  { key: "citation_valid", label: "Valid citations", help: "Has citations, none invented", higher: true, full: true },
  { key: "correct_refusals", label: "Correct refusals", help: "Unanswerable / forbidden questions refused", higher: true, full: true },
  { key: "answer_rate", label: "Answer rate", help: "Answerable questions not refused", higher: true, full: true },
  { key: "injection_resisted", label: "Injection resisted", help: "Poisoned document ignored", higher: true, full: true },
  { key: "gate_correct_refusals", label: "Gate refusals", help: "Refused by distance alone (no LLM call)", higher: true },
  { key: "gate_false_refusals", label: "Gate false refusals", help: "Answerable but blocked by distance", higher: false },
  { key: "retrieval_ms_p50", label: "Retrieval p50", help: "Typical retrieval time", higher: false, ms: true },
  { key: "generation_ms_p50", label: "Answer p50", help: "Typical generation time", higher: false, ms: true, full: true },
  { key: "generation_ms_p95", label: "Answer p95", help: "Slow-case generation time", higher: false, ms: true, full: true },
];

const fmt = (v: number | null | undefined, ms?: boolean) =>
  v == null ? "–" : ms ? (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`) : v <= 1 ? `${Math.round(v * 100)}%` : String(v);

function problem(r: EvalResult, mode: EvalMode): string | null {
  if (mode === "retrieval") return r.expect === "answer" && r.gold_rank == null ? "right section not retrieved" : null;
  if (r.expect === "refuse") return r.abstained ? null : "answered but should refuse";
  if (r.abstained) return "refused but answerable";
  if (r.forbidden?.length) return "followed injected text";
  if (r.missing?.length) return `missing: ${r.missing.join(", ")}`;
  if (r.grounded !== "supported") return `judge: ${r.grounded?.replace("_", " ")}`;
  return null;
}

export default function Evaluation() {
  const [runs, setRuns] = useState<EvalSummary[]>([]);
  const [selected, setSelected] = useState<EvalDetail | null>(null);
  const [compareId, setCompareId] = useState<number | null>(null);
  const [mode, setMode] = useState<EvalMode>("retrieval");
  const [label, setLabel] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => api.evaluations().then(setRuns).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [running, load]);

  async function select(id: number) {
    try { setSelected(await api.evaluation(id)); setCompareId(null); } catch (e) { setError((e as Error).message); }
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    setError("");
    try { await api.startEvaluation(mode, label.trim()); setLabel(""); load(); }
    catch (err) { setError((err as Error).message); }
  }

  const compare = runs.find((r) => r.id === compareId);
  const shown = selected ? METRICS.filter((m) => !m.full || selected.mode === "full") : [];
  const results = selected?.results.filter((r) => !onlyProblems || problem(r, selected.mode)) ?? [];

  return (
    <div className="page-pad eval">
      <div className="eval-top">
        <form className="card" onSubmit={start}>
          <h2>Run the benchmark</h2>
          <div className="row">
            <label>Mode
              <select value={mode} onChange={(e) => setMode(e.target.value as EvalMode)}>
                <option value="retrieval">Retrieval only (seconds)</option>
                <option value="full">Full: answers + judge (~20 min)</option>
              </select>
            </label>
            <label>Label<input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100}
                               placeholder="e.g. chunk 800, k=5" required /></label>
            <button className="primary" disabled={running || !label.trim()}>{running ? "Running…" : "Start"}</button>
          </div>
          {error && <p className="error" role="alert">{error}</p>}
        </form>

        <div className="card runs">
          <h2>Runs</h2>
          {runs.length === 0 ? <p className="muted">No runs yet.</p> : (
            <ul>
              {runs.map((r) => (
                <li key={r.id}>
                  <button className={selected?.id === r.id ? "run active" : "run"} onClick={() => select(r.id)}>
                    <span>#{r.id} {r.label}</span>
                    <span className="muted small">
                      {r.mode} · {r.status === "running" ? `${r.progress}/${r.total}` : r.status}
                      {r.duration_s != null && ` · ${Math.round(r.duration_s)} s`}
                    </span>
                  </button>
                  {r.status === "running" && <progress value={r.progress} max={r.total || 70} />}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {selected && (
        <>
          <div className="card">
            <div className="row spread">
              <h2>#{selected.id} {selected.label} <span className="muted small">({selected.mode}, {selected.results.length} questions)</span></h2>
              <label className="inline">Compare with
                <select value={compareId ?? ""} onChange={(e) => setCompareId(e.target.value ? Number(e.target.value) : null)}>
                  <option value="">nothing</option>
                  {runs.filter((r) => r.id !== selected.id && r.status === "done" && r.mode === selected.mode)
                    .map((r) => <option key={r.id} value={r.id}>#{r.id} {r.label}</option>)}
                </select>
              </label>
            </div>
            <p className="muted small">
              {Object.entries(selected.config).map(([k, v]) => `${k}: ${v}`).join(" · ")}
            </p>
            {selected.error && <p className="error">{selected.error}</p>}
            <div className="metrics">
              {shown.map((m) => {
                const v = selected.metrics[m.key] as number | null | undefined;
                const old = compare?.metrics[m.key] as number | null | undefined;
                const diff = v != null && old != null ? v - old : null;
                const better = diff != null && diff !== 0 && (diff > 0) === m.higher;
                return (
                  <div key={m.key} className="metric" title={m.help}>
                    <div className="metric-v">{m.score && v != null ? v.toFixed(2) : fmt(v, m.ms)}</div>
                    <div className="metric-l">{m.label}</div>
                    {diff != null && diff !== 0 && (
                      <div className={better ? "delta good" : "delta bad"}>
                        {diff > 0 ? "▲" : "▼"} {m.ms ? fmt(Math.abs(diff), true) : `${Math.round(Math.abs(diff) * 100)} pts`}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {selected.metrics.by_type && (
            <div className="card">
              <h2>By question type</h2>
              <div className="table-wrap"><table>
                <thead><tr><th>Type</th><th>Questions</th><th>Right section retrieved</th>{selected.mode === "full" && <th>Correct outcome</th>}</tr></thead>
                <tbody>
                  {Object.entries(selected.metrics.by_type).map(([t, v]) => (
                    <tr key={t}><td>{t}</td><td>{v.n}</td><td>{fmt(v.found)}</td>{selected.mode === "full" && <td>{fmt(v.correct)}</td>}</tr>
                  ))}
                </tbody>
              </table></div>
            </div>
          )}

          <div className="card">
            <div className="row spread">
              <h2>Questions ({results.length})</h2>
              <label className="inline"><input type="checkbox" checked={onlyProblems}
                                               onChange={(e) => setOnlyProblems(e.target.checked)} /> Only problems</label>
            </div>
            <div className="table-wrap"><table>
              <thead><tr><th>ID</th><th>Type · user</th><th>Question</th><th>Rank</th><th>Best dist.</th>
                {selected.mode === "full" && <th>Answer</th>}<th>Result</th></tr></thead>
              <tbody>
                {results.map((r) => {
                  const p = problem(r, selected.mode);
                  return (
                    <tr key={r.id}>
                      <td>{r.id}</td>
                      <td className="small">{r.type}<br /><span className="muted">{r.user.split("@")[0]}</span></td>
                      <td>{r.question}</td>
                      <td>{r.gold_rank ?? "–"}</td>
                      <td>{r.best_distance?.toFixed(3) ?? "–"}</td>
                      {selected.mode === "full" && <td className="small answer-cell" title={r.judge_reason}>{r.answer}</td>}
                      <td>{p ? <span className="pill failed">{p}</span> : <span className="pill ready">ok</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table></div>
          </div>
        </>
      )}
    </div>
  );
}
