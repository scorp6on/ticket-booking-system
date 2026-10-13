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
  | "in_progress";

export type PayResult = {
  outcome: PayOutcome;
  attempts_left: number | null;
  seconds_left: number | null;
};

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

/** Returns seconds left on the hold. Throws ApiError "seat_taken", "ticket_limit_reached",
 * or "queue_token_invalid" (our time to pick a seat ran out). */
export const claimSeat = async (seatId: number, queueToken: string) =>
  (await call<{ seconds_left: number }>("POST", `/seats/${seatId}/claim`, undefined, { "X-Queue-Token": queueToken }))
    .seconds_left;

/** One call per press of Pay. A new key each press: a retry after a decline is a new request.
 * No amount is sent: the server decides the price. */
export const pay = (seatId: number) =>
  call<PayResult>("POST", `/seats/${seatId}/pay`, undefined, { "Idempotency-Key": crypto.randomUUID() });
