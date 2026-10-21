import { useCallback, useEffect, useState } from "react";
import { addSeat, ApiError, getEvent, holdSeats, isLoggedOut, type Event, type Seat } from "./api";
import {
  clock,
  eventId,
  formatDate,
  labels,
  money,
  NO_EVENT,
  PASS_EXPIRED,
  plural,
  seatName,
  SOMETHING_WRONG,
  TICKET_LIMIT,
  TIME_UP,
  total,
  useCountdown,
  WARN_AT_SECONDS,
  type View,
} from "./shared";

const REFRESH_MS = 5000;

type EventView = Extract<View, { kind: "event" }>;

/** What a failed hold or add means for the buyer. */
const BUY_ERRORS: Record<string, string> = {
  seat_taken: "Sorry, that seat was just taken by someone else. Please pick another.",
  ticket_limit_reached: "You can have at most 3 seats for this event, counting ones you've already bought.",
  cart_active: "You already have seats on hold in another tab or window. Finish there, or wait until that hold ends.",
  cart_locked: "Payment has started for your seats in another tab or window, so they can't be changed here.",
};

export function EventPage({ view, setView, onLoggedOut }: {
  view: EventView;
  setView: (v: View) => void;
  onLoggedOut: () => void;
}) {
  const [event, setEvent] = useState<Event | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [picks, setPicks] = useState<number[]>([]); // chosen on the map, not held yet
  const [priceFilter, setPriceFilter] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const cart = view.cart;
  const passLeft = useCountdown(view.pass.secondsLeft, view.pass.startedAt);
  const cartLeft = useCountdown(cart?.secondsLeft ?? 0, cart?.startedAt ?? 0);

  const load = useCallback(() => {
    getEvent(eventId).then(
      (e) => {
        setEvent(e);
        setReconnecting(false);
      },
      (e) => {
        if (e instanceof ApiError && e.status === 404) setFatal(NO_EVENT);
        else setReconnecting(true); // keep showing the last map; the next refresh may work
      },
    );
  }, []);

  // The map is only a hint and goes stale, so refresh it. The hold decides who gets a seat.
  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // A seat someone else took since we picked it drops out of our picks.
  useEffect(() => {
    if (event) setPicks((p) => p.filter((id) => event.seats.find((s) => s.id === id)?.status === "open"));
  }, [event]);

  useEffect(() => {
    if (!cart && passLeft <= 0) setView({ kind: "queue", note: PASS_EXPIRED });
  }, [cart, passLeft, setView]);

  useEffect(() => {
    if (cart && cartLeft <= 0) setView({ ...view, cart: undefined, note: TIME_UP });
  }, [cart, cartLeft]);

  function onBuyError(e: unknown) {
    if (isLoggedOut(e)) return onLoggedOut();
    const detail = e instanceof ApiError ? e.detail : "";
    if (detail === "queue_token_invalid") return setView({ kind: "queue", note: PASS_EXPIRED });
    if (detail === "cart_expired") return setView({ ...view, cart: undefined, note: TIME_UP });
    setView({ ...view, note: BUY_ERRORS[detail] ?? SOMETHING_WRONG });
    load();
  }

  async function hold() {
    if (!event) return;
    setBusy(true);
    try {
      const r = await holdSeats(picks, view.pass.token);
      const held = event.seats.filter((s) => r.held.includes(s.id));
      const taken = event.seats.filter((s) => r.taken.includes(s.id));
      const newCart = { id: r.cart_id, seats: held, secondsLeft: r.seconds_left, startedAt: performance.now() };
      setPicks([]);
      if (taken.length === 0) return setView({ kind: "checkout", pass: view.pass, cart: newCart });
      // Some seats went to someone else: keep the rest held and let the buyer replace them.
      setView({
        ...view,
        cart: newCart,
        note:
          `${taken.length === 1 ? "Seat" : "Seats"} ${labels(taken)} ${taken.length === 1 ? "was" : "were"} just taken ` +
          `by someone else. ${labels(held)} ${held.length === 1 ? "is" : "are"} held for you until the timer runs out. ` +
          `Pick another seat, or pay for the ${held.length === 1 ? "one" : plural(held.length, "seat")} you have.`,
      });
      load();
    } catch (e) {
      setPicks([]);
      onBuyError(e);
    } finally {
      setBusy(false);
    }
  }

  async function addReplacement(seat: Seat) {
    if (!cart) return;
    setBusy(true);
    try {
      const secondsLeft = await addSeat(cart.id, seat.id, view.pass.token);
      setView({
        ...view,
        note: undefined,
        cart: { ...cart, seats: [...cart.seats, seat], secondsLeft, startedAt: performance.now() },
      });
      load();
    } catch (e) {
      onBuyError(e);
    } finally {
      setBusy(false);
    }
  }

  function onSeat(seat: Seat) {
    if (cart) return addReplacement(seat);
    setPicks((p) => (p.includes(seat.id) ? p.filter((id) => id !== seat.id) : [...p, seat.id]));
  }

  if (fatal) return <div className="page-center"><p className="note warn">{fatal}</p></div>;
  if (!event) return <div className="page-center"><p>Loading…</p></div>;

  const mine = cart ? cart.seats : event.seats.filter((s) => picks.includes(s.id));
  const mineIds = new Set(mine.map((s) => s.id));
  const full = mine.length >= TICKET_LIMIT;
  const timeLeft = cart ? cartLeft : passLeft;

  const rows = new Map<string, Seat[]>();
  for (const s of event.seats) rows.set(s.label[0], [...(rows.get(s.label[0]) ?? []), s]);

  const prices = [...new Set(event.seats.map((s) => s.price_cents))].sort((a, b) => b - a);
  const openCount = (seats: Seat[]) => seats.filter((s) => s.status === "open").length;
  const tiers = prices.map((price) => {
    const seats = event.seats.filter((s) => s.price_cents === price);
    const rowLetters = [...new Set(seats.map((s) => s.label[0]))];
    const name = rowLetters.length === 1
      ? `Row ${rowLetters[0]}`
      : `Rows ${rowLetters[0]}–${rowLetters[rowLetters.length - 1]}`;
    return { price, name, left: openCount(seats) };
  });

  return (
    <>
      <section className="event-header">
        <div className="wrap event-header-inner">
          <div className="titles">
            <span className="eyebrow">On sale now</span>
            <h1>{event.name}</h1>
            <span className="when">{formatDate(event.starts_at)}</span>
          </div>
          <div className={`timer-pill ${timeLeft <= WARN_AT_SECONDS ? "warn" : ""}`}>
            <span>{cart ? "Your seats are held for" : "Time to pick your seats"}</span>
            <strong>{clock(timeLeft)}</strong>
          </div>
        </div>
      </section>

      {reconnecting && <div className="banner" role="status">Connection lost. Reconnecting… The map may be out of date.</div>}

      <div className="wrap">
        {view.note && <p className="note warn" role="alert">{view.note}</p>}

        <div className="event-body">
          <section className="card seat-map-card">
            <div className="stage">STAGE</div>
            <div className="seat-map">
              {[...rows].map(([row, seats]) => (
                <div className="row" key={row}>
                  <span className="row-label">{row}</span>
                  {seats.map((s) => {
                    const isMine = mineIds.has(s.id);
                    const open = s.status === "open" && !isMine;
                    const filteredOut = open && priceFilter !== null && s.price_cents !== priceFilter;
                    const state = isMine ? "your pick" : open ? "available" : "taken";
                    return (
                      <button
                        key={s.id}
                        className={`seat ${isMine ? "mine" : open ? "open" : "taken"} ${filteredOut ? "dim" : ""}`}
                        disabled={busy || (cart ? !open || full : !isMine && (!open || full)) || filteredOut}
                        aria-label={`${seatName(s)}, ${money(s.price_cents)}, ${state}`}
                        title={`${s.label} · ${money(s.price_cents)}`}
                        onClick={() => onSeat(s)}
                      >
                        {s.label.slice(1)}
                      </button>
                    );
                  })}
                  <span className="row-label">{row}</span>
                </div>
              ))}
            </div>
            <div className="legend">
              <span><i className="swatch open" />Available</span>
              <span><i className="swatch mine" />Your pick</span>
              <span><i className="swatch taken" />Taken</span>
            </div>
          </section>

          <aside className="side">
            <section className="card">
              <h2>Prices</h2>
              <div className="tiers">
                <button
                  className={`tier ${priceFilter === null ? "active" : ""}`}
                  aria-pressed={priceFilter === null}
                  onClick={() => setPriceFilter(null)}
                >
                  <span><strong>All seats</strong><small>{openCount(event.seats)} left</small></span>
                  <strong>from {money(prices[prices.length - 1] ?? 0)}</strong>
                </button>
                {tiers.map((t) => (
                  <button
                    key={t.price}
                    className={`tier ${priceFilter === t.price ? "active" : ""}`}
                    aria-pressed={priceFilter === t.price}
                    onClick={() => setPriceFilter(t.price)}
                  >
                    <span><strong>{t.name}</strong><small>{t.left} left</small></span>
                    <strong>{money(t.price)}</strong>
                  </button>
                ))}
              </div>
            </section>

            <section className="card cart">
              <div className="cart-title">
                <h2>Your seats</h2>
                <small>{mine.length} of {TICKET_LIMIT}</small>
              </div>
              {mine.length === 0 && <p className="muted">Pick up to 3 seats on the map.</p>}
              {mine.map((s) => (
                <div className="cart-line" key={s.id}>
                  <span><strong>{seatName(s)}</strong><small>{money(s.price_cents)}{cart && " · held"}</small></span>
                  {!cart && (
                    <button aria-label={`Remove seat ${s.label}`} onClick={() => onSeat(s)}>Remove</button>
                  )}
                </div>
              ))}
              {full && <p className="note warn small">That's the limit: 3 seats per person for this event.</p>}
              <div className="cart-total"><span>Total</span><span>{money(total(mine))}</span></div>
              {cart ? (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => setView({ kind: "checkout", pass: view.pass, cart })}
                >
                  Pay for {plural(cart.seats.length, "seat")}
                </button>
              ) : (
                <button className="primary" disabled={busy || mine.length === 0} onClick={hold}>
                  {mine.length === 0 ? "Pick a seat" : `Hold ${plural(mine.length, "seat")} and pay`}
                </button>
              )}
              <p className="muted small">
                Seats are held for you for 5 minutes while you pay. The price shown is the price you pay.
              </p>
            </section>
          </aside>
        </div>
      </div>
    </>
  );
}
