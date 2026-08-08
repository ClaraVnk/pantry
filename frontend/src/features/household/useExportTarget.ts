import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getExportTarget, registerExportTarget, revokeExportTarget } from '../../api/endpoints';
import type { ShoppingExportTarget, ShoppingExportTargetDraft } from '../../api/types';

/**
 * One export destination as the settings screen sees it — withdrawal included.
 *
 * Deliberately not `useExportTargets` from `features/shopping`: that hook answers
 * "where may this list go right now" and hides a destination whose agreement was
 * withdrawn, which is exactly right for deciding whether to show a button and
 * exactly wrong here. ADR-0010 keeps the row so a household can see what it once
 * authorised; a settings screen that hid it would erase that record in effect.
 *
 * Nothing in this module holds a token. The value typed into the form lives in
 * the form's own state for the length of one submission and is never returned by
 * the server, so there is nothing here to cache, persist or accidentally log.
 */

interface Result {
  nonce: number;
  target: ShoppingExportTarget | null;
  error: string | null;
  unsupported: boolean;
}

export interface ExportTargetState {
  /** `null` when nothing was ever registered, which is the normal state. */
  target: ShoppingExportTarget | null;
  loading: boolean;
  error: string | null;
  /** The server predates ADR-0010's configuration endpoints. */
  unsupported: boolean;
  reload: () => void;
  register: (draft: ShoppingExportTargetDraft) => Promise<void>;
  revoke: () => Promise<void>;
}

export function useExportTarget(target: string): ExportTargetState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getExportTarget(target, controller.signal)
      .then((found) => {
        setResult({ nonce, target: found, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && cause.status === 404) {
          // Two different `404`s, told apart by the problem type: "nothing is
          // registered" is data, "there is no such route" is a deployment that
          // predates the feature. Collapsing them would show an older server's
          // users a form that cannot work.
          const registered = cause.problemType === 'export-target-not-registered';
          setResult({
            nonce,
            target: null,
            error: null,
            unsupported: !registered,
          });
          return;
        }
        setResult({
          nonce,
          target: null,
          error: describeError(cause),
          unsupported: cause instanceof ApiError && cause.status === 501,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce, target]);

  const register = useCallback(
    async (draft: ShoppingExportTargetDraft) => {
      await registerExportTarget(target, draft);
      reload();
    },
    [reload, target],
  );

  const revoke = useCallback(async () => {
    await revokeExportTarget(target);
    reload();
  }, [reload, target]);

  return {
    target: result?.target ?? null,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    unsupported: result?.unsupported ?? false,
    reload,
    register,
    revoke,
  };
}
