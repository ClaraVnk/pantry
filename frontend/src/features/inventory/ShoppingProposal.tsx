import { useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { addShoppingListItems } from '../../api/endpoints';
import type { DepletedProduct } from '../../api/types';
import { Button, Checkbox } from '../../components/ui';
import styles from './Inventory.module.css';

interface Props {
  /** Products depleted since the last dismissal, oldest first. */
  items: DepletedProduct[];
  onDismiss: () => void;
  /**
   * "Never propose these again." Distinct from `onDismiss`, which only closes
   * the strip — including after a successful add, where declining what was just
   * put on the list would be exactly wrong.
   */
  onDecline: (productIds: string[]) => void;
  onDone: (message: string) => void;
}

/**
 * The repurchase proposal of contract v1.1 §6bis.
 *
 * Form chosen deliberately: a strip docked above the navigation bar, never a
 * dialog. Emptying a fridge after a meal can deplete five items, and five modals
 * in a row would end the feature — worse, each one would interrupt the gesture
 * in progress, which is tidying a kitchen, not managing a shopping list.
 *
 * So: one strip for N products, it accumulates instead of reappearing, it takes
 * no focus (`role="status"`, announced politely once the removal is done), it
 * covers nothing that can be tapped, and the list underneath stays fully usable.
 * Nothing is written until the user presses the button — the whole point of
 * §6bis is that a list which fills itself is a list nobody reads.
 *
 * Refusal is not remembered here: "Non merci" tells the server (§6bis — "son
 * refus est mémorisé"), and the answer comes back on the next depletion as
 * `previously_declined`. Duplicating that state in the client is how the two
 * drift apart.
 */
export function ShoppingProposal({ items, onDismiss, onDecline, onDone }: Props) {
  const [excluded, setExcluded] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const selected = items.filter((item) => !excluded.includes(item.product_id));

  const toggle = (productId: string, checked: boolean) => {
    setExcluded((current) =>
      checked ? current.filter((id) => id !== productId) : [...current, productId],
    );
  };

  const add = () => {
    if (selected.length === 0) return;
    setBusy(true);
    setError(null);

    addShoppingListItems(
      selected.map((item) => ({ product_id: item.product_id, source: 'depleted' as const })),
    )
      .then(() => {
        onDone(
          selected.length === 1
            ? `${selected[0]?.product_name ?? ''} a été ajouté à la liste de courses.`
            : `${String(selected.length)} articles ont été ajoutés à la liste de courses.`,
        );
        onDismiss();
      })
      .catch((cause: unknown) => {
        setError(
          cause instanceof ApiError && (cause.status === 404 || cause.status === 501)
            ? 'Ce serveur ne gère pas encore la liste de courses.'
            : describeError(cause),
        );
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const heading =
    items.length === 1
      ? `${items[0]?.product_name ?? ''} : il n’en reste plus`
      : `${String(items.length)} articles épuisés`;

  return (
    <aside className={styles.proposal} aria-label="Proposition de rachat">
      <div className={styles.proposalBody} role="status">
        <p className={styles.proposalTitle}>{heading}</p>
        <p className={styles.proposalHint}>
          Les ajouter à la liste de courses ? Rien n’est ajouté sans votre accord.
        </p>
      </div>

      {expanded ? (
        <ul className={styles.proposalList}>
          {items.map((item) => (
            <li key={item.product_id}>
              <Checkbox
                checked={!excluded.includes(item.product_id)}
                onChange={(checked) => {
                  toggle(item.product_id, checked);
                }}
              >
                {item.product_name}
              </Checkbox>
            </li>
          ))}
        </ul>
      ) : null}

      {error ? (
        <p className={styles.proposalError} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.proposalActions}>
        <Button
          variant="ghost"
          onClick={() => {
            onDecline(items.map((item) => item.product_id));
            onDismiss();
          }}
        >
          Non merci
        </Button>
        {items.length > 1 ? (
          <Button
            variant="ghost"
            aria-expanded={expanded}
            onClick={() => {
              setExpanded((value) => !value);
            }}
          >
            {expanded ? 'Masquer le détail' : 'Choisir'}
          </Button>
        ) : null}
        <Button variant="primary" loading={busy} disabled={selected.length === 0} onClick={add}>
          Ajouter{selected.length > 1 ? ` (${String(selected.length)})` : ''}
        </Button>
      </div>
    </aside>
  );
}
