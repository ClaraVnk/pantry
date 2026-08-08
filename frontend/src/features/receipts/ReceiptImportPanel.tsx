import { useId, useState } from 'react';
import { ApiError, describeError, problemField } from '../../api/client';
import { importReceipt } from '../../api/endpoints';
import type { Receipt } from '../../api/types';
import { Button, Callout } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import styles from './Receipts.module.css';

interface Props {
  onImported: (receipt: Receipt) => void;
  onOpenExisting: (receiptId: string) => void;
  onCancel: () => void;
}

/** What the server reads (§6ter). Advisory: it re-checks the bytes themselves. */
const ACCEPT = 'image/jpeg,image/png,image/webp,application/pdf,.jpg,.jpeg,.png,.webp,.pdf';

const MEASURE_LABELS: Record<string, string> = {
  bytes: 'son poids',
  pages: 'son nombre de pages',
  characters: 'la quantité de texte qu’il contient',
};

/**
 * Import failures in French, by problem type.
 *
 * The server's `detail` is written in English for an operator reading a log, and
 * it quotes byte counts and page limits. The refusals a user actually meets — a
 * HEIC photo from an iPhone, a scanned PDF, no vision-capable model — are
 * translated here. Anything unrecognised falls through to `describeError`.
 *
 * `receipt-import-unavailable` is the exception and is **not** rewritten: its
 * `detail` and `remedy` are already French and are written by the provider
 * layer, which is the only place that knows which of the household's
 * configurations is at fault (ADR-0005's `unavailable` case). Both halves are
 * shown, because a reason without a remedy is a dead end and this refusal has
 * one — including "send the PDF instead", which needs no model at all.
 */
function describeImportError(cause: unknown): string {
  if (!(cause instanceof ApiError)) return describeError(cause);
  switch (cause.problemType) {
    case 'receipt-import-unavailable': {
      const reason = cause.problem?.detail ?? 'L’import de tickets est indisponible.';
      const remedy = problemField(cause, 'remedy');
      return remedy === null ? reason : `${reason} ${remedy}`;
    }
    case 'document-too-large': {
      const measure = problemField(cause, 'measure');
      const what = (measure === null ? undefined : MEASURE_LABELS[measure]) ?? 'son poids';
      return `Ce fichier dépasse ce que Chaudron lit en une fois : ${what} est trop important. Reprenez la photo en cadrant le ticket seul, ou envoyez le PDF de la commande.`;
    }
    case 'unsupported-document':
      return 'Chaudron lit une photo de ticket (JPEG, PNG, WebP) ou le PDF d’un récapitulatif de commande drive. Ce fichier n’est ni l’un ni l’autre.';
    case 'document-unreadable':
      // Written by `infra/documents`, in English but actionable and specific —
      // the HEIC sentence names the iPhone setting to change. Shown as-is rather
      // than flattened into a generic French message that would lose the remedy.
      return cause.problem?.detail ?? 'Ce document n’a pas pu être lu.';
    case 'receipt-unreadable':
      return 'Le modèle a répondu, mais pas avec quelque chose qui ressemble à un ticket. Reprenez la photo à plat, bien en face, sans ombre portée.';
    default:
      return describeError(cause);
  }
}

/**
 * The two entrances of the receipt import, and they are not equivalent.
 *
 * **The PDF of a drive order recap is the better one, and the screen says so.**
 * Its text is already in the document: reading it calls no model, costs the
 * household nothing, and cannot invent a line because nothing generates one.
 * A photograph has to go through a vision model, which
 * `docs/technical-notes-ingestion.md` §3.4 measures at 0.49 F1 on line items —
 * and which fabricates lines to make their sum match the printed total.
 *
 * The photo input asks for the rear camera (`capture="environment"`) but does
 * not force it: a receipt already photographed and sitting in the gallery is the
 * common case, and a control that only opens the camera would make it
 * unreachable.
 */
export function ReceiptImportPanel({ onImported, onOpenExisting, onCancel }: Props) {
  const photoId = useId();
  const pdfId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [duplicate, setDuplicate] = useState<string | null>(null);

  const run = (file: File): void => {
    setBusy(true);
    setError(null);
    setDuplicate(null);
    importReceipt(file)
      .then(onImported)
      .catch((cause: unknown) => {
        // The mobile double-submit: the photo was already read, and the answer
        // carries the receipt it produced. Offering to open it beats asking the
        // user to photograph a receipt Chaudron has already understood.
        if (cause instanceof ApiError && cause.problemType === 'receipt-already-imported') {
          const existing = problemField(cause, 'receipt_id');
          if (existing !== null) {
            setDuplicate(existing);
            return;
          }
        }
        setError(describeImportError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const pick = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    // Reset the control so choosing the same file twice still fires a change
    // event — a failed import that cannot be retried without picking a different
    // file is a dead end.
    event.target.value = '';
    if (file) run(file);
  };

  return (
    <section className={styles.card} aria-labelledby="receipt-import-heading">
      <h2 className={styles.sectionTitle} id="receipt-import-heading">
        Importer un ticket
      </h2>
      <p className={styles.lead}>
        Le montant total alimentera votre budget ; les articles ne rejoindront votre stock qu’après
        votre relecture. La photo n’est pas conservée : elle est lue puis jetée.
      </p>

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={pdfId}>
          Récapitulatif de commande drive (PDF)
        </label>
        <input
          id={pdfId}
          className={[controlClass(), styles.fileInput].join(' ')}
          type="file"
          accept="application/pdf,.pdf"
          disabled={busy}
          onChange={pick}
        />
        <p className={styles.hint}>
          Le plus fiable : le texte est déjà dans le document, aucun modèle n’est appelé et rien ne
          vous est facturé.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.fieldLabel} htmlFor={photoId}>
          Photo du ticket de caisse
        </label>
        <input
          id={photoId}
          className={[controlClass(), styles.fileInput].join(' ')}
          type="file"
          accept={ACCEPT}
          capture="environment"
          disabled={busy}
          onChange={pick}
        />
        <p className={styles.hint}>
          À plat, bien en face, sans ombre portée : de biais, la lecture perd les deux tiers de sa
          précision. Un ticket thermique s’efface en quelques semaines — photographiez-le tôt.
        </p>
      </div>

      {duplicate !== null ? (
        <Callout tone="info" title="Ce ticket est déjà importé">
          <p>
            Chaudron a déjà lu ce fichier. Ouvrez la lecture existante plutôt que de recommencer.
          </p>
          <Button
            variant="secondary"
            onClick={() => {
              onOpenExisting(duplicate);
            }}
          >
            Ouvrir le ticket
          </Button>
        </Callout>
      ) : null}

      {error !== null ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button variant="ghost" loading={busy} onClick={onCancel}>
          Annuler
        </Button>
      </div>
    </section>
  );
}
