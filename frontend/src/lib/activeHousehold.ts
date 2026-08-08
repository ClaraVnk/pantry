/**
 * Which household this browser was last looking at.
 *
 * Persisted so that an account belonging to two households (family home *and*
 * flatshare) does not have to pick one on every load. It is a preference, not a
 * credential: the value is an identifier the server re-checks against the
 * signed-in account's memberships on every single request, so a tampered entry
 * here buys a `403` and nothing else.
 *
 * Not keyed by account on purpose — the account is not known when this is read,
 * and a stale entry is harmless because membership is verified server-side.
 */

const KEY = 'chaudron.active-household';

export function readActiveHousehold(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    // Private browsing and blocked storage both throw. Falling back to "ask the
    // user" is a worse day than a broken screen, but only slightly.
    return null;
  }
}

export function writeActiveHousehold(id: string | null): void {
  try {
    if (id === null) window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, id);
  } catch {
    /* ignored, see above */
  }
}
