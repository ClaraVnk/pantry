/**
 * The unit vocabulary every quantity field in the application draws from.
 *
 * Lifted out of `features/shopping/` because it was not a shopping concern: the
 * add-item form kept its own list — `pièce`, `portion`, `boîte`, `sachet`,
 * `bocal`, `mL` — of which not one was a code in the `unit` table, so manual
 * entry answered `422` on its own default value. Two lists, one authority: the
 * second list is the bug.
 *
 * A closed set, and a `<select>` rather than a text field because of it: the
 * server resolves a code against the `unit` table and refuses anything else, so
 * a free-text unit is a `422` waiting for somebody to type "kilos". One tap
 * beats one round trip and one error message.
 *
 * The table is the authority, not this file — which is why `unitOptions` takes
 * the codes already present in the data and keeps any it does not know. An
 * instance with a unit this build has never heard of must not lose it during a
 * review.
 */

/** Codes seeded by migration `0002`, with the symbol each one is read as. */
const KNOWN_UNITS: { code: string; symbol: string }[] = [
  { code: 'g', symbol: 'g' },
  { code: 'kg', symbol: 'kg' },
  { code: 'mg', symbol: 'mg' },
  { code: 'ml', symbol: 'ml' },
  { code: 'cl', symbol: 'cl' },
  { code: 'dl', symbol: 'dl' },
  { code: 'l', symbol: 'L' },
  { code: 'tsp', symbol: 'c. à c.' },
  { code: 'tbsp', symbol: 'c. à s.' },
  { code: 'piece', symbol: 'pièce' },
];

const SYMBOLS = new Map(KNOWN_UNITS.map((unit) => [unit.code, unit.symbol]));

/** How a unit code is written on screen. Unknown codes are shown verbatim. */
export function unitSymbol(code: string): string {
  return SYMBOLS.get(code) ?? code;
}

/**
 * The options of a unit picker, including any code already in use that this
 * build does not know about.
 */
export function unitOptions(inUse: readonly string[]): { code: string; symbol: string }[] {
  const extra = inUse
    .filter((code) => code !== '' && !SYMBOLS.has(code))
    .filter((code, index, all) => all.indexOf(code) === index)
    .map((code) => ({ code, symbol: code }));
  return [...KNOWN_UNITS, ...extra];
}
