import { useState } from 'react';
import { describeError } from '../../api/client';
import { clearRecipeFeedback, setRecipeFeedback } from '../../api/endpoints';
import type { RecipeFeedbackVerdict } from '../../api/types';
import { Chip, ChipRow } from '../../components/ui';
import styles from './Recipes.module.css';

interface Props {
  suggestionId: string;
  /** Card title, so the group and each control name what they are about. */
  title: string;
  onRecorded: () => void;
}

const LABELS: Record<RecipeFeedbackVerdict, string> = {
  cooked: 'J’ai cuisiné',
  not_interested: 'Pas pour moi',
};

/**
 * The whole feedback gesture: two chips, one tap, no form.
 *
 * An opinion that costs a dialog is an opinion nobody gives, and a feedback loop
 * with no data is a maintenance cost with nothing on the other side. So the
 * controls sit on the card itself, they are pre-existing primitives with a 44 px
 * target, and there is nothing to confirm.
 *
 * Tapping the chip that is already on **withdraws** the opinion. That is the
 * "remove" path, and it deliberately costs the same single tap as giving one:
 * people mis-tap on a phone in a kitchen, and an answer that cannot be taken back
 * without hunting for a menu is an answer the next person stops giving.
 *
 * The state is optimistic and reverts on failure. Showing a verdict the server
 * refused would be worse than showing none: the ranking would not reflect it and
 * nothing on screen would say so.
 */
export function FeedbackControls({ suggestionId, title, onRecorded }: Props) {
  const [verdict, setVerdict] = useState<RecipeFeedbackVerdict | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const choose = (value: RecipeFeedbackVerdict) => {
    if (saving) return;
    const previous = verdict;
    const next = previous === value ? null : value;
    setVerdict(next);
    setSaving(true);
    setError(null);

    const call =
      next === null ? clearRecipeFeedback(suggestionId) : setRecipeFeedback(suggestionId, next);
    call
      .then((state) => {
        // The server's answer wins over the guess, so two quick taps cannot leave
        // the card claiming something the row does not say.
        setVerdict(state.feedback);
        onRecorded();
      })
      .catch((cause: unknown) => {
        setVerdict(previous);
        setError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  return (
    <div className={styles.feedback}>
      <ChipRow label={`Votre avis sur « ${title} »`}>
        {(Object.keys(LABELS) as RecipeFeedbackVerdict[]).map((value) => (
          <Chip
            key={value}
            active={verdict === value}
            // The visible text is short enough for a thumb; the accessible name
            // says which dish it applies to, because a screen reader user meets
            // ten identical "J’ai cuisiné" buttons down the page otherwise.
            label={`${LABELS[value]} — ${title}`}
            onClick={() => {
              choose(value);
            }}
          >
            {LABELS[value]}
          </Chip>
        ))}
      </ChipRow>
      {/* A live region either way: without one, "Avis enregistré" is a silent
          change for a screen reader, and the chip's aria-pressed alone does not
          say whether the write reached the server. `alert` is assertive and
          reserved for the failure. */}
      <p className={styles.feedbackHint} role={error ? 'alert' : 'status'}>
        {error ??
          (saving
            ? // A tap during the write is ignored to keep two answers from racing;
              // saying so is what stops that from reading as an unresponsive button.
              'Enregistrement…'
            : verdict === null
              ? 'Votre avis oriente le classement des prochaines propositions. Il ne retire jamais une recette.'
              : 'Avis enregistré. Touchez à nouveau pour le retirer.')}
      </p>
    </div>
  );
}
