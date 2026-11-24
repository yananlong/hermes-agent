import { describe, expect, it } from 'vitest'

import { toChatMessages } from './chat-messages'

describe('Codex subagent activity transcript hydration', () => {
  it('turns existing opaque rows into trace cards without dropping adjacent text', () => {
    const payload = JSON.stringify({
      id: 'activity-1',
      kind: 'started',
      agentThreadId: 'child-thread-private',
      agentPath: '/private/agent/config',
      model: 'gpt-6.2-codex'
    })

    const [message] = toChatMessages([
      {
        role: 'assistant',
        content: `I started a child.\n[codex subAgentActivity] ${payload}\nI will check its trace.`,
        timestamp: 12
      }
    ])

    expect(message?.parts.map(part => part.type)).toEqual(['text', 'tool-call', 'text'])
    expect(message?.parts[0]).toMatchObject({ type: 'text', text: 'I started a child.\n' })
    expect(message?.parts[1]).toMatchObject({
      type: 'tool-call',
      toolCallId: 'codex_subAgentActivity_activity-1',
      toolName: 'codex_subagent_activity',
      args: { kind: 'started', agent_thread_id: 'child-thread-private', model: 'gpt-6.2-codex' },
      result: ''
    })
    expect(message?.parts[2]).toMatchObject({ type: 'text', text: '\nI will check its trace.' })
  })

  it('hydrates structured backend tool calls and hides malformed legacy rows only as ordinary visible text', () => {
    const structured = toChatMessages([
      {
        role: 'assistant',
        content: '',
        timestamp: 15,
        tool_calls: [
          {
            id: 'codex_activity-2',
            type: 'function',
            function: {
              name: 'codex_subagent_activity',
              arguments: JSON.stringify({ kind: 'started', agent_thread_id: 'child-2' })
            }
          }
        ]
      },
      { role: 'tool', content: '', name: 'codex_subagent_activity', tool_call_id: 'codex_activity-2', timestamp: 15 },
      { role: 'assistant', content: '[codex subAgentActivity] {broken', timestamp: 16 }
    ])

    expect(structured[0]?.parts).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: 'tool-call',
          toolName: 'codex_subagent_activity',
          args: { kind: 'started', agent_thread_id: 'child-2' },
          result: ''
        })
      ])
    )
    expect(structured.map(message => message.parts.map(part => part.type)).flat()).toContain('text')
    expect(structured.map(message => message.parts).flat()).toEqual(
      expect.arrayContaining([expect.objectContaining({ type: 'text', text: '[codex subAgentActivity] {broken' })])
    )
  })
})
