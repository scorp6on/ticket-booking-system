import { useEffect, useState } from "react";
import type { Seat } from "./api";

export const eventId = Number(new URLSearchParams(location.search).get("event") ?? 1);
export const TICKET_LIMIT = 3;
export const WARN_AT_SECONDS = 60;
export const TIME_UP = "Your time is up, so your seats went back on sale. You haven't been charged.";
export const PASS_EXPIRED = "Your time to pick seats ran out, so you've rejoined the line.";
export const SOMETHING_WRONG = "Something went wrong. Please try again.";
export const NO_EVENT = "This event doesn't exist. Did you run the seed script?";

/** Proof we waited our turn, plus how long it lasts. */
export type Pass = { token: string; secondsLeft: number; startedAt: number };

/** Seats held for us together, and the countdown the server sent for them. */
export type Cart = { id: number; seats: Seat[]; secondsLeft: number; startedAt: number };

export type View =
  | { kind: "queue"; note?: string }
  | { kind: "event"; pass: Pass; note?: string; cart?: Cart } // cart: some seats held, picking a replacement
  | { kind: "checkout"; pass: Pass; cart: Cart; message?: string }
  | { kind: "sold"; seats: Seat[] };

export const money = (cents: number) => `$${(cents / 100).toFixed(2)}`;
export const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
export const total = (seats: Seat[]) => seats.reduce((sum, s) => sum + s.price_cents, 0);
export const seatName = (s: Seat) => `Row ${s.label[0]}, Seat ${s.label.slice(1)}`;
export const labels = (seats: Seat[]) => seats.map((s) => s.label).join(", ");
export const formatDate = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Counts down from the server's duration. performance.now() only moves forward,
 * so changing the computer's clock can't speed up or slow down the countdown.
 * Computed on every render, so a new countdown is right from its first render. */
export function useCountdown(seconds: number, startedAt: number): number {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(timer);
  }, []);
  return Math.max(0, Math.ceil(seconds - (performance.now() - startedAt) / 1000));
}
