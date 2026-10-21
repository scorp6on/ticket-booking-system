// Bot attack load test: the 300 fans from buyers.js buy while bots attack at the same
// time. Every bot is verified and logged in, the strongest version of each attack.
//
//   skippers   claim seats without ever waiting in line
//   forgers    claim seats with made-up queue tokens
//   reusers    all join the line with ONE solved CAPTCHA
//   hammerers  arrive after the fans, then check their place nonstop to get ahead
//   scalper    one real account in 20 tabs, grabbing every seat it can
//
// It passes when the thresholds below pass (k6 prints them) AND
// `python -m app.loadtest check <event>` passes. Run it through
//
//   python -m app.loadtest bot-test 300   (from backend/)
//
// which first runs the fans alone to measure how fast they are today, then passes
// 2x those numbers in as MAX_FAN_MED_MS and MAX_FAN_P95_MS.
import http from "k6/http";
import exec from "k6/execution";
import { sleep } from "k6";
import { Counter } from "k6/metrics";
import { BASE, EVENT, post, detail, solveCaptcha, waitForTurn, buyOneSeat, fan } from "./buyers.js";

const BOTS = JSON.parse(open("./bots.json"));
const BUYERS = Number(__ENV.BUYERS || 300);
const OPENS_AT = Number(__ENV.OPENS_AT);
const MAX_FAN_MED_MS = Number(__ENV.MAX_FAN_MED_MS);
const MAX_FAN_P95_MS = Number(__ENV.MAX_FAN_P95_MS);
const ATTACK_UNTIL = OPENS_AT + 120 * 1000; // bots give up 2 minutes after the sale opens
const SCALPER_TABS = 20;

const gotThrough = new Counter("bot_got_through");
const refused = new Counter("bot_refused");


const once = (n, execName, startTime = "0s") => ({
  executor: "per-vu-iterations",
  vus: n,
  iterations: 1,
  maxDuration: "15m",
  exec: execName,
  startTime,
  tags: { who: execName },
});

export const options = {
  scenarios: {
    fan: once(BUYERS, "fan"),
    skipper: once(BOTS.skipper.length, "skipper"),
    forger: once(BOTS.forger.length, "forger"),
    reuser: once(BOTS.reuser.length, "reuser"),
    hammerer: once(BOTS.hammerer.length, "hammerer"),
    scalper: once(SCALPER_TABS, "scalper"),
  },
  thresholds: {
    "http_req_duration{who:fan}": [`med<${MAX_FAN_MED_MS}`, `p(95)<${MAX_FAN_P95_MS}`],
    "bot_got_through{kind:skipper}": ["count==0"],
    "bot_got_through{kind:forger}": ["count==0"],
    "bot_got_through{kind:reuser}": ["count<=1"], // one CAPTCHA, solved once: at most one way in
  },
};

// Runs once before everyone: one bot solves a single CAPTCHA for all the reusers.
export function setup() {
  if (!EVENT || !OPENS_AT || !MAX_FAN_MED_MS || !MAX_FAN_P95_MS) {
    throw new Error("Run through: python -m app.loadtest bot-test 300 (it measures the fans-only baseline first)");
  }
  return { sharedCaptcha: solveCaptcha() };
}

export { fan };

function logInAs(bot) {
  http.cookieJar().set(BASE, "session", bot.token);
}

function waitUntil(ms) {
  const seconds = (ms - Date.now()) / 1000;
  if (seconds > 0) sleep(seconds);
}

function openSeats() {
  const ev = http.get(`${BASE}/events/${EVENT}`);
  return ev.status === 200 ? ev.json("seats").filter((s) => s.status === "open") : [];
}

function result(kind, res) {
  if (res.status === 200 || res.status === 201) gotThrough.add(1, { kind });
  else refused.add(1, { kind, detail: String(detail(res)) });
}

// Keep trying open seats twice a second until the sale is over.
function attackSeats(kind, headersFor) {
  waitUntil(OPENS_AT);
  while (Date.now() < ATTACK_UNTIL) {
    const open = openSeats();
    if (open.length === 0 && Date.now() > OPENS_AT + 30 * 1000) return; // sold out (or all held)
    for (const seat of open.slice(0, 3)) {
      result(kind, post("/carts", { seat_ids: [seat.id] }, headersFor()));
    }
    sleep(0.5);
  }
}

export function skipper() {
  logInAs(BOTS.skipper[exec.scenario.iterationInTest]);
  attackSeats("skipper", () => ({})); // no X-Queue-Token at all
}

export function forger() {
  const me = BOTS.forger[exec.scenario.iterationInTest];
  logInAs(me);
  const randomHex = () => Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  // Right format, right event, own user id, long expiry; only the signature is invented.
  attackSeats("forger", () => ({ "X-Queue-Token": `${EVENT}:${Date.now() + 3600 * 1000}:${me.user_id}:${randomHex()}` }));
}

export function reuser(data) {
  logInAs(BOTS.reuser[exec.scenario.iterationInTest]);
  result("reuser", post(`/events/${EVENT}/queue`, data.sharedCaptcha));
  // A reuser that got in just stops checking in, and the heartbeat drops it.
}

export function hammerer() {
  logInAs(BOTS.hammerer[exec.scenario.iterationInTest]);
  waitUntil(OPENS_AT + 1000); // arrives after the fans are already in line
  const token = waitForTurn(0); // checks its place nonstop instead of every 3 seconds
  if (token !== null) buyOneSeat(token);
}

export function scalper() {
  logInAs(BOTS.scalper); // every tab shares one account
  const token = waitForTurn();
  if (token === null) return;
  // Each tab keeps buying with the same token until something stops it.
  while (buyOneSeat(token) === "sold");
}
