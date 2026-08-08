import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getExportTargets } from '../../api/endpoints';
import type { ShoppingExportTarget } from '../../api/types';

/**
 * Which destinations this household may currently send its shopping list to.
 *
 * This module used to be a workaround. §6bis and ADR-0010 say Todoist is only
 * offered to a household that registered a destination, and the API had no way
 * to ask: resolution happened inside `POST /export/{target}`, so the only signal
 * was that call failing. The answer was therefore learnt from the first refusal
 * and remembered in `localStorage`, which turned a button that failed every time
 * into a button that failed once.
 *
 * `GET /v1/shopping-lists/export/targets` replaced it, and the difference is not
 * only tidiness. A remembered refusal was **per browser**: registering a token on
 * a phone left the tablet still hiding the button, and withdrawing consent left
 * every browser that had once succeeded still offering it. The server answers for
 * the household, so both directions are now right everywhere at once — and
 * nothing about what this household has configured is written to disk here.
 *
 * The list carries **consented destinations only**: one whose agreement was
 * withdrawn is absent rather than present-and-flagged, so a caller cannot offer a
 * button by forgetting to check a second field.
 */

/** Stable identity: consumers put this array in memo and effect dependencies. */
const NO_TARGETS: ShoppingExportTarget[] = [];

interface Result {
  nonce: number;
  targets: ShoppingExportTarget[];
  error: string | null;
  unsupported: boolean;
}

export interface ExportTargetsState {
  targets: ShoppingExportTarget[];
  loading: boolean;
  /** A transport or server failure. Not "there are none", which is an empty list. */
  error: string | null;
  /** The server predates ADR-0010's configuration endpoints. */
  unsupported: boolean;
  reload: () => void;
}

export function useExportTargets(): ExportTargetsState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getExportTargets(controller.signal)
      .then((targets) => {
        setResult({ nonce, targets, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // A missing route is a deployment state, not a failure to shout about:
        // an older server simply offers no third-party export.
        const missing = cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
        setResult({
          nonce,
          targets: NO_TARGETS,
          error: missing ? null : describeError(cause),
          unsupported: missing,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  return {
    targets: result?.targets ?? NO_TARGETS,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    unsupported: result?.unsupported ?? false,
    reload,
  };
}

/** Whether this household may send its list to `target` right now. */
export function hasConsentedTarget(state: ExportTargetsState, target: string): boolean {
  return state.targets.some((entry) => entry.target === target && entry.is_consented);
}
