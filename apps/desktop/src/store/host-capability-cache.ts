/**
 * The `-32601` verdict for `session.foreign.status`, kept where the socket that
 * produced it can clear it.
 *
 * A gateway that predates the capability RPC answers "method not found", and asking it again for
 * every linked view in the window is pure latency. The verdict is therefore cached per serving
 * scope — but it belongs to ONE gateway build: after a restart (an upgrade, a profile switch, a
 * new remote) the same scope may well answer properly, so the cache may not outlive the socket
 * that filled it.
 *
 * That rule lives here, in a leaf module with no imports, because the two sides of it sit in
 * different layers: `store/gateway.ts` observes a socket reaching `open` and clears the cache, and
 * `app/session-import/capability.ts` is the only reader/writer. Importing the feature module from
 * the store would create a cycle (`store/gateway → app/session-import/capability → …/api →
 * store/gateway`), so the shared state lives below both of them.
 */

/** Serving scopes whose gateway answered -32601. */
const UNSUPPORTED_SCOPES = new Set<string>()

export function markHostCapabilityUnsupported(scope: string): void {
  UNSUPPORTED_SCOPES.add(scope)
}

export function isHostCapabilityUnsupported(scope: string): boolean {
  return UNSUPPORTED_SCOPES.has(scope)
}

/** Drop every cached verdict. Called when a gateway socket opens and by tests. */
export function resetHostCapabilityCache(): void {
  UNSUPPORTED_SCOPES.clear()
}
