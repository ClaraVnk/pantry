import { useState } from 'react';
import { describeError } from '../../api/client';
import { clearBudgetTarget, setBudgetTarget } from '../../api/endpoints';
import type { BudgetPeriod, BudgetTarget } from '../../api/types';
import { Button } from '../../components/ui';
import { normaliseMoney } from './money';
import styles from './Budget.module.css';

interface Props {
  period: BudgetPeriod;
  current: BudgetTarget | null;
  onChanged: () => void;
  onCancel: () => void;
}

const CURRENCY_RE = /^[A-Za-z]{3}$/;

/**
 * Sets, replaces or removes the optional target.
 *
 * A target does one thing: it puts a second number next to the first. It raises
 * no alert, blocks nothing, and is never required — which is why this form is
 * behind a button rather than on the screen by default.
 */
export function TargetForm({ period, current, onChanged, onCancel }: Props) {
  const [amount, setAmount] = useState(current?.amount ?? '');
  const [currency, setCurrency] = useState(current?.currency ?? 'EUR');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = (): void => {
    const normalised = normaliseMoney(amount);
    if (normalised === null) {
      setError('Indiquez un montant positif, avec au plus deux décimales.');
      return;
    }
    if (!CURRENCY_RE.test(currency.trim())) {
      setError('La devise est un code ISO de trois lettres, par exemple EUR ou CHF.');
      return;
    }

    setBusy(true);
    setError(null);
    setBudgetTarget({ period, amount: normalised, currency: currency.trim().toUpperCase() })
      .then(() => {
        onChanged();
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
    clearBudgetTarget(period)
      .then(() => {
        onChanged();
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <div className={styles.form}>
      <div className={styles.formRow}>
        <div className={styles.formField}>
          <label className={styles.formLabel} htmlFor="budget-target-amount">
            Objectif
          </label>
          <input
            id="budget-target-amount"
            className={styles.input}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
            }}
          />
        </div>
        <div className={styles.formField}>
          <label className={styles.formLabel} htmlFor="budget-target-currency">
            Devise
          </label>
          <input
            id="budget-target-currency"
            className={styles.input}
            type="text"
            maxLength={3}
            autoComplete="off"
            value={currency}
            onChange={(event) => {
              setCurrency(event.target.value);
            }}
          />
        </div>
      </div>

      {error ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button variant="primary" loading={busy} onClick={submit}>
          Enregistrer l’objectif
        </Button>
        {current ? (
          <Button variant="ghost" disabled={busy} onClick={remove}>
            Ne plus suivre d’objectif
          </Button>
        ) : null}
        <Button variant="ghost" disabled={busy} onClick={onCancel}>
          Annuler
        </Button>
      </div>
    </div>
  );
}
