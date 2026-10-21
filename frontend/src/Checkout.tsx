import { useEffect, useState } from "react";
import { isLoggedOut, payCart } from "./api";
import {
  clock,
  labels,
  money,
  plural,
  seatName,
  SOMETHING_WRONG,
  TIME_UP,
  total,
  useCountdown,
  WARN_AT_SECONDS,
  type Cart,
  type View,
} from "./shared";

type CheckoutView = Extract<View, { kind: "checkout" }>;

export function Checkout({ view, setView, onLoggedOut }: {
  view: CheckoutView;
  setView: (v: View) => void;
  onLoggedOut: () => void;
}) {
  const [paying, setPaying] = useState(false);
  const { cart } = view;
  const remaining = useCountdown(cart.secondsLeft, cart.startedAt);
  const backToSeats = (note: string, keep?: Cart) => setView({ kind: "event", pass: view.pass, note, cart: keep });

  useEffect(() => {
    if (remaining <= 0) backToSeats(TIME_UP);
  }, [remaining]);

  async function onPay() {
    setPaying(true);
    try {
      const r = await payCart(cart.id, cart.seats.map((s) => s.id));
      // Every answer that keeps Alice here comes with a fresh countdown from the server.
      const stay = (message: string) =>
        setView({ ...view, message, cart: { ...cart, secondsLeft: r.seconds_left ?? 0, startedAt: performance.now() } });

      switch (r.outcome) {
        case "sold":
          return setView({ kind: "sold", seats: cart.seats });
        case "declined":
          return stay(`Your card was declined. ${plural(r.attempts_left ?? 0, "attempt")} left.`);
        case "no_reply":
        case "in_progress":
          return stay("Your bank hasn't answered yet. If it doesn't answer in time, you won't be charged.");
        case "cart_changed":
          return stay("Your seats changed in another tab or window. Reload this page to see them before paying.");
        case "seat_released":
          return backToSeats("Your card was declined 3 times, so your seats went back on sale.");
        case "expired": {
          // Nothing was charged. If only some seats were lost, the rest are still held.
          const lost = cart.seats.filter((s) => r.expired_seat_ids.includes(s.id));
          const kept = cart.seats.filter((s) => !r.expired_seat_ids.includes(s.id));
          if (lost.length > 0 && kept.length > 0 && r.seconds_left) {
            return backToSeats(
              `${labels(lost)} ${lost.length === 1 ? "is" : "are"} no longer held for you, and you haven't been ` +
                `charged. Pick another seat, or pay for the ${kept.length === 1 ? "one" : plural(kept.length, "seat")} you have.`,
              { ...cart, seats: kept, secondsLeft: r.seconds_left, startedAt: performance.now() },
            );
          }
          return backToSeats(TIME_UP);
        }
        case "not_holder":
          return backToSeats(TIME_UP);
      }
    } catch (e) {
      if (isLoggedOut(e)) onLoggedOut();
      else setView({ ...view, message: SOMETHING_WRONG });
    } finally {
      setPaying(false);
    }
  }

  const warn = remaining <= WARN_AT_SECONDS;
  const price = money(total(cart.seats));
  return (
    <>
      <div className={`timer-bar ${warn ? "warn" : ""}`} role="timer">
        <div className="wrap timer-bar-inner">
          <strong>{warn ? "Less than a minute left to pay" : "Your seats are held"}</strong>
          <span className="timer-bar-right">
            <span>Seats {labels(cart.seats)} · {price}</span>
            <span className="timer-clock">{clock(remaining)}</span>
          </span>
        </div>
      </div>

      <div className="wrap checkout">
        <section className="checkout-main">
          <h1>Checkout</h1>
          {view.message && <p className="note warn" role="alert">{view.message}</p>}
          <div className="card form">
            <h2>Payment</h2>
            <div className="pay-method">
              <strong>Demo card</strong>
              <small>Charged by the test bank. No real money moves.</small>
            </div>
            <button className="primary big" disabled={paying} onClick={onPay}>
              {paying ? "Paying…" : `Pay ${price}`}
            </button>
            <p className="muted small">
              One payment for all your seats. If time runs out before the bank says yes, you won't be charged
              and the seats go back on sale.
            </p>
          </div>
        </section>

        <aside className="card order">
          <h2>Your order</h2>
          {cart.seats.map((s) => (
            <div className="cart-line" key={s.id}>
              <span>{seatName(s)}</span>
              <strong>{money(s.price_cents)}</strong>
            </div>
          ))}
          <div className="cart-total"><span>Total</span><span>{price}</span></div>
        </aside>
      </div>
    </>
  );
}
