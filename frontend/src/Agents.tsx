import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, streamWorkflow, type Claim, type Collection, type ReportSource, type RunDetail, type RunEvent, type RunStatus, type RunSummary, type User } from "./api";

const STEPS = ["orchestrator", "research", "analysis", "verification", "report", "approval"];
const STEP_LABELS: Record<string, string> = {
  orchestrator: "Plan", research: "Research", analysis: "Analysis", verification: "Verification", report: "Report", approval: "Human approval",
  recovery: "Recovery", failed: "Failed", done: "Finished",
};
const STATUS_LABELS: Record<RunStatus, string> = {
  queued: "queued", running: "running", awaiting_approval: "needs approval", approved: "approved",
  rejected: "rejected", completed: "no findings", failed: "failed",
};
const WORKING: RunStatus[] = ["queued", "running"];
const CATEGORY: Record<Claim["category"], string> = { gap: "Gap", met: "Met", conflict: "Conflict", finding: "Finding" };
const EXAMPLE = "Compare our information security policy with the API authentication guide and prepare a gap report.";

function where(s: ReportSource) {
  return [s.filename, s.page ? `p.${s.page}` : null, s.heading].filter(Boolean).join(" · ");
}

function ClaimList({ claims, sources, onCite }: { claims: Claim[]; sources: ReportSource[]; onCite: (n: number) => void }) {
  const known = new Set(sources.map((s) => s.n));
  return (
    <div className="table-wrap"><table>
      <thead><tr><th>Type</th><th>Statement</th><th>Evidence</th></tr></thead>
      <tbody>
        {claims.map((c, i) => (
          <tr key={i}>
            <td><span className={`pill cat-${c.category}`}>{CATEGORY[c.category]}</span></td>
            <td>{c.text}<div className="muted small">{c.reason}</div></td>
            <td className="nowrap">
              {c.sources.map((n) => known.has(n)
                ? <button key={n} className="cite" onClick={() => onCite(n)}>{n}</button>
                : <span key={n} className="cite static">{n}</span>)}
            </td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

function Report({ run }: { run: RunDetail }) {
  const [open, setOpen] = useState<number | null>(null);
  const r = run.report;
  if (!r) return null;
  const draft = run.status === "awaiting_approval";
  const toggle = (n: number) => setOpen((cur) => (cur === n ? null : n));
  return (
    <div className="card report">
      <div className="row spread">
        <h2>Report</h2>
        {draft && <span className="pill queued">Draft: not approved yet</span>}
        {run.status === "approved" && <span className="pill ready">Approved</span>}
        {run.status === "rejected" && <span className="pill failed">Rejected</span>}
      </div>
      <p>{r.summary}</p>

      {r.findings.length > 0 && <><h3>Verified findings ({r.findings.length})</h3><ClaimList claims={r.findings} sources={r.sources} onCite={toggle} /></>}
      {r.conflicts.length > 0 && <><h3>Conflicts between documents ({r.conflicts.length})</h3><ClaimList claims={r.conflicts} sources={r.sources} onCite={toggle} /></>}
      {r.unsupported.length > 0 && (
        <div className="unverified">
          <h3>Could not be verified ({r.unsupported.length})</h3>
          <p className="muted small">These are not findings. The agents wrote them, but the documents don't back them up.</p>
          <ClaimList claims={r.unsupported} sources={r.sources} onCite={toggle} />
        </div>
      )}

      {(r.out_of_scope?.length ?? 0) > 0 && (
        <>
          <h3>Outside the other document's scope ({r.out_of_scope!.length})</h3>
          <p className="muted small">The requirement is real, but the other document wouldn't be expected to cover it, so this isn't counted as a gap. A reviewer should still check this judgement.</p>
          <ClaimList claims={r.out_of_scope!} sources={r.sources} onCite={toggle} />
        </>
      )}

      {(r.off_topic?.length ?? 0) > 0 && (
        <details>
          <summary>Left out: true but off topic ({r.off_topic!.length})</summary>
          <p className="muted small">The documents back these statements, but they don't answer the request.</p>
          <ClaimList claims={r.off_topic!} sources={r.sources} onCite={toggle} />
        </details>
      )}

      {r.coverage && (
        <>
          <h3>What this report checked</h3>
          <p>{r.coverage.sections_examined} of {r.coverage.sections_total} sections examined. {r.coverage.passages_used} of {r.coverage.passages_retrieved} retrieved passages were used.
            {r.coverage.claim_limit_reached && <b> The claim limit was reached, so some requirements may be unchecked.</b>}</p>
          {r.coverage.not_examined.length > 0 && (
            <details>
              <summary>Sections not examined ({r.coverage.not_examined.length})</summary>
              <ul className="limits">{r.coverage.not_examined.map((s) => <li key={s}>{s}</li>)}</ul>
            </details>
          )}
        </>
      )}

      {r.sources.length > 0 && <h3>Sources</h3>}
      <ol className="sources">
        {r.sources.map((s) => (
          <li key={s.n} className={open === s.n ? "open" : ""}>
            <button className="source-head" onClick={() => toggle(s.n)} aria-expanded={open === s.n}>
              <span className="cite static">{s.n}</span> {where(s)}
            </button>
            {open === s.n && <blockquote>{s.snippet}{s.snippet.length >= 400 ? "…" : ""}</blockquote>}
          </li>
        ))}
      </ol>

      <h3>Limitations</h3>
      <ul className="limits">{r.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
      {r.retries > 0 && <p className="muted small">Research was repeated {r.retries} time(s) to look for missing evidence.</p>}
    </div>
  );
}

function Timeline({ events, status }: { events: RunEvent[]; status: RunStatus }) {
  const reached = new Set(events.map((e) => e.step));
  const current = WORKING.includes(status) ? events.at(-1)?.step : null;
  const nextIndex = current ? STEPS.indexOf(current) + 1 : 0;
  return (
    <>
      <ol className="steps-bar" aria-label="Workflow steps">
        {STEPS.map((s, i) => (
          <li key={s} className={reached.has(s) ? "reached" : WORKING.includes(status) && i === nextIndex ? "active" : ""}>{STEP_LABELS[s]}</li>
        ))}
      </ol>
      <ul className="events">
        {events.map((e) => (
          <li key={e.id}><span className="muted small">{new Date(e.created_at).toLocaleTimeString()}</span> <b>{STEP_LABELS[e.step] ?? e.step}:</b> {e.message}</li>
        ))}
        {WORKING.includes(status) && <li className="muted">Working… this can take several minutes on a CPU.</li>}
      </ul>
    </>
  );
}

export default function Agents({ user }: { user: User }) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [request, setRequest] = useState(EXAMPLE);
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const stream = useRef<AbortController | null>(null);

  const loadRuns = useCallback(() => api.runs().then(setRuns).catch((e) => setError(e.message)), []);

  const follow = useCallback(async (id: number) => {
    stream.current?.abort();
    const controller = new AbortController();
    stream.current = controller;
    try {
      const detail = await api.run(id);
      setRun(detail);
      if (!WORKING.includes(detail.status)) return;
      await streamWorkflow(id, detail.events.at(-1)?.id ?? 0, (e) => {
        if (e.type === "event") {
          setRun((cur) => cur && cur.id === id
            ? { ...cur, events: [...cur.events, { id: e.id, step: e.step, message: e.message, created_at: new Date().toISOString() }] }
            : cur);
        }
      }, controller.signal);
      setRun(await api.run(id));   // the stream ended: reload the report and final status
      loadRuns();
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    }
  }, [loadRuns]);

  useEffect(() => {
    loadRuns();
    api.collections().then(setCollections).catch(() => {});
    return () => stream.current?.abort();
  }, [loadRuns]);

  async function start(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await api.startReport(request.trim(), collectionId);
      await loadRuns();
      follow(created.id);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  async function decide(decision: "approved" | "rejected") {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      await api.decide(run.id, decision, comment);
      setComment("");
      await follow(run.id);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  async function retry() {
    if (!run) return;
    setError("");
    try { await api.retryRun(run.id); follow(run.id); } catch (err) { setError((err as Error).message); }
  }

  return (
    <div className="page-pad agents">
      <div className="eval-top">
        <form className="card" onSubmit={start}>
          <h2>Start a report</h2>
          <p className="muted small">A team of agents researches your documents, checks every claim, and writes a report for a reviewer to approve.</p>
          <label>What do you need?
            <textarea rows={3} value={request} onChange={(e) => setRequest(e.target.value)} maxLength={2000} required minLength={5} />
          </label>
          <div className="row">
            <label>Search in
              <select value={collectionId ?? ""} onChange={(e) => setCollectionId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All my collections</option>
                {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </label>
            <button className="primary" disabled={busy || request.trim().length < 5}>Start</button>
          </div>
          {error && <p className="error" role="alert">{error}</p>}
        </form>

        <div className="card runs">
          <h2>Runs</h2>
          {runs.length === 0 ? <p className="muted">No runs yet.</p> : (
            <ul>
              {runs.map((r) => (
                <li key={r.id}>
                  <button className={run?.id === r.id ? "run active" : "run"} onClick={() => follow(r.id)}>
                    <span className="run-title">#{r.id} {r.request}</span>
                    <span className={`pill ${r.status}`}>{STATUS_LABELS[r.status]}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {run && (
        <>
          <div className="card">
            <div className="row spread">
              <h2>#{run.id} {run.task_type ? run.task_type.replace("_", " ") : "report"}</h2>
              <span className={`pill ${run.status}`}>{STATUS_LABELS[run.status]}</span>
            </div>
            <p className="muted">{run.request}</p>
            <Timeline events={run.events} status={run.status} />
            {run.status === "failed" && (
              <div className="row spread">
                <p className="error" role="alert">{run.error}</p>
                {run.user_id === user.id && <button className="primary" onClick={retry}>Continue from last step</button>}
              </div>
            )}
          </div>

          <Report run={run} />

          {run.status === "awaiting_approval" && (
            <div className="card">
              <h2>Decision</h2>
              {run.can_review ? (
                <>
                  <label>Comment (optional)
                    <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} maxLength={2000} />
                  </label>
                  <div className="row actions">
                    <button className="primary" disabled={busy} onClick={() => decide("approved")}>Approve</button>
                    <button className="ghost" disabled={busy} onClick={() => decide("rejected")}>Reject</button>
                  </div>
                </>
              ) : (
                <p className="muted">Waiting for a reviewer: an analyst or admin who can read these collections, other than the person who started the run.</p>
              )}
            </div>
          )}
          {run.approval?.decision && (
            <div className="card">
              <h2>Decision</h2>
              <p><b>{run.approval.decision === "approved" ? "Approved" : "Rejected"}</b>
                {run.approval.decided_at && ` on ${new Date(run.approval.decided_at).toLocaleString()}`}</p>
              {run.approval.comment && <blockquote>{run.approval.comment}</blockquote>}
            </div>
          )}
        </>
      )}
    </div>
  );
}
