import { useState, type FormEvent } from 'react';
import { describeError } from '../../api/client';
import { createInventoryItem } from '../../api/endpoints';
import type {
  CreateInventoryItem,
  ExpiryKind,
  ItemSource,
  Product,
  StorageLocation,
} from '../../api/types';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { hasValidGtinChecksum, isPlausibleGtin, normaliseGtin } from '../../lib/gtin';
import { normaliseAmount } from '../../lib/quantity';
import { unitOptions } from '../../lib/units';
import styles from './Add.module.css';

/**
 * The unit vocabulary, from the one module that holds it (`lib/units.ts`).
 *
 * This form used to carry its own list — `pièce`, `portion`, `boîte`, `sachet`,
 * `bocal`, `mL`, `L` — and not one of those is a code in the `unit` reference
 * table seeded by migration `0002`. Every one came back `422 unknown-unit`, and
 * `pièce` was the field's *default value*: "saisir un article à la main" failed
 * on submit for anybody who did not happen to type `g` or `kg`.
 */
const UNITS = unitOptions([]);

export interface ItemDraft {
  /** Set when the GTIN resolved to a known product; we then send product_id. */
  product: Product | null;
  gtin: string | null;
  source: ItemSource;
  /** Why we landed on this form — shown above it, never as an error. */
  notice: { tone: 'info' | 'warn'; title: string; body: string } | null;
}

interface Props {
  draft: ItemDraft;
  locations: StorageLocation[];
  onSaved: (label: string) => void;
  onCancel: () => void;
}

interface FieldErrors {
  name?: string;
  amount?: string;
  unit?: string;
  location?: string;
  gtin?: string;
}

