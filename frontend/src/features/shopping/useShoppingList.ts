import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getCurrentShoppingList } from '../../api/endpoints';
import type { ShoppingList, ShoppingListItem } from '../../api/types';

export interface ShoppingListState {
  list: ShoppingList | null;
  loading: boolean;
  error: string | null;
  /** The server predates contract v1.1 and has no `/v1/shopping-lists` route. */
  unsupported: boolean;
  reload: () => void;
  /** Replaces the whole list, for the calls that answer with one. */
  replace: (list: ShoppingList) => void;
  /** Applies one item in place, for the calls that answer with an item. */
  applyItem: (item: ShoppingListItem) => void;
  /** Drops one item locally, for the `204` of a removal. */
  dropItem: (itemId: string) => void;
}

interface Result {
  /** Which request this answer belongs to; anything else is stale. */
  key: number;
  list: ShoppingList | null;
  error: string | null;
  unsupported: boolean;
}

/**
 * The household's current list (contract §6bis).
 *
 * The `GET` creates the list when the household has none, so there is no "no
 * list yet" state to handle here — an empty `items` array is the empty state,
 * and it is a different thing from a failure to load.
 *
 * Every answer is tagged with the request that produced it, so a reload shows a
 * loading row rather than the previous list under a new heading, and so a
 * response that arrives after its request was superseded is discarded instead of
 * overwriting a fresher one.
 *
 * The three local mutators exist so that ticking an item is one request rather
 * than two. Every write endpoint already answers with what it changed; going
 * back to the server to ask what the list now looks like is how a list goes
 * stale between the aisle and the checkout.
 */
export function useShoppingList(): ShoppingListState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getCurrentShoppingList(controller.signal)
      .then((list) => {
        if (controller.signal.aborted) return;
        setResult({ key: nonce, list, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // A missing route is a deployment state, not a failure to shout about.
        const missing = cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
        setResult({
          key: nonce,
          list: null,
          error: missing ? null : describeError(cause),
          unsupported: missing,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const replace = useCallback((list: ShoppingList) => {
    setResult((current) =>
      current === null ? current : { ...current, list, error: null, unsupported: false },
    );
  }, []);

  const applyItem = useCallback((item: ShoppingListItem) => {
    setResult((current) => {
      if (current?.list == null) return current;
      const items = current.list.items.map((existing) =>
        existing.id === item.id ? item : existing,
      );
      return { ...current, list: { ...current.list, items } };
    });
  }, []);

  const dropItem = useCallback((itemId: string) => {
    setResult((current) => {
      if (current?.list == null) return current;
      const items = current.list.items.filter((item) => item.id !== itemId);
      return { ...current, list: { ...current.list, items } };
    });
  }, []);

  const fresh = result?.key === nonce ? result : null;

  return {
    list: fresh?.list ?? null,
    loading: fresh === null,
    error: fresh?.error ?? null,
    unsupported: fresh?.unsupported ?? false,
    reload,
    replace,
    applyItem,
    dropItem,
  };
}
