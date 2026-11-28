import { expect, it } from 'vitest'

import { toChatMessages } from './chat-messages'

it('hydrates Codex-provided reasoning and structured tool contents without exposing opaque replay data', () => {
  const messages = toChatMessages([
    { role: 'user', content: 'Continue the native thread.' },
    {
      role: 'assistant',
      content: '',
      reasoning: 'Codex summary: check the recipient, then report the result.',
      codex_reasoning_items: [{ type: 'reasoning', encrypted_content: 'opaque-replay-payload' }],
      tool_calls: [
        {
          id: 'codex_dynamicToolCall_item-1',
          type: 'function',
          function: { name: 'send_message', arguments: '{"recipient":"agent-2","message":"Done"}' }
        }
      ]
    },
    {
      role: 'tool',
      name: 'send_message',
      tool_call_id: 'codex_dynamicToolCall_item-1',
      content: '{"status":"sent","message":"Delivered"}'
    }
  ])

  const assistant = messages.find(message => message.role === 'assistant')

  expect(assistant).toBeDefined()

  if (!assistant) {
    return
  }

  const reasoning = assistant.parts.find(part => part.type === 'reasoning')
  const tool = assistant.parts.find(part => part.type === 'tool-call')

  expect(reasoning).toMatchObject({ type: 'reasoning', text: 'Codex summary: check the recipient, then report the result.' })
  expect(tool).toMatchObject({
    type: 'tool-call',
    toolName: 'send_message',
    args: { recipient: 'agent-2', message: 'Done' },
    result: { status: 'sent', message: 'Delivered' }
  })
  expect(JSON.stringify(assistant.parts)).not.toContain('opaque-replay-payload')
})
