import { useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/client';
import { MACHINE_TOKEN_SCOPES } from '../../api/types';
import type { MachineTokenCreated, MachineTokenScope } from '../../api/types';
import { Badge, Button, Callout, Checkbox, Field, Fieldset, LoadingRow } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useMachineTokens } from './useMachineTokens';
import styles from './Household.module.css';

/** What each scope actually authorises, said the way the contract says it. */
const SCOPE_LABELS: Record<MachineTokenScope, string> = {
  'inventory:read': 'Lire le stock, les emplacements et les péremptions',
  'inventory:write': 'Ajouter, corriger et retirer du stock',
  'shopping:read': 'Lire la liste de courses en cours',
  'shopping:write': 'Ajouter, cocher et retirer des articles de la liste',
  'budget:read': 'Lire la dépense et l’objectif de budget',
};

/**
 * The expiry choices, and « sans expiration » is one of them.
 *
 * The contract allows a token that never expires, and a form that hid the option
 * would push people towards a one-day-per-year renewal they will forget — after
 * which the integration stops and nobody remembers why. Naming it plainly, next
 * to the fact that revocation is one button away, is the honest version.
 */
const EXPIRY_CHOICES: { value: string; days: number | null; label: string }[] = [
  { value: '30', days: 30, label: '30 jours' },
  { value: '90', days: 90, label: '90 jours' },
  { value: '365', days: 365, label: 'Un an' },
  { value: 'never', days: null, label: 'Sans expiration' },
];

const DATE_FORMAT: Intl.DateTimeFormatOptions = {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
};

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('fr-CH', DATE_FORMAT);
}

/**
 * The screen where a household issues, inspects and revokes machine tokens.
 *
 * Three rules shape it, and each one is a rule the server enforces — this screen
 * states them, it does not implement them.
 *
 * **The value is shown once.** The creation response is the only one that carries
 * it, and no route returns it afterwards. So the panel puts it in a field the
 * moment it arrives, with a copy button and a warning that says plainly it will
 * not be shown again. Dismissing that box is irreversible, which is why the
 * button that dismisses it says so.
 *
 * **A token opens one household, and only part of it.** The scopes are ticked one
 * by one, none is ticked by default, and the form refuses to submit with none —
 * the same refusal the server makes. Recipe suggestions and household members are
 * absent from the list because no scope reaches them at all: the first spends
 * money on every call, the second carries allergens and infant age bands.
 *
 * **Revoking is immediate and needs nothing else.** No password, no rotation of
 * anything, no effect on the other tokens. That is the whole reason a machine
 * token exists rather than an integration holding an account password.
 */
