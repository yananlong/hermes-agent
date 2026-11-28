import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  api: vi.fn(), request: vi.fn(), route: vi.fn(), scoped: vi.fn()
}))

vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: mocks.request, gatewayRouteState: mocks.route }))
vi.mock('./client', () => ({
  ambientOwnerConnectionId: () => 'ambient', capabilityScoped: mocks.scoped,
  connectionScoped: () => ({ connectionId: 'ambient' }), getApiRequestConnection: () => 'ambient',
  getApiRequestProfile: () => 'default', hermesApi: mocks.api,
  profileScoped: () => ({}), sessionReadOwnerPin: () => ({})
}))

const { getLatestSessionMessages } = await import('./sessions')

const result = (id: string) => ({
  session_id: id, linked_codex: true, linked_history_identity: 'identity-a', reset: true, next_cursor: 'older',
  rows: [{ role: 'user', content: 'hello', codex_native_row_id: 'native-1' }]
})

beforeEach(() => {
  vi.clearAllMocks()
  mocks.scoped.mockImplementation((scope: { connectionId: string; profile: string }) => scope)
  mocks.route.mockReturnValue({ gateway: null })
})

describe('linked native history transport', () => {
  it('loads a bounded canonical page from the exact owner instead of REST messages', async () => {
    mocks.api.mockResolvedValue({ id: 's', profile: 'work', codex_linked: true })
    mocks.request.mockResolvedValue(result('s'))
    const page = await getLatestSessionMessages('s', { connectionId: 'other', profile: 'work' })
    expect(mocks.api).toHaveBeenCalledTimes(1)
    expect(mocks.request).toHaveBeenCalledWith('other', 'work', 'session.transcript', {
      session_id: 's', profile: 'work', order: 'latest', limit: 100
    })
    expect(page.native_history?.next_cursor).toBe('older')
  })

  it('does not dial a gateway for passive linked reads', async () => {
    mocks.api.mockResolvedValue({ id: 's', profile: 'work', codex_linked: true })
    await expect(getLatestSessionMessages('s', { connectionId: 'other', profile: 'work' }, { passive: true }))
      .rejects.toThrow('not connected')
    expect(mocks.api.mock.calls[0][0]).toMatchObject({ passive: true })
    expect(mocks.request).not.toHaveBeenCalled()
  })

  it('refuses a page for a different session and does not fall back to REST history', async () => {
    mocks.api.mockResolvedValue({ id: 's', profile: 'work', codex_linked: true })
    mocks.request.mockResolvedValue(result('other'))
    await expect(getLatestSessionMessages('s', { connectionId: 'other', profile: 'work' })).rejects.toThrow('invalid history')
    expect(mocks.api).toHaveBeenCalledTimes(1)
  })

  it('keeps ordinary sessions on their existing REST history path', async () => {
    mocks.api.mockResolvedValueOnce({ id: 's', profile: 'work' }).mockResolvedValueOnce({
      session_id: 's', profile: 'work', messages: [], pagination: { limit: 120, offset: 0, order: 'latest', returned: 0 }
    })
    await getLatestSessionMessages('s', { connectionId: 'other', profile: 'work' })
    expect(mocks.api.mock.calls[1][0].path).toContain('/api/sessions/s/messages')
    expect(mocks.request).not.toHaveBeenCalled()
  })
})
