// Capacity test: how many people can wait in line before the site gets too slow?
// Each waiting page asks "where am I?" every 3 seconds, so N people waiting means
// N/3 requests per second. The test steps N up (300, 600, 1000, ...), holds each step,
// and measures each step on its own.
//
// k6 doesn't run one browser per person (thousands wouldn't fit in memory). It sends
// the same requests at the same rate, each with that person's own session.
//
//   python -m app.loadtest capacity 5000   (from backend/; sets everything up and prints the table)
import http from "k6/http";
import exec from "k6/execution";
import { SharedArray } from "k6/data";

// 127.0.0.1, not localhost: on Windows, "localhost" tries IPv6 first and adds ~200 ms per connection.
const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const EVENT = __ENV.EVENT_ID;
const STEPS = (__ENV.STEPS || "").split(",").filter(Boolean).map(Number); // people waiting
const SESSIONS = new SharedArray("waiters", () => JSON.parse(open("./waiters.json")));
const POLL_SECONDS = 3; // same as the page
const RAMP_SECONDS = 10; // moving to the next step; not measured
const HOLD_SECONDS = 40; // measured
const P95_LIMIT_MS = 3000;
const ERROR_LIMIT = 0.01;

const stages = STEPS.flatMap((n) => [
  { target: Math.round(n / POLL_SECONDS), duration: `${RAMP_SECONDS}s` },
  { target: Math.round(n / POLL_SECONDS), duration: `${HOLD_SECONDS}s` },
]);

// One threshold pair per step, so the summary shows each step's p95 and error rate.
const thresholds = {};
for (const n of STEPS) {
  thresholds[`http_req_duration{waiters:${n}}`] = [`p(95)<${P95_LIMIT_MS}`];
  thresholds[`http_req_failed{waiters:${n}}`] = [`rate<${ERROR_LIMIT}`];
}

export const options = {
  scenarios: {
    waiting: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      stages,
      preAllocatedVUs: 300,
      maxVUs: 600, // each in-flight request needs one; more would crowd the laptop's memory
    },
  },
  thresholds,
};

// Which step we're in right now, and whether it's being measured (held) or ramping up.
function currentStep() {
  const seconds = exec.instance.currentTestRunDuration / 1000;
  const block = Math.min(Math.floor(seconds / (RAMP_SECONDS + HOLD_SECONDS)), STEPS.length - 1);
  const ramping = seconds - block * (RAMP_SECONDS + HOLD_SECONDS) < RAMP_SECONDS;
  return { waiters: STEPS[block], measured: !ramping };
}

export default function () {
  const { waiters, measured } = currentStep();
  // The first `waiters` people take turns, so each one checks in about every 3 seconds.
  const session = SESSIONS[exec.scenario.iterationInTest % waiters];
  http.post(`${BASE}/events/${EVENT}/queue`, null, {
    headers: { Cookie: `session=${session}` },
    tags: { waiters: measured ? String(waiters) : "ramp" },
    timeout: "10s", // a check this slow has failed the fan anyway
  });
}
