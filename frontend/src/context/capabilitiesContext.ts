import { createContext, useContext } from 'react';
import type { ProviderCapabilities } from '../api/types';

export interface CapabilitiesState {
  /** null while loading, or when the call failed. */
  capabilities: ProviderCapabilities | null;
  loading: boolean;
  /**
   * Whether the shell may paint: the probe has answered, or it has taken long
   * enough that a blank window is the worse of the two failures.
   *
   * Separate from `!loading` because it never goes back to false — a manual
   * `refresh()` must not blank a screen the user is already reading.
   *
   * The banner this gates is what pushed `main` from y=69 to y=166 on every
   * cold load; see `CapabilitiesProvider` for why the answer is to wait rather
   * than to reserve the space.
   */
  paintable: boolean;
  /** Set when /v1/providers/capabilities itself could not be reached. */
  error: string | null;
  refresh: () => void;
}

export const CapabilitiesContext = createContext<CapabilitiesState | null>(null);

export function useCapabilities(): CapabilitiesState {
  const value = useContext(CapabilitiesContext);
  if (!value) throw new Error('useCapabilities must be used inside <CapabilitiesProvider>.');
  return value;
}
