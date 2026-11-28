import { describe, expect, it } from 'vitest'

import { codexSubagentActivityFromArgs, splitLegacyCodexSubagentActivity } from './codex-subagent-activity'

describe('Codex subagent activity parsing', () => {
  it('keeps the child trace target and readable lifecycle metadata without promoting private fields', () => {
    expect(
      codexSubagentActivityFromArgs({
        agentThreadId: 'child-private-thread',
        agentPath: '/home/private/agent-config',
        kind: 'interacted',
        model: 'gpt-6.2-codex',
        reasoningEffort: 'high'
      })
    ).toEqual({
      agentThreadId: 'child-private-thread',
      kind: 'interacted',
      model: 'gpt-6.2-codex',
      reasoningEffort: 'high'
    })
  })

  it.each([null, [], 'not json', { kind: 'unknown', agent_thread_id: 42, model: ' ' }])(
    'fails closed on malformed or missing activity fields: %j',
    value => {
      expect(codexSubagentActivityFromArgs(value)).toEqual({})
    }
  )

  it('replaces valid legacy opaque rows and preserves surrounding assistant prose', () => {
    const payload = JSON.stringify({
      id: 'item-1',
      kind: 'completed',
      agentThreadId: 'child-thread',
      agentPath: 'root/worker',
      model: 'gpt-6.2-codex',
      reasoningEffort: 'medium'
    })

    const segments = splitLegacyCodexSubagentActivity(`Before.\n[codex subAgentActivity] ${payload}\nAfter.`)

    expect(segments).toEqual([
      { type: 'text', text: 'Before.\n' },
      {
        type: 'activity',
        id: 'codex_subAgentActivity_item-1',
        activity: {
          agentThreadId: 'child-thread',
          kind: 'completed',
          model: 'gpt-6.2-codex',
          reasoningEffort: 'medium'
        }
      },
      { type: 'text', text: '\nAfter.' }
    ])
  })

  it('keeps malformed legacy rows visible and still renders valid records with incomplete metadata', () => {
    expect(splitLegacyCodexSubagentActivity('[codex subAgentActivity] {broken')).toEqual([
      { type: 'text', text: '[codex subAgentActivity] {broken' }
    ])

    expect(splitLegacyCodexSubagentActivity('[codex subAgentActivity] {"kind":"completed"}')).toEqual([
      { type: 'activity', id: 'codex_subAgentActivity_legacy_0', activity: { kind: 'completed' } }
    ])
  })
})
