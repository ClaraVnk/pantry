import { useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/client';
import { Badge, Button, Callout, Checkbox, Field, LoadingRow } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useSession } from '../../context/sessionContext';
import { useExportTarget } from './useExportTarget';
import styles from './Household.module.css';

/** The one destination this build ships (ADR-0010 §4-5). */
const TODOIST = 'todoist';

/** Matches the server's `_LIST_ID_PATTERN`, so a typo is caught before a round trip. */
const PROJECT_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** Matches the server's `MIN_API_KEY_LENGTH`. */
const TOKEN_MIN = 8;

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('fr-CH', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
}

/**
 * What leaves the instance, said in full, next to the box that authorises it.
 *
 * Kept as its own component because it has to appear in two places — beside the
 * consent checkbox when the agreement is being given, and in the summary when it
 * has been — and a consent notice that drifts between the two is a consent
 * notice that was not really read.
 */
function WhatLeaves() {
  return (
    <ul className={styles.consentList}>
      <li>le nom de chaque article restant à acheter, et sa quantité ;</li>
      <li>
        rien d’autre : ni code-barres, ni date de péremption, ni allergène, ni identifiant de foyer,
        ni membre — même quand Chaudron les connaît ;
      </li>
      <li>les articles déjà cochés ne partent pas ;</li>
      <li>
        l’envoi n’a lieu que quand vous appuyez sur « Envoyer vers Todoist », jamais tout seul.
      </li>
    </ul>
  );
}

/**
 * Registering, inspecting and withdrawing this household's export destination.
 *
 * Three rules shape this screen, and each one is a rule the server also enforces
 * — the interface states them, it does not implement them.
 *
 * **The agreement is a separate gesture from the token.** Pasting a credential
 * says "I have a Todoist account"; ticking the box says "send this household's
 * shopping list to it". The box starts unticked, is never ticked on the user's
 * behalf, and sits next to a plain-language list of exactly what will leave the
 * instance. Todoist is an American service, and a shopping list says what
 * identifiable people eat — which in a household with an allergy or an
 * observance is health or religious data.
 *
 * **The token is never shown again.** The server does not return it in any form;
 * the last four characters are what comes back, so someone with two accounts can
 * tell which key is installed. Changing it means pasting a new one, which is also
 * how it is rotated.
 *
 * **Withdrawing keeps the record.** The row survives, so this screen keeps
 * showing what was authorised and when it stopped — an agreement that vanished
 * without trace would leave nobody able to answer "what did you send, and on
 * whose say-so?".
 */
