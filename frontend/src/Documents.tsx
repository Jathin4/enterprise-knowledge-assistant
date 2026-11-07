import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, type Collection, type DocumentOut, type User } from "./api";

const WORKING = new Set(["queued", "extracting", "embedding"]);

function size(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Documents({ user }: { user: User }) {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => api.documents().then(setDocs).catch((e) => setError(e.message)), []);

  useEffect(() => {
    api.collections().then((cs) => {
      setCollections(cs);
      setCollectionId((cur) => cur ?? cs[0]?.id ?? null);
    }).catch((e) => setError(e.message));
    load();
  }, [load]);

  // While anything is still processing, refresh every 2 seconds to show its status change.
  const processing = docs.some((d) => WORKING.has(d.status));
  useEffect(() => {
    if (!processing) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [processing, load]);

  async function upload(e: FormEvent) {
    e.preventDefault();
    const file = fileInput.current?.files?.[0];
    if (!file || collectionId === null) return;
    setUploading(true);
    setError("");
    try {
      await api.upload(collectionId, file);
      if (fileInput.current) fileInput.current.value = "";
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function remove(doc: DocumentOut) {
    if (!confirm(`Delete "${doc.filename}" and all its chunks?`)) return;
    try { await api.deleteDocument(doc.id); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : "Delete failed"); }
  }

  const collectionName = (id: number) => collections.find((c) => c.id === id)?.name ?? `#${id}`;

  return (
    <div className="page-pad">
      <form className="card upload" onSubmit={upload}>
        <h2>Upload a document</h2>
        <div className="row">
          <label>Collection
            <select value={collectionId ?? ""} onChange={(e) => setCollectionId(Number(e.target.value))}>
              {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
          <label>File (PDF, DOCX or TXT, max 20 MB)
            <input ref={fileInput} type="file" accept=".pdf,.docx,.txt" required />
          </label>
          <button className="primary" disabled={uploading || collectionId === null}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
        {collections.length === 0 && <p className="muted">You aren't a member of any collection yet. Ask an admin.</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </form>

      <div className="card">
        <h2>Documents {processing && <span className="muted small">· updating…</span>}</h2>
        {docs.length === 0 ? <p className="muted">No documents yet.</p> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>File</th><th>Collection</th><th>Status</th><th>Pages</th><th>Chunks</th><th>Size</th><th>Uploaded</th><th /></tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.filename}</td>
                    <td>{collectionName(d.collection_id)}</td>
                    <td>
                      <span className={`pill ${d.status}`}>{d.status}</span>
                      {d.error && <div className="error small">{d.error}</div>}
                    </td>
                    <td>{d.page_count ?? "–"}</td>
                    <td>{d.chunk_count || "–"}{d.quarantined_chunks > 0 && <span className="pill failed" title="Passages that tried to instruct the AI were not indexed. Details are in the audit log."> {d.quarantined_chunks} quarantined</span>}</td>
                    <td>{size(d.size_bytes)}</td>
                    <td>{new Date(d.created_at).toLocaleString()}</td>
                    <td>
                      {(user.role === "admin" || d.owner_id === user.id) &&
                        <button className="ghost small" onClick={() => remove(d)}>Delete</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
