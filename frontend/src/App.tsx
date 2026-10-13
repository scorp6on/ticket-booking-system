import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  claimSeat,
  getCaptcha,
  getEvent,
  logIn,
  logOut,
  me,
  pay,
  queueStatus,
  signUp,
  verifyId,
  type Account,
  type Captcha,
  type Event,
  type QueueStatus,
  type Seat,
} from "./api";

const eventId = Number(new URLSearchParams(location.search).get("event") ?? 1);
const REFRESH_MS = 5000;
const QUEUE_POLL_MS = 3000;
const WARN_AT_SECONDS = 60;
const TIME_UP = "Your time is up, so the seat went back on sale. You haven't been charged.";
const PASS_EXPIRED = "Your time to pick a seat ran out, so you've rejoined the line.";

/** Proof we waited our turn, plus how long it lasts. */
type Pass = { token: string; secondsLeft: number; startedAt: number };

type View =
  | { kind: "queue"; note?: string }
  | { kind: "lobby"; pass: Pass; note?: string }
  | { kind: "checkout"; pass: Pass; seat: Seat; secondsLeft: number; startedAt: number; message?: string }
  | { kind: "sold"; seat: Seat };

const money = (cents: number) => `$${(cents / 100).toFixed(2)}`;
const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

export default function App() {
  // undefined = still checking the session cookie, null = logged out
  const [account, setAccount] = useState<Account | null | undefined>(undefined);
  const [view, setView] = useState<View>({ kind: "queue" });
  const onAdmitted = useCallback((pass: Pass) => setView({ kind: "lobby", pass }), []);

  const refreshAccount = useCallback(() => {
    me().then(setAccount, () => setAccount(null));
  }, []);
  useEffect(refreshAccount, [refreshAccount]);

  async function onLogOut() {
    await logOut();
    setAccount(null);
    setView({ kind: "queue" });
  }

  return (
    <main>
      <header>
        <h1>Ticket Booking</h1>
        {account && (
          <span className="who">
            {account.email} <button className="link" onClick={onLogOut}>Log out</button>
          </span>
        )}
      </header>
      {account === undefined && <p>Loading…</p>}
      {account === null && <Auth onLoggedIn={refreshAccount} />}
      {account && !account.id_verified && <VerifyId onVerified={refreshAccount} />}
      {account?.id_verified && <Buying view={view} setView={setView} onAdmitted={onAdmitted} />}
    </main>
  );
}

function Auth({ onLoggedIn }: { onLoggedIn: () => void }) {
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
        : "Something went wrong. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card form">
      <h2>Log in or create an account</h2>
      <label>Email <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      <label>Password <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p className="note error">{error}</p>}
      <div className="actions">
        <button className="pay" disabled={busy} onClick={() => submit("log_in")}>Log in</button>
        <button disabled={busy} onClick={() => submit("sign_up")}>Create account</button>
      </div>
    </section>
  );
}

function VerifyId({ onVerified }: { onVerified: () => void }) {
  const [idNumber, setIdNumber] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setError(null);
    try {
      await verifyId(idNumber);
      onVerified();
    } catch (e) {
      setError(
        e instanceof ApiError && e.detail === "id_already_used"
          ? "That ID already verified another account. Each person can have one account."
          : "Enter an ID number of at least 4 characters.",
      );
    }
  }

  return (
    <section className="card form">
      <h2>Verify your ID</h2>
      <p>
        Only verified buyers can join the line, so bots can't fill it with fake accounts. Verify now,
        before the sale starts.
      </p>
      <p className="demo">Demo: any ID number works, but each one can verify only one account.</p>
      <label>ID number <input value={idNumber} onChange={(e) => setIdNumber(e.target.value)} /></label>
      {error && <p className="note error">{error}</p>}
      <button className="pay" onClick={submit}>Verify my ID</button>
    </section>
  );
}

function Buying({ view, setView, onAdmitted }: {
  view: View;
  setView: (v: View) => void;
  onAdmitted: (pass: Pass) => void;
}) {
  return (
    <>
      {view.kind === "queue" && <Queue note={view.note} onAdmitted={onAdmitted} />}
      {view.kind === "lobby" && <Lobby view={view} setView={setView} />}
      {view.kind === "checkout" && <Checkout view={view} setView={setView} />}
      {view.kind === "sold" && (
        <section className="card">
          <h2>🎉 Seat {view.seat.label} is yours</h2>
          <p>{money(view.seat.price_cents)} charged.</p>
          <button onClick={() => setView({ kind: "queue" })}>Buy another seat</button>
        </section>
      )}
    </>
  );
}

