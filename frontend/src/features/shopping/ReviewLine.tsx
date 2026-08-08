import { useId } from 'react';
import { Badge, Button } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { CONFIDENCE_LABELS, describeFlag, draftError, editLabel, type DraftLine } from './review';
import { unitOptions } from '../../lib/units';
import styles from './Shopping.module.css';

interface Props {
  line: DraftLine;
  position: number;
  onChange: (line: DraftLine) => void;
  onRemove: () => void;
}

/**
 * One line of the review, and the screen where the parser's documented failures
 * get repaired.
 *
 * The three repairs cost one gesture each, which is the whole design:
 *
 * - **the label is a text field, already focusable** — no "edit" button to find
 *   first. `3 paquets de pâtes` becomes `pâtes` by typing;
 * - **the unit is a picker whose first option clears the quantity** — one tap
 *   undoes the `2` that the numbering of `2. tomates` was read as, amount
 *   included, because a lone amount is not a quantity anyone can act on;
 * - **removal is a button on the card** — the document's own title, read as an
 *   article, goes in one tap.
 *
 * A review that costs three gestures per line gets approved wholesale, and a
 * wholesale approval is exactly what this screen exists to prevent.
 *
 * An unmatched line is shown as **the text it is**. There is no placeholder
 * product, no "produit inconnu", no guess: matching is exact normalised
 * equality (`PDT NOUV` will never find `Pommes de terre nouvelles`), so a miss
 * says nothing about the article and the interface says nothing either.
 */
export function ReviewLine({ line, position, onChange, onRemove }: Props) {
  const id = useId();
  const labelId = `${id}-label`;
  const amountId = `${id}-amount`;
  const unitId = `${id}-unit`;
  const errorId = `${id}-error`;

  const error = draftError(line);
  const hasQuantity = line.unit !== '';
  const showsRaw = line.raw !== '' && line.raw !== line.label;

  const setUnit = (unit: string): void => {
    // Clearing the unit clears the amount with it. The two are stored together
    // or not at all, and a leftover "2" with nothing beside it is the bogus
    // reading this control exists to undo.
    onChange(unit === '' ? { ...line, unit: '', amount: '' } : { ...line, unit });
  };

  return (
    <li className={[styles.reviewCard, error ? styles.reviewCardInvalid : ''].join(' ')}>
      <div className={styles.reviewHead}>
        <label className="visually-hidden" htmlFor={labelId}>
          Libellé de l’article {position}
        </label>
        <input
          id={labelId}
          className={[controlClass(error !== null), styles.reviewLabel].join(' ')}
          type="text"
          autoComplete="off"
          enterKeyHint="done"
          maxLength={200}
          aria-describedby={error === null ? undefined : errorId}
          aria-invalid={error !== null}
          value={line.label}
          onChange={(event) => {
            onChange(editLabel(line, event.target.value));
          }}
        />
        <Button
          variant="ghost"
          className={styles.reviewRemove}
          aria-label={`Retirer « ${line.label || line.raw} » de l’import`}
          onClick={onRemove}
        >
          <span aria-hidden="true">✕</span>
        </Button>
      </div>

      <div
        className={styles.reviewQuantity}
        role="group"
        aria-label={`Quantité de l’article ${String(position)}`}
      >
        <span className={styles.reviewQuantityLabel}>Quantité</span>
        <input
          id={amountId}
          className={[controlClass(), styles.reviewAmount].join(' ')}
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
          id={unitId}
          className={[controlClass(), styles.reviewUnit].join(' ')}
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
      </div>

      {error === null ? null : (
        <p className={styles.reviewError} id={errorId} role="alert">
          {error}
        </p>
      )}

      <div className={styles.reviewMeta}>
        {line.productId === null ? null : <Badge tone="ok">Produit du catalogue</Badge>}
        {line.flags.map((flag) => (
          <Badge key={flag} tone="warn">
            Allergène déclaré : {describeFlag(flag)}
          </Badge>
        ))}
        {line.needsReview ? (
          <Badge tone="neutral">{CONFIDENCE_LABELS[line.confidence]}</Badge>
        ) : null}
      </div>

      {showsRaw ? (
        <p className={styles.reviewRaw}>
          <span className={styles.reviewRawLabel}>Ligne lue :</span> {line.raw}
        </p>
      ) : null}
    </li>
  );
}
