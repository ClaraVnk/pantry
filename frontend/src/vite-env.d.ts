/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  /*
   * There is deliberately no VITE_HOUSEHOLD_ID. Vite inlines every VITE_* value
   * into the built bundle, which the service worker then precaches onto every
   * visitor's device — so a household identifier declared here was published
   * with the application. The active household is runtime state derived from the
   * session instead (`api/session.ts`).
   */
  /** Public origin of this deployment. Read by vite.config.ts only. */
  readonly VITE_SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/**
 * Torch (and its capability flag) is standardised in the MediaStream Image
 * Capture spec but absent from TypeScript's DOM lib. Chrome on Android exposes
 * it; Safari on iOS never does — which is why the UI probes `getCapabilities()`
 * rather than assuming it exists.
 */
interface MediaTrackCapabilities {
  torch?: boolean;
}

interface MediaTrackConstraintSet {
  torch?: ConstrainBoolean;
}

/**
 * zxing-wasm ships the reader binary as a package export. Importing it with
 * `?url` makes Vite emit it as a build asset, so it is served from our own
 * origin and precached by the service worker instead of being fetched from a
 * CDN at first scan.
 */
declare module 'zxing-wasm/reader/zxing_reader.wasm?url' {
  const url: string;
  export default url;
}
