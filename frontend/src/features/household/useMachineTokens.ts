import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { createMachineToken, getMachineTokens, revokeMachineToken } from '../../api/endpoints';
import type { MachineToken, MachineTokenCreated, MachineTokenDraft } from '../../api/types';

/**
 * This household's machine tokens, as the settings screen needs them.
 *
 * `create` is the one mutation in the codebase whose **return value matters**.
 * Everywhere else a mutation reloads and the screen re-reads the list; here the
 * server's answer contains the token itself and nothing will ever return it
 * again, so the value is handed straight back to the caller and this module
 * keeps no copy of it. Nothing here caches, stores or logs a token.
 */

/** Stable identity: consumers put this array in memo dependencies. */
const NO_TOKENS: MachineToken[] = [];

interface Result {
  nonce: number;
  tokens: MachineToken[];
  error: string | null;
  unsupported: boolean;
}

export interface MachineTokensState {
  tokens: MachineToken[];
  loading: boolean;
  error: string | null;
  /** The server predates contract v1.1 §10 and has no /v1/tokens route. */
  unsupported: boolean;
  reload: () => void;
  /** Resolves with the created token, **including its one and only value**. */
  create: (draft: MachineTokenDraft) => Promise<MachineTokenCreated>;
  revoke: (id: string) => Promise<void>;
}

export function useMachineTokens(): MachineTokensState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getMachineTokens(controller.signal)
      .then((tokens) => {
        setResult({ nonce, tokens, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // A missing route is a deployment state, not a failure to shout about.
        const missing = cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
        setResult({
          nonce,
          tokens: NO_TOKENS,
          error: missing ? null : describeError(cause),
          unsupported: missing,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const create = useCallback(
    async (draft: MachineTokenDraft) => {
      const created = await createMachineToken(draft);
      reload();
      return created;
    },
    [reload],
  );

  const revoke = useCallback(
    async (id: string) => {
      await revokeMachineToken(id);
      reload();
    },
    [reload],
  );

  return {
    tokens: result?.tokens ?? NO_TOKENS,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    unsupported: result?.unsupported ?? false,
    reload,
    create,
    revoke,
  };
}