export function MachineTokenPanel() {
  const state = useMachineTokens();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<MachineTokenScope[]>([]);
  const [expiry, setExpiry] = useState('365');
  const [nameError, setNameError] = useState<string | null>(null);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [issued, setIssued] = useState<MachineTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [status, setStatus] = useState('');

  const resetForm = () => {
    setName('');
    setScopes([]);
    setExpiry('365');
    setNameError(null);
    setScopeError(null);
    setSubmitError(null);
  };

  const toggleScope = (scope: MachineTokenScope, checked: boolean) => {
    setScopes((current) =>
      checked ? [...current, scope] : current.filter((held) => held !== scope),
    );
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);

    const cleaned = name.trim();
    const nextNameError =
      cleaned === '' ? 'Donnez un nom, pour reconnaître ce jeton plus tard.' : null;
    const nextScopeError =
      scopes.length === 0
        ? 'Cochez au moins une autorisation : sans elles, le jeton ne peut rien.'
        : null;
    setNameError(nextNameError);
    setScopeError(nextScopeError);
    if (nextNameError !== null || nextScopeError !== null) return;

    const chosen = EXPIRY_CHOICES.find((choice) => choice.value === expiry);
    setSaving(true);
    state
      .create({ name: cleaned, scopes, expires_in_days: chosen?.days ?? null })
      .then((created) => {
        setIssued(created);
        setCopied(false);
        setCopyFailed(false);
        setCreating(false);
        resetForm();
        setStatus('Le jeton est créé. Copiez-le maintenant : il ne sera plus jamais affiché.');
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.problemType === 'token-limit-reached') {
          setSubmitError(
            'Ce foyer a déjà le nombre maximum de jetons. Révoquez-en un qui ne sert plus.',
          );
          return;
        }
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const copy = (value: string) => {
    // `navigator.clipboard` is absent outside a secure context and can be
    // refused by permission. The value is in a readable field either way, so a
    // failure is told plainly rather than swallowed into a button that silently
    // did nothing.
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        setCopyFailed(false);
        setStatus('Le jeton est copié dans le presse-papiers.');
      })
      .catch(() => {
        setCopied(false);
        setCopyFailed(true);
      });
  };

  const revoke = (id: string, tokenName: string) => {
    setRevoking(id);
    setSubmitError(null);
    state
      .revoke(id)
      .then(() => {
        setStatus(`Le jeton « ${tokenName} » est révoqué. Il ne fonctionne plus.`);
      })
      .catch((cause: unknown) => {
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setRevoking(null);
      });
  };

  if (state.unsupported) return null;

  return (
    <section className={styles.card} aria-labelledby="machine-tokens-heading">
      <div className={styles.cardHead}>
        <h2 className={styles.formHeading} id="machine-tokens-heading">
          Jetons d’accès machine
        </h2>
        {state.tokens.length > 0 ? <Badge tone="neutral">{state.tokens.length}</Badge> : null}
      </div>

      <p className={styles.lead}>
        Un jeton permet à un logiciel — Home Assistant, un script, un tableau de bord — de lire ou
        de modifier une partie de ce foyer, sans jamais connaître votre mot de passe. Il n’ouvre que
        ce foyer, seulement ce que vous cochez, et vous pouvez le révoquer à tout moment.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {issued !== null ? (
        <div className={styles.tokenReveal} aria-live="assertive">
          <Callout tone="warn" title="Copiez ce jeton maintenant">
            <p>
              C’est la seule fois où il s’affiche. Chaudron n’en garde qu’une empreinte : ni cet
              écran ni l’API ne pourront vous le remontrer. Si vous le perdez, révoquez-le et
              créez-en un autre.
            </p>
          </Callout>

          <Field label={`Jeton « ${issued.name} »`}>
            {({ id, describedBy }) => (
              <input
                id={id}
                className={controlClass()}
                type="text"
                readOnly
                value={issued.token}
                aria-describedby={describedBy}
                spellCheck={false}
                onFocus={(event) => {
                  event.target.select();
                }}
              />
            )}
          </Field>

          {copyFailed ? (
            <p className={styles.consentNote}>
              La copie automatique a été refusée par le navigateur. Sélectionnez le champ ci-dessus
              et copiez-le à la main.
            </p>
          ) : null}

          <div className={styles.cardActions}>
            <Button
              variant="primary"
              onClick={() => {
                copy(issued.token);
              }}
            >
              {copied ? 'Copié' : 'Copier le jeton'}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setIssued(null);
                setCopied(false);
                setCopyFailed(false);
              }}
            >
              J’ai copié le jeton, masquer
            </Button>
          </div>
        </div>
      ) : null}

      {state.loading && state.tokens.length === 0 ? (
        <LoadingRow label="Chargement des jetons…" />
      ) : state.error !== null ? (
        <Callout tone="warn" title="Impossible de lire les jetons">
          <p>{state.error}</p>
          <div className={styles.cardActions}>
            <Button variant="secondary" onClick={state.reload}>
              Réessayer
            </Button>
          </div>
        </Callout>
      ) : state.tokens.length === 0 ? (
        <p className={styles.consentNote}>
          Aucun jeton n’est actif. Tant qu’il n’y en a pas, aucun logiciel tiers ne peut lire ce
          foyer.
        </p>
      ) : (
        <ul className={styles.list}>
          {state.tokens.map((token) => (
            <li key={token.id} className={styles.tokenRow}>
              <div className={styles.cardHead}>
                <span className={styles.cardName}>{token.name}</span>
                <code className={styles.tokenTail}>
                  {token.prefix}…{token.last4}
                </code>
              </div>

              <ul className={styles.scopeList}>
                {token.scopes.map((scope) => (
                  <li key={scope}>{SCOPE_LABELS[scope]}</li>
                ))}
              </ul>

              <div className={styles.cardFacts}>
                <span className={styles.cardLine}>
                  <span className={styles.cardLabel}>Créé le</span>
                  {formatDate(token.created_at)}
                </span>
                <span className={styles.cardLine}>
                  <span className={styles.cardLabel}>Dernière utilisation</span>
                  {token.last_used_at === null ? 'Jamais utilisé' : formatDate(token.last_used_at)}
                </span>
                <span className={styles.cardLine}>
                  <span className={styles.cardLabel}>Expiration</span>
                  {token.expires_at === null ? 'Sans expiration' : formatDate(token.expires_at)}
                </span>
              </div>

              <div className={styles.cardActions}>
                <Button
                  variant="danger"
                  loading={revoking === token.id}
                  onClick={() => {
                    revoke(token.id, token.name);
                  }}
                >
                  Révoquer
                  <span className="visually-hidden"> le jeton {token.name}</span>
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {submitError !== null && !creating ? (
        <Callout tone="danger" title="Action impossible">
          <p>{submitError}</p>
        </Callout>
      ) : null}

      {creating ? (
        <form className={styles.form} onSubmit={submit} noValidate>
          <Field
            label="Nom du jeton"
            required
            error={nameError}
            hint="Ce que vous lirez dans la liste : « Home Assistant », « Tableau de bord salon »."
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                className={controlClass(invalid)}
                type="text"
                autoComplete="off"
                maxLength={120}
                value={name}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
            )}
          </Field>

          <Fieldset
            legend="Ce que ce jeton pourra faire"
            error={scopeError}
            hint="Rien n’est coché par défaut, et rien n’est implicite : autoriser l’écriture n’autorise pas la lecture. Aucune case ne donne accès aux suggestions de recettes ni aux membres du foyer."
          >
            <div className={styles.scopeGrid}>
              {MACHINE_TOKEN_SCOPES.map((scope) => (
                <Checkbox
                  key={scope}
                  checked={scopes.includes(scope)}
                  onChange={(checked) => {
                    toggleScope(scope, checked);
                  }}
                  detail={<code className={styles.scopeCode}>{scope}</code>}
                >
                  {SCOPE_LABELS[scope]}
                </Checkbox>
              ))}
            </div>
          </Fieldset>

          <Field
            label="Expiration"
            hint="Un jeton expiré est refusé comme un jeton inconnu. Vous pouvez le révoquer avant, quelle que soit l’échéance."
          >
            {({ id, describedBy, invalid }) => (
              <select
                id={id}
                className={controlClass(invalid)}
                value={expiry}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setExpiry(event.target.value);
                }}
              >
                {EXPIRY_CHOICES.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            )}
          </Field>

          {submitError !== null ? (
            <Callout tone="danger" title="Création impossible">
              <p>{submitError}</p>
            </Callout>
          ) : null}

          <div className={styles.formActions}>
            <Button type="submit" variant="primary" loading={saving}>
              Créer le jeton
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setCreating(false);
                resetForm();
              }}
            >
              Annuler
            </Button>
          </div>
        </form>
      ) : (
        <Button
          variant="secondary"
          block
          onClick={() => {
            resetForm();
            setCreating(true);
          }}
        >
          Créer un jeton
        </Button>
      )}
    </section>
  );
}
