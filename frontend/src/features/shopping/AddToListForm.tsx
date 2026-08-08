import { useId, useState } from 'react';
import { describeError } from '../../api/client';
import { addShoppingListItems } from '../../api/endpoints';
import type { ShoppingList } from '../../api/types';
import { Button } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { normaliseAmount } from '../../lib/quantity';
import { unitOptions } from '../../lib/units';
import styles from './Shopping.module.css';

interface Props {
  onAdded: (list: ShoppingList) => void;
}

/**
 * Adding an article by hand.
 *
 * Free text, deliberately: "quelque chose pour le dessert" is a real line of a
 * real shopping list, and forcing every entry through the product catalogue
 * would make the list unusable for exactly the things nobody has scanned yet.
 *
 * `source: 'manual'` is sent on every item. The field exists to answer "does
 * the repurchase proposal actually get used?" with a measurement rather than an
 * assumption (§6bis), which only works if the ordinary case reports itself too.
 */
export function AddToListForm({ onAdded }: Props) {
  const id = useId();
  const [label, setLabel] = useState('');
  const [amount, setAmount] = useState('');
  const [unit, setUnit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = (event: React.FormEvent): void => {
    event.preventDefault();
    const free_text = label.trim();
    if (free_text === '') {
      setError('Indiquez ce que vous voulez acheter.');
      return;
    }

    const normalised = unit === '' ? null : normaliseAmount(amount);
    if (unit !== '' && normalised === null) {
      setError('Indiquez une quantité positive, par exemple 1,5, ou choisissez « sans quantité ».');
      return;
    }

    setBusy(true);
    setError(null);
    addShoppingListItems([
      {
        free_text,
        ...(normalised === null ? {} : { amount: normalised, unit }),
        source: 'manual',
      },
    ])
      .then((list) => {
        onAdded(list);
        setLabel('');
        setAmount('');
        setUnit('');
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <form className={styles.addForm} onSubmit={submit}>
      <label className={styles.fieldLabel} htmlFor={`${id}-label`}>
        Ajouter un article
      </label>
      <input
        id={`${id}-label`}
        className={controlClass(error !== null && label.trim() === '')}
        type="text"
        autoComplete="off"
        enterKeyHint="done"
        maxLength={200}
        placeholder="Pain, lait, quelque chose pour le dessert…"
        value={label}
        onChange={(event) => {
          setLabel(event.target.value);
          setError(null);
        }}
      />

      <div className={styles.addRow}>
        <label className="visually-hidden" htmlFor={`${id}-amount`}>
          Quantité
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
            setError(null);
          }}
        />
        <label className="visually-hidden" htmlFor={`${id}-unit`}>
          Unité
        </label>
        <select
          id={`${id}-unit`}
          className={[controlClass(), styles.reviewUnit].join(' ')}
          value={unit}
          onChange={(event) => {
            setUnit(event.target.value);
            if (event.target.value === '') setAmount('');
            setError(null);
          }}
        >
          <option value="">— sans quantité</option>
          {unitOptions([unit]).map((option) => (
            <option key={option.code} value={option.code}>
              {option.symbol}
            </option>
          ))}
        </select>
        <Button variant="primary" type="submit" loading={busy}>
          Ajouter
        </Button>
      </div>

      {error !== null ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}
