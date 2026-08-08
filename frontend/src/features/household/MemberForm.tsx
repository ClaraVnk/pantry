import { useMemo, useState, type FormEvent } from 'react';
import { describeError } from '../../api/client';
import { ALLERGEN_CODES } from '../../api/types';
import type {
  AgeBand,
  AllergenCode,
  Diet,
  HouseholdMember,
  InfantTexture,
  MemberDraft,
} from '../../api/types';
import { Button, Callout, Checkbox, ClassTag, Field, Fieldset } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { AllergenDisclaimer, InfantDisclaimer } from '../../components/SafetyNotice';
import {
  AGE_BANDS,
  AGE_BAND_LABELS,
  ALLERGEN_LABELS,
  DIETS,
  DIET_LABELS,
  TEXTURES,
  TEXTURE_LABELS,
  detectAllergenTerms,
  isInfantBand,
} from '../../lib/dietary';
import styles from './Household.module.css';

/** Bounds on free text. Everything typed here reaches a prompt; the backend
 * neutralises it, and a length limit keeps the payload sane besides. */
const NAME_MAX = 60;
const FREE_TEXT_MAX = 280;

interface Props {
  member: HouseholdMember | null;
  onSubmit: (draft: MemberDraft) => Promise<void>;
  onCancel: () => void;
}

interface FieldErrors {
  name?: string;
  texture?: string;
}

export function MemberForm({ member, onSubmit, onCancel }: Props) {
  const [name, setName] = useState(member?.display_name ?? '');
  const [ageBand, setAgeBand] = useState<AgeBand>(member?.age_band ?? 'adult');
  const [diet, setDiet] = useState<Diet>(member?.diet ?? 'omnivore');
  const [allergens, setAllergens] = useState<AllergenCode[]>(member?.allergens ?? []);
  const [texture, setTexture] = useState<InfantTexture>(member?.infant_texture ?? 'smooth');
  const [freeText, setFreeText] = useState(member?.free_text_restrictions ?? '');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const infant = isInfantBand(ageBand);

  // Regulated allergens typed into the preference field would silently do
  // nothing: the field never becomes a filter.
  const mistyped = useMemo(
    () => detectAllergenTerms(freeText).filter((code) => !allergens.includes(code)),
    [freeText, allergens],
  );

  const toggleAllergen = (code: AllergenCode, checked: boolean) => {
    setAllergens((current) =>
      checked
        ? ALLERGEN_CODES.filter((value) => value === code || current.includes(value))
        : current.filter((value) => value !== code),
    );
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);

    const nextErrors: FieldErrors = {};
    if (name.trim() === '') nextErrors.name = 'Le nom est obligatoire.';
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    const draft: MemberDraft = {
      display_name: name.trim().slice(0, NAME_MAX),
      age_band: ageBand,
      diet,
      allergens,
      free_text_restrictions: freeText.trim().slice(0, FREE_TEXT_MAX),
      // The contract 422s on an infant band without a texture, and on a texture
      // without an infant band. Enforced here so the server never has to.
      infant_texture: infant ? texture : null,
    };

    setSaving(true);
    onSubmit(draft)
      .catch((cause: unknown) => {
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <h2 className={styles.formHeading}>
        {member ? `Modifier ${member.display_name}` : 'Nouveau membre'}
      </h2>

      <Field label="Prénom ou surnom" required error={errors.name}>
        {({ id, describedBy, invalid }) => (
          <input
            id={id}
            aria-describedby={describedBy}
            aria-invalid={invalid}
            className={controlClass(invalid)}
            type="text"
            autoComplete="off"
            maxLength={NAME_MAX}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        )}
      </Field>

      <Field
        label="Tranche d’âge"
        hint="Jamais de date de naissance : la tranche suffit à appliquer les règles."
      >
        {({ id, describedBy }) => (
          <select
            id={id}
            aria-describedby={describedBy}
            className={controlClass()}
            value={ageBand}
            onChange={(event) => {
              setAgeBand(event.target.value as AgeBand);
            }}
          >
            {AGE_BANDS.map((band) => (
              <option key={band} value={band}>
                {AGE_BAND_LABELS[band]}
              </option>
            ))}
          </select>
        )}
      </Field>

      {infant ? (
        <>
          <InfantDisclaimer />
          <Field label="Texture" required error={errors.texture}>
            {({ id, describedBy }) => (
              <select
                id={id}
                aria-describedby={describedBy}
                className={controlClass()}
                value={texture}
                onChange={(event) => {
                  setTexture(event.target.value as InfantTexture);
                }}
              >
                {TEXTURES.map((value) => (
                  <option key={value} value={value}>
                    {TEXTURE_LABELS[value]}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </>
      ) : null}

      <Field label="Régime">
        {({ id, describedBy }) => (
          <select
            id={id}
            aria-describedby={describedBy}
            className={controlClass()}
            value={diet}
            onChange={(event) => {
              setDiet(event.target.value as Diet);
            }}
          >
            {DIETS.map((value) => (
              <option key={value} value={value}>
                {DIET_LABELS[value]}
              </option>
            ))}
          </select>
        )}
      </Field>

      <AllergenDisclaimer />

      <Fieldset
        legend="Allergènes à déclaration obligatoire"
        hint={
          <>
            <ClassTag kind="filter" /> Les produits portant un allergène coché sont retirés de
            l’inventaire <strong>avant</strong> que le modèle ne soit consulté. Les 14 allergènes du
            règlement UE 1169/2011.
          </>
        }
      >
        <div className={styles.allergenGrid}>
          {ALLERGEN_CODES.map((code) => (
            <Checkbox
              key={code}
              checked={allergens.includes(code)}
              onChange={(checked) => {
                toggleAllergen(code, checked);
              }}
            >
              {ALLERGEN_LABELS[code]}
            </Checkbox>
          ))}
        </div>
      </Fieldset>

      <Field
        label="Autres restrictions"
        hint="Par exemple : pas de coriandre, pas trop épicé, intolérance au lactose."
      >
        {({ id, describedBy }) => (
          <>
            <p className={styles.preferenceNote}>
              <ClassTag kind="preference" /> Ce texte est transmis au modèle comme préférence. Il
              n’est <strong>pas</strong> appliqué comme filtre : Chaudron ne sait pas quels produits
              contiennent de la coriandre. N’y mettez pas un allergène — cochez la case.
            </p>
            <textarea
              id={id}
              aria-describedby={describedBy}
              className={controlClass()}
              rows={3}
              maxLength={FREE_TEXT_MAX}
              value={freeText}
              onChange={(event) => {
                setFreeText(event.target.value);
              }}
            />
          </>
        )}
      </Field>

      <div aria-live="polite">
        {mistyped.length > 0 ? (
          <Callout tone="warn" title="Un allergène ne doit pas rester dans ce champ">
            <p>
              Ce texte mentionne{' '}
              {mistyped.length > 1 ? 'des allergènes réglementaires' : 'un allergène réglementaire'}
              . Écrit ici, il ne filtre rien. Cochez la case correspondante pour qu’il soit
              réellement appliqué.
            </p>
            <div className={styles.suggestRow}>
              {mistyped.map((code) => (
                <Button
                  key={code}
                  variant="secondary"
                  onClick={() => {
                    toggleAllergen(code, true);
                  }}
                >
                  Cocher « {ALLERGEN_LABELS[code]} »
                </Button>
              ))}
            </div>
          </Callout>
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
          {member ? 'Enregistrer' : 'Ajouter au foyer'}
        </Button>
      </div>
    </form>
  );
}
