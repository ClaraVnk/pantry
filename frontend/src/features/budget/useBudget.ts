import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getBudget, getBudgetHistory } from '../../api/endpoints';
import type { Budget, BudgetPeriod } from '../../api/types';

/** Stable identity: consumers put this array in memo dependencies. */
const NO_PERIODS: Budget[] = [];

/** Enough to see a direction without turning the screen into a spreadsheet. */
const HISTORY_COUNT = 6;

export interface BudgetState {
  budget: Budget | null;
  history: Budget[];
  loading: boolean;
  error: string | null;
  /** The server predates contract v1.1 and has no /v1/budget route. */
  unsupported: boolean;
  reload: () => void;
}

interface Result {
  /** Which request this answer belongs to; anything else is stale. */
  key: string;
  budget: Budget | null;
  history: Budget[];
  error: string | null;
  unsupported: boolean;
}

/**
 * Spending over the current period, plus the complete periods before it.
 *
 * **Nothing is requested while `enabled` is false.** That is the whole point of
 * the opt-in (contract §6ter): a household that has not asked for its money to
 * be counted must not have it counted, and an effect that fetched "just to have
 * it ready" would make the proposal a formality.
 *
 * Every answer is tagged with the request that produced it, so switching from
 * month to week shows a loading row rather than last month's figure under this
 * week's heading — and a disabled hook reports nothing at all, without the
 * effect having to reach back and clear state.
 *
 * The history call is allowed to fail on its own. A trend is a nicety; the
 * period in progress is the answer to the question the user asked, and losing
 * the first because the second timed out would be a poor trade.
 */
export function useBudget(period: BudgetPeriod, enabled: boolean): BudgetState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const key = `${String(nonce)}|${period}`;

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    const load = async (): Promise<void> => {
      const budget = await getBudget(period, controller.signal);
      const history = await getBudgetHistory(period, HISTORY_COUNT, controller.signal).catch(
        () => null,
      );
      if (controller.signal.aborted) return;
      setResult({
        key,
        budget,
        history: history?.periods ?? NO_PERIODS,
        error: null,
        unsupported: false,
      });
    };

    load().catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      // A missing route is a deployment state, not a failure to shout about.
      const missing = cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
      setResult({
        key,
        budget: null,
        history: NO_PERIODS,
        error: missing ? null : describeError(cause),
        unsupported: missing,
      });
    });

    return () => {
      controller.abort();
    };
  }, [key, period, enabled]);

  const fresh = enabled && result?.key === key ? result : null;

  return {
    budget: fresh?.budget ?? null,
    history: fresh?.history ?? NO_PERIODS,
    loading: enabled && fresh === null,
    error: fresh?.error ?? null,
    unsupported: fresh?.unsupported ?? false,
    reload,
  };
}
