// Typed client for the FastAPI backend. Types mirror backend/app/schemas.py.

export type User = { id: number; email: string; role: string };
export type Collection = { id: number; name: string };
export type DocStatus = "queued" | "extracting" | "embedding" | "ready" | "failed";
export type DocumentOut = {
  id: number; collection_id: number; owner_id: number; filename: string; file_type: string;
  size_bytes: number; status: DocStatus; error: string | null; page_count: number | null;
  chunk_count: number; quarantined_chunks: number; created_at: string; updated_at: string;
};
export type Source = {
  n: number; chunk_id: number; document_id: number; filename: string;
  page: number | null; heading: string | null; distance: number; snippet: string;
};
export type Message = {
  id: number; role: "user" | "assistant"; content: string; sources: Source[];
  abstained: boolean; latency_ms: number | null; created_at: string;
};
export type Conversation = { id: number; title: string; created_at: string };
export type ConversationDetail = Conversation & { messages: Message[] };

export type EvalMode = "retrieval" | "full";
export type EvalSummary = {
  id: number; label: string; mode: EvalMode; status: "running" | "done" | "failed";
  progress: number; total: number; error: string | null; duration_s: number | null; created_at: string;
  config: Record<string, string | number>;
  metrics: Record<string, number | null> & { by_type?: Record<string, { n: number; found: number | null; correct?: number | null }> };
};
export type EvalResult = {
  id: string; type: string; user: string; question: string; expect: "answer" | "refuse";
  gold_rank: number | null; best_distance: number | null; gate_refuses: boolean; top: [string, string | null, number][];
  answer?: string; abstained?: boolean; missing?: string[]; forbidden?: string[]; cited?: [string, string | null][];
  cited_gold?: boolean; grounded?: string; judge_reason?: string; generation_ms?: number;
};
export type EvalDetail = EvalSummary & { results: EvalResult[] };

export type RunStatus = "queued" | "running" | "awaiting_approval" | "approved" | "rejected" | "completed" | "failed";
export type RunSummary = {
  id: number; user_id: number; request: string; task_type: string | null; status: RunStatus;
  current_step: string | null; error: string | null; created_at: string; finished_at: string | null;
};
export type Claim = { text: string; category: "gap" | "met" | "conflict" | "finding"; sources: number[]; status: string; reason: string };
export type ReportSource = { n: number; filename: string; page: number | null; heading: string | null; snippet: string };
export type WorkflowReport = {
  request: string; task_type: string | null; summary: string; findings: Claim[]; conflicts: Claim[];
  unsupported: Claim[]; out_of_scope?: Claim[]; off_topic?: Claim[]; limitations: string[]; retries: number; sources: ReportSource[];
  coverage?: { sections_total: number; sections_examined: number; not_examined: string[]; passages_retrieved: number; passages_used: number; claim_limit_reached: boolean } | null;
};
export type RunEvent = { id: number; step: string; message: string; created_at: string };
export type RunDetail = RunSummary & {
  collection_ids: number[]; report: WorkflowReport | null; events: RunEvent[]; can_review: boolean;
  approval: { id: number; decision: string | null; comment: string | null; decided_by: number | null; decided_at: string | null } | null;
};
export type WorkflowStreamEvent =
  | { type: "event"; id: number; step: string; message: string }
  | { type: "status"; status: RunStatus };

export type StreamEvent =
  | { type: "start"; conversation_id: number }
  | { type: "sources"; sources: Source[] }
  | { type: "token"; text: string }
  | { type: "done"; message: Message }
  | { type: "error"; detail: string };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

// The login token lives in memory and sessionStorage (cleared when the tab closes).
let token: string | null = (() => { try { return sessionStorage.getItem("token"); } catch { return null; } })();
let onUnauthorized = () => {};

export function setToken(t: string | null) {
  token = t;
  try { t ? sessionStorage.setItem("token", t) : sessionStorage.removeItem("token"); } catch { /* storage blocked */ }
}
export const hasToken = () => token !== null;
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

function authHeaders(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  if (token) h.set("Authorization", `Bearer ${token}`);
  return h;
}

async function fail(res: Response): Promise<never> {
  if (res.status === 401 && token) { setToken(null); onUnauthorized(); }
  let detail = res.statusText;
  try {
    const body = await res.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch { /* not JSON */ }
  throw new ApiError(res.status, detail);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { ...init, headers: authHeaders(init.headers) });
  if (!res.ok) return fail(res);
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  async login(email: string, password: string) {
    const r = await request<{ access_token: string }>("/api/auth/login", {
      method: "POST", body: new URLSearchParams({ username: email, password }),
    });
    setToken(r.access_token);
  },
  me: () => request<User>("/api/auth/me"),
  collections: () => request<Collection[]>("/api/collections"),
  documents: () => request<DocumentOut[]>("/api/documents"),
  upload(collectionId: number, file: File) {
    const form = new FormData();
    form.append("collection_id", String(collectionId));
    form.append("file", file);
    return request<DocumentOut>("/api/documents", { method: "POST", body: form });
  },
  deleteDocument: (id: number) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  conversations: () => request<Conversation[]>("/api/conversations"),
  conversation: (id: number) => request<ConversationDetail>(`/api/conversations/${id}`),
  deleteConversation: (id: number) => request<void>(`/api/conversations/${id}`, { method: "DELETE" }),
  runs: () => request<RunSummary[]>("/api/workflows"),
  run: (id: number) => request<RunDetail>(`/api/workflows/${id}`),
  approvals: () => request<RunSummary[]>("/api/approvals"),
  startReport: (text: string, collectionId: number | null) => request<RunSummary>("/api/reports", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request: text, collection_id: collectionId }),
  }),
  retryRun: (id: number) => request<RunSummary>(`/api/workflows/${id}/retry`, { method: "POST" }),
  decide: (id: number, decision: "approved" | "rejected", comment: string) => request<RunSummary>(`/api/approvals/${id}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, comment: comment.trim() || null }),
  }),
  evaluations: () => request<EvalSummary[]>("/api/evaluations"),
  evaluation: (id: number) => request<EvalDetail>(`/api/evaluations/${id}`),
  startEvaluation: (mode: EvalMode, label: string) => request<EvalSummary>("/api/evaluations", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode, label }),
  }),
};

/** POST /api/chat/stream and call onEvent for each Server-Sent Event.
 *  Uses fetch (not EventSource) because EventSource can't send the Authorization header. */
export async function streamChat(
  body: { question: string; conversation_id: number | null; collection_id: number | null },
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
) {
  const res = await fetch("/api/chat/stream", {
    method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body), signal,
  });
  if (!res.ok || !res.body) return fail(res);
  await readEvents(res.body, onEvent);
}

/** Follow a workflow's progress. Saved events are replayed, so `after` makes reconnecting lossless. */
export async function streamWorkflow(id: number, after: number, onEvent: (e: WorkflowStreamEvent) => void, signal?: AbortSignal) {
  const res = await fetch(`/api/workflows/${id}/events?after=${after}`, { headers: authHeaders(), signal });
  if (!res.ok || !res.body) return fail(res);
  await readEvents(res.body, onEvent);
}

async function readEvents<T>(body: ReadableStream<Uint8Array<ArrayBuffer>>, onEvent: (e: T) => void) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {   // a blank line ends one event
      const raw = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      for (const line of raw.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}
