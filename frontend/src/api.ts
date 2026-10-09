export type Seat = {
  id: number;
  label: string;
  price_cents: number;
  status: "open" | "held" | "payment_pending" | "sold";
};

export type Event = {
  id: number;
  name: string;
  starts_at: string;
  seats: Seat[];
};

export type PayOutcome =
  | "sold"
  | "declined"
  | "seat_released"
  | "no_reply"
  | "expired"
  | "not_holder"
  | "in_progress";

export type PayResult = {
  outcome: PayOutcome;
  attempts_left: number | null;
  seconds_left: number | null;
};

// Stand-in for a login: each browser tab is a different buyer, so two tabs
// can race for the same seat. Replaced by real accounts in the anti-bot milestone.
export const userId = (() => {
  let id = sessionStorage.getItem("userId");
  if (!id) {
    id = `buyer-${Math.random().toString(36).slice(2, 6)}`;
    sessionStorage.setItem("userId", id);
  }
  return id;
})();

export async function getEvent(eventId: number): Promise<Event> {
  const r = await fetch(`/api/events/${eventId}`);
  if (!r.ok) throw new Error(`event ${eventId} not found`);
  return r.json();
}

/** Returns seconds left on the hold, or null if someone else got the seat first. */
export async function claimSeat(seatId: number): Promise<number | null> {
  const r = await fetch(`/api/seats/${seatId}/claim`, {
    method: "POST",
    headers: { "X-User-Id": userId },
  });
  if (r.status === 409) return null;
  if (!r.ok) throw new Error(`claim failed: ${r.status}`);
  return (await r.json()).seconds_left;
}

/** One call per press of Pay. A new key each press: a retry after a decline is a new request.
 * No amount is sent: the server decides the price. */
export async function pay(seatId: number): Promise<PayResult> {
  const r = await fetch(`/api/seats/${seatId}/pay`, {
    method: "POST",
    headers: { "X-User-Id": userId, "Idempotency-Key": crypto.randomUUID() },
  });
  if (!r.ok) throw new Error(`pay failed: ${r.status}`);
  return r.json();
}
