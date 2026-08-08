import { useCallback, useEffect, useMemo, useState } from 'react';
import { describeError } from '../../api/client';
import { declineRepurchase, deleteInventoryItem, getInventory } from '../../api/endpoints';
import type {
  DepletedProduct,
  InventoryItem,
  RemovalReason,
  StorageLocation,
  UpdatedInventoryItem,
} from '../../api/types';
import { Button, Chip, ChipRow, EmptyState, ErrorState, LoadingRow } from '../../components/ui';
import { SOON_DAYS, formatAmount, locationKindLabel } from '../../lib/expiry';
import { unitSymbol } from '../../lib/units';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { LocationSetup } from '../locations/LocationSetup';
import { InventoryItemRow } from './InventoryItemRow';
import { ShoppingProposal } from './ShoppingProposal';
import styles from './Inventory.module.css';

const PAGE_SIZE = 50;

/** Stable identity so memo dependencies do not change on every render. */
const NO_ITEMS: InventoryItem[] = [];

interface Props {
  locations: StorageLocation[];
  /**
   * Whether the locations request has ever answered. The list waits on this
   * before its first paint — see the `ready` gate below.
   */
  locationsSettled: boolean;
  locationsError: string | null;
  /** Bumped by the shell whenever an item is added elsewhere. */
  refreshToken: number;
  onChanged: () => void;
  onLocationCreated: (location: StorageLocation) => void;
  onAddItem: () => void;
}

interface Group {
  id: string;
  name: string;
  kindLabel: string;
  items: InventoryItem[];
}

/** The answer currently held, tagged with the query it answers. */
interface Result {
  key: string;
  items: InventoryItem[];
  total: number;
  error: string | null;
}

/**
 * Group key for lots that are not filed anywhere.
 *
 * `inventory_lot.storage_location_id` is nullable and the API says so
 * (`LocationRefOut | None`), so an item without a location is a state to render,
 * not an impossibility to assume away. Reading `item.location.id` for one of
 * these is what turned the inventory into a blank page. A `#` cannot appear in a
 * UUID, so this key can never collide with a real location.
 */
const UNFILED = '#unfiled';

function groupByLocation(items: InventoryItem[], locations: StorageLocation[]): Group[] {
  const order = new Map(locations.map((location, index) => [location.id, index]));
  const groups = new Map<string, Group>();

  for (const item of items) {
    const key = item.location?.id ?? UNFILED;
    const existing = groups.get(key);
    if (existing) {
      existing.items.push(item);
      continue;
    }
    groups.set(key, {
      id: key,
      name: item.location?.name ?? 'Sans emplacement',
      kindLabel: item.location ? locationKindLabel(item.location.kind) : 'Non rangé',
      items: [item],
    });
  }

  // Unfiled items sort last: they are the exception, and putting them at the top
  // would push the stock the user came to read below the fold.
  const rank = (id: string) =>
    id === UNFILED ? Number.MAX_SAFE_INTEGER : (order.get(id) ?? Number.MAX_SAFE_INTEGER - 1);
  return [...groups.values()].sort((a, b) => rank(a.id) - rank(b.id));
}

