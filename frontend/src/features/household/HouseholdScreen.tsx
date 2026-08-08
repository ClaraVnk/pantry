import { useState } from 'react';
import type { HouseholdMember, MemberDraft } from '../../api/types';
import { Badge, Button, Callout, EmptyState, ErrorState, LoadingRow } from '../../components/ui';
import type { MembersState } from '../../hooks/useMembers';
import {
  AGE_BAND_LABELS,
  ALLERGEN_LABELS,
  DIET_LABELS,
  TEXTURE_LABELS,
  isInfantBand,
} from '../../lib/dietary';
import { AccountSecurityPanel } from './AccountSecurityPanel';
import { ExportTargetPanel } from './ExportTargetPanel';
import { MachineTokenPanel } from './MachineTokenPanel';
import { MemberForm } from './MemberForm';
import styles from './Household.module.css';

interface Props {
  state: MembersState;
}

function MemberCard({
  member,
  onEdit,
  onRemove,
}: {
  member: HouseholdMember;
  onEdit: () => void;
  onRemove: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = () => {
    setBusy(true);
    void onRemove().finally(() => {
      setBusy(false);
      setConfirming(false);
    });
  };

  return (
    <li className={styles.card}>
      <div className={styles.cardHead}>
        <h3 className={styles.cardName}>{member.display_name}</h3>
        <span className={styles.cardBand}>{AGE_BAND_LABELS[member.age_band]}</span>
      </div>

      <div className={styles.cardFacts}>
        <Badge tone="neutral">{DIET_LABELS[member.diet]}</Badge>
        {member.infant_texture ? (
          <Badge tone="neutral">{TEXTURE_LABELS[member.infant_texture]}</Badge>
        ) : null}
      </div>

      <p className={styles.cardLine}>
        <span className={styles.cardLabel}>Allergènes exclus</span>
        {member.allergens.length === 0
          ? 'aucun'
          : member.allergens.map((code) => ALLERGEN_LABELS[code]).join(', ')}
      </p>

      {member.free_text_restrictions.trim() !== '' ? (
        <p className={styles.cardLine}>
          <span className={styles.cardLabel}>Préférences transmises</span>
          {member.free_text_restrictions}
        </p>
      ) : null}

      {confirming ? (
        <div className={styles.cardActions}>
          <Button variant="danger" loading={busy} onClick={remove}>
            Supprimer définitivement
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setConfirming(false);
            }}
          >
            Annuler
          </Button>
        </div>
      ) : (
        <div className={styles.cardActions}>
          <Button variant="secondary" onClick={onEdit}>
            Modifier
            <span className="visually-hidden"> {member.display_name}</span>
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Supprimer
            <span className="visually-hidden"> {member.display_name}</span>
          </Button>
        </div>
      )}
    </li>
  );
}

/**
 * Creating, editing and deleting household members.
 *
 * Deleting a member deletes their constraints with them — the contract says so,
 * and it is the erasure obligation of ADR-0009 rather than a convenience.
 */
export function HouseholdScreen({ state }: Props) {
  const { members, loading, error, unsupported, reload, create, update, remove } = state;
  const [editing, setEditing] = useState<HouseholdMember | null>(null);
  const [creating, setCreating] = useState(false);
  const [status, setStatus] = useState('');

  const infants = members.filter((member) => isInfantBand(member.age_band));

  const submit = async (draft: MemberDraft) => {
    if (editing) {
      await update(editing.id, draft);
      setStatus(`${draft.display_name} a été mis à jour.`);
    } else {
      await create(draft);
      setStatus(`${draft.display_name} a été ajouté au foyer.`);
    }
    setEditing(null);
    setCreating(false);
  };

  if (creating || editing) {
    return (
      <section className={styles.screen} aria-labelledby="household-heading">
        <h1 className={styles.heading} id="household-heading">
          Foyer
        </h1>
        <MemberForm
          key={editing?.id ?? 'new'}
          member={editing}
          onSubmit={submit}
          onCancel={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      </section>
    );
  }

  return (
    <section className={styles.screen} aria-labelledby="household-heading">
      <h1 className={styles.heading} id="household-heading">
        Foyer
      </h1>
      <p className={styles.lead}>
        Les contraintes sont portées par membre, pas par foyer : au moment de cuisiner, vous
        choisissez pour qui, et l’union de leurs contraintes s’applique.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {unsupported ? (
        <Callout tone="info" title="Fonctionnalité non disponible sur ce serveur">
          <p>
            Ce serveur Chaudron ne propose pas encore la gestion des membres du foyer (contrat
            v1.1). Mettez l’API à jour pour saisir régimes et allergènes.
          </p>
        </Callout>
      ) : loading && members.length === 0 ? (
        <LoadingRow label="Chargement des membres…" />
      ) : error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : members.length === 0 ? (
        <EmptyState
          title="Aucun membre enregistré"
          body="Ajoutez les personnes du foyer pour que les suggestions tiennent compte de leurs allergies, régimes et textures."
          action={
            <Button
              variant="primary"
              onClick={() => {
                setCreating(true);
              }}
            >
              Ajouter un membre
            </Button>
          }
        />
      ) : (
        <>
          <ul className={styles.list}>
            {members.map((member) => (
              <MemberCard
                key={member.id}
                member={member}
                onEdit={() => {
                  setEditing(member);
                }}
                onRemove={async () => {
                  await remove(member.id);
                  setStatus('Le membre et ses contraintes ont été supprimés.');
                }}
              />
            ))}
          </ul>

          {infants.length > 0 ? (
            <p className={styles.lead}>
              {infants.length > 1
                ? `${String(infants.length)} nourrissons sont enregistrés.`
                : 'Un nourrisson est enregistré.'}{' '}
              Le mode nourrisson s’applique dès qu’il est sélectionné dans une suggestion.
            </p>
          ) : null}

          <Button
            variant="primary"
            block
            onClick={() => {
              setCreating(true);
            }}
          >
            Ajouter un membre
          </Button>
        </>
      )}

      {/* Outside the members branch on purpose: where this household's shopping
          list may be sent is a household setting, and it has to be reachable
          before anybody has entered a single member. The same applies to the
          machine tokens below — wiring up Home Assistant is the first thing some
          households do, and it must not be hidden behind entering an eater.

          The account panel is here rather than behind the avatar menu because
          this is the only settings surface the application has, and a "sign out
          everywhere" nobody can find is a remedy nobody has. It sits above the
          two credential panels it is about. */}
      <AccountSecurityPanel />
      <ExportTargetPanel />
      <MachineTokenPanel />
    </section>
  );
}
