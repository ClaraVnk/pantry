/**
 * Build-time configuration, validated once at module load.
 *
 * Fail fast and visibly: a PWA pointed at nothing produces a stream of network
 * errors that look like backend outages. `configError` lets the shell render one
 * honest screen instead.
 *
 * **There is exactly one value here, and it is a URL.** `VITE_HOUSEHOLD_ID` used
 * to live alongside it and had to go, because a `VITE_*` variable is not
 * configuration in any private sense: Vite substitutes it at build time and
 * inlines the literal into `dist/assets/index-*.js`, which the service worker
 * then precaches onto every visitor's device. While `X-Household-Id` was the
 * whole of the access control, that meant the credential was *shipped with the
 * application* — loading the page was enough to read it, and CORS changed
 * nothing, being a browser-side rule that `curl` has never heard of.
 *
 * So the active household is now **runtime state derived from the session**
 * (`api/session.ts`), not a build constant. That is also what makes a household
 * *selector* possible at all: one build used to mean one household, forever.
 *
 * The rule this file now has to keep: nothing secret, and nothing
 * authorisation-bearing, may ever be read from `import.meta.env` again.
 */

export interface ApiConfig {
  baseUrl: string;
}

function read(name: string): string {
  const raw = (import.meta.env[name] as string | undefined) ?? '';
  return raw.trim();
}

function build(): { config: ApiConfig | null; error: string | null } {
  const baseUrl = read('VITE_API_BASE_URL').replace(/\/+$/, '');

  if (!baseUrl) {
    return {
      config: null,
      error: `Variable d'environnement manquante : VITE_API_BASE_URL. Copiez .env.example vers .env.local, renseignez-la, puis relancez la construction.`,
    };
  }
  return { config: { baseUrl }, error: null };
}

const result = build();

export const apiConfig: ApiConfig | null = result.config;
export const configError: string | null = result.error;
