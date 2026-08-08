import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { fetchSession, logout, type HouseholdSummary, type Session } from '../api/auth';
import { clearSession, onSessionLost, setActiveHouseholdId, setCsrfToken } from '../api/session';
import { SessionContext, type SessionState } from './sessionContext';
import { readActiveHousehold, writeActiveHousehold } from '../lib/activeHousehold';

/**
 * Owns the answer to "who is signed in, and which household are we looking at?".
 *
 * The only writer of `api/session.ts`, which is where `client.ts` reads the CSRF
 * token and the active household from. Keeping both in one place is what makes
 * "log out" a single operation rather than three that can disagree.
 *
 * Two behaviours are worth stating.
 *
 * *The session is re-checked on every load, never restored from storage.* The
 * cookie survives a refresh and the in-memory CSRF token does not, so a boot
 * always costs one `GET /v1/auth/session`. Caching the answer in `localStorage`
 * would save that request and buy a client that believes it is signed in after
 * the server has revoked the session — which is exactly the failure server-side
 * sessions exist to prevent.
 *
 * *A `401` from anywhere ends the session here.* `client.ts` reports it,
 * whichever screen or background refresh discovered it, and the shell switches
 * to the sign-in form. What it does **not** do is unmount the rest of the
 * application state: `App` keeps the current tab above this boundary, so signing
 * back in returns the user where they were.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [householdId, setHouseholdId] = useState<string | null>(null);

  /** Push a session into the module the API client reads. */
  const install = useCallback((next: Session | null) => {
    setSession(next);
    setCsrfToken(next?.csrf_token ?? null);

    if (!next) {
      setHouseholdId(null);
      setActiveHouseholdId(null);
      return;
    }

    // Prefer the household the user last looked at, when they are still a member
    // of it; otherwise the only one; otherwise nothing, and the picker asks.
    const remembered = readActiveHousehold();
    const known = next.households.map((entry) => entry.id);
    const only = next.households.length === 1 ? next.households[0] : null;
    const chosen = remembered && known.includes(remembered) ? remembered : (only?.id ?? null);

    setHouseholdId(chosen);
    setActiveHouseholdId(chosen);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    fetchSession(controller.signal)
      .then((found) => {
        if (controller.signal.aborted) return;
        install(found);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        // Every failure means the same thing to the shell: nobody is signed in.
        // A 401 is the normal answer for a visitor rather than an error to
        // report, and a network failure or a 500 leaves the user at the sign-in
        // screen, which surfaces the real reason the moment they try.
        install(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [install]);

  useEffect(() => {
    onSessionLost(() => {
      setSession(null);
      setHouseholdId(null);
    });
    return () => {
      onSessionLost(null);
    };
  }, []);

  const adopt = useCallback(
    (next: Session) => {
      install(next);
      setLoading(false);
    },
    [install],
  );

  const selectHousehold = useCallback(
    (id: string) => {
      if (!session?.households.some((entry) => entry.id === id)) return;
      setHouseholdId(id);
      setActiveHouseholdId(id);
      writeActiveHousehold(id);
    },
    [session],
  );

  const signOut = useCallback(async () => {
    try {
      await logout();
    } catch {
      // The server may already have forgotten this session; either way the local
      // state has to go, or the interface shows a signed-in shell nothing works in.
    }
    clearSession();
    writeActiveHousehold(null);
    setSession(null);
    setHouseholdId(null);
  }, []);

  const activeHousehold = useMemo<HouseholdSummary | null>(
    () => session?.households.find((entry) => entry.id === householdId) ?? null,
    [session, householdId],
  );

  const value = useMemo<SessionState>(
    () => ({ session, loading, activeHousehold, adopt, selectHousehold, signOut }),
    [session, loading, activeHousehold, adopt, selectHousehold, signOut],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}
