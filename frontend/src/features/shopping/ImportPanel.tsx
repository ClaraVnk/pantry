import { useId, useState } from 'react';
import { ApiError, describeError, problemField } from '../../api/client';
import { importShoppingList, importShoppingListFile } from '../../api/endpoints';
import type { ShoppingImport } from '../../api/types';
import { Button, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import styles from './Shopping.module.css';

interface Props {
  onProposed: (proposal: ShoppingImport) => void;
  onCancel: () => void;
}

/** Media types and extensions the server reads (§7.2). Advisory: it re-checks. */
const ACCEPT = '.pdf,.txt,.md,.csv,application/pdf,text/plain';

const MEASURE_LABELS: Record<string, string> = {
  bytes: 'sa taille',
  pages: 'son nombre de pages',
  chars: 'la quantité de texte qu’il contient',
};

/**
 * Import failures in French.
 *
 * The server's `detail` is written in English — it is addressed to an operator
 * reading a log, and it quotes byte counts and page limits. Those refusals are
 * the ones a user meets most often (a photo instead of a PDF, a 40-page
 * catalogue), so they are translated here by problem type rather than shown raw.
 * Anything unrecognised still falls through to the shared `describeError`.
 */
function describeImportError(cause: unknown): string {
  if (!(cause instanceof ApiError)) return describeError(cause);
  switch (cause.problemType) {
    case 'document-too-large': {
      const measure = problemField(cause, 'measure');
      const what = (measure === null ? undefined : MEASURE_LABELS[measure]) ?? 'sa taille';
      return `Ce document dépasse ce que Chaudron lit en une fois : ${what} est trop importante. Découpez-le, ou collez le texte directement.`;
    }
    case 'unsupported-document':
      return 'Chaudron lit un PDF, un fichier texte (.txt), ou du texte collé. Ce fichier n’est ni l’un ni l’autre.';
    case 'document-unreadable':
      return 'Ce document n’a pas pu être lu. S’il s’agit d’un PDF scanné, son texte n’est pas extractible : recopiez la liste dans le champ ci-dessous.';
    case 'validation-failed':
      return 'Ce document n’a pas pu être lu. Vérifiez qu’il contient bien du texte.';
    default:
      return describeError(cause);
  }
}

/**
 * The three entrances of §7: a PDF, a `.txt`, or pasted text.
 *
 * All three land on the same review, and none of them writes anything — the
 * response is a proposal, and the server keeps neither the file nor the
 * proposal. A shopping list says what a household is about to eat; there is no
 * reason to hold it longer than the request that read it.
 */
export function ImportPanel({ onProposed, onCancel }: Props) {
  const fileId = useId();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = (task: Promise<ShoppingImport>): void => {
    setBusy(true);
    setError(null);
    task
      .then(onProposed)
      .catch((cause: unknown) => {
        setError(describeImportError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className={styles.card} aria-labelledby="import-heading">
      <h3 className={styles.sectionTitle} id="import-heading">
        Importer une liste
      </h3>
      <p className={styles.lead}>
        Un PDF, un fichier texte, ou la liste collée ci-dessous. Vous relirez chaque ligne avant que
        quoi que ce soit ne soit ajouté.
      </p>

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={fileId}>
          Fichier PDF ou texte
        </label>
        <input
          id={fileId}
          className={[controlClass(), styles.fileInput].join(' ')}
          type="file"
          accept={ACCEPT}
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            // Reset the control so choosing the same file twice still fires a
            // change event — a failed import that cannot be retried without
            // picking a different file is a dead end.
            event.target.value = '';
            if (file) run(importShoppingListFile(file));
          }}
        />
      </div>

      <Field
        label="Ou collez votre liste"
        hint="Une ligne par article. « 2 kg de pommes de terre » est compris ; le reste sera repris tel quel."
      >
        {({ id, describedBy }) => (
          <textarea
            id={id}
            className={[controlClass(), styles.textarea].join(' ')}
            aria-describedby={describedBy}
            rows={5}
            disabled={busy}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
            }}
          />
        )}
      </Field>

      {error !== null ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button variant="ghost" onClick={onCancel}>
          Annuler
        </Button>
        <Button
          variant="primary"
          loading={busy}
          disabled={text.trim() === ''}
          onClick={() => {
            run(importShoppingList(text));
          }}
        >
          Analyser le texte
        </Button>
      </div>
    </section>
  );
}