export function ExportTargetPanel() {
  const state = useExportTarget(TODOIST);
  const { activeHousehold } = useSession();
  // Registering accepts a third party's credential and dates an agreement in the
  // household's name; withdrawing takes it back. The server refuses both to
  // anyone but an owner, and this hides the form rather than letting a member
  // fill it in and meet a 403 at the end. The check is a courtesy — the refusal
  // that matters is the server's.
  const isOwner = activeHousehold?.role === 'owner';
  const [token, setToken] = useState('');
  const [projectId, setProjectId] = useState('');
  const [consent, setConsent] = useState(false);
  const [editing, setEditing] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);

  const registered = state.target;
  const showForm = isOwner && (editing || registered === null);

  const resetForm = () => {
    // The token is dropped from memory as soon as the form closes: it has no
    // reason to outlive the submission, and the server will never hand it back.
    setToken('');
    setProjectId('');
    setConsent(false);
    setTokenError(null);
    setProjectError(null);
    setSubmitError(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);

    const cleaned = token.trim();
    const project = projectId.trim();
    const nextTokenError = cleaned.length < TOKEN_MIN ? 'Collez le jeton Todoist en entier.' : null;
    const nextProjectError =
      project !== '' && !PROJECT_ID_PATTERN.test(project)
        ? 'Un identifiant de projet Todoist ne contient que lettres, chiffres, tirets et soulignés.'
        : null;
    setTokenError(nextTokenError);
    setProjectError(nextProjectError);
    if (nextTokenError !== null || nextProjectError !== null) return;

    setSaving(true);
    state
      .register({
        token: cleaned,
        consent_granted: consent,
        ...(project === '' ? {} : { external_list_id: project }),
      })
      .then(() => {
        setStatus('La destination Todoist est enregistrée et autorisée.');
        setEditing(false);
        resetForm();
      })
      .catch((cause: unknown) => {
        // The server's own sentence is English and describes an API; these are
        // the two refusals a person can act on, so they are said in French.
        if (cause instanceof ApiError && cause.problemType === 'export-consent-required') {
          setSubmitError('Cochez l’autorisation : sans elle, rien n’est enregistré.');
          return;
        }
        if (cause instanceof ApiError && cause.problemType === 'export-token-invalid') {
          setSubmitError('Ce jeton est trop court pour être un jeton Todoist.');
          return;
        }
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const revoke = () => {
    setRevoking(true);
    setSubmitError(null);
    state
      .revoke()
      .then(() => {
        setStatus('L’autorisation a été retirée. Plus rien ne part vers Todoist.');
      })
      .catch((cause: unknown) => {
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setRevoking(false);
      });
  };

  if (state.unsupported) return null;

  return (
    <section className={styles.card} aria-labelledby="export-target-heading">
      <div className={styles.cardHead}>
        <h2 className={styles.formHeading} id="export-target-heading">
          Envoi de la liste de courses
        </h2>
        {registered !== null ? (
          <Badge tone={registered.is_consented && registered.registrant_is_member ? 'ok' : 'warn'}>
            {!registered.is_consented
              ? 'Retiré'
              : registered.registrant_is_member
                ? 'Autorisé'
                : 'À revoir'}
          </Badge>
        ) : null}
      </div>

      <p className={styles.lead}>
        Chaudron peut déposer votre liste de courses dans Todoist. C’est facultatif, et tant que
        vous n’enregistrez rien ici, aucune liste ne quitte cette instance.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {state.loading ? <LoadingRow label="Chargement de la destination…" /> : null}

      {state.error !== null ? (
        <Callout tone="warn" title="Impossible de lire la destination">
          <p>{state.error}</p>
          <div className={styles.cardActions}>
            <Button variant="secondary" onClick={state.reload}>
              Réessayer
            </Button>
          </div>
        </Callout>
      ) : null}

      {registered !== null && !showForm ? (
        <>
          <p className={styles.cardLine}>
            <span className={styles.cardLabel}>Jeton enregistré</span>
            <span>
              se terminant par <code>{registered.token_last4}</code>
            </span>
          </p>
          <p className={styles.cardLine}>
            <span className={styles.cardLabel}>Projet Todoist</span>
            {registered.external_list_id ?? 'Boîte de réception (par défaut)'}
          </p>
          <p className={styles.cardLine}>
            <span className={styles.cardLabel}>Autorisation donnée le</span>
            {formatDate(registered.consented_at)}
          </p>
          <p className={styles.cardLine}>
            <span className={styles.cardLabel}>Autorisation donnée par</span>
            {/* The question this answers is "whose Todoist account receives our
                shopping list?", and until the server recorded it the screen could
                only show four characters of a credential. `null` is an older row,
                not an anonymous agreement, and it says so rather than showing a
                blank. */}
            {registered.registered_by ?? 'inconnu (enregistrement antérieur au suivi)'}
          </p>
          {registered.consent_revoked_at !== null ? (
            <p className={styles.cardLine}>
              <span className={styles.cardLabel}>Retirée le</span>
              {formatDate(registered.consent_revoked_at)}
            </p>
          ) : null}

          {registered.is_consented && !registered.registrant_is_member ? (
            <Callout
              tone="danger"
              title="La personne qui avait autorisé cet envoi a quitté le foyer"
            >
              <p>
                Le jeton enregistré est le sien : la liste partirait dans <em>son</em> compte
                Todoist. Chaudron a donc cessé d’envoyer, sans effacer la trace de ce qui avait été
                autorisé.{' '}
                {isOwner
                  ? 'Enregistrez un nouveau jeton, ou retirez l’autorisation.'
                  : 'Un propriétaire du foyer doit enregistrer un nouveau jeton ou retirer l’autorisation.'}
              </p>
            </Callout>
          ) : null}

          {registered.is_consented ? (
            <details className={styles.consentDetails}>
              <summary>Ce qui part vers Todoist</summary>
              <WhatLeaves />
            </details>
          ) : (
            <Callout tone="info" title="Envoi désactivé">
              <p>
                L’autorisation a été retirée : Chaudron n’envoie plus rien vers Todoist. La ligne
                est conservée pour que vous puissiez voir ce qui avait été autorisé. Pour
                réautoriser, enregistrez à nouveau un jeton.
              </p>
            </Callout>
          )}

          {isOwner ? (
            <div className={styles.cardActions}>
              <Button
                variant="secondary"
                onClick={() => {
                  resetForm();
                  setEditing(true);
                }}
              >
                {registered.is_consented ? 'Changer de jeton' : 'Réautoriser'}
              </Button>
              {registered.is_consented ? (
                <Button variant="danger" loading={revoking} onClick={revoke}>
                  Retirer l’autorisation
                </Button>
              ) : null}
            </div>
          ) : (
            <Callout tone="info" title="Réservé aux propriétaires du foyer">
              <p>
                Enregistrer un jeton, en changer ou retirer l’autorisation relève d’un propriétaire
                du foyer : ce qui est en jeu est l’accord du foyer entier à envoyer sa liste à un
                service tiers. Vous pouvez toujours l’envoyer depuis l’écran Courses.
              </p>
            </Callout>
          )}
        </>
      ) : null}

      {registered === null && !isOwner && !state.loading ? (
        <Callout tone="info" title="Aucune destination enregistrée">
          <p>
            Personne n’a encore autorisé l’envoi de la liste de ce foyer vers Todoist. Seul un
            propriétaire du foyer peut le faire.
          </p>
        </Callout>
      ) : null}

      {showForm && !state.loading ? (
        <form className={styles.form} onSubmit={submit} noValidate>
          <Field
            label="Jeton personnel Todoist"
            required
            error={tokenError}
            hint="Todoist › Paramètres › Intégrations › Développeur. Le jeton est chiffré ici et ne vous est jamais réaffiché : seuls ses quatre derniers caractères restent visibles."
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                className={controlClass(invalid)}
                // `password`, so the browser masks it, keeps it out of the
                // autofill history of ordinary text fields, and does not offer
                // it back on the next form.
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={token}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setToken(event.target.value);
                }}
              />
            )}
          </Field>

          <Field
            label="Identifiant du projet Todoist"
            error={projectError}
            hint="Facultatif. Vide, les articles arrivent dans votre boîte de réception Todoist."
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                className={controlClass(invalid)}
                type="text"
                inputMode="text"
                autoComplete="off"
                spellCheck={false}
                value={projectId}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setProjectId(event.target.value);
                }}
              />
            )}
          </Field>

          <div className={styles.consentBlock}>
            <p className={styles.cardLabel}>Ce qui partira vers Todoist</p>
            <WhatLeaves />
            <p className={styles.consentNote}>
              Todoist est un service américain, exploité par un tiers : une fois la liste partie,
              elle suit les conditions de ce tiers et non celles de Chaudron. Vous pouvez retirer
              cette autorisation à tout moment depuis cet écran ; l’envoi cesse immédiatement.
            </p>
            <Checkbox checked={consent} onChange={setConsent}>
              J’autorise Chaudron à envoyer la liste de courses de ce foyer vers Todoist.
            </Checkbox>
          </div>

          {submitError !== null ? (
            <Callout tone="danger" title="Enregistrement impossible">
              <p>{submitError}</p>
            </Callout>
          ) : null}

          <div className={styles.formActions}>
            <Button type="submit" variant="primary" loading={saving} disabled={!consent}>
              Enregistrer la destination
            </Button>
            {registered !== null ? (
              <Button
                variant="ghost"
                onClick={() => {
                  setEditing(false);
                  resetForm();
                }}
              >
                Annuler
              </Button>
            ) : null}
          </div>
        </form>
      ) : null}
    </section>
  );
}
