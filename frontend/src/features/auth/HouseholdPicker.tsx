import type { HouseholdSummary } from '../../api/auth';
import { Button } from '../../components/ui';
import styles from './Auth.module.css';

const ROLE_LABELS: Record<HouseholdSummary['role'], string> = {
  owner: 'Propriétaire',
  member: 'Membre',
  viewer: 'Lecture seule',
};

interface Props {
  households: HouseholdSummary[];
  onSelect: (id: string) => void;
  onSignOut: () => void;
}

/**
 * Shown when an account belongs to several households and none is chosen yet.
 *
 * An account may be in a family home *and* a flatshare — that is written into
 * `UserAccount` on the server — so the server refuses to guess and answers `409
 * household-selection-required` when a request names none. This is the screen
 * that answers it. Picking one is remembered locally
 * (`lib/activeHousehold.ts`), so the question is asked once per browser rather
 * than once per load.
 */
export function HouseholdPicker({ households, onSelect, onSignOut }: Props) {
  return (
    <main className={styles.screen}>
      <div className={styles.brand}>
        <img src="/icon-192.png" alt="" width={48} height={48} />
        <h1 className={styles.title}>Chaudron</h1>
      </div>

      <div className={styles.form}>
        <h2 className={styles.heading}>Quel foyer ouvrir ?</h2>
        <p className={styles.note}>
          Votre compte appartient à plusieurs foyers. Vous pourrez en changer à tout moment depuis
          le menu de votre compte, en haut à droite.
        </p>

        <ul className={styles.choices}>
          {households.map((household) => (
            <li key={household.id}>
              <button
                type="button"
                className={styles.choice}
                onClick={() => {
                  onSelect(household.id);
                }}
              >
                <span className={styles.choiceName}>{household.name}</span>
                <span className={styles.choiceRole}>{ROLE_LABELS[household.role]}</span>
              </button>
            </li>
          ))}
        </ul>

        <Button variant="ghost" block onClick={onSignOut}>
          Se déconnecter
        </Button>
      </div>
    </main>
  );
}
