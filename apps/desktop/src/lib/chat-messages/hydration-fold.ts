import { skillInvocationText } from '@hermes/shared'

import { formatRefValue } from '@/components/assistant-ui/directive-text'
import { splitLeadingAttachmentRefs } from '@/components/assistant-ui/reference-kinds'
import { CODEX_SUBAGENT_ACTIVITY_TOOL, splitLegacyCodexSubagentActivity } from '@/lib/codex-subagent-activity'
import { extractImageRefs } from '@/lib/embedded-images'
import { parseErrorSurface } from '@/lib/error-surface'
import { dedupeGeneratedImageEchoesInParts } from '@/lib/generated-images'
import { isTodoToolName } from '@/lib/todos'
import type { MessageReaction, SessionMessage } from '@/types/hermes'

import {
  assistantTextPart,
  chatMessageText,
  dedupeRepeatedTextInParts,
  reasoningPart,
  reasoningTextFromDetails,
  renderMediaTags,
  textPart
} from './parts'
import {
  applyStoredToolResult,
  applyStoredToolResultToParts,
  storedToolMessagePart,
  textFromUnknown,
  toolPartFromStoredCall,
  withUniqueToolCallIds
} from './tool-parts'
import type { ChatMessage, ChatMessagePart } from './types'

const ATTACHED_CONTEXT_MARKER_RE = /(?:^|\n)--- Attached Context ---\s*\n/
// A background-process heartbeat wake persisted by a backend older than the
// one that types those rows `display_kind=hidden`. It is model scaffolding,
// not something the user wrote, so it never paints as a bubble.
const LEGACY_HEARTBEAT_ROW_RE = /^\[Background process \S+ heartbeat #\d+ /
const CONTEXT_WARNINGS_MARKER_RE = /(?:^|\n)--- Context Warnings ---[\s\S]*$/
const CONTEXT_REF_RE = /@(file|folder|url|image|tool|terminal):(?:"[^"\n]+"|'[^'\n]+'|`[^`\n]+`|\S+)/g

// Native ingress persists the image-routing hints followed by one placeholder
// per flattened image part. Recognize that suffix only, not quoted caption prose.
function persistedImageRefs(text: string) {
  const suffix = /\n\n((?:\[Image attached(?: at)?: [^\n]+\]\n)+)((?:\[screenshot\](?:\n|$))+)$/.exec(text)

  if (!suffix) { return extractImageRefs(text) }
  const paths = [...suffix[1].matchAll(/^\[Image attached(?: at)?: (.+)\]$/gm)].map(match => match[1])

  if (paths.length !== suffix[2].split('[screenshot]').length - 1) { return extractImageRefs(text) }
  const extracted = extractImageRefs(text.slice(0, suffix.index))

  return { cleanedText: extracted.cleanedText, refs: [...extracted.refs, ...paths.map(path => `@image:${formatRefValue(path)}`)] }
}

// Gateway routing note for Discord turns (gateway/run_inbound.py::discord_triggering_note).
// Current gateways persist the authored text; this heals rows written before that fix. Only
// the note is model-facing — the `[Replying to: …]` pointer next to it is kept.
const DISCORD_TRIGGERING_NOTE_RE =
  /(^|\n)\[Triggering message id: `[^`\n]*` — use as `message_id` for reply\/react\/pin via the discord tools\.\]\n*/

/**
 * Backend history projection authorizes/sanitizes public commentary before it
 * reaches Desktop. Raw Responses sidecars are used only for final-answer fallback;
 * phase=analysis and raw phase=commentary are never promoted to assistant text.
 */
function codexMessageItemText(message: SessionMessage): { commentary: string[]; reply: string } {
  let items = message.codex_message_items

  const commentary = Array.isArray(message.display_commentary)
    ? message.display_commentary.filter((part): part is string => typeof part === 'string' && Boolean(part.trim()))
    : []

  const replies: string[] = []

  // REST carries SQLite JSON text; RPC history carries the decoded list.
  if (typeof items === 'string') {
    try {
      items = JSON.parse(items)
    } catch {
      return { commentary, reply: '' }
    }
  }

  if (!Array.isArray(items)) {
    return { commentary, reply: '' }
  }

  for (const item of items) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      continue
    }

    const record = item as Record<string, unknown>

    if (record.type !== 'message' || record.role !== 'assistant') {
      continue
    }

    const phase = typeof record.phase === 'string' ? record.phase.trim().toLowerCase() : ''

    if (phase === 'analysis' || phase === 'commentary' || !Array.isArray(record.content)) {
      continue
    }

    const chunks: string[] = []

    for (const part of record.content) {
      if (!part || typeof part !== 'object' || Array.isArray(part)) {
        continue
      }

      const partRecord = part as Record<string, unknown>

      if ((partRecord.type === 'output_text' || partRecord.type === 'text') && typeof partRecord.text === 'string') {
        chunks.push(partRecord.text)
      }
    }

    const text = chunks.join('')

    if (text) {
      replies.push(text)
    }
  }

  return { commentary, reply: replies.join('') }
}

function displayContentForMessage(role: SessionMessage['role'], content: unknown): string {
  const rawText = textFromUnknown(content)

  if (role !== 'user') {
    return rawText
  }

  const textContent = rawText.replace(DISCORD_TRIGGERING_NOTE_RE, '$1')

  // A `/skill` turn is stored expanded (the whole skill body). Current
  // gateways project it to the invocation before it ever reaches us; this is
  // the fallback for an older backend that still ships the raw payload.
  const invocation = skillInvocationText(textContent)

  if (invocation) {
    return invocation
  }

  const marker = textContent.match(ATTACHED_CONTEXT_MARKER_RE)

  if (!marker || marker.index === undefined) {
    return textContent.replace(CONTEXT_WARNINGS_MARKER_RE, '').trim()
  }

  const visibleText = textContent.slice(0, marker.index).replace(CONTEXT_WARNINGS_MARKER_RE, '').trim()
  const attachedContext = textContent.slice(marker.index + marker[0].length)
  const refs = [...new Set(Array.from(attachedContext.matchAll(CONTEXT_REF_RE)).map(match => match[0]))]

  // The prose keeps the `@file:` token the user typed, so it already chips in
  // place. Only hoist a ref the prose is missing — a turn persisted by an older
  // backend that stripped the tokens. Re-listing an inline ref would chip twice.
  const missing = refs.filter(ref => !visibleText.includes(ref))

  return [missing.join('\n'), visibleText].filter(Boolean).join('\n\n') || visibleText
}

function transcriptContent(
  displayKind: SessionMessage['display_kind'],
  role: SessionMessage['role'],
  content: string
): string | null {
  if (displayKind === 'hidden') {
    return null
  }

  return role === 'user' && LEGACY_HEARTBEAT_ROW_RE.test(content.trim()) ? null : content
}

/**
 * Backend-authored transcript notices. The gateway persists these itself and no
 * view "sent" them, so they render as system rows.
 */
const NOTICE_DISPLAY_KINDS = [
  'model_switch',
  'async_delegation_complete',
  'process_complete',
  'auto_continue',
  'personality_switch',
  // Hermes closing a failed turn, not the model speaking.
  'failed_turn',
  // A safety-refused turn retained for display: the user's own message, shown
  // greyed with a notice. Rendered through the system path, never a user bubble.
  'content_refused'
] as const

// agent/turn_failure_copy.py::CONTENT_REFUSED_DISPLAY_KIND. The retained turn
// arrives as a single `user` row carrying the user's original message plus a
// notice in display_metadata.content_refused (CONTENT_REFUSED_METADATA_KEY).
const CONTENT_REFUSED_DISPLAY_KIND = 'content_refused'

function isMachineNotice(displayKind: SessionMessage['display_kind']): boolean {
  return displayKind !== undefined && (NOTICE_DISPLAY_KINDS as readonly string[]).includes(displayKind)
}

// A remote backend older than this app serves display_metadata as raw JSON text,
// and `in` throws on a primitive — which used to fail the whole session resume.
function parseDisplayMetadata(metadata: SessionMessage['display_metadata']): null | Record<string, unknown> {
  let parsed: unknown = metadata

  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch {
      return null
    }
  }

  return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null
}

// A micro-compaction merge (`display_metadata.model_only`) is the model's copy of prompts whose originals
// the history shows: the owner's resume snapshot keeps it as an undo target, and it is never painted.
function isPainted(message: SessionMessage): boolean {
  return parseDisplayMetadata(message.display_metadata)?.model_only !== true
}

// `agent/conversation_loop.py::_failed_turn_display_metadata`: the failed turn's error card,
// kept on its boundary row so a reopened session shows it after the live frame is gone.
function failedTurnError(
  metadata: SessionMessage['display_metadata']
): null | Pick<ChatMessage, 'error' | 'errorSurface'> {
  const parsed = parseDisplayMetadata(metadata)
  const errorSurface = parseErrorSurface(parsed?.error_surface)

  if (!errorSurface) {
    return null
  }

  const error = typeof parsed?.error === 'string' && parsed.error.trim() ? parsed.error : errorSurface.code

  return { error, errorSurface }
}

// agent/turn_failure_copy.py::CONTENT_REFUSED_NOTICE, stored under
// display_metadata.content_refused.notice. Absent on older backends — the
// renderer falls back to an i18n string then.
function contentRefusedNotice(metadata: SessionMessage['display_metadata']): string | undefined {
  const refused = parseDisplayMetadata(metadata)?.content_refused

  if (!refused || typeof refused !== 'object') {
    return undefined
  }

  const notice = (refused as Record<string, unknown>).notice

  return typeof notice === 'string' && notice.trim() ? notice : undefined
}

// The spreadable contentRefused field for a hydrated row: present only on a
// retained safety-refused turn, carrying its notice when the backend supplied
// one. Extracted so toChatMessages keeps its complexity within the ratchet.
function contentRefusedField(
  message: SessionMessage
): { contentRefused: { notice?: string } } | Record<string, never> {
  if (message.display_kind !== CONTENT_REFUSED_DISPLAY_KIND) {
    return {}
  }

  const notice = contentRefusedNotice(message.display_metadata)

  return { contentRefused: notice ? { notice } : {} }
}

function timelineTaskCount(metadata: SessionMessage['display_metadata']): number | undefined {
  const count = parseDisplayMetadata(metadata)?.task_count

  return typeof count === 'number' ? count : undefined
}

function timelineDisplayText(metadata: SessionMessage['display_metadata']): string | undefined {
  const text = parseDisplayMetadata(metadata)?.display_text

  return typeof text === 'string' && text.trim() ? text : undefined
}

function messageInterrupted(metadata: SessionMessage['display_metadata']): boolean {
  return parseDisplayMetadata(metadata)?.interrupted === true
}

/** An assistant row read into the tool bubble before it. A reply stopped after a
 *  tool round folds in here, and its row still says the reply was stopped. */
function foldIntoToolBubble(
  bubble: ChatMessage,
  message: SessionMessage,
  parts: ChatMessagePart[],
  durableComplete: boolean | undefined
): void {
  bubble.parts = [...bubble.parts, ...parts]
  bubble.durableComplete = durableComplete

  if (messageInterrupted(message.display_metadata)) {
    bubble.interrupted = true
  }
}

function messageReactions(metadata: SessionMessage['display_metadata']): MessageReaction[] {
  const reactions = parseDisplayMetadata(metadata)?.reactions

  if (!Array.isArray(reactions)) {
    return []
  }

  return reactions.filter(
    (r): r is MessageReaction => Boolean(r) && typeof r === 'object' && typeof (r as MessageReaction).emoji === 'string'
  )
}

// Only parse producer-owned boundaries, never render the model's task preamble.
// Older backends can persist an unwrapped result rather than an envelope.
function asyncResultBody(content: string): string | undefined {
  let bodies = [content]

  if (content.startsWith('[IMPORTANT: ')) {
    // Background-process completion: one `[IMPORTANT: …]` block per process, a batch header first.
    bodies = content
      .split(/\n\n(?=\[IMPORTANT: )/)
      .map(block => block.replace(/^\[IMPORTANT:\s*/, '').replace(/\]$/, ''))
      .filter(block => !/^\d+ background processes completed\./.test(block))
  } else if (content.startsWith('[ASYNC DELEGATION')) {
    if (content.startsWith('[ASYNC DELEGATION BATCH COMPLETE')) {
      // Task goals can span lines; stopping at a newline leaks the next goal and transcript footer.
      bodies = content.split(/^--- [✓✗⚠] TASK \d+\/\d+(?:: [\s\S]*?)? {2}\(status=[^\n]*\) ---\r?\n/gm).slice(1)
    } else {
      const result = content.match(/^--- (?:RESULT|ERROR) ---\r?\n/m)
      bodies = result ? [content.slice(result.index! + result[0].length)] : []
    }
  }

  return (
    bodies
      .map(body => {
        const output = body.startsWith('Cron job ') ? body.match(/^--- JOB OUTPUT ---\r?\n/m) : null
        const result = output ? body.slice(output.index! + output[0].length) : body

        return result.replace(/\nFull live transcript \(complete tool\/assistant trace\): [^\n]*\n*$/, '').trim()
      })
      .filter(Boolean)
      .join('\n\n') || undefined
  )
}

function timelineDisplayContent(message: SessionMessage, content: string): string {
  if (message.display_kind === 'model_switch') {
    return 'model changed'
  }

  if (message.display_kind === 'auto_continue') {
    return 'resumed interrupted turn'
  }

  if (message.display_kind === 'personality_switch') {
    return 'personality changed'
  }

  if (message.display_kind === 'async_delegation_complete') {
    const count = timelineTaskCount(message.display_metadata)

    return (
      timelineDisplayText(message.display_metadata) ??
      (count === undefined
        ? 'background agent work finished'
        : `${count} background agent${count === 1 ? '' : 's'} finished`)
    )
  }

  if (message.display_kind === 'process_complete') {
    return timelineDisplayText(message.display_metadata) ?? 'background process finished'
  }

  return content
}

function legacyActivityParts(reply: string, timestamp: number | undefined): ChatMessagePart[] {
  return splitLegacyCodexSubagentActivity(reply).flatMap((segment, segmentIndex) => {
    if (segment.type === 'text') {
      return segment.text ? [assistantTextPart(segment.text, timestamp)] : []
    }
    const args = {
      kind: segment.activity.kind,
      agent_thread_id: segment.activity.agentThreadId,
      model: segment.activity.model,
      reasoning_effort: segment.activity.reasoningEffort
    }
    const activityPart = toolPartFromStoredCall(
      {
        id: segment.id || `codex_subAgentActivity_legacy_${segmentIndex}`,
        function: { name: CODEX_SUBAGENT_ACTIVITY_TOOL, arguments: JSON.stringify(args) }
      },
      segmentIndex,
      timestamp
    )
    return [{ ...activityPart, completedAt: timestamp, result: '' } as ChatMessagePart]
  })
}

function messageDisplayProjection(
  message: SessionMessage
): { displayRole: SessionMessage['role']; displayContent: string; attachmentRefs?: string[] } {
  const content = message.display_content !== undefined
    ? message.display_content
    : message.content || message.text || message.context || message.name
  const raw = transcriptContent(
    message.display_kind,
    message.role,
    timelineDisplayContent(message, displayContentForMessage(message.role, content))
  ) ?? ''
  const displayRole = isMachineNotice(message.display_kind) ? 'system' : message.role
  const imageRefs = displayRole === 'user' && raw ? persistedImageRefs(raw) : null
  const fileRefs = imageRefs ? splitLeadingAttachmentRefs(imageRefs.cleanedText) : null
  const displayContent = fileRefs ? fileRefs.text : raw
  const refs = [...(fileRefs?.refs ?? []), ...(imageRefs?.refs ?? [])]
  return { displayRole, displayContent, ...(refs.length ? { attachmentRefs: refs } : {}) }
}

function messageParts(
  message: SessionMessage,
  displayRole: SessionMessage['role'],
  displayContent: string
): ChatMessagePart[] {
  const parts: ChatMessagePart[] = []
  appendMessageReasoning(parts, message)
  appendMessageText(parts, message, displayRole, displayContent)
  appendStoredToolCalls(parts, message)
  attachSourceRowId(parts, message)
  return parts
}

function appendMessageReasoning(parts: ChatMessagePart[], message: SessionMessage): void {
  const raw = message.reasoning || message.reasoning_content || reasoningTextFromDetails(message.reasoning_details)
  const reasoning = message.display_reasoning !== undefined ? message.display_reasoning : raw
  if (reasoning && message.role === 'assistant') {
    parts.push(reasoningPart(reasoning, message.timestamp))
  }
}

function appendMessageText(
  parts: ChatMessagePart[],
  message: SessionMessage,
  displayRole: SessionMessage['role'],
  displayContent: string
): void {
  const codexText = displayRole === 'assistant' && message.display_kind !== 'hidden'
    ? codexMessageItemText(message)
    : null
  const commentary = codexText?.commentary ?? []
  const reply = message.display_content !== undefined ? displayContent : displayContent || codexText?.reply
  const commentaryIsReply = Boolean(
    reply && commentary.length && normalizedDisplayText(commentary.join('\n\n')) === normalizedDisplayText(reply)
  )
  if (!commentaryIsReply) {
    parts.push(...commentary.map(text => assistantTextPart(text, message.timestamp)))
  }
  if (reply) {
    parts.push(...(displayRole === 'assistant'
      ? legacyActivityParts(reply, message.timestamp)
      : [textPart(reply, message.timestamp)]))
  }
}

function appendStoredToolCalls(parts: ChatMessagePart[], message: SessionMessage): void {
  if (message.role === 'assistant' && Array.isArray(message.tool_calls)) {
    parts.push(...message.tool_calls.map((call, callIndex) =>
      toolPartFromStoredCall(call, callIndex, message.timestamp, message.tool_call_labels)))
  }
}

function attachSourceRowId(parts: ChatMessagePart[], message: SessionMessage): void {
  const rowId = message.row_id ?? (typeof message.id === 'number' ? message.id : undefined)
  if (rowId !== undefined) {
    for (const part of parts) {
      if (part.type === 'text') {
        part.sourceRowId = rowId
      }
    }
  }
}

function normalizedDisplayText(value: string): string {
  return renderMediaTags(value).replace(/\s+/g, ' ').trim()
}

function asyncResultForMessage(message: SessionMessage): string | undefined {
  const content = message.display_content !== undefined
    ? message.display_content
    : message.content || message.text || message.context || message.name
  return asyncResultBody(displayContentForMessage(message.role, message.content || content))
}

class TranscriptFolder {
  readonly result: ChatMessage[] = []
  private pendingToolParts: ChatMessagePart[] = []
  private pendingToolTimestamp: number | undefined
  private pendingToolRows = 0
  private activeAssistantIndex: number | null = null
  private nearestAssistant: SessionMessage | null = null

  accept(message: SessionMessage, index: number): void {
    if (message.role === 'assistant') {
      this.nearestAssistant = message
    } else if (message.role === 'user' || message.role === 'system') {
      this.nearestAssistant = null
    }
    if (message.role === 'tool') {
      this.foldTool(message, index)
      return
    }
    this.foldMessage(message, index)
  }

  finish(rowCount: number): ChatMessage[] {
    this.flushPendingTools(rowCount)
    const deduped = this.result.map(message => message.role === 'assistant'
      ? { ...message, parts: dedupeRepeatedTextInParts(dedupeGeneratedImageEchoesInParts(message.parts)) }
      : message)
    return withUniqueToolCallIds(deduped.filter(message =>
      chatMessageText(message).trim() || message.parts.some(part => part.type !== 'text') ||
      message.attachmentRefs?.length || message.error || message.contentRefused))
  }

  private isPairedTodoResult(message: SessionMessage): boolean {
    const id = message.tool_call_id
    if (!id || !Array.isArray(this.nearestAssistant?.tool_calls)) {
      return false
    }
    return this.nearestAssistant.tool_calls.some((call, index) => {
      const part = toolPartFromStoredCall(call, index)
      if (part.type !== 'tool-call' || part.toolCallId !== id) {
        return false
      }
      if (isTodoToolName(part.toolName)) {
        return true
      }
      const args = part.args as { calls?: unknown }
      return part.toolName === 'tool_call' && Array.isArray(args?.calls) &&
        args.calls.some(inner => inner && typeof inner === 'object' && isTodoToolName(inner.name))
    })
  }

  private foldTool(message: SessionMessage, index: number): void {
    if (isTodoToolName(message.tool_name) && !this.isPairedTodoResult(message)) {
      this.queueTool(storedToolMessagePart(message, index), message.timestamp)
      return
    }
    const updated = applyStoredToolResultToParts(this.pendingToolParts, message)
    if (updated) {
      this.pendingToolParts = updated
      this.pendingToolRows += 1
      return
    }
    if (!applyStoredToolResult(this.result, message)) {
      this.queueTool(storedToolMessagePart(message, index), message.timestamp)
    }
  }

  private queueTool(part: ChatMessagePart, timestamp?: number): void {
    this.pendingToolParts = [...this.pendingToolParts, part]
    this.pendingToolTimestamp ??= timestamp
    this.pendingToolRows += 1
  }

  private clearPendingTools(): void {
    this.pendingToolParts = []
    this.pendingToolTimestamp = undefined
    this.pendingToolRows = 0
  }

  private absorbRows(message: ChatMessage | undefined, rows: number): void {
    if (message && rows > 0) {
      message.serverRowSpan = (message.serverRowSpan ?? 1) + rows
    }
  }

  private appendPartsToActiveAssistant(parts: ChatMessagePart[], timestamp?: number): boolean {
    if (this.activeAssistantIndex === null) {
      return false
    }
    const active = this.result[this.activeAssistantIndex]
    if (!active || active.role !== 'assistant') {
      this.activeAssistantIndex = null
      return false
    }
    active.parts = [...active.parts, ...parts]
    active.durableComplete = false
    active.timestamp = earliestTimestamp(active.timestamp, timestamp, ...parts.map(part => part.timestamp))
    this.absorbRows(active, this.pendingToolRows)
    return true
  }

  private flushPendingTools(index: number): void {
    if (!this.pendingToolParts.length) {
      return
    }
    if (!this.appendPartsToActiveAssistant(this.pendingToolParts, this.pendingToolTimestamp)) {
      this.result.push({
        id: `${this.pendingToolTimestamp || Date.now()}-${index}-tools`,
        role: 'assistant',
        parts: this.pendingToolParts,
        durableComplete: false,
        ...(this.pendingToolRows > 1 ? { serverRowSpan: this.pendingToolRows } : {}),
        timestamp: this.pendingToolTimestamp
      })
      this.activeAssistantIndex = this.result.length - 1
    }
    this.clearPendingTools()
  }

  private lendStoppedMark(message: SessionMessage, index: number): void {
    if (!messageInterrupted(message.display_metadata)) {
      return
    }
    this.flushPendingTools(index)
    const bubble = this.activeAssistantIndex === null ? undefined : this.result[this.activeAssistantIndex]
    if (bubble?.role === 'assistant') {
      bubble.interrupted = true
    }
  }

  private foldMessage(message: SessionMessage, index: number): void {
    const projection = messageDisplayProjection(message)
    const parts = messageParts(message, projection.displayRole, projection.displayContent)
    const rowId = message.row_id ?? (typeof message.id === 'number' ? message.id : undefined)
    const sourceHasTools = Array.isArray(message.tool_calls) && message.tool_calls.length > 0
    const durableComplete = sourceHasTools ? false : rowId !== undefined ? true : undefined
    if (!parts.length && !projection.attachmentRefs?.length) {
      if (message.role === 'assistant') {
        this.lendStoppedMark(message, index)
      } else {
        this.flushPendingTools(index)
        this.activeAssistantIndex = null
      }
      return
    }
    if (message.role === 'assistant' && parts.length > 0 && parts.every(part => part.type === 'tool-call')) {
      for (const part of parts) {
        this.queueTool(part, message.timestamp)
      }
      return
    }
    const boundary = this.foldAssistantBoundary(message, index, parts, durableComplete, projection.displayRole)
    if (boundary.folded) {
      return
    }
    const failure = message.display_kind === 'failed_turn' ? failedTurnError(message.display_metadata) : null
    if (failure) {
      this.result.push({
        id: `${message.timestamp || Date.now()}-${index}-failed-turn-error`,
        role: 'assistant',
        parts: [],
        pending: false,
        serverRowSpan: 0,
        ...failure,
        ...(message.timestamp ? { timestamp: message.timestamp } : {})
      })
    }
    this.pushProjectedMessage(message, index, projection, parts, durableComplete, rowId, boundary.absorbedRows)
  }

  private foldAssistantBoundary(
    message: SessionMessage,
    index: number,
    parts: ChatMessagePart[],
    durableComplete: boolean | undefined,
    displayRole: SessionMessage['role']
  ): { folded: boolean; absorbedRows: number } {
    if (displayRole !== 'assistant') {
      this.flushPendingTools(index)
      return { folded: false, absorbedRows: 0 }
    }
    let absorbedRows = 0
    if (this.pendingToolParts.length) {
      if (!this.appendPartsToActiveAssistant(this.pendingToolParts, message.timestamp ?? this.pendingToolTimestamp)) {
        parts.unshift(...this.pendingToolParts)
        absorbedRows = this.pendingToolRows
      }
      this.clearPendingTools()
    }
    const active = this.activeAssistantIndex === null ? null : this.result[this.activeAssistantIndex]
    const activeAssistant = active?.role === 'assistant' ? active : null
    const currentHasToolCall = parts.some(part => part.type === 'tool-call')
    const activeHasToolCall = Boolean(activeAssistant?.parts.some(part => part.type === 'tool-call'))
    if (activeAssistant && (currentHasToolCall || activeHasToolCall)) {
      foldIntoToolBubble(activeAssistant, message, parts, durableComplete)
      activeAssistant.timestamp = earliestTimestamp(
        activeAssistant.timestamp, message.timestamp, ...parts.map(part => part.timestamp)
      )
      this.absorbRows(activeAssistant, 1)
      return { folded: true, absorbedRows: 0 }
    }
    return { folded: false, absorbedRows }
  }

  private pushProjectedMessage(
    message: SessionMessage,
    index: number,
    projection: ReturnType<typeof messageDisplayProjection>,
    parts: ChatMessagePart[],
    durableComplete: boolean | undefined,
    rowId: number | undefined,
    absorbedRows: number
  ): void {
    const reactions = messageReactions(message.display_metadata)
    this.result.push({
      id: `${message.timestamp || Date.now()}-${index}-${projection.displayRole}`,
      role: projection.displayRole,
      parts,
      ...(message.role === 'assistant' && durableComplete !== undefined ? { durableComplete } : {}),
      ...(message.display_kind === 'async_delegation_complete' || message.display_kind === 'process_complete'
        ? { asyncResult: asyncResultForMessage(message) }
        : {}),
      ...(message.display_kind === 'process_complete' ? { asyncResultKind: 'process' as const } : {}),
      timestamp: earliestTimestamp(message.timestamp, ...parts.map(part => part.timestamp)),
      ...(rowId !== undefined ? { rowId } : {}),
      ...(absorbedRows > 0 ? { serverRowSpan: absorbedRows + 1 } : {}),
      ...(reactions.length ? { reactions } : {}),
      ...(message.role === 'assistant' && messageInterrupted(message.display_metadata) ? { interrupted: true } : {}),
      ...(projection.attachmentRefs ? { attachmentRefs: projection.attachmentRefs } : {}),
      ...contentRefusedField(message)
    })
    this.activeAssistantIndex = message.role === 'assistant' ? this.result.length - 1 : null
  }
}

function earliestTimestamp(...values: (number | undefined)[]): number | undefined {
  const timestamps = values.filter((value): value is number => value !== undefined)
  return timestamps.length ? Math.min(...timestamps) : undefined
}

export function toChatMessages(messages: SessionMessage[]): ChatMessage[] {
  const folder = new TranscriptFolder()
  messages.forEach((message, index) => {
    if (isPainted(message)) {
      folder.accept(message, index)
    }
  })
  return folder.finish(messages.length)
}
