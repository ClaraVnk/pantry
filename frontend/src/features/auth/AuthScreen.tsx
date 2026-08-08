import { useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { login, register, type Session } from '../../api/auth';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import styles from './Auth.module.css';

type Mode = 'login' | 'register';

/** The shortest password the server accepts. Quoted so the form can say it first. */
const MIN_PASSWORD_LENGTH = 12;

interface Props {
  onSignedIn: (session: Session) => void;
  /** Set when a session ended under the user rather than at first load. */
  expired?: boolean;
}

/**
 * Sign in, or create an account and its first household.
 *
 * One screen with two modes rather than two routes: the application has no
 * router, and a person who mistyped their address should be one click from
 * creating the account rather than one navigation.
 *
 * **What is deliberately missing: "forgot my password".** Chaudron sends no
 * email at all — there is no SMTP configuration anywhere in the project — and a
 * reset flow that does not verify an address is an unauthenticated way to take
 * over an account. Offering a link that leads to an apology is worse than not
 * offering it, so the screen says what actually helps: another owner of the
 * household can invite the person again.
 */
export function AuthScreen({ onSignedIn, expired = false }: Props) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [householdName, setHouseholdName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      const session =
        mode === 'login'
          ? await login({ email, password })
          : await register({
              email,
              password,
              display_name: displayName,
              household_name: householdName,
            });
      onSignedIn(session);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setBusy(false);
    }
  };

  const registering = mode === 'register';

  return (
    <main className={styles.screen}>
      <div className={styles.brand}>
        <img src="/icon-192.png" alt="" width={48} height={48} />
        <h1 className={styles.title}>Chaudron</h1>
      </div>

      {expired ? (
        <Callout tone="warn" title="Session terminée">
          Votre session a pris fin. Reconnectez-vous pour reprendre où vous en étiez.
        </Callout>
      ) : null}

      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          // The handler is async and `onSubmit` wants void: floating the promise
          // explicitly says the rejection path is handled inside `submit`.
          event.preventDefault();
          void submit();
        }}
      >
        <h2 className={styles.heading}>{registering ? 'Créer un compte' : 'Se connecter'}</h2>

        {error ? (
          <Callout tone="danger" title="Connexion impossible">
            {error}
          </Callout>
        ) : null}

        <Field label="Adresse e-mail" required>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="email"
              name="email"
              autoComplete="email"
              inputMode="email"
              autoCapitalize="none"
              spellCheck={false}
              required
              value={email}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(event) => {
                setEmail(event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Mot de passe"
          required
          hint={registering ? `Au moins ${String(MIN_PASSWORD_LENGTH)} caractères.` : undefined}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              name="password"
              autoComplete={registering ? 'new-password' : 'current-password'}
              required
              minLength={registering ? MIN_PASSWORD_LENGTH : undefined}
              value={password}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
          )}
        </Field>

        {registering ? (
          <>
            <Field label="Votre nom" hint="Affiché dans le foyer.">
              {({ id, describedBy }) => (
                <input
                  id={id}
                  className={controlClass()}
                  type="text"
                  name="display-name"
                  autoComplete="name"
                  value={displayName}
                  aria-describedby={describedBy}
                  onChange={(event) => {
                    setDisplayName(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Nom du foyer" hint="Par exemple « Maison » ou « Coloc rue Verte ».">
              {({ id, describedBy }) => (
                <input
                  id={id}
                  className={controlClass()}
                  type="text"
                  name="household-name"
                  value={householdName}
                  aria-describedby={describedBy}
                  onChange={(event) => {
                    setHouseholdName(event.target.value);
                  }}
                />
              )}
            </Field>
          </>
        ) : null}

        <Button type="submit" variant="primary" block loading={busy}>
          {registering ? 'Créer le compte' : 'Se connecter'}
        </Button>

        <p className={styles.switch}>
          {registering ? 'Vous avez déjà un compte ?' : 'Pas encore de compte ?'}{' '}
          <button
            type="button"
            className={styles.link}
            onClick={() => {
              setMode(registering ? 'login' : 'register');
              setError(null);
            }}
          >
            {registering ? 'Se connecter' : 'Créer un compte'}
          </button>
        </p>
      </form>

      <p className={styles.note}>
        Mot de passe oublié ? Chaudron n’envoie aucun e-mail, il n’y a donc pas de réinitialisation
        automatique. Demandez au propriétaire de votre foyer de vous réinviter.
      </p>
    </main>
  );
}

/** Server problems, turned into something a person can act on. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    switch (cause.problemType) {
      case 'invalid-credentials':
        return 'Adresse e-mail ou mot de passe incorrect.';
      case 'email-already-registered':
        return 'Un compte existe déjà pour cette adresse. Connectez-vous.';
      case 'password-too-weak':
        return `Le mot de passe doit faire au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`;
      case 'invalid-email':
        return 'Cette adresse e-mail n’est pas valide.';
      case 'rate-limited':
        return cause.retryAfterSeconds
          ? `Trop de tentatives. Réessayez dans ${String(Math.ceil(cause.retryAfterSeconds / 60))} minute(s).`
          : 'Trop de tentatives. Réessayez plus tard.';
      case 'validation-failed':
        return `Le mot de passe doit faire au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`;
      default:
        break;
    }
  }
  return describeError(cause);
}
