import { expect, it } from 'vitest'

import { toLinkedHistoryMessages } from '../domain/linkedHistoryMessages.js'

it('preserves policy-filtered reasoning and full tool arguments/results, but hides protected rows', () => {
  const messages = toLinkedHistoryMessages([
    { role: 'assistant', text: '', reasoning: 'safe reasoning' },
    { role: 'tool', name: 'terminal', args: { command: 'run the full command' },
      content: `result ${'x'.repeat(1000)} result-end` },
    { role: 'assistant', display_kind: 'hidden', text: 'SECRET', reasoning: 'SECRET' }
  ])
  expect(messages[0]).toMatchObject({ kind: 'trail', thinking: 'safe reasoning', linkedHistory: true })
  expect(messages[1]?.text).toContain('run the full command')
  expect(messages[1]?.text).toContain('result-end')
  expect(JSON.stringify(messages)).not.toContain('SECRET')
})
