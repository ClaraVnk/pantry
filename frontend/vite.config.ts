import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

/**
 * Two documents are served from this build, and they exist for opposite reasons.
 *
 * `/` is the public landing page: static, indexable, no JavaScript, describing
 * the project and nothing that belongs to a household.
 *
 * `/app/` is the application: every screen behind it is one household's private
 * data, so it carries `noindex` and is refused to crawlers in `robots.txt`.
 *
 * Splitting them is what makes "optimise the public page" and "never index the
 * application" two separate, verifiable statements instead of one contradiction.
 * See docs/public-page-and-indexing.md.
 */

/** No trailing slash. Substituted into `__SITE_URL__` in the landing page. */
const SITE_URL_PLACEHOLDER = '__SITE_URL__';

/**
 * Deliberately unresolvable. `.example` is reserved by RFC 2606, so a build that
 * forgot `VITE_SITE_URL` produces canonical and Open Graph URLs that point
 * nowhere real rather than at somebody else's domain.
 *
 * This used to be reached with only a `config.logger.warn`, and a warning in
 * several hundred lines of build output is not a signal. The `dist/` it produced
 * looked perfectly normal and carried `https://chaudron.example` in `canonical`,
 * `og:url` and the `Sitemap:` line — so a deployment that shipped it pointed its
 * own search ranking, and every shared link preview, at a domain the project does
 * not own. Nothing at deploy time would have said so.
 *
 * A `build` therefore now FAILS rather than falling back. See `resolveSiteUrl`.
 */
const DEFAULT_SITE_URL = 'https://chaudron.example';

/**
 * Escape hatch for the one legitimate case: building locally to look at the
 * output, with no intention of serving it. Named so that it cannot appear in a
 * deployment script by accident, and so that `grep` finds it when one does.
 */
const ALLOW_DEFAULT_SITE_URL = 'VITE_ALLOW_DEFAULT_SITE_URL';

const LANDING_PATHS = new Set(['/index.html', 'index.html']);

/** Paths the crawlers are asked to leave alone, in `robots.txt` order. */
const DISALLOWED_PATHS = [
  '/app/', // the application shell and every screen it renders
  '/assets/', // hashed bundle chunks; nothing to index, and they change every build
  '/sw.js',
  '/registerSW.js',
  '/v1/', // the API, when a reverse proxy puts it on this origin
  '/docs',
  '/openapi.json',
];

/** The slice of `http.ServerResponse` the dev middleware below actually uses. */
interface ResponseLike {
  setHeader(name: string, value: string): void;
  end(chunk: string): void;
}

/** Matches every flavour of Vite's internal HTML proxy id. Mirrors `isHtmlProxyRE`. */
const HTML_PROXY_RE = /[?&]html-proxy\b/;

/**
 * Hands Vite's own HTML proxy ids straight back, before `vite:resolve` can
 * rewrite them.
 *
 * `vite:build-html` turns each inline `<style>` or `<script>` block into an
 * import of `<absolute path of the html file>?html-proxy&inline-css&index=N.css`,
 * and `vite:html-inline-proxy` later looks the block up again by that path.
 * Between the two sits `vite:resolve`, which treats any id starting with `/` as
 * root-relative — including one that is already an absolute filesystem path —
 * whenever `<root>/<root>` happens to exist as a directory. That is Vite's
 * `rootInRoot` escape hatch, and it is off unless the root path is reproducible
 * inside itself.
 *
 * Both halves line up when this directory is built in a container mounted at
 * `/app`, which is the usual convention, because `frontend/app/` exists:
 *
 *     root                 /app
 *     landing proxy id     /app/index.html?html-proxy&inline-css&index=0.css
 *     rewritten to         /app/app/index.html?html-proxy&inline-css&index=0.css
 *
 * The id now names the APPLICATION entry, which has no inline style, so the
 * lookup misses and the build dies with "No matching HTML proxy module found".
 * Nothing in that message mentions the landing page, the mount point, or the
 * inline CSS that is the actual subject — and moving the checkout one directory
 * deeper makes it disappear, which is why it reads like a flake.
 *
 * Reproduce, from `frontend/`:
 *   podman run --rm -v "$PWD":/app:Z -w /app -e VITE_SITE_URL=https://example.test \
 *     docker.io/library/node:22.23.2-bookworm-slim npm run build
 *
 * `vite:html-inline-proxy` resolves these ids to themselves; this does the same
 * thing one plugin earlier, so the only behaviour dropped is the rewrite.
 */
