import type {
  AllergenAssessment,
  ExpiryPressure,
  MealTemperature,
  RecipePreparation,
} from '../../api/types';
import { Badge, ClassTag } from '../../components/ui';
import { SERVING_TEMPERATURE_LABELS } from '../../lib/dietary';
import styles from './Recipes.module.css';

/**
 * The allergen block of a suggestion.
 *
 * `statement` is written by the server and displayed verbatim. The interface
 * never composes its own sentence out of `declared_clear_of`: a household must
 * never read "without nuts" when the truth is "none of the identified products
 * declared nuts" (ADR-0009). That is why `declared_clear_of` is not rendered as
 * a list of reassuring chips — it would say exactly the forbidden thing.
 *
 * Two visual states, and they are told apart by shape as much as by colour:
 *
 * - complete data — solid border, filled background, a check mark;
 * - incomplete data — dashed border, hatched background, a question mark, and
 *   the words "non vérifié". Never green, never a check. A product with no
 *   allergen data is not a product without allergens.
 */
export function AllergenAssessmentPanel({
  assessment,
}: {
  assessment: AllergenAssessment | undefined;
}) {
  if (!assessment) {
    return (
      <div className={[styles.assessment, styles.assessmentUnknown].join(' ')}>
        <p className={styles.assessmentHead}>
          <span className={styles.assessmentGlyph} aria-hidden="true">
            ?
          </span>
          Allergènes — non vérifié
        </p>
        <p>
          Ce serveur n’a fourni aucune évaluation allergène pour cette suggestion. Traitez-la comme
          non vérifiée.
        </p>
      </div>
    );
  }

  const incomplete = assessment.unverified_product_count > 0;
  const count = assessment.unverified_product_count;

  return (
    <div
      className={[
        styles.assessment,
        incomplete ? styles.assessmentUnknown : styles.assessmentComplete,
      ].join(' ')}
    >
      <p className={styles.assessmentHead}>
        <span className={styles.assessmentGlyph} aria-hidden="true">
          {incomplete ? '?' : '✓'}
        </span>
        {incomplete
          ? `Allergènes — non vérifié (${String(count)} produit${count > 1 ? 's' : ''})`
          : 'Allergènes — tous les produits ont des données'}
      </p>
      {/* Server-authored wording, displayed as-is. Do not reformat, do not extend. */}
      <p className={styles.assessmentStatement}>{assessment.statement}</p>
      {incomplete ? (
        <p className={styles.assessmentDetail}>
          {count > 1
            ? `${String(count)} produits n’ont aucune donnée allergène : ils n’ont pas pu être contrôlés.`
            : 'Un produit n’a aucune donnée allergène : il n’a pas pu être contrôlé.'}
        </p>
      ) : null}
    </div>
  );
}

/**
 * What the model says about its own recipe, plus the divergence from what was
 * asked. Self-declared, so it is framed as a claim rather than a fact — and a
 * divergence never hides the suggestion, it just shows up next to it.
 */
export function PreparationPanel({
  preparation,
  requested,
}: {
  preparation: RecipePreparation | undefined;
  requested: MealTemperature;
}) {
  if (!preparation) return null;

  const diverges =
    requested !== 'any' &&
    preparation.serving_temperature !== 'either' &&
    preparation.serving_temperature !== requested;

  return (
    <div className={styles.declared}>
      <p className={styles.blockHead}>
        <span className={styles.blockTitle}>Déclaré par le modèle</span>
        <ClassTag kind="preference" />
      </p>
      <div className={styles.facts}>
        <Badge tone="neutral">{SERVING_TEMPERATURE_LABELS[preparation.serving_temperature]}</Badge>
        {preparation.requires_cooking ? <Badge tone="neutral">cuisson</Badge> : null}
        {preparation.requires_oven ? <Badge tone="warn">four nécessaire</Badge> : null}
      </div>
      <p className={styles.blockLead}>
        Ces indications viennent du modèle et ne sont pas vérifiées.
        {preparation.requires_oven
          ? ' Cette recette demande d’allumer le four.'
          : ' Cette recette ne demande pas de four.'}
      </p>
      {diverges ? (
        <p className={styles.divergence}>
          Vous avez demandé {requested === 'cold' ? 'plutôt froid' : 'plutôt chaud'} ; cette
          suggestion se sert {preparation.serving_temperature === 'cold' ? 'froide' : 'chaude'}.
          Elle reste affichée : à vous de trancher.
        </p>
      ) : null}
    </div>
  );
}

/** What the suggestion saved from the bin — and, honestly, what it did not. */
export function ExpiryPressurePanel({ pressure }: { pressure: ExpiryPressure | undefined }) {
  if (!pressure) return null;

  return (
    <div className={styles.pressure}>
      <p className={styles.blockHead}>
        <span className={styles.blockTitle}>Produits urgents</span>
        <ClassTag kind="computed" />
      </p>
      {pressure.urgent_items.length > 0 ? (
        <ul className={styles.blockList}>
          {pressure.urgent_items.map((item) => (
            <li key={item.inventory_item_id}>
              <span className={styles.blockLabel}>{item.product_name}</span>
              {item.days_left <= 0
                ? 'à consommer aujourd’hui'
                : `encore ${String(item.days_left)} jour${item.days_left > 1 ? 's' : ''}`}
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.blockLead}>Cette recette n’utilise aucun produit urgent.</p>
      )}
      {pressure.urgent_items_left_unused > 0 ? (
        <p className={styles.leftUnused}>
          {pressure.urgent_items_left_unused > 1
            ? `${String(pressure.urgent_items_left_unused)} produits urgents ne sont pas utilisés par cette recette.`
            : 'Un produit urgent n’est pas utilisé par cette recette.'}
        </p>
      ) : null}
    </div>
  );
}
