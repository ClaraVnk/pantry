/**
 * The two runtime values every request needs, and who is allowed to set them.
 *
 * Deliberately a module singleton rather than React state, because `request()`
 * in `client.ts` is a plain function called from hooks, effects and event
 * handlers alike — threading a context through every call site would be a large
 * change for no benefit, and would still need a mutable escape hatch for the
 * 401 path. `SessionProvider` is the only writer; everything else reads.
 *
 * **Neither value is a credential.**
 *
 * The credential is the `__Host-chaudron_session` cookie, which is `HttpOnly`:
 * this file cannot read it, and neither can an injected script. What lives here
 * is the CSRF token — useless without the cookie, and the thing that makes a
 * forged cross-site write fail — and the identifier of the household currently
 * being looked at, which the server accepts only if the signed-in account is a
 * member of it.
 *
 * Both are held **in memory only**. Nothing is written to `localStorage`: a
 * value put there survives the tab, is readable by any script on the origin, and
 * would go stale the moment the server revoked the session. On a reload the app
 * asks `GET /v1/auth/session` and gets a fresh token, which costs one request and
 * removes a whole class of "signed in according to the client, signed out
 * according to the server" bugs.
 */

let csrfToken: string | null = null;
let activeHouseholdId: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

/** Which household the interface is currently showing. `null` until known. */
export function setActiveHouseholdId(id: string | null): void {
  activeHouseholdId = id;
}

export function getActiveHouseholdId(): string | null {
  return activeHouseholdId;
}

/**
 * Called once for every `401`, from anywhere in the application.
 *
 * A session can end between two requests — it expired, an owner revoked it, the
 * account was disabled — and the first thing the user sees must be the sign-in
 * screen rather than an error toast on a screen they can no longer use.
 */
export function onSessionLost(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export function reportSessionLost(): void {
  csrfToken = null;
  activeHouseholdId = null;
  onUnauthorized?.();
}

/** Forget everything. Called on sign-out, before the screen changes. */
export function clearSession(): void {
  csrfToken = null;
  activeHouseholdId = null;
}
