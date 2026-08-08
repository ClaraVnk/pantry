/**
 * The six calls that make up a session, and the shape the server answers with.
 *
 * The credential never appears here. Sign-in returns a `Set-Cookie` the browser
 * stores and this code cannot read; what the body carries is the CSRF token and
 * the list of households the account may open.
 *
 * The last two — `revokeAllSessions` and `changePassword` — are what a person who
 * thinks their session has leaked can do about it. Both answer with a **new**
 * `Session`, because both end every session of the account including the one that
 * made the call: a stolen cookie is a copy of *this* one, so sparing it would
 * spare the thief. The caller must adopt the answer (`SessionProvider.adopt`) or
 * its next write will echo a CSRF token the server has already forgotten.
 */

import { request } from './client';

export interface HouseholdSummary {
  id: string;
  name: string;
  role: 'owner' | 'member' | 'viewer';
}

export interface Session {
  user_id: string;
  email: string;
  display_name: string;
  csrf_token: string;
  households: HouseholdSummary[];
}

export interface Credentials {
  email: string;
  password: string;
}

export interface Registration extends Credentials {
  display_name: string;
  household_name: string;
}

/**
 * Who is signed in, if anyone.
 *
 * Called on every load: the cookie survives a refresh, the in-memory CSRF token
 * does not, and this is where it comes back. A `401` here is not an error to
 * report — it is the normal answer for a visitor who has not signed in — so
 * callers treat it as "no session" rather than surfacing it.
 */
export function fetchSession(signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/session', { signal });
}

export function login(credentials: Credentials, signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/login', { method: 'POST', body: credentials, signal });
}

export function register(payload: Registration, signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/register', { method: 'POST', body: payload, signal });
}

export function logout(signal?: AbortSignal): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST', signal });
}

export interface PasswordChange {
  current_password: string;
  new_password: string;
}

/**
 * End every session of this account and start a fresh one here.
 *
 * "Sign me out everywhere", for a cookie somebody thinks has been copied. It does
 * not touch this household's machine tokens: those are a separate credential with
 * their own list and their own revocation, and cutting a household's integrations
 * because one person lost a laptop is a surprise nobody asked for.
 */
export function revokeAllSessions(signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/sessions/revoke-all', { method: 'POST', signal });
}

/**
 * Replace the password, having proved the current one.
 *
 * The current password is required and there is no way round it: this instance
 * has no outbound mail, so there is no reset link and nothing else that can stand
 * in for "you are this person". A forgotten password is a forgotten account, and
 * the interface says so rather than offering a recovery path that does not exist.
 */
export function changePassword(payload: PasswordChange, signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/password', { method: 'POST', body: payload, signal });
}
