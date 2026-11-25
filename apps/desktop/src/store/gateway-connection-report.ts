// Connection-state reporting for the gateway registry (sibling of ``store/gateway.ts``):
// mirror a backend's socket state into the global composer state, holding OS notifications
// and per-build capability caches on open. Extracted from ``gateway.ts`` for the file ratchet.

import { markNativeNotifyBaseline } from '@/store/notify-baseline'
import { resetHostCapabilityCache } from '@/store/host-capability-cache'
import { setGatewayState } from '@/store/session'

import type { ConnectionState } from '@hermes/shared'
import { gatewayState } from '@/store/gateway-registry-state'

const g = gatewayState()

const normKey = (profile: string | null | undefined): string => (profile ?? '').trim() || 'default'

// Mirror a backend's connection state into the global composer state, but only
// when that backend is the one the user is currently looking at. Lets the
// composer reflect the active profile's socket without a background reconnect
// flipping the foreground enabled/disabled state.
export function reportGatewayState(profile: string, state: ConnectionState): void {
  // Any socket opening replays parked prompts; hold OS notifications so a
  // launch/reconnect doesn't alert about state that already existed.
  if (state === 'open') {
    markNativeNotifyBaseline()
    // A socket that just opened may be a NEW gateway build: the backend restarted
    // (upgrade, profile switch, new remote) and may well now serve
    // `session.foreign.status`. Drop the `-32601` verdicts cached against the
    // previous build so the next probe asks instead of assuming "unknown" until
    // the window is relaunched.
    resetHostCapabilityCache()
  }

  if (normKey(profile) === g.activeKey) {
    setGatewayState(state)
  }
}

export function reportPrimaryGatewayState(state: ConnectionState): void {
  reportGatewayState(g.primaryProfile, state)
}
