// Correctness load test: many buyers, all arriving at once, each doing what a real
// buyer does on the site. Afterwards, `python -m app.loadtest check <event>` checks
// the database. This script only creates the pressure; the rows are the proof.
// attack.js reuses the fan from here and adds bots.
//
//   k6 run -e EVENT_ID=<id> -e BUYERS=300 loadtest/buyers.js
import http from "k6/http";
import exec from "k6/execution";
import { SharedArray } from "k6/data";
import { sleep } from "k6";
import { Counter } from "k6/metrics";

// 127.0.0.1, not localhost: on Windows, "localhost" tries IPv6 first and adds ~200 ms per connection.
export const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
export const EVENT = __ENV.EVENT_ID;
const BUYERS = Number(__ENV.BUYERS || 300);
// Session tokens from setup: everyone logged in before the sale, like real fans.
const SESSIONS = new SharedArray("sessions", () => JSON.parse(open("./sessions.json")));
const POLL_SECONDS = 3; // same as the page

const bought = new Counter("seats_bought");
const seatTaken = new Counter("seat_taken");
const limitReached = new Counter("ticket_limit_reached");
const unexpected = new Counter("unexpected_responses");

export const options = {
  scenarios: {
    // Every buyer starts at the same moment and runs once.
    buyers: { executor: "per-vu-iterations", vus: BUYERS, iterations: 1, maxDuration: "15m" },
  },
};

// k6 keeps cookies per virtual user, so each buyer has their own session, like a browser.
export function post(path, body, headers = {}) {
  const json = body === undefined ? null : JSON.stringify(body);
  const h = body === undefined ? headers : { ...headers, "Content-Type": "application/json" };
  return http.post(`${BASE}${path}`, json, { headers: h });
}

export function detail(res) {
  try {
    return res.json("detail");
  } catch (e) {
    return `error_${res.status}`;
  }
}

function fail(res) {
  unexpected.add(1, { status: String(res.status), detail: String(detail(res)) });
  return "error";
}

// Our mock CAPTCHA is solvable by a script; a real one wouldn't be.
export function solveCaptcha() {
  const c = http.get(`${BASE}/captcha`);
  if (c.status !== 200) {
    fail(c);
    return null;
  }
  const captcha = c.json();
  const [, a, b] = captcha.question.match(/(\d+) \+ (\d+)/);
  return { captcha_id: captcha.captcha_id, answer: String(Number(a) + Number(b)) };
}

// CAPTCHA, join the line, then check our place every `pollSeconds`.
// Returns the queue token, or null if the event sold out while we waited.
export function waitForTurn(pollSeconds = POLL_SECONDS) {
  const captcha = solveCaptcha();
  if (captcha === null) return null;
  let res = post(`/events/${EVENT}/queue`, captcha);
  for (;;) {
    if (res.status !== 200) {
      fail(res);
      return null;
    }
    const status = res.json();
    if (status.state === "admitted") return status.token;
    if (status.state === "sold_out") return null;
    sleep(pollSeconds);
    res = post(`/events/${EVENT}/queue`);
  }
}

// Pick a random open seat, claim it, pay. Another seat if someone beat us to it.
export function buyOneSeat(token) {
  for (;;) {
    const ev = http.get(`${BASE}/events/${EVENT}`);
    if (ev.status !== 200) return fail(ev);
    const open = ev.json("seats").filter((s) => s.status === "open");
    if (open.length === 0) {
      // Everything is held by someone; holds can still come back. Sold out ends it.
      const q = post(`/events/${EVENT}/queue`);
      if (q.status === 200 && q.json("state") === "sold_out") return "sold_out";
      sleep(POLL_SECONDS);
      continue;
    }
    const seat = open[Math.floor(Math.random() * open.length)];

    const claim = post(`/seats/${seat.id}/claim`, undefined, { "X-Queue-Token": token });
    if (claim.status === 409 && detail(claim) === "seat_taken") {
      seatTaken.add(1);
      continue;
    }
    if (claim.status === 409 && detail(claim) === "ticket_limit_reached") {
      limitReached.add(1);
      return "limit";
    }
    if (claim.status === 403 && detail(claim) === "queue_token_invalid") return "token_expired";
    if (claim.status !== 200) return fail(claim);

    const key = `${__VU}-${seat.id}-${Date.now()}-${Math.random()}`; // a new key per press of Pay
    const pay = post(`/seats/${seat.id}/pay`, undefined, { "Idempotency-Key": key });
    if (pay.status === 200 && pay.json("outcome") === "sold") {
      bought.add(1);
      return "sold";
    }
    return fail(pay);
  }
}

// One fan per iteration: the scenario's iteration number picks their account.
export function fan() {
  if (!EVENT) throw new Error("Run with -e EVENT_ID=<id printed by: python -m app.loadtest setup>");
  const n = exec.scenario.iterationInTest;
  if (n >= SESSIONS.length) throw new Error("More BUYERS than setup logged in");

  http.cookieJar().set(BASE, "session", SESSIONS[n]); // what the browser stored at login

  // Buyers want 1 to 4 tickets. Those who want 4 try to go over the limit.
  let wanted = 1 + (n % 4);
  while (wanted > 0) {
    const token = waitForTurn();
    if (token === null) return; // sold out
    const result = buyOneSeat(token);
    if (result === "sold") wanted--; // then back to the queue, like "Buy another seat" on the site
    else if (result !== "token_expired") return;
  }
}

export default fan;
