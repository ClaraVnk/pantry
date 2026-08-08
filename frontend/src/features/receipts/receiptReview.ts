/**
 * The draft model of the receipt review, and the rules for turning it back into
 * the body of the one call that writes stock (contract §6ter and §7).
 *
 * Kept out of the components because three of the rules below are decisions
 * rather than formatting, and a decision that lives inside an `onChange` handler
 * is a decision nobody finds again:
 *
 * - editing a label drops the catalogue match;
 * - a half-quantity is no quantity;
 * - a kept line with no quantity is legal, and creates no lot.
 *
 * The last one is the one that looks like a bug and is not. A receipt line whose
 * quantity nobody could read is a line the reviewer chose to keep without
 * completing; inventing "1 pièce" for it would be inventing an inventory, which
 * is the exact failure §6ter separates the budget from the stock to avoid.
 */

import type { ReceiptLine, ReceiptConfirmLine } from '../../api/types';
import { formatAmount } from '../../lib/expiry';
import { normaliseAmount } from '../../lib/quantity';

/**
 * One line as the user is editing it.
 *
 * `amount` and `unit` are the raw contents of two controls, not a parsed
 * quantity: what somebody has half-typed must survive a re-render, and a field
 * that reformats under the caret is a field nobody can correct.
 */
export interface ReceiptDraftLine {
  /** The server's line identifier. The confirm call addresses lines by it. */
  id: string;
  label: string;
  amount: string;
  unit: string;
  /** The catalogue match, or `null`. Dropped as soon as the label is edited. */
  productId: string | null;
  /** What the till printed. Shown whenever it differs from `label`. */
  raw: string;
  /** A reading the lexicon produced but declined to trust. One tap to accept. */
  suggestion: string | null;
  unitPrice: string | null;
  totalPrice: string | null;
  /** True when the server proposed a product and a human has not yet agreed. */
  suggestedMatch: boolean;
  /** Removed from the review. Kept in the list so the removal is undoable. */
  removed: boolean;
}

export function toReceiptDrafts(lines: ReceiptLine[]): ReceiptDraftLine[] {
  return lines.map((line) => ({
    id: line.id,
    label: line.label,
    amount: line.quantity === null ? '' : formatAmount(line.quantity),
    unit: line.unit ?? '',
    productId: line.matched_product_id,
    raw: line.raw_label,
    suggestion: line.suggested_label,
    unitPrice: line.unit_price,
    totalPrice: line.total_price,
    suggestedMatch: line.match_status === 'suggested',
    removed: false,
  }));
}

/**
 * Rewriting the label breaks the catalogue match, on purpose.
 *
 * The match was made on exact normalised equality with the text the receipt
 * printed. Once that text is gone, keeping `product_id` would put the product
 * the *machine* chose in the cupboard under the name the *human* typed.
 */
export function editReceiptLabel(line: ReceiptDraftLine, label: string): ReceiptDraftLine {
  return { ...line, label, productId: label === line.raw ? line.productId : null };
}

/** Accept the lexicon's reading. One tap, and it drops the match for the same reason. */
export function acceptSuggestion(line: ReceiptDraftLine): ReceiptDraftLine {
  return line.suggestion === null ? line : editReceiptLabel(line, line.suggestion);
}

/** Is this line ready to be written? Empty labels and half-quantities are not. */
export function receiptDraftError(line: ReceiptDraftLine): string | null {
  if (line.removed) return null;
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
 * The body of the confirm call: the lines the reviewer kept.
 *
 * Removed lines are simply **absent**. The server marks them `ignored` and keeps
 * their price, because "do not put this in my cupboard" is not "this was not on
 * the receipt" — and the sum shown beside the total has to stay the sum of what
 * was printed.
 *
 * `product_id` and `label` are never both sent: the server would then hold a
 * catalogue name and a free text that disagree.
 */
export function toReceiptConfirmLines(lines: ReceiptDraftLine[]): ReceiptConfirmLine[] {
  return lines
    .filter((line) => !line.removed)
    .map((line) => {
      const amount = line.amount.trim() === '' ? null : normaliseAmount(line.amount);
      const unit = line.unit.trim();
      const quantity = amount !== null && unit !== '' ? { amount, unit } : undefined;
      return line.productId !== null
        ? { id: line.id, product_id: line.productId, quantity }
        : { id: line.id, label: line.label.trim(), quantity };
    });
}
