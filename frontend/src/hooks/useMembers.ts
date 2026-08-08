import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../api/client';
import { createMember, deleteMember, getMembers, updateMember } from '../api/endpoints';
import type { HouseholdMember, MemberDraft } from '../api/types';

/** Stable identity: consumers put this array in memo dependencies. */
const NO_MEMBERS: HouseholdMember[] = [];

interface Result {
  nonce: number;
  members: HouseholdMember[];
  error: string | null;
  unsupported: boolean;
}

export interface MembersState {
  members: HouseholdMember[];
  loading: boolean;
  error: string | null;
  /** The server predates contract v1.1 and has no /v1/members route. */
  unsupported: boolean;
  reload: () => void;
  create: (draft: MemberDraft) => Promise<void>;
  update: (id: string, draft: MemberDraft) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

/**
 * Household members, loaded once and shared by the household screen and the
 * suggestion panel.
 *
 * These records are health data (ADR-0009, RGPD art. 9), so they live in memory
 * for the lifetime of the tab and nowhere else: no localStorage, no query
 * string, no console. Reloading the app re-fetches them over TLS or shows
 * nothing at all.
 */
export function useMembers(): MembersState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getMembers(controller.signal)
      .then((members) => {
        setResult({ nonce, members, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // A missing route is a deployment state, not a failure to shout about.
        const missing = cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
        setResult({
          nonce,
          members: NO_MEMBERS,
          error: missing ? null : describeError(cause),
          unsupported: missing,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const create = useCallback(
    async (draft: MemberDraft) => {
      await createMember(draft);
      reload();
    },
    [reload],
  );

  const update = useCallback(
    async (id: string, draft: MemberDraft) => {
      await updateMember(id, draft);
      reload();
    },
    [reload],
  );

  const remove = useCallback(
    async (id: string) => {
      await deleteMember(id);
      reload();
    },
    [reload],
  );

  return {
    members: result?.members ?? NO_MEMBERS,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    unsupported: result?.unsupported ?? false,
    reload,
    create,
    update,
    remove,
  };
}
