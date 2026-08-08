import { useCallback, useEffect, useState } from 'react';
import { describeError } from '../../api/client';
import { getPendingReceipts, getReceipt } from '../../api/endpoints';
import type {
  Receipt,
  ReceiptConfirmResult,
  ReceiptSummary,
  StorageLocation,
} from '../../api/types';
import { Button, Callout, EmptyState, LoadingRow } from '../../components/ui';
import { formatMoney } from '../budget/money';
import { ReceiptImportPanel } from './ReceiptImportPanel';
import { ReceiptReview } from './ReceiptReview';
import styles from './Receipts.module.css';

interface Props {
  locations: StorageLocation[];
  /** Called once stock has actually been written, so the inventory refetches. */
  onStockChanged: () => void;
  onDone: () => void;
}

type Stage =
  | { kind: 'choice' }
  | { kind: 'loading'; receiptId: string }
  | { kind: 'review'; receipt: Receipt }
  | { kind: 'saved'; result: ReceiptConfirmResult };

function plural(count: number, one: string, many: string): string {
  return count > 1 ? many : one;
}

function money(value: string | null, currency: string | null): string {
  if (value === null) return 'montant non lu';
  return currency === null ? value : formatMoney(value, currency);
}

/**
 * Import a receipt, review it, and put the shopping away.
 *
 * The pending list at the top is not a nicety. A receipt counts towards the
 * budget as soon as it is read — contract §6ter counts it from the moment it
 * carries a total and a currency, deliberately before its lines are accepted, so
 * that an inventory chore never hides what was spent. The consequence is that an
 * import abandoned mid-review is money on a screen with nothing pointing at it.
 * This list is what points at it, and what makes throwing it away possible.
 */
export function ReceiptsScreen({ locations, onStockChanged, onDone }: Props) {
  const [stage, setStage] = useState<Stage>({ kind: 'choice' });
  const [pending, setPending] = useState<ReceiptSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reloadPending = useCallback(() => {
    getPendingReceipts()
      .then((page) => {
        setPending(page.receipts);
        setError(null);
      })
      .catch((cause: unknown) => {
        setPending([]);
        setError(describeError(cause));
      });
  }, []);

  useEffect(() => {
    reloadPending();
  }, [reloadPending]);

  const open = useCallback((receiptId: string) => {
    setStage({ kind: 'loading', receiptId });
    getReceipt(receiptId)
      .then((receipt) => {
        setStage({ kind: 'review', receipt });
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
        setStage({ kind: 'choice' });
      });
  }, []);

  if (stage.kind === 'loading') {
    return (
      <div className={styles.screen}>
        <LoadingRow label="Ouverture du ticket…" />
      </div>
    );
  }

  if (stage.kind === 'review') {
    return (
      <div className={styles.screen}>
        <ReceiptReview
          receipt={stage.receipt}
          locations={locations}
          onCancelled={() => {
            reloadPending();
            setStage({ kind: 'choice' });
          }}
          onConfirmed={(result) => {
            onStockChanged();
            reloadPending();
            setStage({ kind: 'saved', result });
          }}
        />
      </div>
    );
  }

  if (stage.kind === 'saved') {
    const { created_lot_count: lots, ignored_line_count: ignored } = stage.result;
    return (
      <div className={styles.screen}>
        <div aria-live="polite">
          <EmptyState
            title="Ticket enregistré"
            body={
              lots === 0
                ? 'Aucun article n’est entré en stock. Le montant du ticket, lui, compte dans votre budget.'
                : `${String(lots)} ${plural(lots, 'article est entré', 'articles sont entrés')} en stock, et le montant du ticket compte dans votre budget.${
                    ignored > 0
                      ? ` ${String(ignored)} ${plural(ignored, 'ligne a été écartée', 'lignes ont été écartées')} du stock, sans changer le total.`
                      : ''
                  }`
            }
            action={
              <Button
                variant="primary"
                onClick={() => {
                  setStage({ kind: 'choice' });
                }}
              >
                Importer un autre ticket
              </Button>
            }
          />
        </div>
        <Button variant="ghost" onClick={onDone}>
          Retour
        </Button>
      </div>
    );
  }

  return (
    <div className={styles.screen}>
      {pending === null ? <LoadingRow label="Chargement des tickets…" /> : null}

      {error !== null ? (
        <Callout tone="warn" title="Tickets en attente indisponibles">
          <p>{error}</p>
        </Callout>
      ) : null}

      {pending !== null && pending.length > 0 ? (
        <section className={styles.card} aria-labelledby="receipt-pending-heading">
          <h2 className={styles.sectionTitle} id="receipt-pending-heading">
            {`${String(pending.length)} ${plural(pending.length, 'ticket attend', 'tickets attendent')} votre relecture`}
          </h2>
          <p className={styles.lead}>
            Leur montant compte déjà dans votre budget. Leurs articles n’entreront en stock qu’une
            fois relus — ou vous pouvez supprimer un ticket, et son montant avec lui.
          </p>
          <ul className={styles.pendingList}>
            {pending.map((receipt) => (
              <li key={receipt.id} className={styles.pendingItem}>
                <span className={styles.pendingLabel}>
                  {receipt.merchant ?? 'Enseigne non reconnue'}
                  <span className={styles.pendingMeta}>
                    {`${money(receipt.total_amount, receipt.currency)} · ${String(receipt.line_count)} ${plural(receipt.line_count, 'ligne', 'lignes')}`}
                  </span>
                </span>
                <Button
                  variant="secondary"
                  onClick={() => {
                    open(receipt.id);
                  }}
                >
                  Relire
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <ReceiptImportPanel
        onImported={(receipt) => {
          reloadPending();
          setStage({ kind: 'review', receipt });
        }}
        onOpenExisting={open}
        onCancel={onDone}
      />
    </div>
  );
}
