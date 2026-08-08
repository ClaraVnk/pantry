import { useState, type FormEvent } from 'react';
import { changePassword, revokeAllSessions } from '../../api/auth';
import { ApiError, describeError } from '../../api/client';
import { controlClass } from '../../components/controlClass';
import { Button, Callout, Field } from '../../components/ui';
import { useSession } from '../../context/sessionContext';
import styles from './Household.module.css';

/** Matches the server's `MIN_PASSWORD_LENGTH`, so a typo is caught before a round trip. */
const PASSWORD_MIN = 12;

/**
 * The two things a person can do when they think their session has been copied.
 *
 * Until this panel existed there was nothing at all: no "sign out everywhere", no
 * way to change a password, and therefore no remedy short of waiting thirty days
 * for the cookie to expire or asking whoever runs the server to open a database
 * console. The server-side lever had been written and was reachable from nothing.
 *
 * Three things about this screen are deliberate.
 *
 * **Both actions end every session, including this one, and both hand back a new
 * one.** The interface says so before the button rather than after, because the
 * behaviour is surprising until it is explained: a stolen cookie is not a second
 * session, it is a copy of the one in this browser, so sparing it to "stay signed
 * in" would spare exactly the credential the user came here to kill. Rotating
 * instead keeps them where they are.
 *
 * **The current password is required, and the copy says why.** There is no
 * outbound mail anywhere in Chaudron, so there is no reset link and nothing else
 * that can stand in for "you are this person". Saying that plainly is better than
 * a "forgot your password?" link that leads to an apology.
 *
 * **Machine tokens are not touched, and the screen says so** — they are listed
 * and revoked in the panel below this one. Signing every browser out because a
 * laptop was lost should not also unplug the household's Home Assistant.
 */
export function AccountSecurityPanel() {
  const { session, adopt } = useSession();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);

  if (!session) return null;

  const reset = () => {
    // Dropped from memory as soon as the submission ends: neither value has any
    // reason to outlive it, and the server never hands either back.
    setCurrent('');
    setNext('');
    setConfirmation('');
    setFieldError(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);

    if (next.length < PASSWORD_MIN) {
      setFieldError(`Choisissez un mot de passe d’au moins ${String(PASSWORD_MIN)} caractères.`);
      return;
    }
    if (next !== confirmation) {
      setFieldError('Les deux saisies du nouveau mot de passe ne correspondent pas.');
      return;
    }
    setFieldError(null);

    setSaving(true);
    changePassword({ current_password: current, new_password: next })
      .then((rotated) => {
        // Adopting is not optional: the response carries a new CSRF token, and a
        // client that kept the old one would be refused on its next write.
        adopt(rotated);
        setStatus(
          'Le mot de passe a été changé. Toutes les autres sessions ont été fermées ; celle-ci reste ouverte.',
        );
        reset();
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.problemType === 'current-password-invalid') {
          setFormError('Le mot de passe actuel ne correspond pas. Rien n’a été changé.');
          return;
        }
        setFormError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const revokeEverything = () => {
    setRevoking(true);
    setFormError(null);
    revokeAllSessions()
      .then((rotated) => {
        adopt(rotated);
        setStatus(
          'Toutes les sessions ont été fermées, y compris celle qui a servi à le demander. Cet appareil a reçu une session neuve.',
        );
        setConfirmingRevoke(false);
      })
      .catch((cause: unknown) => {
        setFormError(describeError(cause));
      })
      .finally(() => {
        setRevoking(false);
      });
  };

  return (
    <section className={styles.card} aria-labelledby="account-security-heading">
      <div className={styles.cardHead}>
        <h2 className={styles.formHeading} id="account-security-heading">
          Sécurité du compte
        </h2>
      </div>

      <p className={styles.lead}>
        Si vous pensez que quelqu’un a récupéré votre session — un ordinateur partagé, un téléphone
        perdu — ces deux actions sont ce qui la coupe. Les deux ferment{' '}
        <strong>toutes vos sessions</strong>, y compris celle de cet appareil : un cookie volé est
        une copie de celle-ci, l’épargner reviendrait à épargner la copie. Cet appareil reçoit
        aussitôt une session neuve, vous n’avez donc pas à vous reconnecter.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      <p className={styles.cardLine}>
        <span className={styles.cardLabel}>Compte</span>
        {session.email}
      </p>

      {formError !== null ? (
        <Callout tone="danger" title="Opération impossible">
          <p>{formError}</p>
        </Callout>
      ) : null}

      <form className={styles.form} onSubmit={submit} noValidate>
        <Field
          label="Mot de passe actuel"
          required
          hint="Il est demandé parce que Chaudron n’envoie aucun courriel : il n’y a pas de lien de réinitialisation, et c’est la seule preuve que c’est bien vous."
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              autoComplete="current-password"
              value={current}
              aria-describedby={describedBy}
              onChange={(event) => {
                setCurrent(event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Nouveau mot de passe"
          required
          error={fieldError}
          hint={`Au moins ${String(PASSWORD_MIN)} caractères. Une phrase dont vous vous souvenez vaut mieux qu’un mot court et compliqué.`}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              autoComplete="new-password"
              value={next}
              aria-describedby={describedBy}
              onChange={(event) => {
                setNext(event.target.value);
              }}
            />
          )}
        </Field>

        <Field label="Confirmer le nouveau mot de passe" required>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              autoComplete="new-password"
              value={confirmation}
              aria-describedby={describedBy}
              onChange={(event) => {
                setConfirmation(event.target.value);
              }}
            />
          )}
        </Field>

        <div className={styles.formActions}>
          <Button
            type="submit"
            variant="primary"
            loading={saving}
            disabled={current === '' || next === ''}
          >
            Changer le mot de passe
          </Button>
        </div>
      </form>

      <Callout tone="warn" title="Fermer toutes les sessions">
        <p>
          Ferme la session de tous vos appareils, y compris cet onglet, qui reçoit immédiatement une
          session neuve. Vos jetons de programme (ci-dessous) ne sont <strong>pas</strong> concernés
          : ils se révoquent un par un, pour qu’un ordinateur perdu ne débranche pas les
          intégrations du foyer.
        </p>
        {confirmingRevoke ? (
          <div className={styles.cardActions}>
            <Button variant="danger" loading={revoking} onClick={revokeEverything}>
              Fermer toutes les sessions
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setConfirmingRevoke(false);
              }}
            >
              Annuler
            </Button>
          </div>
        ) : (
          <div className={styles.cardActions}>
            <Button
              variant="secondary"
              onClick={() => {
                setConfirmingRevoke(true);
              }}
            >
              Me déconnecter partout
            </Button>
          </div>
        )}
      </Callout>
    </section>
  );
}
