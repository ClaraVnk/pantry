import { getActiveHouseholdId } from '../../api/session';

/**
 * Whether this household asked Chaudron to compute its grocery spending.
 *
 * Contract §6ter says the application *proposes* to track spending and does not
 * impose it: a household may want to manage its stock without having its money
 * counted. So the budget screen fetches nothing at all until this flag is set —
 * no silent collection, and no figure computed behind the user's back.
 *
 * The flag is a local preference, like the meal temperature: it says what this
 * browser wants to see, not what the household is. Turning it off stops the
 * display and leaves any target the user set on the server, where a deliberate
 * "stop tracking" removes it.
 */

const KEY = 'chaudron.budget-tracking';

function storageKey(): string | null {
  // The active household is runtime state now, not a build constant.
  const householdId = getActiveHouseholdId();
  return householdId === null ? null : `${KEY}.${householdId}`;
}

export function readBudgetOptIn(): boolean {
  const key = storageKey();
  if (key === null) return false;
  try {
    return window.localStorage.getItem(key) === 'on';
  } catch {
    // Private browsing and blocked storage both throw. Defaulting to "off" is
    // the safe answer: the worst case is one extra tap, not an unrequested
    // computation.
    return false;
  }
}

export function writeBudgetOptIn(enabled: boolean): void {
  const key = storageKey();
  if (key === null) return;
  try {
    window.localStorage.setItem(key, enabled ? 'on' : 'off');
  } catch {
    /* ignored, see above */
  }
}
