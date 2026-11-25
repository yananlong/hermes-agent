/**
 * One capability read for the dashboard, over the same gateway WebSocket every
 * other dashboard RPC uses.
 *
 * Only surfaces that actually need a linked host answer call this (the linked
 * chat notice), and only while they are showing a linked thread: the socket is
 * created for that view and closed with it. A gateway that predates
 * `session.foreign.status` answers -32601 → `unknown`, the caller keeps its
 * read-only copy, and the negative verdict is cached for the rest of the page.
 */
import { useEffect, useState, useSyncExternalStore } from 'react'

import { GatewayClient } from '@/lib/gatewayClient'

import { type HostCapability, readHostCapability } from './foreign-capability'
import {
  dashboardConnectionGeneration,
  subscribeDashboardConnection,
} from './gateway-connection-generation'

export function useForeignCapability(active: boolean, profile: string): HostCapability | undefined {
  const [capability, setCapability] = useState<HostCapability | undefined>(undefined)
  // Every (re)opened dashboard socket may be a new gateway build, so the probe
  // re-runs against it: without the generation here a view that rendered
  // "unknown" (a gateway predating the RPC) would keep that verdict after an
  // upgrade until the page was reloaded.
  const generation = useSyncExternalStore(
    subscribeDashboardConnection,
    dashboardConnectionGeneration,
    dashboardConnectionGeneration
  )

  useEffect(() => {
    if (!active) {
      setCapability(undefined)

      return undefined
    }

    const client = new GatewayClient()
    let cancelled = false

    client
      .connect()
      .then(() =>
        readHostCapability((method, params) => client.request(method, params ?? {}), profile)
      )
      .then((result: HostCapability) => {
        if (!cancelled) {
          setCapability(result)
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setCapability({ state: 'error', message: cause instanceof Error ? cause.message : String(cause) })
        }
      })

    return () => {
      cancelled = true
      client.close()
    }
  }, [active, profile, generation])

  return capability
}
