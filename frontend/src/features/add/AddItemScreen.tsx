import { useCallback, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { lookupProduct } from '../../api/endpoints';
import type { StorageLocation } from '../../api/types';
import { Button, Callout, EmptyState, LoadingRow } from '../../components/ui';
import { isRestrictedCirculationNumber } from '../../lib/gtin';
import { LocationSetup } from '../locations/LocationSetup';
import { ReceiptsScreen } from '../receipts/ReceiptsScreen';
import { ManualItemForm, type ItemDraft } from './ManualItemForm';
import { ScannerView } from './ScannerView';
import styles from './Add.module.css';

type Stage =
  | { kind: 'choice' }
  | { kind: 'scanning' }
  | { kind: 'receipt' }
  | { kind: 'looking-up'; gtin: string }
  | { kind: 'form'; draft: ItemDraft }
  | { kind: 'saved'; label: string };

interface Props {
  locations: StorageLocation[];
  locationsError: string | null;
  onAdded: () => void;
  onLocationCreated: (location: StorageLocation) => void;
  onGoToInventory: () => void;
}

const BLANK_DRAFT: ItemDraft = { product: null, gtin: null, source: 'manual', notice: null };

export function AddItemScreen({
  locations,
  locationsError,
  onAdded,
  onLocationCreated,
  onGoToInventory,
}: Props) {
  const [stage, setStage] = useState<Stage>({ kind: 'choice' });

  const handleDetected = useCallback((gtin: string) => {
    // Store-internal codes (GS1 restricted circulation, prefixes 02 and 20–29)
    // are weighed-in-store items. They are never in a public catalogue and the
    // same product carries a different code on every package, so calling the
    // API is a guaranteed 404 that burns a shared rate limit. Straight to the
    // form, with an honest explanation. See technical-notes-scanning.md §4.4.
    if (isRestrictedCirculationNumber(gtin)) {
      setStage({
        kind: 'form',
        draft: {
          product: null,
          gtin: null,
          source: 'barcode_scan',
          notice: {
            tone: 'info',
            title: 'Code interne au magasin',
            body: "Ce code est propre à l'enseigne (produit pesé en rayon). Il n'existe dans aucun catalogue public — décrivez le produit vous-même.",
          },
        },
      });
      return;
    }

    setStage({ kind: 'looking-up', gtin });

    lookupProduct(gtin)
      .then((product) => {
        setStage({
          kind: 'form',
          draft: { product, gtin, source: 'barcode_scan', notice: null },
        });
      })
      .catch((cause: unknown) => {
        // A 404 must never be a dead end: it turns straight into a prefilled
        // form. "Produit inconnu / OK" is what makes people abandon an
        // inventory app on the third use (§4.1).
        if (cause instanceof ApiError && cause.status === 404) {
          setStage({
            kind: 'form',
            draft: {
              product: null,
              gtin,
              source: 'barcode_scan',
              notice: {
                tone: 'info',
                title: 'Produit inconnu de la base',
                body: `Le code ${gtin} n'est pas répertorié. Donnez-lui un nom : il sera enregistré pour vos prochains scans.`,
              },
            },
          });
          return;
        }
        if (cause instanceof ApiError && cause.status === 422) {
          setStage({
            kind: 'form',
            draft: {
              product: null,
              gtin: null,
              source: 'barcode_scan',
              notice: {
                tone: 'info',
                title: 'Code interne au magasin',
                body: 'Ce code ne peut pas être résolu — décrivez le produit vous-même.',
              },
            },
          });
          return;
        }

        const retry =
          cause instanceof ApiError && cause.retryAfterSeconds !== null
            ? ` Réessayez dans ${String(cause.retryAfterSeconds)} s.`
            : '';
        setStage({
          kind: 'form',
          draft: {
            product: null,
            gtin,
            source: 'barcode_scan',
            notice: {
              tone: 'warn',
              title: 'Base produits indisponible',
              body: `${describeError(cause)}${retry} L'article peut quand même être ajouté : renseignez-le à la main.`,
            },
          },
        });
      });
  }, []);

  const backToChoice = useCallback(() => {
    setStage({ kind: 'choice' });
  }, []);

  if (stage.kind === 'scanning') {
    return (
      <ScannerView
        onDetected={handleDetected}
        onManualEntry={() => {
          setStage({ kind: 'form', draft: BLANK_DRAFT });
        }}
        onCancel={backToChoice}
      />
    );
  }

  if (stage.kind === 'receipt') {
    // The third way in, and the only one that fills the cupboard and the budget
    // in the same gesture (contract §6ter). It lives here rather than under its
    // own tab because a receipt *is* an addition to stock — a bulk one — and a
    // seventh tab on a 390 px screen would cost every other one its label.
    return <ReceiptsScreen locations={locations} onStockChanged={onAdded} onDone={backToChoice} />;
  }

  if (stage.kind === 'looking-up') {
    return (
      <div className={styles.screen}>
        <h1 className={styles.heading}>Recherche du produit</h1>
        <p className={styles.lead} aria-live="polite">
          Code {stage.gtin} — interrogation de la base produits…
        </p>
        <LoadingRow label="Recherche en cours…" />
        <Button variant="ghost" onClick={backToChoice}>
          Annuler
        </Button>
      </div>
    );
  }

  if (stage.kind === 'form') {
    if (locations.length === 0) {
      // Two different situations, and only one of them is a failure. A household
      // that simply has no location yet gets the form that creates one; a list
      // that could not be fetched gets the reason, because creating a location
      // blind would not help.
      if (locationsError !== null) {
        return (
          <div className={styles.screen}>
            <h1 className={styles.heading}>Ajouter un article</h1>
            <Callout tone="danger" title="Emplacements indisponibles">
              <p>{locationsError}</p>
              <p>Un article doit être rangé quelque part ; réessayez une fois la liste revenue.</p>
            </Callout>
            <Button variant="secondary" onClick={backToChoice}>
              Retour
            </Button>
          </div>
        );
      }
      return (
        <div className={styles.screen}>
          <h1 className={styles.heading}>Ajouter un article</h1>
          <LocationSetup existing={locations} onCreated={onLocationCreated}>
            <p className={styles.lead}>
              Un article doit être rangé quelque part, et ce foyer n’a encore aucun emplacement.
              Créez-en un : le formulaire d’ajout reprend juste après.
            </p>
          </LocationSetup>
          <Button variant="ghost" onClick={backToChoice}>
            Retour
          </Button>
        </div>
      );
    }
    return (
      <ManualItemForm
        draft={stage.draft}
        locations={locations}
        onSaved={(label) => {
          onAdded();
          setStage({ kind: 'saved', label });
        }}
        onCancel={backToChoice}
      />
    );
  }

  if (stage.kind === 'saved') {
    return (
      <div className={styles.screen}>
        <div aria-live="polite">
          <EmptyState
            title="Article ajouté"
            body={`« ${stage.label} » est entré au stock.`}
            action={
              <Button
                variant="primary"
                onClick={() => {
                  setStage({ kind: 'scanning' });
                }}
              >
                Scanner l’article suivant
              </Button>
            }
          />
        </div>
        <Button variant="secondary" onClick={backToChoice}>
          Ajouter autrement
        </Button>
        <Button variant="ghost" onClick={onGoToInventory}>
          Voir l’inventaire
        </Button>
      </div>
    );
  }

  return (
    <div className={styles.screen}>
      <h1 className={styles.heading}>Ajouter un article</h1>
      <p className={styles.lead}>
        Trois chemins, également valables : le ticket de caisse pour toutes les courses d’un coup,
        le scan pour un produit emballé, la saisie pour ce qui est vendu au poids ou sans
        code-barres.
      </p>

      <div className={styles.choices}>
        <button
          type="button"
          className={[styles.choice, styles.choicePrimary].join(' ')}
          onClick={() => {
            setStage({ kind: 'receipt' });
          }}
        >
          <span className={styles.choiceTitle}>Importer un ticket de caisse</span>
          <span className={styles.choiceBody}>
            Toutes les courses d’un coup, et le montant rejoint votre budget. La photo est lue puis
            jetée ; vous relisez chaque ligne avant qu’elle n’entre en stock.
          </span>
        </button>

        <button
          type="button"
          className={styles.choice}
          onClick={() => {
            setStage({ kind: 'scanning' });
          }}
        >
          <span className={styles.choiceTitle}>Scanner un code-barres</span>
          <span className={styles.choiceBody}>
            La caméra ne démarre qu’après votre accord, et l’image reste sur l’appareil.
          </span>
        </button>

        <button
          type="button"
          className={styles.choice}
          onClick={() => {
            setStage({ kind: 'form', draft: BLANK_DRAFT });
          }}
        >
          <span className={styles.choiceTitle}>Saisir un article à la main</span>
          <span className={styles.choiceBody}>
            Fruits et légumes en vrac, boucherie, fromage à la coupe, conserves maison.
          </span>
        </button>
      </div>

      {locationsError ? (
        <Callout tone="warn" title="Emplacements indisponibles">
          <p>{locationsError}</p>
        </Callout>
      ) : null}
    </div>
  );
}
