import { useState } from 'react';
import type { ShoppingImport, ShoppingListItem } from '../../api/types';
import { Button, EmptyState, ErrorState, LoadingRow } from '../../components/ui';
import { AddToListForm } from './AddToListForm';
import { ExportActions } from './ExportActions';
import { ImportPanel } from './ImportPanel';
import { ImportReview } from './ImportReview';
import { ShoppingItemRow } from './ShoppingItemRow';
import { useShoppingList } from './useShoppingList';
import styles from './Shopping.module.css';

type Mode = 'list' | 'import' | 'review';

function byOrder(a: ShoppingListItem, b: ShoppingListItem): number {
  return a.sort_order - b.sort_order;
}

/**
 * The shopping list (contract §6bis and §7).
 *
 * Three states in one screen rather than three routes, because the import is a
 * detour from the list and returns to it: the review has to be able to say
 * "annuler" and put the user back exactly where they were, with nothing
 * written. The application has no router, and adding one for a two-step detour
 * would be the wrong trade.
 *
 * Checked items stay on screen, in their own group. Hiding them would make the
 * list shrink as it is used, which reads as items being lost, and unticking
 * something picked up by mistake would have nowhere to happen.
 */
export function ShoppingScreen() {
  const state = useShoppingList();
  const [mode, setMode] = useState<Mode>('list');
  const [proposal, setProposal] = useState<ShoppingImport | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const list = state.list;
  const pending = list === null ? [] : list.items.filter((item) => !item.checked).sort(byOrder);
  const done = list === null ? [] : list.items.filter((item) => item.checked).sort(byOrder);

  if (mode === 'import') {
    return (
      <section className={styles.screen} aria-labelledby="shopping-heading">
        <h2 className={styles.heading} id="shopping-heading">
          Importer une liste
        </h2>
        <ImportPanel
          onProposed={(imported) => {
            setProposal(imported);
            setMode('review');
          }}
          onCancel={() => {
            setMode('list');
          }}
        />
      </section>
    );
  }

  if (mode === 'review' && proposal !== null) {
    return (
      <section className={styles.screen} aria-labelledby="shopping-heading">
        <h2 className={styles.heading} id="shopping-heading">
          Import de liste
        </h2>
        <ImportReview
          proposal={proposal}
          onCancel={() => {
            setProposal(null);
            setMode('list');
          }}
          onConfirmed={(result) => {
            setProposal(null);
            setMode('list');
            setNotice(
              `${String(result.created_item_count)} ${
                result.created_item_count > 1 ? 'articles ajoutés' : 'article ajouté'
              } à « ${result.shopping_list_name} ».`,
            );
            state.reload();
          }}
        />
      </section>
    );
  }

  return (
    <section className={styles.screen} aria-labelledby="shopping-heading">
      <div className={styles.headingRow}>
        <h2 className={styles.heading} id="shopping-heading">
          {list?.name ?? 'Liste de courses'}
        </h2>
        <Button
          variant="secondary"
          onClick={() => {
            setNotice(null);
            setMode('import');
          }}
        >
          Importer
        </Button>
      </div>

      {notice !== null ? (
        <p className={styles.note} role="status">
          {notice}
        </p>
      ) : null}

      {state.loading ? <LoadingRow label="Chargement de la liste…" /> : null}

      {state.unsupported ? (
        <EmptyState
          title="Ce serveur ne gère pas encore la liste de courses"
          body="L’application est à jour, l’API ne l’est pas encore. La liste apparaîtra sans rien changer ici."
        />
      ) : null}

      {state.error !== null ? <ErrorState message={state.error} onRetry={state.reload} /> : null}

      {list !== null ? (
        <>
          <AddToListForm onAdded={state.replace} />

          {list.items.length === 0 ? (
            <EmptyState
              title="Votre liste est vide"
              body="Ajoutez un article ci-dessus, ou importez une liste depuis un PDF, un fichier texte ou du texte collé. Quand un produit du stock est terminé, Chaudron proposera aussi de l’ajouter ici."
              action={
                <Button
                  variant="primary"
                  onClick={() => {
                    setMode('import');
                  }}
                >
                  Importer une liste
                </Button>
              }
            />
          ) : null}

          {pending.length > 0 ? (
            <section aria-labelledby="shopping-pending-heading">
              <h3 className={styles.sectionTitle} id="shopping-pending-heading">
                À prendre ({pending.length})
              </h3>
              <ul className={styles.list}>
                {pending.map((item) => (
                  <ShoppingItemRow
                    key={item.id}
                    item={item}
                    onChanged={state.applyItem}
                    onRemoved={() => {
                      state.dropItem(item.id);
                    }}
                  />
                ))}
              </ul>
            </section>
          ) : null}

          {done.length > 0 ? (
            <section aria-labelledby="shopping-done-heading">
              <h3 className={styles.sectionTitle} id="shopping-done-heading">
                Déjà pris ({done.length})
              </h3>
              <ul className={styles.list}>
                {done.map((item) => (
                  <ShoppingItemRow
                    key={item.id}
                    item={item}
                    onChanged={state.applyItem}
                    onRemoved={() => {
                      state.dropItem(item.id);
                    }}
                  />
                ))}
              </ul>
            </section>
          ) : null}

          <ExportActions shoppingListId={list.id} hasPendingItems={pending.length > 0} />
        </>
      ) : null}
    </section>
  );
}
