import type { BalanceExcess, WeeklyBalance } from '../../api/types';
import { uncategorisedProductCount } from '../../api/types';
import { ClassTag } from '../../components/ui';
import styles from './Recipes.module.css';

/**
 * Weekly balance against the PNNS reference marks.
 *
 * Gaps are spelled out in words on purpose: "you are missing a fish this week"
 * can be contested by the person reading it, an opaque score cannot (ADR-0009).
 *
 * The count of products that resolve to no marker is displayed next to the
 * figures — and its absence is displayed too. A miscategorised inventory would
 * otherwise produce an indisputable and false verdict.
 */
/**
 * A ceiling in its own unit.
 *
 * Two of the PNNS ceilings are masses and one is a count of glasses. Printing
 * grams for the third would be a fabricated unit next to a real number, which is
 * the same defect as a fabricated number — so the serving case says "portions"
 * and only the gram case says "g".
 */
function formatExcess(excess: BalanceExcess): string {
  if (excess.unit === 'serving' && typeof excess.observed === 'number') {
    return `${String(excess.observed)} portion${excess.observed > 1 ? 's' : ''} consommée${excess.observed > 1 ? 's' : ''}`;
  }
  return `${String(excess.observed_grams)} g consommés`;
}

export function BalancePanel({ balance }: { balance: WeeklyBalance }) {
  const uncategorised = uncategorisedProductCount(balance);
  const nothing = balance.gaps.length === 0 && balance.excesses.length === 0;

  return (
    <section className={styles.balance} aria-labelledby="balance-heading">
      <p className={styles.blockHead}>
        <span className={styles.blockTitle} id="balance-heading">
          Équilibre sur {balance.window_days} jours
        </span>
        <ClassTag kind="computed" />
      </p>

      {nothing ? (
        <p className={styles.blockLead}>Aucun écart aux repères sur cette période.</p>
      ) : (
        <ul className={styles.blockList}>
          {balance.gaps.map((gap) => (
            <li key={`gap-${gap.marker}`}>
              <span className={styles.blockLabel}>{gap.label}</span>
              {gap.shortfall > 1
                ? `il en manque ${String(gap.shortfall)} cette semaine`
                : 'il en manque un cette semaine'}{' '}
              <span className={styles.balanceDetail}>
                (repère : {gap.target} — observé : {gap.observed})
              </span>
            </li>
          ))}
          {balance.excesses.map((excess) => (
            <li key={`excess-${excess.marker}`}>
              <span className={styles.blockLabel}>{excess.label}</span>
              {formatExcess(excess)}{' '}
              <span className={styles.balanceDetail}>(repère : {excess.target})</span>
            </li>
          ))}
        </ul>
      )}

      {!balance.satisfiable_from_stock && balance.note !== null ? (
        <p className={styles.blockLead}>{balance.note}</p>
      ) : null}

      <p className={styles.balanceFootnote}>
        {uncategorised === null
          ? 'Le serveur n’indique pas combien de produits n’ont pas pu être rattachés à un repère ; ces chiffres peuvent en ignorer.'
          : uncategorised === 0
            ? `Tous les produits consommés ont été rattachés à un repère. Référence ${balance.reference}.`
            : `${String(uncategorised)} produit${uncategorised > 1 ? 's' : ''} n’${uncategorised > 1 ? 'ont' : 'a'} pas pu être rattaché${uncategorised > 1 ? 's' : ''} à un repère et ne compte${uncategorised > 1 ? 'nt' : ''} nulle part. Référence ${balance.reference}.`}
      </p>
    </section>
  );
}
