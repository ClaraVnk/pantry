import { createContext, useContext } from 'react';
import type { HouseholdSummary, Session } from '../api/auth';

export interface SessionState {
  /** `null` once the check has run and nobody is signed in. */
  session: Session | null;
  /** True until the first `GET /v1/auth/session` has answered. */
  loading: boolean;
  /** The household the interface is currently showing, if one is decided. */
  activeHousehold: HouseholdSummary | null;
  /** Adopt a session returned by sign-in or sign-up. */
  adopt: (session: Session) => void;
  /** Switch households. Rejected silently if the account is not a member. */
  selectHousehold: (id: string) => void;
  signOut: () => Promise<void>;
}

export const SessionContext = createContext<SessionState | null>(null);

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (!value) throw new Error('useSession doit être utilisé dans un SessionProvider');
  return value;
}
