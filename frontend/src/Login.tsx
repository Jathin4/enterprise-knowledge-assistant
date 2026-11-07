import { useState, type FormEvent } from "react";
import { api, type User } from "./api";

export default function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password);
      onLogin(await api.me());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>Knowledge Assistant</h1>
        <p className="muted">Answers from your company documents, with sources.</p>
        <label>Email<input type="email" autoComplete="username" value={email}
                           onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Password
          <span className="password-field">
            <input type={showPassword ? "text" : "password"} autoComplete="current-password" value={password}
                   onChange={(e) => setPassword(e.target.value)} required />
            <button type="button" className="eye" onClick={() => setShowPassword((s) => !s)}
                    aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword}
                    title={showPassword ? "Hide password" : "Show password"}>
              {showPassword ? (
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" d="M3 3l18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.1A10.4 10.4 0 0 1 12 5c5 0 9 4.5 10 7a12.6 12.6 0 0 1-3.2 4.3M6.1 6.1C3.9 7.5 2.5 9.6 2 12c1 2.5 5 7 10 7a9.8 9.8 0 0 0 4.5-1.1" /></svg>
              ) : (
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" strokeWidth="2" d="M2 12c1-2.5 5-7 10-7s9 4.5 10 7c-1 2.5-5 7-10 7S3 14.5 2 12z" /><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="2" /></svg>
              )}
            </button>
          </span>
        </label>
        {password !== password.trim() && <p className="warn small">Your password starts or ends with a space. Is that intended?</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