function htmlProxyIdsAreAbsolutePaths(): Plugin {
  return {
    name: 'chaudron:html-proxy-ids-are-absolute-paths',
    enforce: 'pre',
    resolveId(id) {
      if (HTML_PROXY_RE.test(id)) return id;
      return null;
    },
  };
}

function resolveSiteUrl(env: Record<string, string>): { url: string; explicit: boolean } {
  const raw = env['VITE_SITE_URL']?.trim();
  if (!raw) return { url: DEFAULT_SITE_URL, explicit: false };
  return { url: raw.replace(/\/+$/, ''), explicit: true };
}

/**
 * Emits `robots.txt` and `sitemap.xml`, and resolves the site URL into the
 * landing page.
 *
 * Both files carry the absolute site URL, which is why neither can be a static
 * file in `public/`: the domain is a deployment decision, not a source constant.
 *
 * The plugin also strips the service-worker registration script from the landing
 * page. `vite-plugin-pwa` injects it into every HTML entry it sees, and on the
 * public page that would mean a render-blocking script plus a precache of the
 * application — including a 1 MB barcode WASM binary — downloaded by a visitor
 * who only came to read about the project.
 */
function seoAssets(): Plugin {
  let siteUrl = DEFAULT_SITE_URL;

  const robotsTxt = () =>
    [
      '# Chaudron.',
      '#',
      '# WHAT THIS FILE IS NOT: an access control. It is a request that',
      '# well-behaved crawlers honour and that nothing enforces. Anyone, and any',
      '# crawler that chooses to, can fetch every path listed below — and reading',
      '# this file is how they learn the paths exist. Authentication and the',
      '# reverse proxy are the controls; see docs/public-page-and-indexing.md.',
      '',
      'User-agent: *',
      ...DISALLOWED_PATHS.map((path) => `Disallow: ${path}`),
      '',
      `Sitemap: ${siteUrl}/sitemap.xml`,
      '',
    ].join('\n');

  const sitemapXml = () =>
    [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
      // No `<lastmod>`: the only value this build could put there is its own
      // timestamp, which would claim the page changed on every rebuild. An
      // inaccurate `lastmod` is discarded by crawlers, so an absent one says
      // strictly more.
      '  <url>',
      `    <loc>${siteUrl}/</loc>`,
      '  </url>',
      '</urlset>',
      '',
    ].join('\n');

  return {
    name: 'chaudron:seo-assets',
    enforce: 'post',

    // `configResolved`, not `config`: `config.env` is only populated once Vite
    // has loaded the `.env` files for the active mode.
    configResolved(config) {
      const env = config.env as Record<string, string>;
      const resolved = resolveSiteUrl(env);
      siteUrl = resolved.url;

      if (resolved.explicit || config.command !== 'build') return;

      // Serving is not the only way this leaks: `sitemap.xml` names the domain
      // to every crawler that reads it, and `og:url` names it in every link
      // preview. Both are wrong in a way nobody looks at until the ranking is
      // already somewhere else.
      if (env[ALLOW_DEFAULT_SITE_URL] === 'true' || env[ALLOW_DEFAULT_SITE_URL] === '1') {
        config.logger.warn(
          `[chaudron:seo-assets] VITE_SITE_URL is unset and ${ALLOW_DEFAULT_SITE_URL} is set: ` +
            `canonical, Open Graph and sitemap URLs point at ${DEFAULT_SITE_URL}. ` +
            `This build MUST NOT be deployed.`,
        );
        return;
      }

      throw new Error(
        `[chaudron:seo-assets] VITE_SITE_URL is not set.\n` +
          `\n` +
          `  A production build without it writes ${DEFAULT_SITE_URL} into <link rel=canonical>,\n` +
          `  og:url, and the Sitemap: line of robots.txt — a domain this project does not own.\n` +
          `  Deployed as-is, the public page tells every crawler that the canonical copy of\n` +
          `  itself lives somewhere else.\n` +
          `\n` +
          `  Fix:   VITE_SITE_URL=https://chaudron.example.tld npm run build\n` +
          `  See:   ops/README.md §7.3, docs/public-page-and-indexing.md\n` +
          `\n` +
          `  To build locally without deploying: ${ALLOW_DEFAULT_SITE_URL}=1 npm run build\n`,
      );
    },

    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const withSite = html.replaceAll(SITE_URL_PLACEHOLDER, siteUrl);
        if (!LANDING_PATHS.has(ctx.path)) return withSite;
        return withSite.replace(
          /\s*<script[^>]*vite-plugin-pwa:register-sw[^>]*>\s*<\/script>/,
          '',
        );
      },
    },

    // Dev server parity: the two files are generated, so without this they only
    // exist in `dist/` and would 404 during development.
    configureServer(server) {
      const serve = (path: string, type: string, body: () => string) => {
        server.middlewares.use(path, (_request, response) => {
          // Structurally typed rather than `http.ServerResponse`: pulling in
          // `@types/node` for two method signatures is a dependency this project
          // otherwise does not need.
          const res = response as unknown as ResponseLike;
          res.setHeader('Content-Type', type);
          res.end(body());
        });
      };
      serve('/robots.txt', 'text/plain; charset=utf-8', robotsTxt);
      serve('/sitemap.xml', 'application/xml; charset=utf-8', sitemapXml);
    },

    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'robots.txt', source: robotsTxt() });
      this.emitFile({ type: 'asset', fileName: 'sitemap.xml', source: sitemapXml() });
    },
  };
}

