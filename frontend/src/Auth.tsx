import { useState, type FormEvent } from "react";
import { ApiError, logIn, signUp, verifyId } from "./api";
import { SOMETHING_WRONG } from "./shared";

export function Auth({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(mode: "log_in" | "sign_up") {
    setBusy(true);
    setError(null);
    try {
      if (mode === "sign_up") await signUp(email, password);
      await logIn(email, password);
      onLoggedIn();
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : "";
      setError(
        detail === "email_taken" ? "That email already has an account. Log in instead."
        : detail === "wrong_email_or_password" ? "Wrong email or password."
        : e instanceof ApiError && e.status === 422 ? "Use a valid email and a password of at least 8 characters."
        : SOMETHING_WRONG,
      );
    } finally {
      setBusy(false);
    }
  }

  // Enter in either field logs in.
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit("log_in");
  };

  return (
    <div className="page-center">
      <form className="card form" onSubmit={onSubmit}>
        <h2>Log in or create an account</h2>
        <label>Email <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <label>Password <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <p className="note warn">{error}</p>}
        <div className="actions">
          <button type="submit" className="primary" disabled={busy}>Log in</button>
          <button type="button" disabled={busy} onClick={() => submit("sign_up")}>Create account</button>
        </div>
      </form>
    </div>
  );
}

export function VerifyId({ onVerified }: { onVerified: () => void }) {
  const [idNumber, setIdNumber] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await verifyId(idNumber);
      onVerified();
    } catch (e) {
      setError(
        e instanceof ApiError && e.detail === "id_already_used"
          ? "That ID already verified another account. Each person can have one account."
          : e instanceof ApiError && e.status === 422
            ? "Enter an ID number of at least 4 characters."
            : SOMETHING_WRONG,
      );
    }
  }

  return (
    <div className="page-center">
      <form className="card form" onSubmit={onSubmit}>
        <h2>Verify your ID</h2>
        <p>
          Only verified buyers can join the line, so bots can't fill it with fake accounts. Verify now,
          before the sale starts.
        </p>
        <p className="demo">Demo: any ID number works, but each one can verify only one account.</p>
        <label>ID number <input value={idNumber} onChange={(e) => setIdNumber(e.target.value)} /></label>
        {error && <p className="note warn">{error}</p>}
        <button type="submit" className="primary">Verify my ID</button>
      </form>
    </div>
  );
}
