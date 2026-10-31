import { useEffect, useState } from "react";
import { api, hasToken, setToken, setUnauthorizedHandler, type User } from "./api";
import Chat from "./Chat";
import Documents from "./Documents";
import Evaluation from "./Evaluation";
import Login from "./Login";

type Tab = "chat" | "documents" | "evaluation";
const LABELS: Record<Tab, string> = { chat: "Chat", documents: "Documents", evaluation: "Evaluation" };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(hasToken());
  const [tab, setTab] = useState<Tab>("chat");

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));   // expired token → back to login
    if (hasToken()) api.me().then(setUser).catch(() => setToken(null)).finally(() => setChecking(false));
  }, []);

  if (checking) return <div className="center muted">Loading…</div>;
  if (!user) return <Login onLogin={setUser} />;

  return (
    <div className="app">
      <header className="topbar">
        <strong>Knowledge Assistant</strong>
        <nav className="tabs" aria-label="Sections">
          {(user.role === "admin" ? ["chat", "documents", "evaluation"] as const : ["chat", "documents"] as const).map((t) => (
            <button key={t} className={tab === t ? "tab active" : "tab"} aria-current={tab === t}
                    onClick={() => setTab(t)}>
              {LABELS[t]}
            </button>
          ))}
        </nav>
        <span className="muted user">{user.email} · {user.role}</span>
        <button className="ghost" onClick={() => { setToken(null); setUser(null); }}>Log out</button>
      </header>
      <main className="content">
        {tab === "chat" ? <Chat /> : tab === "documents" ? <Documents user={user} /> : <Evaluation />}
      </main>
    </div>
  );
}