// The barcode decoder is a ~1 MB WebAssembly module loaded lazily, only when the
// scanner screen opens. Workbox does not see it through a static import graph
// entry, so `.wasm` is listed explicitly in globPatterns — see
// docs/technical-notes-scanning.md §2.3. Without this, scanning breaks offline
// and the failure only shows up on a plane.
export default defineConfig({
  plugins: [
    // Before everything: it only guards Vite's own id resolution.
    htmlProxyIdsAreAbsolutePaths(),
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',

      // THE REGISTRATION SCOPE. Not the same field as `manifest.scope` below,
      // and the difference was costing the public page its central property.
      //
      // `manifest.scope` tells the browser which URLs count as "inside the
      // installed app" for navigation and the window title bar. It has no effect
      // on which URLs the service worker CONTROLS. That is decided by the second
      // argument of `navigator.serviceWorker.register()`, which vite-plugin-pwa
      // takes from THIS option and which defaults to Vite's `base` — `/`.
      //
      // Measured on the built output before this line existed:
      //   dist/registerSW.js → navigator.serviceWorker.register('/sw.js', { scope: '/' })
      //
      // So after any visit to `/app/`, a worker built from application code
      // controlled the public landing page too. That directly contradicts what
      // docs/public-page-and-indexing.md claims about `/`: "static, no
      // JavaScript, loads even if the application build fails." It loaded from a
      // cache the application build owns. Three consequences, in order of
      // seriousness:
      //
      //   * `landing_csp` (`script-src 'none'`) stopped describing what runs on
      //     `/`. A cross-site script that reached any application chunk became
      //     persistent AND reached the public page.
      //   * The `Cache-Control: must-revalidate` that Caddy sets on `/` was
      //     bypassed by the worker's own cache, so an edit to the public page
      //     could not reach a returning visitor.
      //   * "The landing page survives a broken application build" became false
      //     in exactly the situation it was written for.
      //
      // THE TRADE, taken deliberately: the landing page loses offline access.
      // That is worth close to nothing — it is a description of the project,
      // read once, by a first-time visitor who is by definition online. The
      // application keeps offline entirely, which is the case that matters
      // (a kitchen, no signal, `navigateFallbackAllowlist` below).
      //
      // Narrowing needs no server cooperation: a worker at `/sw.js` may always
      // claim a scope at or below its own path. Only WIDENING would need a
      // `Service-Worker-Allowed` header. Verify after any change to this file:
      //   grep -o "scope: '[^']*'" frontend/dist/registerSW.js   # expect /app/
      // and CI asserts the same thing.
      scope: '/app/',

      includeAssets: ['favicon.ico', 'apple-touch-icon.png'],
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2,wasm}'],
        // Marketing weight, not application weight: the social card and the
        // store screenshots are ~750 kB that an installed application never
        // renders. Precaching them would charge every install for the landing
        // page's images.
        //
        // `index.html` — the LANDING page, not `app/index.html` — joins them now
        // that the worker's scope is `/app/`. A worker cannot serve a document it
        // does not control, so precaching the public page downloads bytes that
        // can never be used. The pattern is anchored, so `app/index.html` is
        // untouched.
        globIgnores: ['**/node_modules/**/*', 'index.html', 'social-preview.png', 'screenshots/**'],
        // The reader WASM is ~1 MB raw; the default 2 MB ceiling would drop it.
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        // The SPA fallback must not swallow `/`. Restricted to the application's
        // own prefix so an offline visit to the landing page serves the landing
        // page from the precache, not the application shell.
        navigateFallback: '/app/index.html',
        navigateFallbackAllowlist: [/^\/app\//],
        cleanupOutdatedCaches: true,
      },
      manifest: {
        id: '/app/',
        name: 'Chaudron',
        short_name: 'Chaudron',
        description:
          'Inventaire de cuisine et suggestions de recettes, à partir de ce que vous avez vraiment en stock.',
        lang: 'fr',
        dir: 'ltr',
        start_url: '/app/',
        scope: '/app/',
        display: 'standalone',
        orientation: 'portrait',
        background_color: '#22242A',
        theme_color: '#22242A',
        categories: ['food', 'lifestyle', 'utilities'],
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          {
            src: 'icon-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
        // What turns a bare "Add to home screen" into a rich install prompt.
        // These are real captures of a running stack (tools/screenshots.py), not
        // mock-ups.
        //
        // BOTH form factors are required, for different browsers: Android
        // Chrome reads `narrow`, desktop Chrome shows no rich prompt at all
        // unless a `wide` set exists. Within one form factor every entry must
        // share an aspect ratio, or the whole set is ignored — hence 440 × 952
        // throughout `narrow` and 1280 × 800 throughout `wide`.
        //
        // WebP, and roughly twice the size anything displays them at. The
        // previous set was 780 × 1688 PNG for a box a third that wide, which
        // cost every install prompt a few hundred kilobytes of pixels nobody
        // could see.
        screenshots: [
          {
            src: 'screenshots/inventory.webp',
            sizes: '440x952',
            type: 'image/webp',
            form_factor: 'narrow',
            label: "L'inventaire du foyer, trié par urgence de péremption",
          },
          {
            src: 'screenshots/recipes.webp',
            sizes: '440x952',
            type: 'image/webp',
            form_factor: 'narrow',
            label: 'Des suggestions de recettes tirées du stock disponible',
          },
          {
            src: 'screenshots/courses.webp',
            sizes: '440x952',
            type: 'image/webp',
            form_factor: 'narrow',
            label: 'La liste de courses, alimentée par le stock qui s’épuise',
          },
          {
            src: 'screenshots/add.webp',
            sizes: '440x952',
            type: 'image/webp',
            form_factor: 'narrow',
            label: 'Ajout d’un produit au code-barres ou à la main',
          },
          {
            src: 'screenshots/inventory-wide.webp',
            sizes: '1280x800',
            type: 'image/webp',
            form_factor: 'wide',
            label: "L'inventaire du foyer sur grand écran",
          },
          {
            src: 'screenshots/recipes-wide.webp',
            sizes: '1280x800',
            type: 'image/webp',
            form_factor: 'wide',
            label: 'Des suggestions de recettes tirées du stock disponible',
          },
          {
            src: 'screenshots/courses-wide.webp',
            sizes: '1280x800',
            type: 'image/webp',
            form_factor: 'wide',
            label: 'La liste de courses sur grand écran',
          },
        ],
      },
    }),
    // After VitePWA: this one removes tags that plugin injects.
    seoAssets(),
  ],
  build: {
    target: 'es2022',
    sourcemap: false,
    rollupOptions: {
      input: {
        landing: 'index.html',
        app: 'app/index.html',
      },
    },
  },
});