export function InventoryScreen({
  locations,
  locationsSettled,
  locationsError,
  refreshToken,
  onChanged,
  onLocationCreated,
  onAddItem,
}: Props) {
  const [search, setSearch] = useState('');
  const [locationId, setLocationId] = useState<string | null>(null);
  const [expiringOnly, setExpiringOnly] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [depleted, setDepleted] = useState<DepletedProduct[]>([]);
  const [status, setStatus] = useState('');

  const debouncedSearch = useDebouncedValue(search, 300);
  const trimmedSearch = debouncedSearch.trim();
  const filtered = trimmedSearch !== '' || locationId !== null || expiringOnly;

  const query = useMemo(
    () => ({
      q: trimmedSearch === '' ? undefined : trimmedSearch,
      location_id: locationId ?? undefined,
      expiring_within_days: expiringOnly ? SOON_DAYS : undefined,
      limit: PAGE_SIZE,
    }),
    [trimmedSearch, locationId, expiringOnly],
  );

  // Identifies the request; `loading` is this key not matching the held answer.
  // Deriving it avoids a synchronous setState inside the effect and keeps the
  // previous page visible while the next one is in flight.
  const queryKey = `${JSON.stringify(query)}|${String(refreshToken)}|${String(reloadNonce)}`;

  useEffect(() => {
    const controller = new AbortController();

    getInventory({ ...query, offset: 0 }, controller.signal)
      .then((page) => {
        setResult({ key: queryKey, items: page.items, total: page.total, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ key: queryKey, items: [], total: 0, error: describeError(cause) });
      });

    return () => {
      controller.abort();
    };
  }, [query, queryKey]);

  const loading = result?.key !== queryKey;
  const items = result?.items ?? NO_ITEMS;
  const total = result?.total ?? 0;
  const error = result?.error ?? null;

  const loadMore = useCallback(() => {
    setLoadingMore(true);
    getInventory({ ...query, offset: items.length })
      .then((page) => {
        setResult((previous) =>
          previous === null
            ? previous
            : { ...previous, items: [...previous.items, ...page.items], total: page.total },
        );
      })
      .catch((cause: unknown) => {
        setResult((previous) =>
          previous === null ? previous : { ...previous, error: describeError(cause) },
        );
      })
      .finally(() => {
        setLoadingMore(false);
      });
  }, [query, items.length]);

  /**
   * Queues a depleted product for the grouped repurchase proposal.
   *
   * Three silences, all from contract v1.1 §6bis: a `correction` never proposes
   * (fixing a typo is not finishing a product), something already on the list is
   * not proposed twice, and a product refused before is never asked about again
   * — that state belongs to the server, not to this screen.
   */
  const noteDepleted = useCallback((product: DepletedProduct | null) => {
    if (product === null) return;
    if (product.reason === 'correction') return;
    if (product.already_on_list || product.previously_declined) return;
    setDepleted((current) =>
      current.some((entry) => entry.product_id === product.product_id)
        ? current
        : [...current, product],
    );
  }, []);

  /**
   * Records a refusal so the product stops being proposed (§6bis).
   *
   * Deliberately not awaited and deliberately silent on failure: the user just
   * said "no thanks" and the strip is already gone. Interrupting them to report
   * that the refusal did not stick would be the second interruption of a feature
   * whose whole design is not to interrupt — and the worst that follows is one
   * proposal too many, next time.
   */
  const decline = useCallback((productIds: string[]) => {
    void declineRepurchase(productIds).catch(() => undefined);
  }, []);

  const removeItem = useCallback(
    async (id: string, reason: RemovalReason) => {
      try {
        const outcome = await deleteInventoryItem(id, reason);
        setResult((previous) =>
          previous === null
            ? previous
            : {
                ...previous,
                items: previous.items.filter((item) => item.id !== id),
                total: Math.max(0, previous.total - 1),
              },
        );
        noteDepleted(outcome.depleted);
        onChanged();
      } catch (cause: unknown) {
        setResult((previous) =>
          previous === null ? previous : { ...previous, error: describeError(cause) },
        );
      }
    },
    [onChanged, noteDepleted],
  );

  const adjustedItem = useCallback(
    (updated: UpdatedInventoryItem) => {
      setResult((previous) =>
        previous === null
          ? previous
          : {
              ...previous,
              items: previous.items.map((item) => (item.id === updated.id ? updated : item)),
            },
      );
      setStatus(
        `Quantité de ${updated.product.name} mise à jour : ${formatAmount(updated.quantity.amount)} ${unitSymbol(updated.quantity.unit)}.`,
      );
      noteDepleted(updated.depleted);
      onChanged();
    },
    [onChanged, noteDepleted],
  );

  const groups = useMemo(() => groupByLocation(items, locations), [items, locations]);

  const resetFilters = () => {
    setSearch('');
    setLocationId(null);
    setExpiringOnly(false);
  };

  /**
   * Both answers, or neither.
   *
   * `/v1/locations` and `/v1/inventory` are issued together and race. Painting
   * on whichever wins meant the filter row gained its chips, and the groups were
   * re-ordered, when the loser landed. Measured on the built bundle with
   * `/v1/locations` held back 400 ms: `section._group` moving from y=0 to y=419,
   * a shift of 0.470 on its own at 390 px — and intermittent, because on an
   * unthrottled network the other order costs nothing.
   *
   * The two are settled together instead. Nothing is reserved and no height is
   * guessed: both requests are already in flight in parallel, so the wait is the
   * slower of two round trips rather than their sum, and what is drawn is drawn
   * once and correctly. Only the *first* paint waits — `result !== null` stays
   * true afterwards, so a refresh keeps the current list on screen exactly as
   * before.
   */
  const ready = result !== null && locationsSettled;

  /**
   * A household that has just signed up owns no location, and the API had no way
   * to create one — so this screen rendered an empty grey frame at the exact
   * moment a new user needs to be told what to do. It is a first-run state, not
   * a failure: `locationsError` is a different screen entirely.
   */
  const needsFirstLocation = ready && locationsError === null && locations.length === 0;

  if (!ready) {
    return (
      <section className={styles.screen} aria-labelledby="inventory-heading">
        <h1 id="inventory-heading" className="visually-hidden">
          Inventaire
        </h1>
        <LoadingRow label="Chargement de l’inventaire…" />
      </section>
    );
  }

  if (needsFirstLocation && items.length === 0) {
    return (
      <section className={styles.screen} aria-labelledby="inventory-heading">
        <h1 id="inventory-heading" className="visually-hidden">
          Inventaire
        </h1>
        <LocationSetup existing={locations} onCreated={onLocationCreated}>
          <div>
            <h2 className={styles.firstRunTitle}>Où rangez-vous vos courses ?</h2>
            <p className={styles.firstRunBody}>
              Chaudron range chaque article quelque part : un frigo, un congélateur, un placard.
              Créez-en un pour pouvoir ajouter votre premier article. Vous pourrez en ajouter
              d’autres à tout moment.
            </p>
          </div>
        </LocationSetup>
      </section>
    );
  }

  return (
    <section className={styles.screen} aria-labelledby="inventory-heading">
      <div className={styles.filters}>
        <h1 id="inventory-heading" className="visually-hidden">
          Inventaire
        </h1>

        <div className={styles.searchRow}>
          <label className="visually-hidden" htmlFor="inventory-search">
            Rechercher un article
          </label>
          <input
            id="inventory-search"
            className={styles.search}
            type="search"
            inputMode="search"
            placeholder="Rechercher un article…"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
            }}
          />
        </div>

        <ChipRow label="Filtrer par emplacement">
          <Chip
            active={locationId === null}
            onClick={() => {
              setLocationId(null);
            }}
          >
            Tous
          </Chip>
          {locations.map((location) => (
            <Chip
              key={location.id}
              active={locationId === location.id}
              onClick={() => {
                setLocationId(location.id);
              }}
            >
              {location.name} ({location.item_count})
            </Chip>
          ))}
        </ChipRow>

        <ChipRow label="Filtrer par péremption">
          <Chip
            active={expiringOnly}
            onClick={() => {
              setExpiringOnly((value) => !value);
            }}
          >
            Périme sous {SOON_DAYS} jours
          </Chip>
        </ChipRow>
      </div>

      {locationsError ? (
        <p className={styles.summary} role="status">
          Emplacements indisponibles : {locationsError}
        </p>
      ) : null}

      <p className={styles.summary} aria-live="polite" aria-busy={loading}>
        {loading
          ? 'Chargement de l’inventaire…'
          : error
            ? 'Inventaire indisponible.'
            : `${String(total)} article${total > 1 ? 's' : ''} ${filtered ? 'correspondants' : 'en stock'}.`}
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {error ? (
        <ErrorState
          message={error}
          onRetry={() => {
            setReloadNonce((value) => value + 1);
          }}
        />
      ) : items.length === 0 ? (
        filtered ? (
          <EmptyState
            title="Aucun article ne correspond"
            body="Modifiez la recherche ou retirez les filtres pour voir tout le stock."
            action={
              <Button variant="secondary" onClick={resetFilters}>
                Réinitialiser les filtres
              </Button>
            }
          />
        ) : (
          <EmptyState
            title="Votre stock est vide"
            body="Scannez un code-barres ou saisissez un article à la main pour commencer."
            action={
              <Button variant="primary" onClick={onAddItem}>
                Ajouter un article
              </Button>
            }
          />
        )
      ) : (
        <div className={styles.groups}>
          {groups.map((group) => (
            <section key={group.id} className={styles.group} aria-label={group.name}>
              <header className={styles.groupHeader}>
                <h2 className={styles.groupName}>{group.name}</h2>
                <span className={styles.groupKind}>{group.kindLabel}</span>
              </header>
              <ul className={styles.list}>
                {group.items.map((item) => (
                  <InventoryItemRow
                    key={item.id}
                    item={item}
                    onRemove={removeItem}
                    onAdjusted={adjustedItem}
                  />
                ))}
              </ul>
            </section>
          ))}

          {items.length < total ? (
            <div className={styles.footer}>
              <Button variant="secondary" loading={loadingMore} onClick={loadMore}>
                Charger plus ({String(items.length)} / {String(total)})
              </Button>
            </div>
          ) : null}
        </div>
      )}

      {depleted.length > 0 ? (
        <ShoppingProposal
          items={depleted}
          onDismiss={() => {
            setDepleted([]);
          }}
          onDecline={decline}
          onDone={setStatus}
        />
      ) : null}
    </section>
  );
}
