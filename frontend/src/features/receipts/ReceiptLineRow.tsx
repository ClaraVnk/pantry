import { useId } from 'react';
import { Badge, Button } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { unitOptions } from '../../lib/units';
import { formatMoney } from '../budget/money';
import {
  acceptSuggestion,
  editReceiptLabel,
  receiptDraftError,
  type ReceiptDraftLine,
} from './receiptReview';
import styles from './Receipts.module.css';

interface Props {
  line: ReceiptDraftLine;
  position: number;
  currency: string | null;
  onChange: (line: ReceiptDraftLine) => void;
  onToggleRemoved: () => void;
}

function money(value: string | null, currency: string | null): string | null {
  if (value === null) return null;
  return currency === null ? value : formatMoney(value, currency);
}

/**
 * One line of the receipt review, and the screen where a machine reading gets
 * repaired by the person who did the shopping.
 *
 * Every repair costs **one gesture**, which is the whole design:
 *
 * - the label is a text field, already focusable — no "modifier" button to find
 *   first, so `PDT NOUV 1KG` becomes `Pommes de terre` by typing;
 * - the lexicon's reading, when it produced one it would not vouch for, is a
 *   single button that fills the field — offered, never applied;
 * - the unit is a picker whose first option clears the quantity, amount
 *   included, because a lone amount is not a quantity anyone can act on;
 * - removal is one tap and is **reversible**, so a mis-tap does not cost the
 *   line and there is no confirmation dialogue in the way of the common case.
 *
 * A review that costs three gestures per line gets approved wholesale, and a
 * wholesale approval is exactly what this screen exists to prevent
 * (`docs/technical-notes-ingestion.md` §3.4: a model reading `PDT NOUV 1KG` is
 * right about half the time).
 *
 * An unmatched line is shown as **the text it is**. No placeholder product, no
 * "produit inconnu", no guess: matching is exact normalised equality, so a miss
 * says nothing about the article and the interface says nothing either.
 */
export function ReceiptLineRow({ line, position, currency, onChange, onToggleRemoved }: Props) {
  const id = useId();
  const labelId = `${id}-label`;
  const errorId = `${id}-error`;

  const error = receiptDraftError(line);
  const hasQuantity = line.unit !== '';
  const showsRaw = line.raw !== '' && line.raw !== line.label;
  const total = money(line.totalPrice, currency);

  const setUnit = (unit: string): void => {
    // Clearing the unit clears the amount with it: the two are stored together
    // or not at all, and a leftover number with nothing beside it is precisely
    // the bogus reading this control exists to undo.
    onChange(unit === '' ? { ...line, unit: '', amount: '' } : { ...line, unit });
  };

  return (
    <li
      className={[
        styles.lineCard,
        error ? styles.lineCardInvalid : '',
        line.removed ? styles.lineCardRemoved : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className={styles.lineHead}>
        <label className="visually-hidden" htmlFor={labelId}>
          Libellé de l’article {position}
        </label>
        <input
          id={labelId}
          className={[controlClass(error !== null), styles.lineLabel].join(' ')}
          type="text"
          autoComplete="off"
          enterKeyHint="done"
          maxLength={200}
          disabled={line.removed}
          aria-describedby={error === null ? undefined : errorId}
          aria-invalid={error !== null}
          value={line.label}
          onChange={(event) => {
            onChange(editReceiptLabel(line, event.target.value));
          }}
        />
        <Button
          variant="ghost"
          className={styles.lineRemove}
          aria-label={
            line.removed
              ? `Remettre « ${line.raw} » dans l’import`
              : `Retirer « ${line.label || line.raw} » de l’import`
          }
          onClick={onToggleRemoved}
        >
          <span aria-hidden="true">{line.removed ? '↩' : '✕'}</span>
        </Button>
      </div>

      {line.removed ? (
        <p className={styles.lineNote}>
          Cette ligne n’entrera pas en stock. Elle reste comptée dans le total du ticket : la
          retirer de vos placards ne veut pas dire qu’elle n’était pas sur le ticket.
        </p>
      ) : (
        <>
          <div
            className={styles.lineQuantity}
            role="group"
            aria-label={`Quantité de l’article ${String(position)}`}
          >
            <span className={styles.lineQuantityLabel}>Quantité</span>
            <input
              className={[controlClass(), styles.lineAmount].join(' ')}
              type="text"
              inputMode="decimal"
              autoComplete="off"
              disabled={!hasQuantity}
              aria-label={`Nombre, article ${String(position)}`}
              value={line.amount}
              onChange={(event) => {
                onChange({ ...line, amount: event.target.value });
              }}
            />
            <select
              className={[controlClass(), styles.lineUnit].join(' ')}
              aria-label={`Unité, article ${String(position)}`}
              value={line.unit}
              onChange={(event) => {
                setUnit(event.target.value);
              }}
            >
              <option value="">— sans quantité</option>
              {unitOptions([line.unit]).map((unit) => (
                <option key={unit.code} value={unit.code}>
                  {unit.symbol}
                </option>
              ))}
            </select>
            {total === null ? null : <span className={styles.linePrice}>{total}</span>}
          </div>

          {error === null ? null : (
            <p className={styles.lineError} id={errorId} role="alert">
              {error}
            </p>
          )}

          {line.suggestion === null ? null : (
            <div className={styles.lineSuggestion}>
              <span className={styles.lineSuggestionText}>
                Lecture possible : <strong>{line.suggestion}</strong>
              </span>
              <Button
                variant="secondary"
                onClick={() => {
                  onChange(acceptSuggestion(line));
                }}
              >
                Utiliser
              </Button>
            </div>
          )}

          <div className={styles.lineMeta}>
            {line.productId === null ? null : (
              <Badge tone={line.suggestedMatch ? 'neutral' : 'ok'}>
                {line.suggestedMatch ? 'Produit proposé' : 'Produit du catalogue'}
              </Badge>
            )}
            {line.amount === '' ? <Badge tone="warn">Sans quantité</Badge> : null}
          </div>
        </>
      )}

      {showsRaw ? (
        <p className={styles.lineRaw}>
          <span className={styles.lineRawLabel}>Ligne imprimée :</span> {line.raw}
        </p>
      ) : null}
    </li>
  );
}
