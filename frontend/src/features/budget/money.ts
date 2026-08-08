/**
 * Decimal arithmetic for money, in the spirit of `lib/quantity.ts`.
 *
 * Amounts arrive as decimal strings and are turned into integer centimes with
 * `BigInt` before anything is added or subtracted. `400.00 - 182.47` in
 * JavaScript floats is `217.53000000000003`; on a screen whose whole purpose is
 * to be exact about somebody's money, that is not a rounding detail.
 *
 * The only float in this file is the last step of formatting, where `Intl`
 * requires a number. A `numeric(12,2)` column tops out around 10^12 centimes,
 * comfortably inside `Number.MAX_SAFE_INTEGER`, and `Intl` rounds the result to
 * two fraction digits — so the conversion cannot move a displayed figure.
 */

/** Optional sign, digits, and at most two decimals. Nothing else is money. */
const MONEY_RE = /^-?\d+(?:\.\d{1,2})?$/;

const LOCALE = 'fr-FR';

/** Integer centimes, or null when the text is not a decimal amount. */
export function toCentimes(value: string): bigint | null {
  const trimmed = value.trim().replace(',', '.');
  if (!MONEY_RE.test(trimmed)) return null;

  const negative = trimmed.startsWith('-');
  const [whole = '0', fraction = ''] = trimmed.replace('-', '').split('.');
  const centimes = BigInt(whole) * 100n + BigInt((fraction + '00').slice(0, 2));
  return negative ? -centimes : centimes;
}

/** Canonical wire form for a target the user typed, or null when it is not one. */
export function normaliseMoney(value: string): string | null {
  const centimes = toCentimes(value);
  if (centimes === null || centimes <= 0n) return null;
  const whole = centimes / 100n;
  const fraction = (centimes % 100n).toString().padStart(2, '0');
  return `${whole.toString()}.${fraction}`;
}

/**
 * A currency-formatted amount, falling back to a plain rendering when the code
 * is not one `Intl` knows. An unknown currency makes `Intl` throw, and a screen
 * that crashes on an unexpected three-letter code is worse than one that shows
 * `12.30 XYZ`.
 */
export function formatCentimes(centimes: bigint, currency: string): string {
  const asNumber = Number(centimes) / 100;
  try {
    return new Intl.NumberFormat(LOCALE, { style: 'currency', currency }).format(asNumber);
  } catch {
    return `${asNumber.toFixed(2)} ${currency}`;
  }
}

/** Formats a wire amount. Unparseable text is shown as it arrived, never as 0. */
export function formatMoney(value: string, currency: string): string {
  const centimes = toCentimes(value);
  return centimes === null ? `${value} ${currency}` : formatCentimes(centimes, currency);
}

/**
 * The gap between a target and what was spent, in centimes.
 *
 * Positive means there is money left, negative means the target was passed.
 * Both are facts; neither is an error, and nothing in this module decides how
 * they should look.
 */
export function remainingCentimes(spent: string, target: string): bigint | null {
  const spentCentimes = toCentimes(spent);
  const targetCentimes = toCentimes(target);
  if (spentCentimes === null || targetCentimes === null) return null;
  return targetCentimes - spentCentimes;
}

/** `1er août 2026`, the way a period boundary is read aloud. */
export function formatDay(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'long', year: 'numeric' }).format(
    parsed,
  );
}

/** `août 2026` or `semaine du 3 août`, for a history row. */
export function formatPeriodLabel(period: 'week' | 'month', startIso: string): string {
  const parsed = new Date(`${startIso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return startIso;
  if (period === 'month') {
    return new Intl.DateTimeFormat(LOCALE, { month: 'long', year: 'numeric' }).format(parsed);
  }
  const day = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'long' }).format(parsed);
  return `semaine du ${day}`;
}
