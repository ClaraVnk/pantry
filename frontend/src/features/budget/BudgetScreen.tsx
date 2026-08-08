import { useState } from 'react';
import type { Budget, BudgetCoverage, BudgetCurrency, BudgetPeriod } from '../../api/types';
import { Button, Chip, ChipRow, EmptyState, ErrorState, LoadingRow } from '../../components/ui';
import {
  formatCentimes,
  formatDay,
  formatMoney,
  formatPeriodLabel,
  remainingCentimes,
} from './money';
import { readBudgetOptIn, writeBudgetOptIn } from './optIn';
import { TargetForm } from './TargetForm';
import { useBudget } from './useBudget';
import styles from './Budget.module.css';

const PERIOD_LABELS: Record<BudgetPeriod, string> = {
  month: 'Ce mois-ci',
  week: 'Cette semaine',
};

function plural(count: number, one: string, many: string): string {
  return count > 1 ? many : one;
}

/**
 * What the amount above does not count, spelled out and never collapsed.
 *
 * The zero case is stated rather than omitted: "every receipt of the period
 * carries a total" and a missing line say the same thing to someone skimming,
 * and only one of the two is a claim.
 */
function CoverageNote({ coverage }: { coverage: BudgetCoverage }) {
  const items: string[] = [];

  if (coverage.receipts_missing_total > 0) {
    const n = coverage.receipts_missing_total;
    items.push(
      `${String(n)} ${plural(n, 'ticket', 'tickets')} sans total lisible : ${plural(
        n,
        'son montant n’est pas compté',
        'leurs montants ne sont pas comptés',
      )}.`,
    );
  }

  if (coverage.stock_items_added_without_receipt > 0) {
    const n = coverage.stock_items_added_without_receipt;
    items.push(
      `${String(n)} ${plural(n, 'article ajouté', 'articles ajoutés')} sans ticket : ${plural(
        n,
        'il n’a pas de prix et n’est compté nulle part',
        'ils n’ont pas de prix et ne sont comptés nulle part',
      )}.`,
    );
  }

  if (items.length === 0) {
    items.push(
      `${String(coverage.receipts_with_total)} ${plural(
        coverage.receipts_with_total,
        'ticket porte un total',
        'tickets portent un total',
      )}, et rien d’autre n’est entré en stock sur la période.`,
    );
  }

  return (
    <ul className={styles.coverage}>
      {items.map((text) => (
        <li className={styles.coverageItem} key={text}>
          <span className={styles.coverageMarker} aria-hidden="true">
            •
          </span>
          <span>{text}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * One currency. Passing the target is written as a difference and nothing else:
 * no colour change, no icon, no verb in the imperative.
 */
function CurrencyCard({ entry, coverage }: { entry: BudgetCurrency; coverage: BudgetCoverage }) {
  const remaining = entry.target === null ? null : remainingCentimes(entry.spent, entry.target);

  return (
    <section className={styles.card} aria-label={`Dépense en ${entry.currency}`}>
      <div className={styles.amountRow}>
        <p className={styles.amount}>{formatMoney(entry.spent, entry.currency)}</p>
        <p className={styles.receiptCount}>
          {entry.receipt_count === 0
            ? 'aucun ticket'
            : `${String(entry.receipt_count)} ${plural(entry.receipt_count, 'ticket', 'tickets')}`}
        </p>
      </div>

      <CoverageNote coverage={coverage} />

      {entry.line_sum_mismatch_count > 0 ? (
        <p className={styles.receiptCount}>
          {`${String(entry.line_sum_mismatch_count)} ${plural(
            entry.line_sum_mismatch_count,
            'ticket dont le détail des lignes ne correspond pas au total imprimé',
            'tickets dont le détail des lignes ne correspond pas au total imprimé',
          )}. Le total imprimé fait foi ; l’écart est signalé, jamais corrigé.`}
        </p>
      ) : null}

      {entry.target !== null ? (
        <p className={styles.target}>
          <span className={styles.targetLabel}>Objectif</span>
          <span>{formatMoney(entry.target, entry.currency)}</span>
          {remaining === null ? null : (
            <span className={styles.targetGap}>
              {remaining >= 0n
                ? `— il reste ${formatCentimes(remaining, entry.currency)}`
                : `— ${formatCentimes(-remaining, entry.currency)} au-dessus`}
            </span>
          )}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Complete periods, oldest first.
 *
 * A period with no receipt is listed and says so, rather than being dropped:
 * removing it would close the gap and turn "we know nothing about July" into a
 * sequence that looks continuous. It is not shown as `0,00 €` either, for the
 * same reason the current period is not.
 *
 * Past periods carry no target — the server does not keep a history of one, so
 * there is nothing here to compare against.
 */
function History({ period, periods }: { period: BudgetPeriod; periods: Budget[] }) {
  if (periods.length === 0) return null;

  return (
    <section className={styles.card} aria-labelledby="budget-history-heading">
      <h3 className={styles.sectionTitle} id="budget-history-heading">
        Périodes précédentes
      </h3>
      <ul className={styles.historyList}>
        {periods.map((span) => (
          <li className={styles.historyRow} key={span.period_start}>
            <span className={styles.historyLabel}>
              {formatPeriodLabel(period, span.period_start)}
            </span>
            <span className={styles.historyAmount}>
              {span.currencies.length === 0
                ? 'aucun ticket'
                : span.currencies
                    .map((entry) => formatMoney(entry.spent, entry.currency))
                    .join(' · ')}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The proposal. Nothing has been requested from the server at this point, and
 * nothing will be until the button below is pressed.
 *
 * Contract §6ter: the application *proposes* to track spending. A household may
 * legitimately want to manage its stock without having its money counted, and
 * an expenditure history says where a family shops, how often, on what income
 * and when it is away — which `docs/security-model.md` classifies alongside the
 * inventory as an A3 asset.
 */
function Proposal({ onAccept }: { onAccept: () => void }) {
  return (
    <section className={styles.card} aria-labelledby="budget-proposal-heading">
      <h3 className={styles.sectionTitle} id="budget-proposal-heading">
        Suivre ce que coûtent les courses ?
      </h3>
      <p className={styles.lead}>
        Chaudron peut additionner le total imprimé sur les tickets que vous importez, mois par mois
        ou semaine par semaine. C’est facultatif, et rien n’est calculé tant que vous ne l’avez pas
        demandé.
      </p>
      <p className={styles.lead}>
        Le calcul ne connaît que les tickets : un article saisi à la main ou scanné n’a aucun prix,
        et l’écran le dit à côté du montant plutôt que de faire comme s’il valait zéro.
      </p>
      <div className={styles.actions}>
        <Button variant="primary" onClick={onAccept}>
          Calculer ma dépense
        </Button>
      </div>
    </section>
  );
}

/**
 * Grocery spending (contract §6ter).
 *
 * The figure comes from `receipt.total_amount` — the number printed large on
 * the ticket, which a human checks at a glance — and never from the sum of the
 * parsed lines, which vision models are documented to fabricate in order to
 * make the arithmetic land on that very total
 * (`docs/technical-notes-ingestion.md` §3.4).
 */
export function BudgetScreen() {
  const [enabled, setEnabled] = useState(readBudgetOptIn);
  const [period, setPeriod] = useState<BudgetPeriod>('month');
  const [editingTarget, setEditingTarget] = useState(false);
  const state = useBudget(period, enabled);

  const accept = (): void => {
    writeBudgetOptIn(true);
    setEnabled(true);
  };

  const stop = (): void => {
    writeBudgetOptIn(false);
    setEnabled(false);
    setEditingTarget(false);
  };

  const budget = state.budget;
  const currentTarget = budget?.currencies.find((entry) => entry.target !== null) ?? null;

  return (
    <section className={styles.screen} aria-labelledby="budget-heading">
      <h2 className={styles.heading} id="budget-heading">
        Budget des courses
      </h2>

      {!enabled ? <Proposal onAccept={accept} /> : null}

      {enabled ? (
        <>
          <ChipRow label="Période">
            {(['month', 'week'] as const).map((value) => (
              <Chip
                key={value}
                active={period === value}
                onClick={() => {
                  setPeriod(value);
                }}
              >
                {PERIOD_LABELS[value]}
              </Chip>
            ))}
          </ChipRow>

          {state.loading ? <LoadingRow label="Calcul de la dépense…" /> : null}

          {state.unsupported ? (
            <EmptyState
              title="Ce serveur ne calcule pas encore le budget"
              body="L’application est à jour, l’API ne l’est pas encore. Le suivi apparaîtra sans rien changer ici."
            />
          ) : null}

          {state.error !== null ? (
            <ErrorState message={state.error} onRetry={state.reload} />
          ) : null}

          {budget !== null ? (
            <>
              <p className={styles.periodRow}>
                <span className={styles.periodDates}>
                  Du {formatDay(budget.period_start)} au {formatDay(budget.period_end)}
                </span>
              </p>

              {budget.coverage.receipts_with_total === 0 &&
              budget.coverage.receipts_missing_total === 0 ? (
                <EmptyState
                  title="Aucun ticket sur cette période"
                  body={
                    budget.coverage.stock_items_added_without_receipt > 0
                      ? `Importez un ticket de caisse pour voir ce que vous avez dépensé. ${String(
                          budget.coverage.stock_items_added_without_receipt,
                        )} ${plural(
                          budget.coverage.stock_items_added_without_receipt,
                          'article est entré',
                          'articles sont entrés',
                        )} en stock sans ticket : Chaudron n’en connaît pas le prix.`
                      : 'Importez un ticket de caisse pour voir ce que vous avez dépensé. Sans ticket, Chaudron n’a aucun montant à additionner.'
                  }
                />
              ) : budget.currencies.length === 0 ? (
                <section className={styles.card}>
                  <p className={styles.lead}>
                    Aucun montant lisible sur cette période : les tickets importés ne portent pas de
                    total exploitable.
                  </p>
                  <CoverageNote coverage={budget.coverage} />
                </section>
              ) : (
                budget.currencies.map((entry) => (
                  <CurrencyCard key={entry.currency} entry={entry} coverage={budget.coverage} />
                ))
              )}

              <History period={period} periods={state.history} />

              <section className={styles.card} aria-labelledby="budget-settings-heading">
                <h3 className={styles.sectionTitle} id="budget-settings-heading">
                  Objectif et suivi
                </h3>
                {editingTarget ? (
                  <TargetForm
                    period={period}
                    current={
                      currentTarget === null || currentTarget.target === null
                        ? null
                        : {
                            period,
                            amount: currentTarget.target,
                            currency: currentTarget.currency,
                          }
                    }
                    onChanged={() => {
                      setEditingTarget(false);
                      state.reload();
                    }}
                    onCancel={() => {
                      setEditingTarget(false);
                    }}
                  />
                ) : (
                  <>
                    <p className={styles.lead}>
                      Un objectif affiche un second nombre à côté du premier. Il ne déclenche aucune
                      alerte et n’empêche rien.
                    </p>
                    <div className={styles.actions}>
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setEditingTarget(true);
                        }}
                      >
                        {currentTarget === null ? 'Définir un objectif' : 'Modifier l’objectif'}
                      </Button>
                      <Button variant="ghost" onClick={stop}>
                        Arrêter le suivi
                      </Button>
                    </div>
                  </>
                )}
              </section>
            </>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
