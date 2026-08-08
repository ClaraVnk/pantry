#!/usr/bin/env python
"""Drive the running PWA with a real browser and capture every published screenshot.

    uv run --no-project --with playwright --with pillow python tools/screenshots.py

Requires the whole stack up: PostgreSQL, the API, and a server for the frontend
(the Vite dev server, or ``vite preview`` over a production build). It takes
pictures of a live application talking to a live backend — nothing here renders a
mockup. If a screen cannot be reached, the script fails loudly rather than saving
a half-loaded page, because a screenshot that lies is worse than a missing one.

**It signs in.** Since authentication landed there is no way to reach a single
screen without a session, so the script posts the demonstration credentials
``scripts/seed.py`` creates and then picks the household. Override them with
``CHAUDRON_DEMO_EMAIL`` / ``CHAUDRON_DEMO_PASSWORD`` if the instance was seeded
differently.

Two form factors are produced, because the manifest wants both and because the
interface stopped being phone-only:

* ``narrow`` — an iPhone-sized viewport, the way the product is actually used,
  standing in a kitchen with one hand free;
* ``wide`` — a desktop viewport, which is what desktop Chrome needs before it
  will show a rich install prompt at all.

Captures are taken at ``device_scale_factor=2`` and then **downscaled to twice
the size they are displayed at**, in WebP. The previous set was 780 x 1688 for a
box a third that wide: three and a half times the pixels anybody rendered, in a
format that costs about four times what WebP does for the same picture.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

#: Captures the shipped frontend serves as well as documents: the PWA manifest's
#: ``screenshots`` array and the public landing page both point at these names
#: under ``/screenshots/``. Copied rather than re-shot so the README and the
#: install prompt can never show two different versions of the same screen.
#: Everything not listed here (the degraded banner, the dark variant) stays in
#: ``docs/`` — an installed application never renders it, and precaching a
#: quarter of a megabyte for nobody is not free.
PUBLISHED = (
    "inventory",
    "recipes",
    "courses",
    "add",
    "inventory-wide",
    "recipes-wide",
    "courses-wide",
)
PUBLISHED_OUT = ROOT / "frontend" / "public" / "screenshots"

# `/app/`, not `/`: the root serves the public landing page. The application is a
# separate Vite entry point (frontend/vite.config.ts) so that the indexable page
# and the private one cannot be the same document.
APP_URL = os.environ.get("CHAUDRON_APP_URL", "http://127.0.0.1:5173/app/")

EMAIL = os.environ.get("CHAUDRON_DEMO_EMAIL", "demo@chaudron.test")
PASSWORD = os.environ.get("CHAUDRON_DEMO_PASSWORD", "chaudron-demo-password")

# iPhone 14-ish. The product is used standing in a kitchen, one-handed.
NARROW = {"width": 390, "height": 844}
# A laptop, which is where the shopping list and the budget are actually read.
WIDE = {"width": 1280, "height": 800}

#: Rendered width, in CSS pixels, of the widest box any consumer puts these in —
#: the landing page grid for `narrow`, a full-width figure for `wide`. Everything
#: is emitted at twice this, which is what a 2x display needs and no more.
DISPLAY_WIDTH = {"narrow": 220, "wide": 640}

#: A local model on a small machine takes a while to answer. That is the product
#: being honest about self-hosted inference, not a hang — so the wait is
#: generous, and should stay longer than ``CHAUDRON_OLLAMA_TIMEOUT_SECONDS`` or
#: the browser gives up first and reports a failure the server never had.
#:
#: Raising it does not rescue a model too large for the machine's memory: that
#: fails at the same place, later. Use a smaller model instead.
SUGGESTION_TIMEOUT_MS = 300_000

#: How many times to ask before declaring the feature unphotographable. See
#: :func:`suggest_recipes` for why more than one is the honest number.
SUGGESTION_ATTEMPTS = 3


def find_chromium() -> str | None:
    """Locate a Playwright-managed Chromium, newest build first.

    The pinned Playwright release and the browsers already in the cache do not
    always agree on a build number; launching with an explicit path uses what is
    installed instead of demanding a fresh multi-hundred-megabyte download.
    Returning None lets Playwright resolve it as usual.
    """
    cache = Path.home() / ".cache" / "ms-playwright"
    builds = sorted(
        cache.glob("chromium-*/chrome-linux64/chrome"),
        key=lambda p: int(p.parts[-3].split("-")[-1]),
        reverse=True,
    )
    return str(builds[0]) if builds else None


async def new_page(browser: Browser, form_factor: str = "narrow", scheme: str = "light") -> Page:
    page = await browser.new_page(
        viewport=NARROW if form_factor == "narrow" else WIDE,
        device_scale_factor=2,
        color_scheme=scheme,
        locale="fr-FR",
        timezone_id="Europe/Paris",
    )
    page.on("pageerror", lambda exc: sys.stderr.write(f"  page error: {exc}\n"))
    return page


async def settle(page: Page) -> None:
    await page.wait_for_load_state("networkidle")
    # The inventory renders from a debounced fetch; give the list a beat to paint
    # so captures do not catch a spinner.
    await page.wait_for_timeout(900)


async def sign_in(page: Page) -> None:
    """Get from a cold load to the inventory, through whichever gates appear.

    Three states are possible and all three are normal: the sign-in form, the
    household picker (an account can belong to several), and — when a session
    cookie survived from an earlier run — the application itself.
    """
    await page.goto(APP_URL)
    await settle(page)

    # `input[name=…]`, not the visible label: a required field renders a marker
    # inside its `<label>`, so the accessible name is not the string in the
    # source and an exact match against it silently never resolves.
    form = page.locator('input[name="password"]')
    if await form.count() > 0:
        await page.locator('input[name="email"]').fill(EMAIL)
        await form.fill(PASSWORD)
        await page.get_by_role("button", name="Se connecter").click()
        await settle(page)

    picker = page.get_by_role("heading", name="Quel foyer ouvrir ?")
    if await picker.count() > 0:
        await page.locator("main ul li button").first.click()
        await settle(page)

    if "Inventaire" not in await page.inner_text("body"):
        raise SystemExit(
            "the inventory screen did not render after signing in; is the stack up, "
            "and has scripts/seed.py been run against this database?"
        )


#: Names the caller asked for, or empty for "everything". Set from ``sys.argv``.
#:
#: A screenshot is only as good as the instance behind it, and the instances
#: differ: the published set was taken against a household with a real model
#: configured, and re-running the whole script against one without would quietly
#: replace those pictures with degraded-mode versions of themselves. Overwriting
#: a good capture is not recoverable from here — the tool has no idea what the
#: file it is about to replace was worth.
#:
#: So a re-shoot of one screen after a UI change is a first-class operation:
#:
#:     python tools/screenshots.py budget household
#:
#: Names are the file stems, without ``.webp``.
WANTED: set[str] = set()


def _skip(name: str) -> bool:
    return bool(WANTED) and name not in WANTED


async def shoot(page: Page, name: str, form_factor: str = "narrow") -> Path | None:
    """Capture, downscale to twice the displayed size, and write WebP.

    Playwright cannot resize, so the picture is taken at 2x the viewport for
    sharpness and resampled here. Lanczos, because a screenshot is text and thin
    borders — the two things a box filter destroys first.

    Returns ``None`` when the caller named a subset and this is not in it. The
    navigation that got here still ran, because the screens are reached by
    walking the application rather than by URL.
    """
    if _skip(name):
        return None
    OUT.mkdir(parents=True, exist_ok=True)
    raw = await page.screenshot()
    image = Image.open(io.BytesIO(raw))
    target = DISPLAY_WIDTH[form_factor] * 2
    if image.width > target:
        height = round(image.height * target / image.width)
        image = image.resize((target, height), Image.LANCZOS)
    path = OUT / f"{name}.webp"
    # `method=6` is the slowest, smallest setting. A few seconds once, against
    # bytes every visitor downloads.
    image.convert("RGB").save(path, "WEBP", quality=82, method=6)
    return path


async def tab(page: Page, label: str) -> None:
    """Switch screens from the bottom navigation.

    Scoped to the `<nav>`: "Ajouter" is also the submit button on the shopping
    list, and an unscoped role query matches both and refuses to guess.
    """
    nav = page.locator('nav[aria-label="Navigation principale"]')
    await nav.locator("button").filter(has_text=label).click()
    await settle(page)


async def scroll_past_banner(page: Page) -> None:
    """Put the product, not the warning about it, at the top of the frame.

    The degraded banner is a real screen and gets its own capture. Left in place
    for the others it buries the inventory it is warning about.
    """
    await page.evaluate("() => document.getElementById('main')?.scrollIntoView({block: 'start'})")
    await page.wait_for_timeout(400)


async def opt_into_budget(page: Page) -> None:
    """Get past the "shall I count this at all?" card to the budget itself.

    The screen opens on a consent card rather than on a figure, because the
    budget is opt-in: nothing is computed until a household asks for it. That is
    a real design decision and it is defensible, but it is not a picture of the
    feature — a capture taken here shows a button and a paragraph.

    Idempotent: a household that has already opted in never renders the card, so
    the click is skipped rather than waited for.
    """
    card = page.get_by_role("button", name="Calculer ma dépense")
    if await card.count() == 0:
        return
    await card.click()
    await settle(page)


async def suggest_recipes(page: Page) -> None:
    """Ask for suggestions, wait out the model, and frame the answer.

    The screen is a form first and results second, so a capture taken at the top
    of it shows the request rather than what came back. Scrolling to the first
    card is the difference between photographing a button and photographing the
    feature.

    Retried, because a small self-hosted model without guaranteed structured
    output sometimes returns prose the parser refuses, and the API answers 502.
    That is a real property of this configuration — the degraded banner says so
    in as many words — and it is transient: the same request usually parses on
    the next attempt. Retrying is not papering over it; giving up on a first
    miss would just mean no screenshot of a feature that does work.
    """
    for attempt in range(1, SUGGESTION_ATTEMPTS + 1):
        await page.get_by_role("button", name="Proposer des recettes").click()
        await page.get_by_text("Recherche de recettes à partir de votre stock").wait_for(
            state="detached", timeout=SUGGESTION_TIMEOUT_MS
        )
        await settle(page)
        if await page.locator("article").count() > 0:
            break
        sys.stderr.write(f"  no suggestion rendered (attempt {attempt}); retrying\n")
    else:
        raise SystemExit(
            f"no suggestion was rendered in {SUGGESTION_ATTEMPTS} attempts: the model "
            "returned nothing usable, or no provider is configured for this household"
        )
    card = page.locator("article").first
    # `scroll_into_view_if_needed` scrolls the minimum distance, which on a card
    # taller than the viewport lands somewhere in its middle and cuts off the
    # recipe's title — the one line that says what the suggestion is. Align the
    # top instead, then back off by the sticky header so it does not sit on top
    # of it.
    await card.evaluate("el => el.scrollIntoView({block: 'start'})")
    await page.evaluate("() => window.scrollBy(0, -72)")
    await page.wait_for_timeout(400)


async def capture_set(browser: Browser, form_factor: str) -> list[Path]:
    """Every screen worth publishing, at one form factor.

    Ordered by what it costs to fail. Everything down to the shopping list is
    pure database and renders in milliseconds; the recipe screen is last because
    it is the only one that waits on a model, and a model on a small self-hosted
    machine is the one thing here that can take minutes or not answer at all.
    Losing it must not cost the captures that were already safe.
    """
    suffix = "" if form_factor == "narrow" else "-wide"
    written: list[Path] = []

    def keep(path: Path | None) -> None:
        if path is not None:
            written.append(path)

    page = await new_page(browser, form_factor)
    await sign_in(page)

    if form_factor == "narrow":
        # Kept deliberately, and only once: the degradation notice is an argument
        # for the product, not an embarrassment. The README explains it.
        keep(await shoot(page, "degraded-banner", form_factor))

    await scroll_past_banner(page)
    keep(await shoot(page, f"inventory{suffix}", form_factor))

    await tab(page, "Courses")
    await scroll_past_banner(page)
    keep(await shoot(page, f"courses{suffix}", form_factor))

    if form_factor == "narrow":
        await tab(page, "Ajouter")
        await scroll_past_banner(page)
        keep(await shoot(page, "add", form_factor))

        # Budget and Foyer, narrow only and deliberately not in `PUBLISHED`.
        #
        # Both are screens the earlier set predates, and both answer a question
        # the first four cannot: the inventory shows what a household *has*, and
        # these two show what it *spent* and *who it cooks for*. The dietary
        # panel in particular is the visible half of the constraint engine —
        # allergens and infant rules are a filter, not a suggestion, and a
        # screenshot is the only place that is legible without reading ADR-0009.
        #
        # Out of `PUBLISHED` on the same argument the constant already makes:
        # that tuple is precached by the service worker and rendered in the PWA
        # install prompt, and a picture nobody in that prompt will scroll to is
        # a quarter of a megabyte spent on nobody. The README and `docs/` read
        # these straight out of `docs/screenshots/`.
        await tab(page, "Budget")
        await opt_into_budget(page)
        await scroll_past_banner(page)
        keep(await shoot(page, "budget", form_factor))

        await tab(page, "Foyer")
        await scroll_past_banner(page)
        keep(await shoot(page, "household", form_factor))

    await tab(page, "Recettes")
    await scroll_past_banner(page)
    await suggest_recipes(page)
    keep(await shoot(page, f"recipes{suffix}", form_factor))

    await page.close()
    return written


async def main() -> None:
    written: list[Path] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=find_chromium(),
            # A self-hosted model answers in about a minute, and the tab has to
            # stay alive across that wait on a machine that is also running the
            # model. `/dev/shm` is small in a container and Chromium falls back
            # to disk rather than dying; the rest simply stops it spawning
            # processes this script has no use for.
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=384",
            ],
        )

        written += await capture_set(browser, "narrow")
        written += await capture_set(browser, "wide")

        dark = await new_page(browser, "narrow", scheme="dark")
        await sign_in(dark)
        await scroll_past_banner(dark)
        dark_shot = await shoot(dark, "inventory-dark")
        if dark_shot is not None:
            written.append(dark_shot)
        await dark.close()

        await browser.close()

    PUBLISHED_OUT.mkdir(parents=True, exist_ok=True)
    for name in PUBLISHED:
        if _skip(name):
            continue
        source = OUT / f"{name}.webp"
        target = PUBLISHED_OUT / f"{name}.webp"
        target.write_bytes(source.read_bytes())
        written.append(target)

    for path in written:
        with Image.open(path) as image:
            size = f"{image.width}x{image.height}"
        sys.stdout.write(f"  {path.relative_to(ROOT)}  {size}  {path.stat().st_size // 1024} kB\n")


if __name__ == "__main__":
    WANTED.update(sys.argv[1:])
    if WANTED:
        sys.stdout.write(f"  capturing only: {', '.join(sorted(WANTED))}\n")
    asyncio.run(main())
