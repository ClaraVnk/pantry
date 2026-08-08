import { useState } from 'react';
import { ApiError, describeError, problemField } from '../../api/client';
import { exportShoppingList, getShoppingListText } from '../../api/endpoints';
import { Button, Callout } from '../../components/ui';
import { hasConsentedTarget, useExportTargets } from './exportTargets';
import styles from './Shopping.module.css';

interface Props {
  shoppingListId: string;
  /** Nothing to send: the controls stay out rather than fail politely. */
  hasPendingItems: boolean;
}

const TODOIST = 'todoist';

/**
 * A refusal, in French, plus whether the list of destinations is now stale.
 *
 * `export-target-not-configured` and `export-consent-missing` mean the state
 * this screen was rendered from no longer holds — a destination was withdrawn,
 * or removed, in another tab or on another device between the read and the
 * send. The answer is to re-read it, which makes the button disappear on its
 * own. Everything else is transient — a revoked token, a rate limit, a vendor
 * outage — and each has its own remedy, which is why the server refuses to
 * collapse them into one "export failed".
 */
function describeExportError(cause: unknown): { message: string; stale: boolean } {
  if (!(cause instanceof ApiError)) return { message: describeError(cause), stale: false };
  switch (cause.problemType) {
    case 'export-target-not-configured':
      return {
        message:
          'Aucune destination Todoist n’est enregistrée pour ce foyer. Elle s’enregistre depuis l’onglet Foyer, avec un jeton Todoist personnel. Le partage en texte, lui, ne sort pas d’ici.',
        stale: true,
      };
    case 'export-consent-missing':
      return {
        message:
          'Ce foyer n’a pas (ou plus) autorisé l’envoi de sa liste vers Todoist. L’accord se redonne depuis l’onglet Foyer.',
        stale: true,
      };
    case 'export-target-rejected':
      return {
        message:
          'Todoist a refusé le jeton enregistré : il a probablement été révoqué ou a expiré. Il faut en enregistrer un nouveau.',
        stale: false,
      };
    case 'export-rate-limited':
      return {
        message: 'Todoist limite le débit en ce moment. Réessayez dans quelques instants.',
        stale: false,
      };
    case 'export-partially-applied': {
      const accepted = problemField(cause, 'exported_item_count') ?? '?';
      const rejected = problemField(cause, 'rejected_item_count') ?? '?';
      return {
        message: `Une partie seulement de la liste est arrivée : ${accepted} acceptés, ${rejected} refusés. Vérifiez dans Todoist avant de renvoyer, sinon les articles acceptés y seront en double.`,
        stale: false,
      };
    }
    case 'export-target-unavailable':
      return {
        message: 'Todoist est injoignable pour l’instant. Réessayez plus tard.',
        stale: false,
      };
    default:
      return { message: describeError(cause), stale: false };
  }
}

/**
 * The two ways a list leaves this screen, and they are not the same thing.
 *
 * **Sharing as text** asks the server for the list and hands the string to the
 * share sheet: nothing leaves the instance on its own, the user picks the
 * recipient. `navigator.share` is missing on most desktop browsers and the
 * clipboard is refused without permission in some, so there is a third step —
 * the text itself, on screen, selectable. A share that silently does nothing is
 * worse than a paragraph of text.
 *
 * **Sending to Todoist** transmits personal data to a third party under a
 * stored credential, so it only exists for a household that registered one and
 * agreed to it. That is now a question with an answer: `useExportTargets` reads
 * the household's consented destinations, so the button is offered when it can
 * work and replaced by an explanation when it cannot — on every device, rather
 * than on the ones that happened to have failed once.
 */
export function ExportActions({ shoppingListId, hasPendingItems }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [fallbackText, setFallbackText] = useState<string | null>(null);
  const exportTargets = useExportTargets();
  const canSendToTodoist = hasConsentedTarget(exportTargets, TODOIST);

  if (!hasPendingItems) return null;

  const share = (): void => {
    setBusy('text');
    setError(null);
    setNotice(null);
    setFallbackText(null);

    getShoppingListText(shoppingListId)
      .then(async (text) => {
        if (typeof navigator.share === 'function') {
          try {
            // No `url`: iOS replaces a cross-domain one with the current page's
            // and strips the other's query string, so the list would arrive
            // with a link nobody asked for.
            await navigator.share({ text });
            setNotice('Liste partagée.');
            return;
          } catch (cause) {
            // The user closing the share sheet is not a failure to report.
            if (cause instanceof DOMException && cause.name === 'AbortError') return;
          }
        }
        try {
          await navigator.clipboard.writeText(text);
          setNotice('Liste copiée dans le presse-papier.');
        } catch {
          setFallbackText(text);
          setNotice('Votre navigateur n’autorise ni le partage ni la copie : voici la liste.');
        }
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(null);
      });
  };

  const sendToTodoist = (): void => {
    setBusy(TODOIST);
    setError(null);
    setNotice(null);
    setFallbackText(null);

    exportShoppingList(shoppingListId, TODOIST)
      .then((receipt) => {
        setNotice(
          `${String(receipt.exported_item_count)} ${
            receipt.exported_item_count > 1 ? 'articles envoyés' : 'article envoyé'
          } vers Todoist.`,
        );
      })
      .catch((cause: unknown) => {
        const { message, stale } = describeExportError(cause);
        setError(message);
        // The destination was withdrawn or removed while this screen was open.
        // Re-reading is what makes the button go away; nothing is remembered
        // here, so the next device to look gets the same answer.
        if (stale) exportTargets.reload();
      })
      .finally(() => {
        setBusy(null);
      });
  };

  return (
    <section className={styles.card} aria-labelledby="export-heading">
      <h3 className={styles.sectionTitle} id="export-heading">
        Emporter la liste
      </h3>

      <div className={styles.actions}>
        <Button variant="secondary" loading={busy === 'text'} onClick={share}>
          Partager en texte
        </Button>
        {canSendToTodoist ? (
          <Button variant="secondary" loading={busy === TODOIST} onClick={sendToTodoist}>
            Envoyer vers Todoist
          </Button>
        ) : null}
      </div>

      <p className={styles.note}>
        Le partage en texte ne sort pas de cette instance : Chaudron met la liste dans le
        presse-papier ou dans le partage de votre téléphone, et c’est vous qui choisissez où elle
        va.
      </p>

      {/* Nothing while the answer is still being fetched: a button that appears
          and then vanishes is worse than one that arrives a moment late. */}
      {!exportTargets.loading && !canSendToTodoist ? (
        <Callout tone="info" title="Envoi vers Todoist indisponible">
          <p>
            {exportTargets.error ??
              'Aucune destination Todoist n’est enregistrée et autorisée pour ce foyer. Elle s’enregistre depuis l’onglet Foyer, avec un jeton Todoist personnel — et l’accord peut être retiré à tout moment.'}
          </p>
          {exportTargets.error !== null ? (
            <div className={styles.actions}>
              <Button variant="ghost" onClick={exportTargets.reload}>
                Réessayer
              </Button>
            </div>
          ) : null}
        </Callout>
      ) : null}

      {notice !== null ? (
        <p className={styles.note} role="status">
          {notice}
        </p>
      ) : null}

      {fallbackText !== null ? (
        <textarea
          className={[styles.textarea, styles.fallbackText].join(' ')}
          readOnly
          rows={8}
          aria-label="Votre liste de courses, à copier"
          value={fallbackText}
        />
      ) : null}

      {error !== null ? (
        <p className={styles.formError} role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
