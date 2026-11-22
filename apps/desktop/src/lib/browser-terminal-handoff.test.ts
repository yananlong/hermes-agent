import { describe, expect, it } from 'vitest'

import { buildBrowserTerminalHandoff } from './browser-terminal-handoff'

const SESSION_ID = '20260814_101010_abc123'
const HERMES_HOME = '/home/apps/.hermes'

describe('buildBrowserTerminalHandoff', () => {
  it('pins a validated profile and exact Hermes home in the SSH login shell', () => {
    expect(
      buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome: HERMES_HOME, shell: 'login' })
    ).toBe(`env HERMES_HOME='${HERMES_HOME}' hermes --profile work --tui --resume ${SESSION_ID}`)
  })

  it('pins the default profile explicitly', () => {
    expect(
      buildBrowserTerminalHandoff({
        sessionId: SESSION_ID,
        profile: 'default',
        hermesHome: HERMES_HOME,
        shell: 'login'
      })
    ).toBe(`env HERMES_HOME='${HERMES_HOME}' hermes --profile default --tui --resume ${SESSION_ID}`)
  })

  it('wraps the command in a one-shot interactive zsh login shell', () => {
    expect(
      buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome: HERMES_HOME, shell: 'zsh' })
    ).toBe(
      `zsh -ilf -c 'exec env "$@"' hermes-handoff 'HERMES_HOME=${HERMES_HOME}' hermes --profile work --tui --resume ${SESSION_ID}`
    )
  })

  it('wraps the command in a one-shot interactive bash login shell', () => {
    expect(
      buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome: HERMES_HOME, shell: 'bash' })
    ).toBe(
      `bash -il -c 'exec env "$@"' hermes-handoff 'HERMES_HOME=${HERMES_HOME}' hermes --profile work --tui --resume ${SESSION_ID}`
    )
  })

  it('quotes a Hermes home with spaces while preserving the command argv', () => {
    const hermesHome = '/srv/hermes data/.hermes'

    expect(buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome, shell: 'login' })).toBe(
      `env HERMES_HOME='${hermesHome}' hermes --profile work --tui --resume ${SESSION_ID}`
    )
    expect(buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome, shell: 'zsh' })).toBe(
      `zsh -ilf -c 'exec env "$@"' hermes-handoff 'HERMES_HOME=${hermesHome}' hermes --profile work --tui --resume ${SESSION_ID}`
    )
  })

  it('normalizes a valid profile using the CLI profile convention', () => {
    expect(
      buildBrowserTerminalHandoff({
        sessionId: SESSION_ID,
        profile: ' Work_1 ',
        hermesHome: HERMES_HOME,
        shell: 'login'
      })
    ).toBe(`env HERMES_HOME='${HERMES_HOME}' hermes --profile work_1 --tui --resume ${SESSION_ID}`)
  })

  it.each(["bad'profile", 'bad"profile', 'bad;profile', 'bad$(profile)', '../profile', '', 'profile name'])(
    'rejects unsafe profile %j',
    profile => {
      expect(
        buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile, hermesHome: HERMES_HOME, shell: 'login' })
      ).toBeNull()
    }
  )

  it.each([
    "session' ; touch /tmp/nope",
    'session";touch /tmp/nope',
    'session$(touch /tmp/nope)',
    'session`touch /tmp/nope`',
    'session with spaces',
    '../session',
    '-resume'
  ])('rejects unsafe session ID %j', sessionId => {
    expect(
      buildBrowserTerminalHandoff({ sessionId, profile: 'work', hermesHome: HERMES_HOME, shell: 'login' })
    ).toBeNull()
  })

  it.each([
    'relative/.hermes',
    '/home/bad\npath/.hermes',
    '/home/bad\tpath/.hermes',
    '/home/has\u007fdel/.hermes',
    "/home/has'quote/.hermes",
    '/home/has\\backslash/.hermes',
    '/home/../other/.hermes',
    '/home/./other/.hermes'
  ])('rejects unsafe Hermes home %j', hermesHome => {
    expect(
      buildBrowserTerminalHandoff({ sessionId: SESSION_ID, profile: 'work', hermesHome, shell: 'login' })
    ).toBeNull()
  })

  it.each(['fish', 'zsh;touch /tmp/nope', '', null, undefined])('rejects shell value %j', shell => {
    expect(
      buildBrowserTerminalHandoff({
        sessionId: SESSION_ID,
        profile: 'work',
        hermesHome: HERMES_HOME,
        shell
      } as never)
    ).toBeNull()
  })
})