function Queue({ note, onAdmitted }: { note?: string; onAdmitted: (pass: Pass) => void }) {
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [captcha, setCaptcha] = useState<Captcha | null>(null);
  const [answer, setAnswer] = useState("");
  const [wrongAnswer, setWrongAnswer] = useState(false);
  const [inLine, setInLine] = useState(false);

  const handle = useCallback(
    (s: QueueStatus) => {
      if (s.state === "admitted") {
        onAdmitted({ token: s.token, secondsLeft: s.seconds_left, startedAt: performance.now() });
      } else {
        setStatus(s);
      }
    },
    [onAdmitted],
  );

  const askForCaptcha = useCallback(() => {
    setInLine(false);
    setAnswer("");
    getCaptcha().then(setCaptcha, (e) => setError(String(e.message)));
  }, []);

  const onError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.detail === "captcha_required") askForCaptcha();
      else setError(e instanceof Error ? e.message : String(e));
    },
    [askForCaptcha],
  );

  // First check without a CAPTCHA: if we're already in line (say, after a refresh), we keep our spot.
  useEffect(() => {
    queueStatus(eventId).then((s) => {
      setInLine(true);
      handle(s);
    }, onError);
  }, [handle, onError]);

  // Once in line, keep checking. Each check also lets people in as spots free up.
  useEffect(() => {
    if (!inLine) return;
    let done = false;
    const timer = setInterval(
      () =>
        queueStatus(eventId).then((s) => {
          if (done) return;
          if (s.state === "admitted") done = true;
          handle(s);
        }, onError),
      QUEUE_POLL_MS,
    );
    return () => {
      done = true;
      clearInterval(timer);
    };
  }, [inLine, handle, onError]);

  async function submitCaptcha() {
    if (!captcha) return;
    try {
      const s = await queueStatus(eventId, { captcha_id: captcha.captcha_id, answer });
      setCaptcha(null);
      setWrongAnswer(false);
      setInLine(true);
      handle(s);
    } catch (e) {
      if (e instanceof ApiError && e.detail === "captcha_required") {
        setWrongAnswer(true);
        askForCaptcha(); // each puzzle works once, so a wrong answer gets a new one
      } else {
        onError(e);
      }
    }
  }

  if (error) return <p className="note error">{error}. Did you run the seed script?</p>;
  return (
    <section className="card">
      {note && <p className="note">{note}</p>}
      {captcha ? (
        <div className="form">
          <h2>Before you join the line</h2>
          <p>Prove you're human: <strong>{captcha.question}</strong></p>
          <p className="demo">Demo CAPTCHA. A real site would use a service like Cloudflare Turnstile.</p>
          {wrongAnswer && <p className="note error">That wasn't right. Here's a new one.</p>}
          <label>Answer <input value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
          <button className="pay" onClick={submitCaptcha}>Join the line</button>
        </div>
      ) : status?.state === "sold_out" ? (
        <h2>Sold out</h2>
      ) : status?.state === "waiting" && !status.sale_open ? (
        <>
          <h2>You're in the waiting room</h2>
          <p>
            When the sale opens, everyone here gets a random place in line, so there's no need to rush or
            refresh.
          </p>
        </>
      ) : status?.state === "waiting" ? (
        <>
          <h2>You're number {status.position} in line</h2>
          <p>Keep this page open. You'll go straight to the seat map when it's your turn.</p>
        </>
      ) : (
        <p>Checking the line…</p>
      )}
    </section>
  );
}

