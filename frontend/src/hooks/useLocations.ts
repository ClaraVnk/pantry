import { useCallback, useEffect, useState } from 'react';
import { getLocations } from '../api/endpoints';
import { describeError } from '../api/client';
import type { StorageLocation } from '../api/types';

interface Result {
  nonce: number;
  locations: StorageLocation[];
  error: string | null;
}

/** Stable identity: consumers put this array in memo dependencies. */
const NO_LOCATIONS: StorageLocation[] = [];

export interface LocationsState {
  locations: StorageLocation[];
  loading: boolean;
  /**
   * Whether an answer has *ever* arrived, success or failure. Distinct from
   * `!loading`, which goes back to false on every refresh.
   *
   * The inventory screen waits on this before its first paint: locations decide
   * the order of the groups and populate the filter row, so a list rendered
   * before they land is rebuilt when they do — and that rebuild was the second
   * source of cumulative layout shift on the busiest screen.
   */
  settled: boolean;
  error: string | null;
  reload: () => void;
  /**
   * Insert a location this browser has just created, without a round trip.
   *
   * The server has already answered with the row; asking for the list again
   * would move the content under a user who is about to add their first item.
   * Sorted on insert to match the server's own order (`sort_order`, then name).
   */
  add: (location: StorageLocation) => void;
}

/**
 * Storage locations are read by both the inventory screen and the add form.
 * Lifting them to the shell keeps one request instead of two and keeps the two
 * screens showing the same list.
 *
 * `loading` is derived from "the answer I hold is not the answer I asked for"
 * rather than set synchronously inside the effect. That avoids a cascading
 * render, and it keeps the previous list on screen during a refresh instead of
 * blanking it.
 */
export function useLocations(): LocationsState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  const add = useCallback((location: StorageLocation) => {
    setResult((previous) => {
      // Nothing held yet means the first GET is still in flight, and it was
      // issued before this creation — so it may or may not contain the new row.
      // Its answer decides; merging into a list that does not exist would only
      // let the two disagree. Unreachable in practice: the only caller is the
      // empty state, which is not shown until a list has arrived.
      if (previous === null) return previous;
      if (previous.locations.some((entry) => entry.id === location.id)) return previous;
      return {
        ...previous,
        // The creation succeeded, so whatever error the list carried is stale.
        error: null,
        locations: [...previous.locations, location].sort((a, b) =>
          a.name.localeCompare(b.name, 'fr'),
        ),
      };
    });
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getLocations(controller.signal)
      .then((locations) => {
        setResult({ nonce, locations, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult((previous) => ({
          nonce,
          locations: previous?.locations ?? NO_LOCATIONS,
          error: describeError(cause),
        }));
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  return {
    locations: result?.locations ?? NO_LOCATIONS,
    loading: result?.nonce !== nonce,
    settled: result !== null,
    error: result?.error ?? null,
    reload,
    add,
  };
}
