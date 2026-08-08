import { apiConfig } from './config';
import { getActiveHouseholdId, getCsrfToken, reportSessionLost } from './session';
import type { ProblemDetails } from './types';

/**
 * An API failure carrying the RFC 9457 problem document when the server sent
 * one. Callers branch on `problemType` / `status`, never on message text.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails | null;
  readonly retryAfterSeconds: number | null;

  constructor(
    message: string,
    status: number,
    problem: ProblemDetails | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** Last path segment of the problem type URI, e.g. `product-not-found`. */
  get problemType(): string | null {
    const type = this.problem?.type;
    if (!type || type === 'about:blank') return null;
    const slug = type.split('/').filter(Boolean).pop();
    return slug ?? null;
  }
}

/** Network unreachable, DNS failure, CORS rejection, offline. */
export class NetworkError extends Error {
  constructor(cause?: unknown) {
    super("Le serveur Chaudron est injoignable. Vérifiez votre connexion ou l'URL de l'API.");
    this.name = 'NetworkError';
    this.cause = cause;
  }
}

export class ConfigurationError extends Error {
  constructor() {
    super("L'application n'est pas configurée.");
    this.name = 'ConfigurationError';
  }
}

function isProblem(value: unknown): value is ProblemDetails {
  return (
    typeof value === 'object' &&
    value !== null &&
    'title' in value &&
    typeof value.title === 'string'
  );
}

function parseRetryAfter(header: string | null): number | null {
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const date = Date.parse(header);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.round((date - Date.now()) / 1000));
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  query?: Record<string, string | number | undefined>;
  body?: unknown;
  signal?: AbortSignal;
}

/** Methods that change nothing, and therefore need no CSRF token. */
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * Builds the URL and the two headers every request may need.
 *
 * `X-Household-Id` is sent only when a household is active, and it is a
 * *selector*, not a credential: the server accepts it only if the signed-in
 * account is a member of the household it names. An account with a single
 * household never needs it at all, which is why `null` is a normal state rather
 * than a configuration error.
 *
 * `X-CSRF-Token` goes on every unsafe method. It is what stops a third-party
 * page from making the browser perform a write with the session cookie it
 * attaches automatically — see `api/errors.py::csrf_token_invalid` on the
 * server for the full argument.
 */
function prepare(
  path: string,
  query: Record<string, string | number | undefined> | undefined,
  method: string,
): { url: URL; headers: Record<string, string> } {
  if (!apiConfig) throw new ConfigurationError();

  const url = new URL(`${apiConfig.baseUrl}/v1${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = {};
  const householdId = getActiveHouseholdId();
  if (householdId) headers['X-Household-Id'] = householdId;

  const csrf = getCsrfToken();
  if (csrf && !SAFE_METHODS.has(method.toUpperCase())) headers['X-CSRF-Token'] = csrf;

  return { url, headers };
}

/**
 * Wraps whatever `fetch` throws into the two errors callers know how to read.
 *
 * `credentials: 'include'` is what carries the session cookie. It used to be
 * `'omit'`, which was right when authorisation travelled in a header and is now
 * the difference between a working application and one where every call answers
 * 401. It requires `Access-Control-Allow-Credentials: true` and an explicitly
 * listed origin on the server, which `Settings` refuses to start without
 * (`config.py`).
 */
async function send(url: URL, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, { ...init, credentials: 'include', mode: 'cors' });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new NetworkError(cause);
  }
}

/**
 * Turn a `401` into a single application-wide event, then throw as usual.
 *
 * Every call site keeps its own error handling; this only guarantees that the
 * shell learns the session is gone, wherever the discovery happened.
 */
function noteAuthentication(response: Response): void {
  if (response.status === 401) reportSessionLost();
}

/** Turns a non-2xx response into a typed `ApiError`, problem document included. */
function failure(response: Response, raw: string): never {
  let payload: unknown = null;
  if (raw.length > 0) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }
  const problem = isProblem(payload) ? payload : null;
  const message = problem?.detail ?? problem?.title ?? `Erreur ${String(response.status)}.`;
  throw new ApiError(
    message,
    response.status,
    problem,
    parseRetryAfter(response.headers.get('Retry-After')),
  );
}

/**
 * Single entry point for every call. Sets `X-Household-Id` (the provisional
 * slice-1 household resolution described in the contract) and turns
 * `application/problem+json` into a typed `ApiError`.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const { url, headers } = prepare(path, options.query, method);
  headers.Accept = 'application/json, application/problem+json';
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await send(url, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal ?? null,
  });

  noteAuthentication(response);
  if (response.status === 204) return undefined as T;

  const raw = await response.text();
  if (!response.ok) failure(response, raw);

  if (raw.length === 0) return undefined as T;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null as T;
  }
}

/**
 * A response whose body is text and stays text.
 *
 * One endpoint needs it — the plain-text shopping list handed to
 * `navigator.share`. Running it through `request` would `JSON.parse` a shopping
 * list and hand back `null`, which is a silently empty share sheet rather than
 * an error anyone could diagnose. Failures still arrive as `application/problem+json`
 * and are decoded by the same path as everywhere else.
 */
export async function requestText(path: string, signal?: AbortSignal): Promise<string> {
  const { url, headers } = prepare(path, undefined, 'GET');
  headers.Accept = 'text/plain, application/problem+json';

  const response = await send(url, { method: 'GET', headers, signal: signal ?? null });
  noteAuthentication(response);
  const raw = await response.text();
  if (!response.ok) failure(response, raw);
  return raw;
}

/**
 * A multipart upload. The one body in the application that is a file.
 *
 * `Content-Type` is deliberately **not** set: the browser has to write it
 * itself so that it carries the multipart boundary it generated. Setting it by
 * hand is the classic way an upload becomes an unparseable 422.
 */
export async function requestFile<T>(path: string, file: File, signal?: AbortSignal): Promise<T> {
  const { url, headers } = prepare(path, undefined, 'POST');
  headers.Accept = 'application/json, application/problem+json';

  const form = new FormData();
  form.append('file', file, file.name);

  const response = await send(url, { method: 'POST', headers, body: form, signal: signal ?? null });
  noteAuthentication(response);
  const raw = await response.text();
  if (!response.ok) failure(response, raw);
  return JSON.parse(raw) as T;
}

/**
 * One extension field of a problem document, as text.
 *
 * RFC 9457 extensions are `unknown` by construction — a server may put an
 * object where this build expects a string. Anything that is not already a
 * scalar is reported as absent rather than stringified into `[object Object]`
 * in the middle of a sentence shown to a user.
 */
export function problemField(error: ApiError, key: string): string | null {
  const value = error.problem?.[key];
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return null;
}

/** Human-readable text for any error surfaced to the user. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return 'Votre session a expiré. Reconnectez-vous pour continuer.';
    }
    if (error.status === 403 && error.problemType === 'household-forbidden') {
      return "Vous n'avez pas accès à ce foyer.";
    }
    if (error.status === 403 && error.problemType === 'csrf-token-invalid') {
      return 'Votre session a été interrompue. Rechargez la page, puis réessayez.';
    }
    if (error.status >= 500) {
      return 'Le serveur Chaudron a rencontré une erreur. Réessayez dans un instant.';
    }
    return error.message;
  }
  if (error instanceof NetworkError || error instanceof ConfigurationError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Une erreur inattendue est survenue.';
}
