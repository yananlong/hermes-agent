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
import { useEffect, useState } from 'react'

import { GatewayClient } from '@/lib/gatewayClient'

import { type HostCapability, readHostCapability } from './foreign-capability'

export function useForeignCapability(active: boolean, scope: string): HostCapability | undefined {
  const [capability, setCapability] = useState<HostCapability | undefined>(undefined)

  useEffect(() => {
    if (!active) {
      setCapability(undefined)

      return undefined
    }

    const client = new GatewayClient()
    let cancelled = false

    client
      .connect()
      .then(() => readHostCapability((method, params) => client.request(method, params ?? {}), scope))
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
  }, [active, scope])

  return capability
}
