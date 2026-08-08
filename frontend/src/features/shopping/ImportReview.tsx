import { useState } from 'react';
import { describeError } from '../../api/client';
import { confirmShoppingListImport } from '../../api/endpoints';
import type { ShoppingImport, ShoppingImportResult } from '../../api/types';
import { Button, Callout, EmptyState } from '../../components/ui';
import { ReviewLine } from './ReviewLine';
import { blankDraft, draftError, toConfirmLine, toDrafts, type DraftLine } from './review';
import styles from './Shopping.module.css';

interface Props {
  proposal: ShoppingImport;
  onCancel: () => void;
  onConfirmed: (result: ShoppingImportResult) => void;
}

const SOURCE_LABELS: Record<string, string> = {
  pdf: 'un PDF',
  txt: 'un fichier texte',
  text: 'du texte collé',
};

function plural(count: number, one: string, many: string): string {
  return count > 1 ? many : one;
}

/**
 * The review of an imported list (contract §7.2). **Nothing has been written
 * when this screen opens, and nothing is written until the last button.**
 *
 * This is where the parser's documented failures are repaired by a human, and
 * the screen is built around making that cheap — see `ReviewLine` for the three
 * one-gesture repairs. The failures are known and written down in the backend's
 * own tests: a numbered list reads `2.` as a quantity of two, a container word
 * lands on the label (`3 paquets de pâtes` becomes three pieces of "paquets de
 * pâtes"), two articles on one line stay one line, and the document's title
 * arrives as an article.
 *
 * "Ajouter une ligne" is here for the third of those: `pain et lait` cannot be
 * split by the parser — splitting on "et" would break "sel et poivre" and every
 * product whose name contains it — so the user renames the line and adds the
 * second article. One gesture each, again.
 */
export function ImportReview({ proposal, onCancel, onConfirmed }: Props) {
  const [lines, setLines] = useState<DraftLine[]>(() => toDrafts(proposal.lines));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const invalidCount = lines.filter((line) => draftError(line) !== null).length;
  const flaggedCount = lines.filter((line) => line.flags.length > 0).length;

  const update = (key: string, next: DraftLine): void => {
    setLines((current) => current.map((line) => (line.key === key ? next : line)));
  };

  const remove = (key: string): void => {
    setLines((current) => current.filter((line) => line.key !== key));
  };

  const confirm = (): void => {
    if (lines.length === 0 || invalidCount > 0) return;
    setBusy(true);
    setError(null);

    confirmShoppingListImport(proposal.import_id, lines.map(toConfirmLine))
      .then(onConfirmed)
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className={styles.review} aria-labelledby="import-review-heading">
      <div className={styles.reviewIntro}>
        <h3 className={styles.sectionTitle} id="import-review-heading">
          Vérifiez avant d’ajouter
        </h3>
        <p className={styles.lead}>
          {`Chaudron a lu ${SOURCE_LABELS[proposal.source] ?? 'ce document'} et propose ${String(
            proposal.lines.length,
          )} ${plural(proposal.lines.length, 'ligne', 'lignes')}. Rien n’est ajouté à votre liste tant que vous n’avez pas confirmé.`}
        </p>
      </div>

      {proposal.truncated ? (
        <Callout tone="warn" title="Le document a été coupé">
          <p>
            Il contenait plus de lignes que Chaudron n’en lit en une fois. Les lignes affichées
            ci-dessous sont les premières du document ; les suivantes n’ont pas été lues. Importez
            le reste séparément si besoin.
          </p>
        </Callout>
      ) : null}

      {proposal.parsed_by === 'deterministic+model' ? (
        <Callout tone="info" title="Un modèle a relu certaines lignes">
          <p>
            Les lignes que l’analyse simple n’a pas su découper ont été soumises à un modèle. Ce
            qu’il propose est affiché comme incertain et n’est écrit que si vous le confirmez.
          </p>
        </Callout>
      ) : null}

      {proposal.unparsed_line_count > 0 ? (
        <p className={styles.lead}>
          {`${String(proposal.unparsed_line_count)} ${plural(
            proposal.unparsed_line_count,
            'ligne n’a pas pu être analysée : elle est reprise',
            'lignes n’ont pas pu être analysées : elles sont reprises',
          )} telle${proposal.unparsed_line_count > 1 ? 's' : ''} quelle${
            proposal.unparsed_line_count > 1 ? 's' : ''
          }, sans quantité.`}
        </p>
      ) : null}

      {lines.length === 0 ? (
        <EmptyState
          title="Plus aucune ligne à ajouter"
          body="Vous avez retiré toutes les lignes de cet import. Ajoutez-en une à la main, ou annulez pour revenir à votre liste."
          action={
            <Button
              variant="secondary"
              onClick={() => {
                setLines([blankDraft()]);
              }}
            >
              Ajouter une ligne
            </Button>
          }
        />
      ) : (
        <ul className={styles.reviewList}>
          {lines.map((line, index) => (
            <ReviewLine
              key={line.key}
              line={line}
              position={index + 1}
              onChange={(next) => {
                update(line.key, next);
              }}
              onRemove={() => {
                remove(line.key);
              }}
            />
          ))}
        </ul>
      )}

      {lines.length > 0 ? (
        <Button
          variant="secondary"
          block
          onClick={() => {
            setLines((current) => [...current, blankDraft()]);
          }}
        >
          Ajouter une ligne
        </Button>
      ) : null}

      {/*
        Contract §7.4, and the sentence is deliberate. A flag signals, it never
        removes: a household is allowed to buy what one of its members cannot
        eat. The negative half is stated as what was *not* done — never as
        "sans allergène", which this screen has no way of knowing.
      */}
      <p className={styles.note}>
        Un article correspondant à un allergène déclaré dans le foyer est signalé ici, jamais retiré
        : on a le droit d’acheter ce qu’on ne mangera pas soi-même.{' '}
        {flaggedCount > 0
          ? `${String(flaggedCount)} ${plural(flaggedCount, 'ligne est signalée', 'lignes sont signalées')} ci-dessus.`
          : 'Aucune ligne n’a déclenché de signalement : les autres n’ont pas été contrôlées pour autant.'}
      </p>

      {error !== null ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}

      {invalidCount > 0 ? (
        <p className={styles.note} role="status">
          {`${String(invalidCount)} ${plural(invalidCount, 'ligne est à corriger', 'lignes sont à corriger')} avant d’ajouter.`}
        </p>
      ) : null}

      <div className={styles.reviewActions}>
        <Button variant="ghost" onClick={onCancel}>
          Annuler
        </Button>
        <Button
          variant="primary"
          loading={busy}
          disabled={lines.length === 0 || invalidCount > 0}
          onClick={confirm}
        >
          {`Ajouter ${String(lines.length)} ${plural(lines.length, 'article', 'articles')}`}
        </Button>
      </div>
    </section>
  );
}
