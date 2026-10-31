import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, streamChat, type Collection, type Conversation, type Message, type Source } from "./api";

type Draft = { question: string; text: string; reading: Source[]; phase: "searching" | "writing" };

function where(s: Source) {
  return [s.filename, s.page ? `p.${s.page}` : null, s.heading].filter(Boolean).join(" · ");
}

/** Answer text with each [n] turned into a button that opens source n. */
function CitedText({ text, sources, onCite }: { text: string; sources: Source[]; onCite: (n: number) => void }) {
  const known = new Set(sources.map((s) => s.n));
  return (
    <p className="answer">
      {text.split(/(\[\d+\])/).map((part, i) => {
        const n = /^\[(\d+)\]$/.exec(part)?.[1];
        return n && known.has(Number(n))
          ? <button key={i} className="cite" onClick={() => onCite(Number(n))} title={where(sources.find((s) => s.n === Number(n))!)}>{n}</button>
          : <Fragment key={i}>{part}</Fragment>;
      })}
    </p>
  );
}

function AssistantMessage({ m }: { m: Message }) {
  const [open, setOpen] = useState<number | null>(null);
  if (m.abstained) {
    return <div className="bubble assistant abstained"><p className="answer">{m.content}</p>
      <p className="muted small">No document in your collections answers this.</p></div>;
  }
  return (
    <div className="bubble assistant">
      <CitedText text={m.content} sources={m.sources} onCite={(n) => setOpen(open === n ? null : n)} />
      {m.sources.length > 0 && (
        <ol className="sources">
          {m.sources.map((s) => (
            <li key={s.n} className={open === s.n ? "open" : ""}>
              <button className="source-head" onClick={() => setOpen(open === s.n ? null : s.n)} aria-expanded={open === s.n}>
                <span className="cite static">{s.n}</span> {where(s)}
              </button>
              {open === s.n && <blockquote>{s.snippet}{s.snippet.length >= 400 ? "…" : ""}</blockquote>}
            </li>
          ))}
        </ol>
      )}
      {m.sources.length === 0 && <p className="warn small">This answer has no valid citations: treat it with care.</p>}
      {m.latency_ms != null && <p className="muted small">{(m.latency_ms / 1000).toFixed(1)} s</p>}
    </div>
  );
}

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [question, setQuestion] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const loadList = useCallback(() => api.conversations().then(setConversations).catch((e) => setError(e.message)), []);
  useEffect(() => { loadList(); api.collections().then(setCollections).catch(() => {}); }, [loadList]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [messages, draft]);

  async function open(id: number) {
    if (draft) return;
    setError("");
    setActiveId(id);
    try { setMessages((await api.conversation(id)).messages); } catch (e) { setError((e as Error).message); }
  }

  function newChat() {
    if (draft) return;
    setActiveId(null);
    setMessages([]);
    setError("");
  }

  async function remove(id: number) {
    if (!confirm("Delete this conversation?")) return;
    await api.deleteConversation(id).catch(() => {});
    if (id === activeId) newChat();
    loadList();
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const q = question.trim();
    if (!q || draft) return;
    setQuestion("");
    setError("");
    setDraft({ question: q, text: "", reading: [], phase: "searching" });
    abort.current = new AbortController();
    let convId = activeId;
    try {
      await streamChat({ question: q, conversation_id: activeId, collection_id: collectionId }, (ev) => {
        if (ev.type === "start") { convId = ev.conversation_id; setActiveId(ev.conversation_id); }
        else if (ev.type === "sources") setDraft((d) => d && { ...d, reading: ev.sources, phase: "writing" });
        else if (ev.type === "token") setDraft((d) => d && { ...d, text: d.text + ev.text, phase: "writing" });
        else if (ev.type === "error") setError(ev.detail);
      }, abort.current.signal);
    } catch (err) {
      if ((err as Error).name !== "AbortError") setError((err as Error).message);
    }
    // Reload from the server: it holds the checked final answer (hallucinated citations removed).
    if (convId !== null) setMessages((await api.conversation(convId).catch(() => null))?.messages ?? []);
    setDraft(null);
    loadList();
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  }

  return (
    <div className="chat">
      <aside className="conv-list" aria-label="Conversations">
        <button className="primary wide" onClick={newChat} disabled={!!draft}>+ New chat</button>
        <ul>
          {conversations.map((c) => (
            <li key={c.id} className={c.id === activeId ? "active" : ""}>
              <button className="conv" onClick={() => open(c.id)} title={c.title}>{c.title}</button>
              <button className="ghost x" onClick={() => remove(c.id)} aria-label={`Delete conversation ${c.title}`}>×</button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="thread">
        <div className="messages">
          {messages.length === 0 && !draft && (
            <div className="empty muted">
              <h2>Ask a question about your documents</h2>
              <p>Every answer cites its sources. Click a number like <span className="cite static">1</span> to see the passage.</p>
            </div>
          )}
          {messages.map((m) => m.role === "user"
            ? <div key={m.id} className="bubble user">{m.content}</div>
            : <AssistantMessage key={m.id} m={m} />)}
          {draft && (
            <>
              <div className="bubble user">{draft.question}</div>
              <div className="bubble assistant">
                {draft.text
                  ? <p className="answer">{draft.text}<span className="caret" /></p>
                  : <p className="muted">{draft.phase === "searching" ? "Searching your documents…" : `Reading ${draft.reading.length} passage(s)…`}</p>}
                {draft.reading.length > 0 && (
                  <p className="muted small">From: {draft.reading.map((s) => `[${s.n}] ${where(s)}`).join("; ")}</p>
                )}
              </div>
            </>
          )}
          {error && <p className="error" role="alert">{error}</p>}
          <div ref={bottom} />
        </div>

        <form className="composer" onSubmit={send}>
          <select aria-label="Search in" value={collectionId ?? ""}
                  onChange={(e) => setCollectionId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">All my collections</option>
            {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} onKeyDown={onKey}
                    placeholder="Ask a question… (Enter to send, Shift+Enter for a new line)" rows={2}
                    maxLength={2000} aria-label="Question" />
          {draft
            ? <button type="button" className="ghost" onClick={() => abort.current?.abort()}>Stop</button>
            : <button className="primary" disabled={!question.trim()}>Send</button>}
        </form>
      </section>
    </div>
  );
}
