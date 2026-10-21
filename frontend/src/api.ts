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

export type Account = { email: string; id_verified: boolean };

export type PayOutcome =
  | "sold"
  | "declined"
  | "seat_released"
  | "no_reply"
  | "expired"
  | "not_holder"
  | "cart_changed"
  | "in_progress";

export type PayResult = {
  outcome: PayOutcome;
  attempts_left: number | null;
  seconds_left: number | null;
  expired_seat_ids: number[]; // seats on screen that are no longer held
};

export type HoldResult = { cart_id: number; held: number[]; taken: number[]; seconds_left: number };

export type QueueStatus =
  | { state: "waiting"; position: number; sale_open: boolean }
  | { state: "admitted"; token: string; seconds_left: number; sale_open: boolean }
  | { state: "sold_out" | "not_in_line"; sale_open: boolean };

export type Captcha = { captcha_id: string; question: string };

/** An error the server explained, e.g. "seat_taken" or "captcha_required". */
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

/** The session ended (logged out elsewhere, or expired): the page should ask to log in again. */
export const isLoggedOut = (e: unknown) => e instanceof ApiError && e.status === 401;

/** No answer, or the server failed: likely brief, so keep the page and try again. */
export const isConnectionProblem = (e: unknown) => !(e instanceof ApiError) || e.status >= 500;

// The session cookie is HttpOnly: this code never sees it. The browser attaches it
// to every request automatically, so nothing here sends a user id.
async function call<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? headers : { ...headers, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, typeof data.detail === "string" ? data.detail : `error_${r.status}`);
  return data as T;
}

export const signUp = (email: string, password: string) => call("POST", "/accounts", { email, password });
export const logIn = (email: string, password: string) => call("POST", "/sessions", { email, password });
export const logOut = () => call("DELETE", "/sessions");
export const me = () => call<Account>("GET", "/me");
export const verifyId = (id_number: string) => call("POST", "/me/verify-id", { id_number });
export const getCaptcha = () => call<Captcha>("GET", "/captcha");
export const getEvent = (eventId: number) => call<Event>("GET", `/events/${eventId}`);

/** Joins the line (needs a solved CAPTCHA the first time) and reports where we stand. */
export const queueStatus = (eventId: number, captcha?: { captcha_id: string; answer: string }) =>
  call<QueueStatus>("POST", `/events/${eventId}/queue`, captcha);

/** Holds the seats we win; `taken` lists the ones someone else got. Throws ApiError "seat_taken"
 * (all of them), "ticket_limit_reached", "cart_active", or "queue_token_invalid" (our time to pick ran out). */
export const holdSeats = (seatIds: number[], queueToken: string) =>
  call<HoldResult>("POST", "/carts", { seat_ids: seatIds }, { "X-Queue-Token": queueToken });

/** Adds a replacement seat; it ends at the cart's deadline. Returns seconds left on the cart.
 * Throws ApiError "seat_taken", "ticket_limit_reached", "cart_expired", "cart_locked" or "queue_token_invalid". */
export const addSeat = async (cartId: number, seatId: number, queueToken: string) =>
  (await call<{ seconds_left: number }>("POST", `/carts/${cartId}/seats`, { seat_id: seatId }, { "X-Queue-Token": queueToken }))
    .seconds_left;

/** One call per press of Pay. A new key each press: a retry after a decline is a new request.
 * Sends the seats on screen so the server can tell if they changed. No amount: the server decides the price. */
export const payCart = (cartId: number, seatIds: number[]) =>
  call<PayResult>("POST", `/carts/${cartId}/pay`, { seat_ids: seatIds }, { "Idempotency-Key": crypto.randomUUID() });
