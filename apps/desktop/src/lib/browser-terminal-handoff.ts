/**
 * Build a copyable command for resuming a browser session in a remote Hermes
 * TUI. The caller runs it after opening their own SSH terminal to that host.
 * This contains no connection credentials or control socket details.
 */

export type BrowserTerminalHandoffShell = 'login' | 'zsh' | 'bash'

export interface BrowserTerminalHandoffInput {
  sessionId: string
  profile: string
  hermesHome: string
  shell: BrowserTerminalHandoffShell
}

const PROFILE_SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

// Session IDs emitted by Hermes use letters, digits, dots, underscores, and
// hyphens. Keep the first character alphanumeric so an ID cannot look like an
// argv option. Quotes, whitespace, path separators, and shell metacharacters
// are rejected before a command string is built.
const SAFE_SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
// eslint-disable-next-line no-control-regex -- pasted shell command paths must reject control bytes
const HERMES_HOME_CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/

function validatedProfile(profile: unknown): string | null {
  if (typeof profile !== 'string') {
    return null
  }

  const normalized = profile.trim().toLowerCase()

  return normalized === 'default' || PROFILE_SLUG_RE.test(normalized) ? normalized : null
}

function validatedHermesHome(home: unknown): string | null {
  if (
    typeof home !== 'string' ||
    !home.startsWith('/') ||
    home.length > 4096 ||
    HERMES_HOME_CONTROL_RE.test(home) ||
    home.includes("'") ||
    home.includes('\\')
  ) {
    return null
  }

  if (home.split('/').some(part => part === '.' || part === '..')) {
    return null
  }

  return home
}

/** Quote a value for single-quoted shell input; home validation keeps it fish-safe too. */
function shellQuote(value: string): string {
  return `'${value}'`
}

/**
 * Return the remote command for a selected session and shell, or null when any
 * value is outside its allowlist. `login` uses the SSH login shell as-is;
 * `zsh` and `bash` run one interactive login shell for this command only. The
 * Hermes home is scoped to this process and does not change login config.
 */
export function buildBrowserTerminalHandoff(input: BrowserTerminalHandoffInput): string | null {
  if (!input || typeof input !== 'object') {
    return null
  }

  const { sessionId, profile, hermesHome: rawHermesHome, shell } = input as BrowserTerminalHandoffInput
  const pinnedProfile = validatedProfile(profile)
  const hermesHome = validatedHermesHome(rawHermesHome)

  if (typeof sessionId !== 'string' || !SAFE_SESSION_ID_RE.test(sessionId) || !pinnedProfile || !hermesHome) {
    return null
  }

  const resumeArgs = ['hermes', '--profile', pinnedProfile, '--tui', '--resume', sessionId]
  const resumeCommand = resumeArgs.join(' ')

  switch (shell) {
    case 'login':
      // `env` works in fish as well as POSIX shells; avoid `NAME=value command`
      // which fish does not parse as an environment assignment.
      return `env HERMES_HOME=${shellQuote(hermesHome)} ${resumeCommand}`

    case 'zsh':
      // Pass the env assignment and resume argv positionally instead of
      // embedding values in the shell program. This also keeps env assignment
      // syntax portable when the caller pasted the command from a fish shell.
      // -f avoids zsh's first-run setup prompt and leaves startup files alone.
      return `zsh -ilf -c 'exec env "$@"' hermes-handoff ${shellQuote(`HERMES_HOME=${hermesHome}`)} ${resumeCommand}`

    case 'bash':
      return `bash -il -c 'exec env "$@"' hermes-handoff ${shellQuote(`HERMES_HOME=${hermesHome}`)} ${resumeCommand}`

    default:
      return null
  }
}