function Lobby({ view, setView }: {
  view: Extract<View, { kind: "lobby" }>;
  setView: (v: View) => void;
}) {
  const [event, setEvent] = useState<Event | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [claiming, setClaiming] = useState(false);
  const passLeft = useCountdown(view.pass.secondsLeft, view.pass.startedAt);

  const load = useCallback(() => {
    getEvent(eventId).then(setEvent, (e) => setError(String(e.message)));
  }, []);

  // The map is only a hint and goes stale, so refresh it. The claim decides who gets a seat.
  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (passLeft <= 0) setView({ kind: "queue", note: PASS_EXPIRED });
  }, [passLeft, setView]);

  async function choose(seat: Seat) {
    setClaiming(true);
    try {
      const secondsLeft = await claimSeat(seat.id, view.pass.token);
      setView({ kind: "checkout", pass: view.pass, seat, secondsLeft, startedAt: performance.now() });
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : "";
      if (detail === "queue_token_invalid") return setView({ kind: "queue", note: PASS_EXPIRED });
      if (detail === "ticket_limit_reached")
        return setView({ ...view, note: "You already have 3 seats for this event (held or bought). That's the limit per person." });
      if (detail === "seat_taken") {
        setView({ ...view, note: `Seat ${seat.label} was just taken by someone else. Please pick another seat.` });
        return load();
      }
      throw e;
    } finally {
      setClaiming(false);
    }
  }

  if (error) return <p className="note error">{error}. Did you run the seed script?</p>;
  if (!event) return <p>Loading…</p>;

  const rows = new Map<string, Seat[]>();
  for (const s of event.seats) rows.set(s.label[0], [...(rows.get(s.label[0]) ?? []), s]);
  return (
    <section>
      <h2>{event.name}</h2>
      <p className={`pass ${passLeft <= WARN_AT_SECONDS ? "warn" : ""}`}>Time to pick a seat: {clock(passLeft)}</p>
      {view.note && <p className="note">{view.note}</p>}
      <div className="stage">STAGE</div>
      <div className="seat-map">
        {[...rows].map(([row, seats]) => (
          <div className="row" key={row}>
            <span className="row-label">{row}</span>
            {seats.map((s) => (
              <button
                key={s.id}
                className={`seat ${s.status === "open" ? "open" : "taken"}`}
                disabled={s.status !== "open" || claiming}
                title={`${s.label} · ${money(s.price_cents)}`}
                onClick={() => choose(s)}
              >
                {s.label.slice(1)}
              </button>
            ))}
          </div>
        ))}
      </div>
      <p className="legend"><span className="seat open" /> available <span className="seat taken" /> taken</p>
    </section>
  );
}

function Checkout({ view, setView }: {
  view: Extract<View, { kind: "checkout" }>;
  setView: (v: View) => void;
}) {
  const [paying, setPaying] = useState(false);
  const remaining = useCountdown(view.secondsLeft, view.startedAt);
  const backToSeats = (note: string) => setView({ kind: "lobby", pass: view.pass, note });

  useEffect(() => {
    if (remaining <= 0) backToSeats(TIME_UP);
  }, [remaining]);

  async function onPay() {
    setPaying(true);
    try {
      const r = await pay(view.seat.id);
      // Every answer that keeps Alice here comes with a fresh countdown from the server.
      const stay = (message: string) =>
        setView({ ...view, message, secondsLeft: r.seconds_left ?? 0, startedAt: performance.now() });

      switch (r.outcome) {
        case "sold":
          return setView({ kind: "sold", seat: view.seat });
        case "declined":
          return stay(`Your card was declined. ${r.attempts_left} attempt${r.attempts_left === 1 ? "" : "s"} left.`);
        case "no_reply":
        case "in_progress":
          return stay("Your bank hasn't answered yet. If it doesn't answer in time, you won't be charged.");
        case "seat_released":
          return backToSeats("Your card was declined 3 times, so the seat went back on sale.");
        case "expired":
        case "not_holder":
          return backToSeats(TIME_UP);
      }
    } finally {
      setPaying(false);
    }
  }

  const warn = remaining <= WARN_AT_SECONDS;
  return (
    <section className="card">
      <div className={`countdown ${warn ? "warn" : ""}`}>⏱ {clock(remaining)}</div>
      {warn && <p className="note warn">Less than a minute left to finish paying!</p>}
      <h2>Seat {view.seat.label}</h2>
      <p>{money(view.seat.price_cents)}</p>
      {view.message && <p className="note">{view.message}</p>}
      <button className="pay" disabled={paying} onClick={onPay}>
        {paying ? "Paying…" : `Pay ${money(view.seat.price_cents)}`}
      </button>
    </section>
  );
}

/** Counts down from the server's duration. performance.now() only moves forward,
 * so changing the computer's clock can't speed up or slow down the countdown. */
function useCountdown(seconds: number, startedAt: number): number {
  const compute = () => Math.max(0, Math.ceil(seconds - (performance.now() - startedAt) / 1000));
  const [remaining, setRemaining] = useState(compute);

  useEffect(() => {
    setRemaining(compute());
    const timer = setInterval(() => setRemaining(compute()), 250);
    return () => clearInterval(timer);
  }, [seconds, startedAt]);

  return remaining;
}
