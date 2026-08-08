import type { HouseholdMember, MealTemperature } from '../../api/types';
import { Checkbox, ClassTag, Fieldset, Radio } from '../../components/ui';
import { AllergenDisclaimer, InfantDisclaimer } from '../../components/SafetyNotice';
import {
  AGE_BAND_LABELS,
  ALLERGEN_LABELS,
  DIET_LABELS,
  MEAL_TEMPERATURE_LABELS,
  TEXTURE_LABELS,
} from '../../lib/dietary';
import type { UnionConstraints } from '../../lib/dietary';
import styles from './Recipes.module.css';

interface Props {
  members: HouseholdMember[];
  /** Empty means the whole household, exactly as the contract reads it. */
  selectedIds: string[];
  effective: HouseholdMember[];
  constraints: UnionConstraints;
  mealTemperature: MealTemperature;
  onToggleMember: (id: string, checked: boolean) => void;
  onMealTemperature: (value: MealTemperature) => void;
}

/**
 * "Who are we cooking for?", and what that implies — shown *before* the call.
 *
 * The union of the selected members' constraints is displayed up front rather
 * than explained afterwards: a household that sees an empty result must already
 * know what emptied it.
 *
 * The two blocks below are deliberately not interchangeable. A filter removes
 * products from the inventory before the model is consulted; a preference is
 * only ever a sentence in a prompt, with nothing to verify it. Contract §4bis
 * makes that asymmetry a rule, and the interface has to carry it in its
 * vocabulary — the user must not file both under the same reliability.
 */
export function CookingForPanel({
  members,
  selectedIds,
  effective,
  constraints,
  mealTemperature,
  onToggleMember,
  onMealTemperature,
}: Props) {
  const everyone = selectedIds.length === 0;

  return (
    <div className={styles.panel}>
      <Fieldset
        legend="Pour qui cuisine-t-on ?"
        hint={
          everyone
            ? 'Aucune sélection : les contraintes de tout le foyer s’appliquent.'
            : 'L’union des contraintes des membres cochés s’applique.'
        }
      >
        {members.map((member) => (
          <Checkbox
            key={member.id}
            checked={selectedIds.includes(member.id)}
            detail={`${AGE_BAND_LABELS[member.age_band]} · ${DIET_LABELS[member.diet]}${
              member.allergens.length > 0
                ? ` · ${String(member.allergens.length)} allergène${member.allergens.length > 1 ? 's' : ''}`
                : ''
            }`}
            onChange={(checked) => {
              onToggleMember(member.id, checked);
            }}
          >
            {member.display_name}
          </Checkbox>
        ))}
      </Fieldset>

      <section className={styles.filterBlock} aria-labelledby="applied-filters">
        <p className={styles.blockHead}>
          <span id="applied-filters" className={styles.blockTitle}>
            Ce qui sera exclu
          </span>
          <ClassTag kind="filter" />
        </p>
        <p className={styles.blockLead}>
          Retiré de l’inventaire <strong>avant</strong> que le modèle ne soit consulté. Il ne peut
          pas proposer ce qu’il n’a pas vu.
        </p>
        <ul className={styles.blockList}>
          <li>
            <span className={styles.blockLabel}>Allergènes</span>
            {constraints.allergens.length === 0
              ? 'aucun allergène déclaré par les membres retenus'
              : constraints.allergens.map((code) => ALLERGEN_LABELS[code]).join(', ')}
          </li>
          <li>
            <span className={styles.blockLabel}>Régime</span>
            {constraints.diet === null ? 'aucun' : DIET_LABELS[constraints.diet]}
          </li>
          {constraints.infantTexture !== null ? (
            <li>
              <span className={styles.blockLabel}>Texture nourrisson</span>
              {TEXTURE_LABELS[constraints.infantTexture]}
            </li>
          ) : null}
          {constraints.hasInfant ? (
            <li>
              <span className={styles.blockLabel}>Tranches d’âge</span>
              {constraints.ageBands.map((band) => AGE_BAND_LABELS[band]).join(', ')}
            </li>
          ) : null}
        </ul>
        {effective.length === 0 ? (
          <p className={styles.blockLead}>Aucun membre enregistré : aucune contrainte dure.</p>
        ) : null}
      </section>

      <section className={styles.preferenceBlock} aria-labelledby="sent-preferences">
        <p className={styles.blockHead}>
          <span id="sent-preferences" className={styles.blockTitle}>
            Ce qui sera simplement demandé
          </span>
          <ClassTag kind="preference" />
        </p>
        <p className={styles.blockLead}>
          Transmis au modèle comme préférence. Rien ne permet de le vérifier ensuite : une recette
          annoncée froide peut arriver tiède, et un texte libre peut être ignoré.
        </p>

        <Fieldset legend="Température du repas">
          {(['any', 'hot', 'cold'] as MealTemperature[]).map((value) => (
            <Radio
              key={value}
              name="meal-temperature"
              value={value}
              checked={mealTemperature === value}
              onSelect={(next) => {
                onMealTemperature(next as MealTemperature);
              }}
            >
              {MEAL_TEMPERATURE_LABELS[value]}
            </Radio>
          ))}
        </Fieldset>

        {constraints.preferences.length > 0 ? (
          <ul className={styles.blockList}>
            {constraints.preferences.map((preference) => (
              <li key={preference.memberId}>
                <span className={styles.blockLabel}>{preference.memberName}</span>
                {preference.text}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <AllergenDisclaimer />
      {constraints.hasInfant ? <InfantDisclaimer /> : null}
    </div>
  );
}
