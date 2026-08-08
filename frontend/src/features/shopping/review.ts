/**
 * The draft model of the review screen, and the rules for turning it back into
 * the body of the one call that writes (contract §7.2).
 *
 * Kept out of the components because two of the rules below are decisions, not
 * formatting: editing a label drops the catalogue match, and a half-quantity is
 * no quantity. Both are the kind of thing that silently rots when it lives
 * inside a `onChange` handler.
 */

import type {
  ImportConfidence,
  ShoppingImportConfirmLine,
  ShoppingImportLine,
} from '../../api/types';
import { ALLERGEN_LABELS } from '../../lib/dietary';
import { ALLERGEN_CODES, type AllergenCode } from '../../api/types';
import { formatAmount } from '../../lib/expiry';
import { normaliseAmount } from '../../lib/quantity';

/**
 * One line as the user is editing it.
 *
 * `amount` and `unit` are the raw contents of two text fields, not a parsed
 * quantity: what somebody has half-typed must survive a re-render, and a field
 * that reformats under the caret is a field nobody can correct.
 */
export interface DraftLine {
  /** Stable across edits and independent of the index, so removing a line above
   * this one does not move React's identity onto a different card. */
  key: string;
  label: string;
  amount: string;
  unit: string;
  /**
   * The catalogue match, or `null`. Dropped as soon as the label is edited —
   * see `editLabel`.
   */
  productId: string | null;
  /** What the document actually said. Shown whenever it differs from `label`. */
  raw: string;
  confidence: ImportConfidence;
  needsReview: boolean;
  flags: string[];
}

let counter = 0;

function nextKey(): string {
  counter += 1;
  return `line-${String(counter)}`;
}

/** The proposal, as a set of drafts. Nothing is dropped, nothing is guessed. */
export function toDrafts(lines: ShoppingImportLine[]): DraftLine[] {
  return lines.map((line) => ({
    key: nextKey(),
    label: line.product_name,
    amount: line.quantity === null ? '' : formatAmount(line.quantity.amount),
    unit: line.quantity?.unit ?? '',
    productId: line.matched_product_id,
    raw: line.raw,
    confidence: line.confidence,
    needsReview: line.needs_review,
    flags: line.flags ?? [],
  }));
}

/** An empty line, for the one failure the user has to fix by hand: `pain et lait`. */
export function blankDraft(): DraftLine {
  return {
    key: nextKey(),
    label: '',
    amount: '',
    unit: '',
    productId: null,
    raw: '',
    confidence: 'none',
    needsReview: false,
    flags: [],
  };
}

/**
 * Rewriting the label breaks the catalogue match, on purpose.
 *
 * The match was made on exact normalised equality with the text the parser
 * read. Once that text is gone, keeping `product_id` would write the product
 * the *machine* chose under the name the *human* typed — and since the server
 * stores both, the list would then show a name that is not the one on screen.
 * Losing a match costs a search; keeping a stale one costs trust in the list.
 */
export function editLabel(line: DraftLine, label: string): DraftLine {
  return { ...line, label, productId: label === line.raw ? line.productId : null };
}

/** Is this line ready to be written? Empty labels and half-quantities are not. */
export function draftError(line: DraftLine): string | null {
  if (line.label.trim() === '') return 'Cette ligne n’a pas de libellé.';
  const hasAmount = line.amount.trim() !== '';
  const hasUnit = line.unit.trim() !== '';
  if (hasAmount !== hasUnit) {
    return 'Une quantité a besoin d’un nombre et d’une unité, ou d’aucun des deux.';
  }
  if (hasAmount && normaliseAmount(line.amount) === null) {
    return 'Indiquez une quantité positive, par exemple 1,5.';
  }
  return null;
}

/**
 * The body of the confirm call.
 *
 * `product_id` and `label` are never both sent: the server would store the two
 * side by side, and the list would carry a product name and a free text that
 * disagree. A matched line sends the identifier, an edited one sends the words.
 */
export function toConfirmLine(line: DraftLine): ShoppingImportConfirmLine {
  const amount = line.amount.trim() === '' ? null : normaliseAmount(line.amount);
  const unit = line.unit.trim();
  const quantity = amount !== null && unit !== '' ? { amount, unit } : undefined;

  return line.productId !== null
    ? { product_id: line.productId, quantity }
    : { label: line.label.trim(), quantity };
}

// --------------------------------------------------------------------------
// Flags
// --------------------------------------------------------------------------

const ALLERGEN_PREFIX = 'allergen:';

function isAllergenCode(value: string): value is AllergenCode {
  return (ALLERGEN_CODES as readonly string[]).includes(value);
}

/**
 * A flag, in French.
 *
 * An unknown flag is shown as the token it is rather than swallowed: a signal
 * this build does not recognise is still a signal, and hiding it would be the
 * one thing §7.4 forbids — quietly deciding on the household's behalf.
 */
export function describeFlag(flag: string): string {
  if (flag.startsWith(ALLERGEN_PREFIX)) {
    const code = flag.slice(ALLERGEN_PREFIX.length);
    return isAllergenCode(code) ? ALLERGEN_LABELS[code] : code;
  }
  return flag;
}

/** How much of a line the parser claims to have understood. */
export const CONFIDENCE_LABELS: Record<ImportConfidence, string> = {
  high: 'Lecture sûre',
  medium: 'Lecture probable',
  low: 'Lecture incertaine',
  none: 'Non analysé',
};
