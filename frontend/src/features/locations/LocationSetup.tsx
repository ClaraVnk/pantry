import { useState, type FormEvent, type ReactNode } from 'react';
import { ApiError, describeError } from '../../api/client';
import { createLocation } from '../../api/endpoints';
import type { LocationKind, StorageLocation } from '../../api/types';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { locationKindLabel } from '../../lib/expiry';
import styles from './LocationSetup.module.css';

/**
 * The three names most French households would type first, each already paired
 * with the `kind` that decides how expiry is treated — a freezer suspends it, a
 * cupboard does not.
 *
 * These are *suggestions*, not seeds. Nothing is created until the user taps
 * one, which is the whole difference: the server creates no default location at
 * registration precisely because there is no way to delete one afterwards (see
 * the module docstring of `api/routers/locations.py`). Offering the same three
 * names here costs one tap and leaves the household with exactly the rows it
 * chose.
 */
const SUGGESTIONS: { name: string; kind: LocationKind }[] = [
  { name: 'Frigo', kind: 'fridge' },
  { name: 'Congélateur', kind: 'freezer' },
  { name: 'Placard', kind: 'pantry' },
];

const KINDS: LocationKind[] = ['fridge', 'freezer', 'pantry', 'cellar', 'other'];

interface Props {
  /** Names already taken, so a suggestion that would collide is not offered. */
  existing: StorageLocation[];
  onCreated: (location: StorageLocation) => void;
  /** Rendered above the form; the inventory screen uses it to explain itself. */
  children?: ReactNode;
}

/**
 * Creates the first storage location, or another one.
 *
 * Used by the inventory screen (where a household with none would otherwise see
 * an empty frame) and by the add-item screen (where a missing location used to
 * be a dead end telling the user to go and fix it "côté serveur").
 */
export function LocationSetup({ existing, onCreated, children }: Props) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState<LocationKind>('fridge');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');

  const taken = new Set(existing.map((location) => location.name.toLocaleLowerCase('fr')));
  const offered = SUGGESTIONS.filter((entry) => !taken.has(entry.name.toLocaleLowerCase('fr')));

  const submit = (draftName: string, draftKind: LocationKind) => {
    const trimmed = draftName.trim();
    if (trimmed === '') {
      setError('Donnez un nom à cet emplacement.');
      return;
    }

    setBusy(true);
    setError(null);
    createLocation({ name: trimmed, kind: draftKind })
      .then((location) => {
        setName('');
        setStatus(`Emplacement « ${location.name} » créé.`);
        onCreated(location);
      })
      .catch((cause: unknown) => {
        // The one failure the user can act on, and the only one worth its own
        // sentence: everything else is reported as the server described it.
        if (cause instanceof ApiError && cause.problemType === 'location-name-taken') {
          setError('Un emplacement porte déjà ce nom. Choisissez-en un autre.');
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submit(name, kind);
  };

  return (
    <div className={styles.setup}>
      {children}

      {offered.length > 0 ? (
        <div className={styles.suggestions} role="group" aria-label="Emplacements courants">
          {offered.map((entry) => (
            <Button
              key={entry.name}
              variant="primary"
              disabled={busy}
              onClick={() => {
                submit(entry.name, entry.kind);
              }}
            >
              Créer « {entry.name} »
            </Button>
          ))}
        </div>
      ) : null}

      <form className={styles.form} onSubmit={onSubmit}>
        <p className={styles.formLead}>Ou nommez-le vous-même :</p>

        <Field label="Nom de l’emplacement" required>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              aria-describedby={describedBy}
              aria-invalid={invalid}
              className={controlClass(invalid)}
              type="text"
              autoComplete="off"
              maxLength={80}
              placeholder="Cellier, garage, cave…"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Type"
          hint="Un congélateur suspend les dates de péremption ; un placard les compte."
        >
          {({ id, describedBy }) => (
            <select
              id={id}
              aria-describedby={describedBy}
              className={controlClass()}
              value={kind}
              onChange={(event) => {
                setKind(event.target.value as LocationKind);
              }}
            >
              {KINDS.map((value) => (
                <option key={value} value={value}>
                  {locationKindLabel(value)}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Button type="submit" variant="secondary" loading={busy}>
          Créer l’emplacement
        </Button>
      </form>

      {error ? (
        <Callout tone="danger" title="Création impossible">
          <p>{error}</p>
        </Callout>
      ) : null}

      <p className="visually-hidden" role="status">
        {status}
      </p>
    </div>
  );
}
