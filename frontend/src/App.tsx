import { useCallback, useEffect, useState } from "react";
import { logOut, me, type Account } from "./api";
import { Auth, VerifyId } from "./Auth";
import { Checkout } from "./Checkout";
import { EventPage } from "./EventPage";
import { Queue } from "./Queue";
import { labels, money, total, type Pass, type View } from "./shared";

export default function App() {
  // undefined = still checking the session cookie, null = logged out
  const [account, setAccount] = useState<Account | null | undefined>(undefined);
  const [view, setView] = useState<View>({ kind: "queue" });
  const onAdmitted = useCallback((pass: Pass) => setView({ kind: "event", pass }), []);

  const refreshAccount = useCallback(() => {
    me().then(setAccount, () => setAccount(null));
  }, []);
  useEffect(refreshAccount, [refreshAccount]);

  // The session ended while buying (expired, or logged out elsewhere): ask to log in again.
  const onLoggedOut = useCallback(() => {
    setAccount(null);
    setView({ kind: "queue" });
  }, []);

  async function onLogOut() {
    await logOut().catch(() => {}); // logged out here either way
    onLoggedOut();
  }

  return (
    <>
      <header className="top-bar">
        <div className="wrap top-bar-inner">
          <span className="brand">Ticket Booking</span>
          {account && (
            <span className="who">
              {account.email} <button onClick={onLogOut}>Log out</button>
            </span>
          )}
        </div>
      </header>
      <main>
        {account === undefined && <div className="page-center"><p>Loading…</p></div>}
        {account === null && <Auth onLoggedIn={refreshAccount} />}
        {account && !account.id_verified && <VerifyId onVerified={refreshAccount} />}
        {account?.id_verified && (
          <>
            {view.kind === "queue" && <Queue note={view.note} onAdmitted={onAdmitted} onLoggedOut={onLoggedOut} />}
            {view.kind === "event" && <EventPage view={view} setView={setView} onLoggedOut={onLoggedOut} />}
            {view.kind === "checkout" && <Checkout view={view} setView={setView} onLoggedOut={onLoggedOut} />}
            {view.kind === "sold" && (
              <div className="page-center">
                <section className="card form">
                  <h2>You're going! {view.seats.length === 1 ? "Seat" : "Seats"} {labels(view.seats)} {view.seats.length === 1 ? "is" : "are"} yours.</h2>
                  <p>{money(total(view.seats))} charged.</p>
                  <button onClick={() => setView({ kind: "queue" })}>Buy more seats</button>
                </section>
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