export function ManualItemForm({ draft, locations, onSaved, onCancel }: Props) {
  const [name, setName] = useState(draft.product?.name ?? '');
  const [brand, setBrand] = useState(draft.product?.brand ?? '');
  const [gtin, setGtin] = useState(draft.gtin ?? draft.product?.gtin ?? '');
  const [locationId, setLocationId] = useState(locations[0]?.id ?? '');
  const [amount, setAmount] = useState('1');
  const [unit, setUnit] = useState('piece');
  const [expiresOn, setExpiresOn] = useState('');
  const [expiryKind, setExpiryKind] = useState<ExpiryKind>('use_by');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const knownProduct = draft.product;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);

    const nextErrors: FieldErrors = {};
    const cleanAmount = normaliseAmount(amount);
    const cleanGtin = normaliseGtin(gtin);

    if (!knownProduct && name.trim() === '') nextErrors.name = 'Le nom est obligatoire.';
    if (cleanAmount === null) nextErrors.amount = 'Indiquez une quantité positive, par ex. 1,5.';
    if (unit.trim() === '') nextErrors.unit = "L'unité est obligatoire.";
    if (locationId === '') nextErrors.location = 'Choisissez un emplacement.';
    if (!knownProduct && cleanGtin !== '') {
      if (!isPlausibleGtin(cleanGtin)) {
        nextErrors.gtin = 'Un code-barres compte 8, 12, 13 ou 14 chiffres.';
      } else if (!hasValidGtinChecksum(cleanGtin)) {
        nextErrors.gtin = 'Clé de contrôle invalide — vérifiez les chiffres.';
      }
    }

    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0 || cleanAmount === null) return;

    const common = {
      location_id: locationId,
      amount: cleanAmount,
      unit: unit.trim(),
      expires_on: expiresOn === '' ? null : expiresOn,
      expiry_kind: expiresOn === '' ? null : expiryKind,
      source: draft.source,
    };

    // The contract accepts product_id OR an inline product draft, never both.
    const body: CreateInventoryItem = knownProduct
      ? { ...common, product_id: knownProduct.id }
      : {
          ...common,
          product: {
            name: name.trim(),
            brand: brand.trim() === '' ? null : brand.trim(),
            gtin: cleanGtin === '' ? null : cleanGtin,
            default_unit: unit.trim(),
          },
        };

    setSaving(true);
    createInventoryItem(body)
      .then((item) => {
        onSaved(item.product.name);
      })
      .catch((cause: unknown) => {
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  return (
    <form className={styles.screen} onSubmit={submit} noValidate>
      <h1 className={styles.heading}>Ajouter un article</h1>

      {draft.notice ? (
        <Callout tone={draft.notice.tone} title={draft.notice.title}>
          <p>{draft.notice.body}</p>
        </Callout>
      ) : null}

      <div className={styles.form}>
        {knownProduct ? (
          <div className={styles.productCard}>
            {knownProduct.image_url ? (
              <img className={styles.productImage} src={knownProduct.image_url} alt="" />
            ) : null}
            <div className={styles.productText}>
              <span className={styles.productName}>{knownProduct.name}</span>
              <span className={styles.productMeta}>
                {[knownProduct.brand, knownProduct.gtin].filter(Boolean).join(' · ') ||
                  'Produit reconnu'}
              </span>
            </div>
          </div>
        ) : (
          <>
            <Field label="Nom du produit" required error={errors.name}>
              {({ id, describedBy, invalid }) => (
                <input
                  id={id}
                  aria-describedby={describedBy}
                  aria-invalid={invalid}
                  className={controlClass(invalid)}
                  type="text"
                  autoComplete="off"
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Marque" hint="Facultatif.">
              {({ id, describedBy }) => (
                <input
                  id={id}
                  aria-describedby={describedBy}
                  className={controlClass()}
                  type="text"
                  autoComplete="off"
                  value={brand}
                  onChange={(event) => {
                    setBrand(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Code-barres" hint="Facultatif." error={errors.gtin}>
              {({ id, describedBy, invalid }) => (
                <input
                  id={id}
                  aria-describedby={describedBy}
                  aria-invalid={invalid}
                  className={controlClass(invalid)}
                  type="text"
                  inputMode="numeric"
                  autoComplete="off"
                  value={gtin}
                  onChange={(event) => {
                    setGtin(event.target.value);
                  }}
                />
              )}
            </Field>
          </>
        )}

        <Field label="Emplacement" required error={errors.location}>
          {({ id, describedBy, invalid }) => (
            <select
              id={id}
              aria-describedby={describedBy}
              aria-invalid={invalid}
              className={controlClass(invalid)}
              value={locationId}
              onChange={(event) => {
                setLocationId(event.target.value);
              }}
            >
              <option value="">Choisir…</option>
              {locations.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name}
                </option>
              ))}
            </select>
          )}
        </Field>

        <div className={styles.row}>
          <Field label="Quantité" required error={errors.amount}>
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                aria-describedby={describedBy}
                aria-invalid={invalid}
                className={controlClass(invalid)}
                type="text"
                inputMode="decimal"
                autoComplete="off"
                value={amount}
                onChange={(event) => {
                  setAmount(event.target.value);
                }}
              />
            )}
          </Field>

          {/*
            A `<select>` over the closed set, not a text field with a datalist.
            A datalist *suggests*; it accepts anything typed past it, and what
            the server accepts is a code from one table. The shopping screens
            reached the same conclusion first (`lib/units.ts`); this field now
            behaves like them.
          */}
          <Field label="Unité" required error={errors.unit}>
            {({ id, describedBy, invalid }) => (
              <select
                id={id}
                aria-describedby={describedBy}
                aria-invalid={invalid}
                className={controlClass(invalid)}
                value={unit}
                onChange={(event) => {
                  setUnit(event.target.value);
                }}
              >
                {UNITS.map((entry) => (
                  <option key={entry.code} value={entry.code}>
                    {entry.symbol}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>

        <Field label="Date de péremption" hint="Facultatif.">
          {({ id, describedBy }) => (
            <input
              id={id}
              aria-describedby={describedBy}
              className={controlClass()}
              type="date"
              value={expiresOn}
              onChange={(event) => {
                setExpiresOn(event.target.value);
              }}
            />
          )}
        </Field>

        {expiresOn !== '' ? (
          <Field label="Type de date">
            {({ id, describedBy }) => (
              <select
                id={id}
                aria-describedby={describedBy}
                className={controlClass()}
                value={expiryKind}
                onChange={(event) => {
                  setExpiryKind(event.target.value as ExpiryKind);
                }}
              >
                <option value="use_by">DLC — à consommer jusqu’au</option>
                <option value="best_before">DDM — à consommer de préférence avant</option>
              </select>
            )}
          </Field>
        ) : null}
      </div>

      <div aria-live="assertive">
        {submitError ? (
          <Callout tone="danger" title="Enregistrement impossible">
            <p>{submitError}</p>
          </Callout>
        ) : null}
      </div>

      <div className={styles.formActions}>
        <Button variant="ghost" onClick={onCancel}>
          Annuler
        </Button>
        <Button type="submit" variant="primary" loading={saving}>
          Ajouter au stock
        </Button>
      </div>
    </form>
  );
}
