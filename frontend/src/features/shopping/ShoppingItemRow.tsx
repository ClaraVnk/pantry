import { useId, useState } from 'react';
import { describeError } from '../../api/client';
import { removeShoppingListItem, updateShoppingListItem } from '../../api/endpoints';
import type { ShoppingListItem } from '../../api/types';
import { Button, Checkbox } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { formatAmount } from '../../lib/expiry';
import { normaliseAmount } from '../../lib/quantity';
import { unitOptions, unitSymbol } from '../../lib/units';
import styles from './Shopping.module.css';

interface Props {
  item: ShoppingListItem;
  onChanged: (item: ShoppingListItem) => void;
  onRemoved: () => void;
}

/**
 * What to call this line.
 *
 * A free-text item is shown as **the text it is**. The import matches on exact
 * normalised equality and nothing else, so most lines of a real shopping list
 * never reach the catalogue — and an interface that dressed them up as products
 * would be claiming a match that was never made.
 */
function itemLabel(item: ShoppingListItem): string {
  return item.product_name ?? item.free_text ?? 'Article sans nom';
}

/**
 * One line of the list: tick, untick, correct the quantity, remove.
 *
 * The tick is optimistic and the row reverts if the server refuses. Ticking is
 * the gesture of the aisle — it is repeated twenty times, one-handed, on a
 * connection that comes and goes — and a checkbox that waits for a round trip
 * before it moves reads as broken.
 */
export function ShoppingItemRow({ item, onChanged, onRemoved }: Props) {
  const id = useId();
  const [optimisticChecked, setOptimisticChecked] = useState<boolean | null>(null);
  const [editing, setEditing] = useState(false);
  const [amount, setAmount] = useState('');
  const [unit, setUnit] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const checked = optimisticChecked ?? item.checked;

  const toggle = (next: boolean): void => {
    setOptimisticChecked(next);
    setError(null);
    updateShoppingListItem(item.id, { checked: next })
      .then((updated) => {
        onChanged(updated);
      })
      .catch((cause: unknown) => {
        setOptimisticChecked(null);
        setError(describeError(cause));
      })
      .finally(() => {
        setOptimisticChecked(null);
      });
  };

  const startEditing = (): void => {
    setAmount(item.quantity === null ? '' : formatAmount(item.quantity.amount));
    setUnit(item.quantity?.unit ?? '');
    setError(null);
    setEditing(true);
  };

  const saveQuantity = (): void => {
    const cleared = unit === '';
    const normalised = cleared ? null : normaliseAmount(amount);
    if (!cleared && normalised === null) {
      setError('Indiquez une quantité positive, par exemple 1,5.');
      return;
    }

    setBusy(true);
    setError(null);
    updateShoppingListItem(item.id, {
      quantity: cleared || normalised === null ? null : { amount: normalised, unit },
    })
      .then((updated) => {
        onChanged(updated);
        setEditing(false);
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const remove = (): void => {
    setBusy(true);
    setError(null);
    removeShoppingListItem(item.id)
      .then(onRemoved)
      .catch((cause: unknown) => {
        setError(describeError(cause));
        setBusy(false);
      });
  };

  return (
    <li className={[styles.item, checked ? styles.itemChecked : ''].filter(Boolean).join(' ')}>
      <div className={styles.itemMain}>
        <Checkbox
          checked={checked}
          onChange={toggle}
          detail={
            item.quantity === null
              ? undefined
              : `${formatAmount(item.quantity.amount)} ${unitSymbol(item.quantity.unit)}`
          }
        >
          <span className={checked ? styles.itemNameChecked : styles.itemName}>
            {itemLabel(item)}
          </span>
        </Checkbox>
      </div>

      {editing ? (
        <div className={styles.itemEdit}>
          <label className="visually-hidden" htmlFor={`${id}-amount`}>
            Quantité de {itemLabel(item)}
          </label>
          <input
            id={`${id}-amount`}
            className={[controlClass(), styles.reviewAmount].join(' ')}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            disabled={unit === ''}
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
            }}
          />
          <label className="visually-hidden" htmlFor={`${id}-unit`}>
            Unité de {itemLabel(item)}
          </label>
          <select
            id={`${id}-unit`}
            className={[controlClass(), styles.reviewUnit].join(' ')}
            value={unit}
            onChange={(event) => {
              setUnit(event.target.value);
              if (event.target.value === '') setAmount('');
            }}
          >
            <option value="">— sans quantité</option>
            {unitOptions([unit]).map((option) => (
              <option key={option.code} value={option.code}>
                {option.symbol}
              </option>
            ))}
          </select>
          <Button variant="primary" loading={busy} onClick={saveQuantity}>
            Enregistrer
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setEditing(false);
              setError(null);
            }}
          >
            Annuler
          </Button>
        </div>
      ) : (
        <div className={styles.itemActions}>
          <Button variant="ghost" onClick={startEditing}>
            Quantité
          </Button>
          <Button
            variant="ghost"
            disabled={busy}
            aria-label={`Retirer ${itemLabel(item)} de la liste`}
            onClick={remove}
          >
            Retirer
          </Button>
        </div>
      )}

      {error !== null ? (
        <p className={styles.itemError} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  );
}
