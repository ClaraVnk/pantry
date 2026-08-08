/**
 * Decimal arithmetic for stock amounts.
 *
 * Amounts travel as decimal strings and never become JavaScript numbers here:
 * `0.1 + 0.2` is the classic reason a kitchen inventory ends up with
 * 0.30000000000000004 litres of milk. Everything below works on integers scaled
 * by 1000, which is the precision the add form already accepts.
 */

const SCALE = 3;
const FACTOR = 1000n;

const AMOUNT_RE = /^\d+(?:[.,]\d{1,3})?$/;

/** Scaled integer, or null when the text is not a positive decimal. */
function parseScaled(amount: string): bigint | null {
  const trimmed = amount.trim().replace(',', '.');
  if (!AMOUNT_RE.test(trimmed)) return null;

  const [whole = '0', fraction = ''] = trimmed.split('.');
  return BigInt(whole) * FACTOR + BigInt((fraction + '000').slice(0, SCALE));
}

function renderScaled(scaled: bigint): string {
  const whole = scaled / FACTOR;
  const fraction = (scaled % FACTOR).toString().padStart(SCALE, '0').replace(/0+$/, '');
  return fraction === '' ? whole.toString() : `${whole.toString()}.${fraction}`;
}

/**
 * Canonical decimal string for the wire, or null when the input is not a
 * positive amount with at most three decimals. Shared with the add form so both
 * entry points accept exactly the same thing.
 */
export function normaliseAmount(amount: string): string | null {
  const scaled = parseScaled(amount);
  if (scaled === null || scaled <= 0n) return null;
  return renderScaled(scaled);
}

/**
 * Divides an amount, rounding half up, and never down to zero — "half left" of
 * a very small amount is still some of it, and zero would mean "gone", which is
 * a removal and a different gesture.
 */
export function divideAmount(amount: string, divisor: number): string | null {
  const scaled = parseScaled(amount);
  if (scaled === null || scaled <= 0n || !Number.isInteger(divisor) || divisor <= 0) return null;
  const d = BigInt(divisor);
  const result = (2n * scaled + d) / (2n * d);
  return renderScaled(result <= 0n ? 1n : result);
}

/** Steps an amount by a whole unit. Null when the result would not be positive. */
export function stepAmount(amount: string, delta: number): string | null {
  const scaled = parseScaled(amount);
  if (scaled === null || !Number.isInteger(delta)) return null;
  const result = scaled + BigInt(delta) * FACTOR;
  if (result <= 0n) return null;
  return renderScaled(result);
}
