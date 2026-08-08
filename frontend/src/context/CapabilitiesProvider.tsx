import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getProviderCapabilities } from '../api/endpoints';
import { describeError } from '../api/client';
import type { ProviderCapabilities } from '../api/types';
import { CapabilitiesContext, type CapabilitiesState } from './capabilitiesContext';

interface Result {
  nonce: number;
  capabilities: ProviderCapabilities | null;
  error: string | null;
}

/**
 * How long the first paint waits for the probe before giving up on it.
 *
 * Chosen against the failure it bounds, not against a target: past roughly a
 * second and a half the user is looking at an empty window and has no way to
 * know anything is happening, which is a worse outcome than the banner arriving
 * late and moving the page. Under a normal answer — one request on a warm
 * connection — the timer never fires.
 */
const FIRST_PAINT_BUDGET_MS = 1500;

/**
 * Fetches provider capabilities once at startup, and holds the first paint
 * until it knows the answer.
 *
 * The contract is explicit that degraded mode is shown permanently rather than
 * at the moment of failure: the user must know the limit before trying, not
 * after. So this runs on boot, not lazily from the recipes screen.
 *
 * **Why the shell waits instead of reserving space.** `DegradedBanner` used to
 * mount when this resolved, after the first paint, pushing `main` down from
 * y=69 by the banner's whole height — the dominant layout shift of the
 * application. Measured on the built bundle under Lighthouse's Slow 4G profile
 * with this wait removed: 0.215 at 320 px, 0.128 at 390 px, 0.302 at 400 %
 * zoom, every one of them attributed to `main#main` and to nothing else. Two
 * fixes were available:
 *
 * * *Reserve a fixed slot while pending.* Rejected. The banner's height is not
 *   fixed — zero on a healthy instance, one line for "no provider", up to four
 *   for a degraded one — so any reserved height is wrong for everybody, and on
 *   the common case (nothing to report) it is a permanent empty strip above the
 *   content on every single load. Paying a visible hole forever to avoid a shift
 *   that only a minority of instances ever produce is the wrong trade.
 * * *Hold the first paint.* Taken. The cost is one round trip of blank window,
 *   on the same warm connection the session check already spends one on, and the
 *   shell is already built to show a blank frame rather than a spinner for
 *   exactly this reason (see `Gate` in `App.tsx`). Nothing moves afterwards
 *   because nothing was painted before.
 *
 * The wait is bounded: an instance whose probe hangs paints anyway, accepts the
 * late shift, and stays usable. A blank window is the only outcome worse than a
 * moving one.
 */
export function CapabilitiesProvider({ children }: { children: ReactNode }) {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const [budgetSpent, setBudgetSpent] = useState(false);

  const refresh = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getProviderCapabilities(controller.signal)
      .then((capabilities) => {
        setResult({ nonce, capabilities, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ nonce, capabilities: null, error: describeError(cause) });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  // Only ever armed once: a later `refresh()` must not be able to blank a screen
  // the user is already reading.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setBudgetSpent(true);
    }, FIRST_PAINT_BUDGET_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, []);

  const value = useMemo<CapabilitiesState>(
    () => ({
      capabilities: result?.capabilities ?? null,
      loading: result?.nonce !== nonce,
      paintable: result !== null || budgetSpent,
      error: result?.error ?? null,
      refresh,
    }),
    [result, nonce, budgetSpent, refresh],
  );

  return <CapabilitiesContext value={value}>{children}</CapabilitiesContext>;
}
