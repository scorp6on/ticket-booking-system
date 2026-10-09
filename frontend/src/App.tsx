import { useCallback, useEffect, useState } from "react";
import { claimSeat, getEvent, pay, userId, type Event, type Seat } from "./api";

const eventId = Number(new URLSearchParams(location.search).get("event") ?? 1);
const REFRESH_MS = 5000;
const WARN_AT_SECONDS = 60;
const TIME_UP = "Your time is up, so the seat went back on sale. You haven't been charged.";

type View =
  | { kind: "lobby"; note?: string }
  | { kind: "checkout"; seat: Seat; secondsLeft: number; startedAt: number; message?: string }
  | { kind: "sold"; seat: Seat };

const money = (cents: number) => `$${(cents / 100).toFixed(2)}`;

export default function App() {
  const [view, setView] = useState<View>({ kind: "lobby" });

  return (
    <main>
      <header>
        <h1>Ticket Booking</h1>
        <span className="who">You are {userId}</span>
      </header>
      {view.kind === "lobby" && <Lobby note={view.note} onHeld={(seat, secondsLeft) =>
        setView({ kind: "checkout", seat, secondsLeft, startedAt: performance.now() })} onNote={(note) =>
        setView({ kind: "lobby", note })} />}
      {view.kind === "checkout" && <Checkout view={view} setView={setView} />}
      {view.kind === "sold" && (
        <section className="card">
          <h2>🎉 Seat {view.seat.label} is yours</h2>
          <p>{money(view.seat.price_cents)} charged.</p>
          <button onClick={() => setView({ kind: "lobby" })}>Back to seats</button>
        </section>
      )}
    </main>
  );
}

function Lobby({ note, onHeld, onNote }: {
  note?: string;
  onHeld: (seat: Seat, secondsLeft: number) => void;
  onNote: (note: string) => void;
}) {
  const [event, setEvent] = useState<Event | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [claiming, setClaiming] = useState(false);

  const load = useCallback(() => {
    getEvent(eventId).then(setEvent, (e) => setError(String(e.message)));
  }, []);

  // The map is only a hint and goes stale, so refresh it. The claim decides who gets a seat.
  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function choose(seat: Seat) {
    setClaiming(true);
    try {
      const secondsLeft = await claimSeat(seat.id);
      if (secondsLeft === null) {
        onNote(`Seat ${seat.label} was just taken by someone else. Please pick another seat.`);
        load();
      } else {
        onHeld(seat, secondsLeft);
      }
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
      {note && <p className="note">{note}</p>}
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

  useEffect(() => {
    if (remaining <= 0) setView({ kind: "lobby", note: TIME_UP });
  }, [remaining, setView]);

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
          return setView({ kind: "lobby", note: "Your card was declined 3 times, so the seat went back on sale." });
        case "expired":
        case "not_holder":
          return setView({ kind: "lobby", note: TIME_UP });
      }
    } finally {
      setPaying(false);
    }
  }

  const warn = remaining <= WARN_AT_SECONDS;
  return (
    <section className="card">
      <div className={`countdown ${warn ? "warn" : ""}`}>
        ⏱ {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, "0")}
      </div>
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
