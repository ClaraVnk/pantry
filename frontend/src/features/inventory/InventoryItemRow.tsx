import { useState } from 'react';
import type { InventoryItem, RemovalReason, UpdatedInventoryItem } from '../../api/types';
import { Badge, Button } from '../../components/ui';
import { unitSymbol } from '../../lib/units';
import {
  expiryKindShortLabel,
  expiryLevel,
  formatAmount,
  formatDate,
  formatExpiryRelative,
} from '../../lib/expiry';
import { QuantityAdjuster } from './QuantityAdjuster';
import styles from './Inventory.module.css';

interface Props {
  item: InventoryItem;
  onRemove: (id: string, reason: RemovalReason) => Promise<void>;
  onAdjusted: (item: UpdatedInventoryItem) => void;
}

type Mode = 'idle' | 'removing' | 'adjusting';

export function InventoryItemRow({ item, onRemove, onAdjusted }: Props) {
  const [mode, setMode] = useState<Mode>('idle');
  const [busy, setBusy] = useState(false);

  const level = expiryLevel(item.expires_on);
  const relative = formatExpiryRelative(item.expires_on);
  const rowClass = [
    styles.item,
    level === 'expired' ? styles.itemExpired : '',
    level === 'critical' || level === 'soon' ? styles.itemExpiring : '',
  ]
    .filter(Boolean)
    .join(' ');

  const remove = (reason: RemovalReason) => {
    setBusy(true);
    void onRemove(item.id, reason).finally(() => {
      setBusy(false);
      setMode('idle');
    });
  };

  return (
    <li className={rowClass}>
      <div className={styles.itemMain}>
        <div className={styles.itemText}>
          <span className={styles.itemName}>{item.product.name}</span>
          {item.product.brand ? (
            <span className={styles.itemBrand}>{item.product.brand}</span>
          ) : null}
        </div>
        <span className={styles.itemQuantity}>
          {formatAmount(item.quantity.amount)}
          <span aria-hidden="true"> </span>
          {unitSymbol(item.quantity.unit)}
        </span>
      </div>

      <div className={styles.itemMeta}>
        {item.expires_on ? (
          <>
            <span>
              {expiryKindShortLabel(item.expiry_kind)} {formatDate(item.expires_on)}
            </span>
            {relative ? (
              <Badge tone={level === 'expired' ? 'danger' : 'warn'}>{relative}</Badge>
            ) : null}
          </>
        ) : (
          <span>Sans date</span>
        )}
        {item.opened_at ? <Badge tone="neutral">entamé</Badge> : null}
      </div>

      {mode === 'adjusting' ? (
        <QuantityAdjuster
          item={item}
          onSaved={(updated) => {
            onAdjusted(updated);
            setMode('idle');
          }}
          onCancel={() => {
            setMode('idle');
          }}
        />
      ) : mode === 'removing' ? (
        <div className={styles.itemActions}>
          <Button
            variant="secondary"
            loading={busy}
            onClick={() => {
              remove('consumed');
            }}
          >
            Consommé
          </Button>
          <Button
            variant="danger"
            loading={busy}
            onClick={() => {
              remove('wasted');
            }}
          >
            Jeté
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setMode('idle');
            }}
          >
            Annuler
          </Button>
        </div>
      ) : (
        <div className={styles.itemActions}>
          <Button
            variant="ghost"
            onClick={() => {
              setMode('adjusting');
            }}
          >
            Ajuster
            <span className="visually-hidden"> la quantité de {item.product.name}</span>
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setMode('removing');
            }}
          >
            Retirer du stock
            <span className="visually-hidden"> — {item.product.name}</span>
          </Button>
        </div>
      )}
    </li>
  );
}
