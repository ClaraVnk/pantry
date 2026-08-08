import { useId, useState } from 'react';
import { useSession } from '../../context/sessionContext';
import styles from './AccountMenu.module.css';

/**
 * Who is signed in, which household is open, and the way out.
 *
 * A `<details>` rather than a hand-built dropdown: it opens on click and on
 * Enter, closes on Escape, is focusable and labelled without a line of
 * JavaScript, and needs no outside-click handler. The household switcher lives
 * here because it is the same question as "who am I" — an account in two
 * households is one person looking at one of them.
 */
export function AccountMenu() {
  const { session, activeHousehold, selectHousehold, signOut } = useSession();
  const [busy, setBusy] = useState(false);
  const id = useId();

  if (!session) return null;

  const others = session.households.filter((entry) => entry.id !== activeHousehold?.id);

  return (
    <details className={styles.menu}>
      <summary className={styles.summary} aria-label="Compte et foyer">
        <span className={styles.avatar} aria-hidden="true">
          {initial(session.display_name || session.email)}
        </span>
      </summary>

      <div className={styles.panel} role="group" aria-labelledby={id}>
        <p className={styles.identity} id={id}>
          <span className={styles.name}>{session.display_name || session.email}</span>
          <span className={styles.email}>{session.email}</span>
        </p>

        {activeHousehold ? <p className={styles.current}>Foyer : {activeHousehold.name}</p> : null}

        {others.length > 0 ? (
          <>
            <p className={styles.label}>Changer de foyer</p>
            <ul className={styles.list}>
              {others.map((household) => (
                <li key={household.id}>
                  <button
                    type="button"
                    className={styles.item}
                    onClick={() => {
                      selectHousehold(household.id);
                    }}
                  >
                    {household.name}
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        <button
          type="button"
          className={styles.signOut}
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void signOut().finally(() => {
              setBusy(false);
            });
          }}
        >
          Se déconnecter
        </button>
      </div>
    </details>
  );
}

function initial(label: string): string {
  return label.trim().charAt(0).toUpperCase() || '?';
}
