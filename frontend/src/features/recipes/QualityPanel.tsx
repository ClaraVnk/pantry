import type { ModelQuality, SuggestionQuality } from '../../api/types';
import { ClassTag } from '../../components/ui';
import styles from './Recipes.module.css';

/**
 * What the feedback finally makes measurable, per provider and per model.
 *
 * The point of the panel is the comparison it enables: "does the small local
 * model produce recipes people actually cook?" is answerable here and nowhere
 * else in the product — token counts say what a call consumed, never whether it
 * was worth making.
 *
 * Two rules govern how a number is allowed to appear.
 *
 * A rate **never** appears without its effectif. "67 %" and "2 avis sur 3" are
 * the same fact, and only one of them lets a reader see that a single extra tap
 * would move it by seventeen points.
 *
 * Below `min_responses` answers no rate appears at all — the server sends
 * `cooked_rate: null` and this component does not divide. The threshold travels
 * on the response so the sentence can name it instead of guessing.
 */
function percent(rate: number): string {
  return `${String(Math.round(rate * 100))} %`;
}

function Row({ entry, minResponses }: { entry: ModelQuality; minResponses: number }) {
  // « avis » is invariable in French; only « recette cuisinée » takes the mark.
  const cooked = entry.cooked > 1 ? 'recettes cuisinées' : 'recette cuisinée';
  return (
    <li>
      <span className={styles.blockLabel}>
        {entry.model} — {entry.provider_mode}
      </span>
      {entry.cooked_rate === null ? (
        <span>
          {entry.cooked} {cooked} sur {entry.responses} avis — taux affiché à partir de{' '}
          {minResponses} avis.
        </span>
      ) : (
        <span>
          {percent(entry.cooked_rate)} de recettes cuisinées ({entry.cooked} sur {entry.responses}{' '}
          avis).
        </span>
      )}
    </li>
  );
}

export function QualityPanel({ quality }: { quality: SuggestionQuality }) {
  if (quality.models.length === 0) return null;

  return (
    <section className={styles.applied} aria-labelledby="quality-heading">
      <p className={styles.blockHead}>
        <span className={styles.blockTitle} id="quality-heading">
          Ce que vos avis mesurent
        </span>
        {/* The same taxonomy the rest of the screen uses: this is arithmetic the
            application does over its own rows, not something a model produced. */}
        <ClassTag kind="computed" />
      </p>
      <p className={styles.blockLead}>
        Par fournisseur et par modèle, sur les propositions que vous avez notées. Un avis ne
        supprime jamais une recette : il en change seulement le rang.
      </p>
      <ul className={styles.blockList}>
        {quality.models.map((entry) => (
          <Row
            key={`${entry.provider_mode}-${entry.model}`}
            entry={entry}
            minResponses={quality.min_responses}
          />
        ))}
      </ul>
    </section>
  );
}
