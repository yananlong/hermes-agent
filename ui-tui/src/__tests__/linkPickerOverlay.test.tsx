import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { expect, it, vi } from 'vitest'

import { LinkedThreadPicker } from '../components/linkedThreadPicker.js'
import { DEFAULT_THEME } from '../theme.js'

// The `/link` picker: one `session.foreign.status` decides what is selectable, each linkable
// agent's `session.<agent>.list` supplies rows, and Enter links the selected native thread then
// resumes the Hermes session the backend returned.

const LABELS: Record<string, string> = { claude: 'Claude Code', codex: 'Codex', mistral: 'Mistral Vibe' }

const agentRow = (agent: string, writable = true, reason: null | string = null) => ({
  agent,
  cli: { available: true, command: agent, path: null, reason: null, reason_code: null, source: 'path', version: null },
  continue_status: { reason, reason_code: writable ? null : 'cli_missing', supported: true, writable },
  label: LABELS[agent],
  store: { available: true, readable: true, reason: null, reason_code: null }
})

function mount(request: (method: string, params: any) => Promise<unknown>) {
  const gw = { isCanonical: true, request: vi.fn(request) }
  const stdin = Object.assign(new PassThrough(), { isTTY: true, setRawMode: vi.fn(), ref: vi.fn(), unref: vi.fn() })
  const stdout = Object.assign(new PassThrough(), { columns: 100, isTTY: false, rows: 30 })
  const out = { text: '' }

  stdout.on('data', chunk => {
    out.text += chunk.toString()
  })

  const onCancel = vi.fn()
  const onResume = vi.fn()

  const element = <LinkedThreadPicker gw={gw as any} onCancel={onCancel} onResume={onResume} t={DEFAULT_THEME} />

  const instance = renderSync(element, {
    patchConsole: false,
    stderr: new PassThrough() as any,
    stdin: stdin as any,
    stdout: stdout as any
  })

  return { gw, instance, onCancel, onResume, out, stdin, element }
}

it('lists an agent the host can serve, links the selected thread and resumes it', async () => {
  const picker = mount(async (method: string) => {
    if (method === 'session.foreign.status') {
      return {
        agents: [
          agentRow('codex'),
          agentRow('claude', false, 'Claude Code is not installed on build-host.'),
          agentRow('mistral', false, 'Mistral is not installed on build-host.')
        ],
        host: 'build-host'
      }
    }

    if (method === 'session.codex.list') {
      return { next_cursor: null, threads: [{ id: 'thread-abc123456', title: 'Fix the flaky test', updated_at: 1 }] }
    }

    if (method === 'session.codex.link') {
      return { already_linked: false, session_id: 'hermes-linked' }
    }

    throw Object.assign(new Error(`unknown method: ${method}`), { code: -32601 })
  })

  try {
    // The capability answer is asked ONCE for the whole panel.
    await vi.waitFor(() => expect(picker.gw.request).toHaveBeenCalledWith('session.foreign.status', {}))
    await vi.waitFor(() => expect(picker.gw.request).toHaveBeenCalledWith('session.codex.list', {}))
    picker.instance.rerender(picker.element)

    await vi.waitFor(() => expect(picker.out.text).toContain('Fix the flaky test'))
    expect(picker.out.text).toContain('Link a native thread')
    expect(picker.out.text).toContain('Claude Code')
    expect(picker.out.text).toContain('Claude Code is not installed on build-host.')
    expect(picker.gw.request.mock.calls.filter(([method]) => method === 'session.foreign.status')).toHaveLength(1)
    // An agent the host cannot continue never gets its store read: it is shown, not queried.
    expect(picker.gw.request.mock.calls.filter(([method]) => method === 'session.claude.list')).toHaveLength(0)

    picker.stdin.write('\r')

    await vi.waitFor(() =>
      expect(picker.gw.request).toHaveBeenCalledWith('session.codex.link', { thread_id: 'thread-abc123456' })
    )
    await vi.waitFor(() => expect(picker.onResume).toHaveBeenCalledWith('hermes-linked'))
    expect(picker.onCancel).not.toHaveBeenCalled()
  } finally {
    picker.instance.unmount()
  }
})

it('keeps a failed link on screen as an error line and resumes nothing', async () => {
  const picker = mount(async (method: string) => {
    if (method === 'session.foreign.status') {
      return { agents: [agentRow('codex'), agentRow('claude'), agentRow('mistral')], host: 'build-host' }
    }

    if (method.endsWith('.list')) {
      return { next_cursor: null, threads: [{ id: 'thread-xyz', title: 'Refactor the parser', updated_at: 2 }] }
    }

    throw Object.assign(new Error('busy'), { code: 4009 })
  })

  try {
    await vi.waitFor(() => expect(picker.gw.request).toHaveBeenCalledWith('session.codex.list', {}))
    picker.instance.rerender(picker.element)
    await vi.waitFor(() => expect(picker.out.text).toContain('Refactor the parser'))

    picker.stdin.write('\r')

    await vi.waitFor(() => expect(picker.out.text).toContain('error: '))
    expect(picker.onResume).not.toHaveBeenCalled()
  } finally {
    picker.instance.unmount()
  }
})

it('treats an unanswered capability probe as nothing to link, not as writable', async () => {
  const picker = mount(async (method: string) => {
    if (method === 'session.foreign.status') {
      throw new Error('unknown_method: session.foreign.status')
    }

    throw Object.assign(new Error(`unknown method: ${method}`), { code: -32601 })
  })

  try {
    await vi.waitFor(() => expect(picker.out.text).toContain('unknown_method'))
    picker.instance.rerender(picker.element)
    await vi.waitFor(() => expect(picker.out.text).toContain('This gateway did not report Codex capability.'))
    // No store was read for any agent, so Enter cannot link anything.
    expect(picker.gw.request.mock.calls.every(([method]) => method === 'session.foreign.status')).toBe(true)

    picker.stdin.write('\r')

    await vi.waitFor(() => expect(picker.out.text).toContain('Codex capability'))
    expect(picker.onResume).not.toHaveBeenCalled()
  } finally {
    picker.instance.unmount()
  }
})
