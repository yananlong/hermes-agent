/**
 * One capability read per serving scope, shared by every linked view in the window.
 *
 * `readHostCapability` never throws: an old gateway answers -32601 → `unknown` (cached), a broken
 * dial → `error`. Both keep the pre-capability read-only behaviour, so the linked views can render
 * immediately and upgrade themselves when the answer lands — they never wait on this to open a
 * transcript.
 */
import { useQuery } from '@tanstack/react-query'

import type { SessionOwnerRoute } from '@/store/session-request-router'

import { type HostCapability, hostCapabilityScope, readHostCapability } from './capability'

export function useHostCapability(
  /** Omitted where the session has no exact owner yet: a fresh draft on an unknown backend must
   *  not dial a capability probe, and its linked state (there is none) stays read-only. */
  owner: SessionOwnerRoute | undefined
): HostCapability | undefined {
  const query = useQuery({
    queryKey: ['foreign-host-capability', owner ? hostCapabilityScope(owner) : 'no-owner'],
    queryFn: ({ signal }) => readHostCapability(owner as SessionOwnerRoute, signal),
    enabled: Boolean(owner),
    retry: false,
    staleTime: 30_000
  })

  return query.data
}
