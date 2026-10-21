import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  getCaptcha,
  getEvent,
  isConnectionProblem,
  isLoggedOut,
  queueStatus,
  type Captcha,
  type Event,
  type QueueStatus,
} from "./api";
import { eventId, formatDate, NO_EVENT, SOMETHING_WRONG, type Pass } from "./shared";

const QUEUE_POLL_MS = 3000;

export function Queue({ note, onAdmitted, onLoggedOut }: {
  note?: string;
  onAdmitted: (pass: Pass) => void;
  onLoggedOut: () => void;
}) {
  const [event, setEvent] = useState<Event | null>(null);
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [captcha, setCaptcha] = useState<Captcha | null>(null);
  const [answer, setAnswer] = useState("");
  const [wrongAnswer, setWrongAnswer] = useState(false);
  const [inLine, setInLine] = useState(false);
  // The progress bar measures from the first place we saw, so the server needn't send the line's length.
  const firstPosition = useRef<number | null>(null);

  useEffect(() => {
    getEvent(eventId).then(setEvent, () => {}); // just the name and date; the line works without them
  }, []);

  const handle = useCallback(
    (s: QueueStatus) => {
      setReconnecting(false);
      if (s.state === "admitted") {
        onAdmitted({ token: s.token, secondsLeft: s.seconds_left, startedAt: performance.now() });
        return;
      }
      if (s.state === "waiting" && s.sale_open && firstPosition.current === null) firstPosition.current = s.position;
      setStatus(s);
    },
    [onAdmitted],
  );

  const askForCaptcha = useCallback(() => {
    setInLine(false);
    setAnswer("");
    getCaptcha().then(
      (c) => {
        setReconnecting(false);
        setCaptcha(c);
      },
      () => setReconnecting(true),
    );
  }, []);

  const onError = useCallback(
    (e: unknown) => {
      if (isLoggedOut(e)) onLoggedOut();
      else if (e instanceof ApiError && e.detail === "captcha_required") askForCaptcha();
      else if (e instanceof ApiError && e.detail === "event_not_found") setFatal(NO_EVENT);
      else if (isConnectionProblem(e)) setReconnecting(true); // keep the page; the next check may work
      else setFatal(SOMETHING_WRONG);
    },
    [askForCaptcha, onLoggedOut],
  );

  // First check without a CAPTCHA: if we're already in line (say, after a refresh), we keep our spot.
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const check = () =>
      queueStatus(eventId).then(
        (s) => {
          if (stopped) return;
          setInLine(true);
          handle(s);
        },
        (e) => {
          if (stopped) return;
          onError(e);
          if (isConnectionProblem(e)) timer = window.setTimeout(check, QUEUE_POLL_MS);
        },
      );
    check();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [handle, onError]);

  // Once in line, keep checking. Each check also lets people in as spots free up.
  // The next check is scheduled after the answer arrives, so a slow server never has
  // several of our checks piled up.
  useEffect(() => {
    if (!inLine) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = () =>
      queueStatus(eventId).then(
        (s) => {
          if (stopped) return;
          handle(s);
          if (s.state !== "admitted") timer = window.setTimeout(poll, QUEUE_POLL_MS);
        },
        (e) => {
          if (stopped) return;
          onError(e);
          if (isConnectionProblem(e)) timer = window.setTimeout(poll, QUEUE_POLL_MS);
        },
      );
    timer = window.setTimeout(poll, QUEUE_POLL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [inLine, handle, onError]);

  async function submitCaptcha(e: FormEvent) {
    e.preventDefault();
    if (!captcha) return;
    try {
      const s = await queueStatus(eventId, { captcha_id: captcha.captcha_id, answer });
      setCaptcha(null);
      setWrongAnswer(false);
      setInLine(true);
      handle(s);
    } catch (e) {
      if (e instanceof ApiError && e.detail === "captcha_required") {
        setWrongAnswer(true);
        askForCaptcha(); // each puzzle works once, so a wrong answer gets a new one
      } else {
        onError(e);
      }
    }
  }

  const first = firstPosition.current;
  const progress =
    status?.state === "waiting" && first ? Math.round(((first - status.position) / first) * 100) : 0;

  return (
    <div className="waiting">
      {reconnecting && (
        <div className="banner" role="status">
          Connection lost. Reconnecting…{inLine && " You still have your place in line."}
        </div>
      )}
      <div className="waiting-inner">
        <div className="titles">
          <span className="eyebrow">Waiting room</span>
          <h1>{event?.name ?? "Ticket sale"}</h1>
          {event && <span className="when">{formatDate(event.starts_at)}</span>}
        </div>

        <section className="card">
          {note && <p className="note warn">{note}</p>}
          {fatal ? (
            <p className="note warn">{fatal}</p>
          ) : captcha ? (
            <form className="form" onSubmit={submitCaptcha}>
              <h2>Before you join the line</h2>
              <p>Prove you're human: <strong>{captcha.question}</strong></p>
              <p className="demo">Demo CAPTCHA. A real site would use a service like Cloudflare Turnstile.</p>
              {wrongAnswer && <p className="note warn">That wasn't right. Here's a new one.</p>}
              <label>Answer <input value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
              <button type="submit" className="primary">Join the line</button>
            </form>
          ) : !inLine && reconnecting ? (
            <div className="form">
              <p>We couldn't reach the server.</p>
              <button type="button" onClick={askForCaptcha}>Try again</button>
            </div>
          ) : status?.state === "sold_out" ? (
            <>
              <h2>Sold out</h2>
              <p>Every seat has been sold.</p>
            </>
          ) : status?.state === "waiting" && !status.sale_open ? (
            <>
              <span className="label">The sale hasn't opened yet</span>
              <p className="lead">
                You're in. When the sale opens, everyone who's here gets a <strong>random place in line</strong>,
                so arriving earlier doesn't help. No need to refresh.
              </p>
            </>
          ) : status?.state === "waiting" ? (
            <>
              <span className="label">Your place in line</span>
              <span className="big-number">#{status.position}</span>
              <div
                className="progress"
                role="progressbar"
                aria-label="Progress through the line"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress}
              >
                <div style={{ width: `${progress}%` }} />
              </div>
              <div className="progress-labels">
                <span>You joined at #{first}</span>
                <span>Your turn</span>
              </div>
              <p className="lead">
                Keep this tab open. You'll go straight to the seat map when it's your turn, and you'll have{" "}
                <strong>5 minutes</strong> to pick up to 3 seats.
              </p>
            </>
          ) : (
            <p>Checking the line…</p>
          )}
          {inLine && !fatal && status?.state !== "sold_out" && (
            <p className="info">Refreshing or closing this tab for a moment won't lose your place. Leaving for longer will.</p>
          )}
        </section>
      </div>
    </div>
  );
}
