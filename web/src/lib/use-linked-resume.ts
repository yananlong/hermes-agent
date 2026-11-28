import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { linkedForeignAgent } from "@/lib/linked-session";
import {
  linkedResumeBlocked as linkedResumeBlockedFor,
  linkedResumeOverlay,
  linkedResumeReadonly,
  type LinkedResumeCheck,
} from "@/lib/linked-session";

/**
 * Resolve one resume target's linked-session verdict for the chat pane.
 *
 * The answer is TRI-STATE and fails CLOSED. `verified` is only true when the gateway answered for
 * this exact id: a 404 is definitive (no such stored row, so it cannot be a pointer, and the
 * ordinary resume path stays available), while any other failure means we cannot prove the row is
 * not a pointer, so no PTY is spawned for it.
 *
 * A single-session lookup (`GET /api/sessions/{id}`, which stamps the same `*_linked` markers as
 * the list) is deliberate: the paged list would make a linked row outside the first page, or any
 * list error, read as an ordinary session and get resumed into a PTY.
 *
 * A verified linked row is OPENED, not refused — the embedded TUI is a canonical client that pages
 * the native transcript and appends to the same native thread — but it stays read-only here (no
 * model/workspace controls, no second transcript). What must still wait is an UNANSWERED lookup.
 */
export function useLinkedResume(resumeParam: string | null | undefined, profile: string | undefined) {
  const [check, setCheck] = useState<LinkedResumeCheck | null>(null);

  useEffect(() => {
    // No resume target: nothing to check. The stale check stays keyed to its own id, so a later
    // resume target reads as "pending" until its own lookup lands.
    if (!resumeParam) return;
    let cancelled = false;
    api
      .getSessionDetail(resumeParam, profile ?? undefined)
      .then((row) => {
        if (cancelled) return;
        setCheck({ id: resumeParam, agent: linkedForeignAgent(row), verified: true });
      })
      .catch((err: unknown) => {
        const status = (err as { status?: number } | null)?.status ?? 0;
        if (!cancelled) setCheck({ id: resumeParam, agent: null, verified: status === 404 });
      });
    return () => {
      cancelled = true;
    };
  }, [resumeParam, profile]);

  return {
    check,
    unverified: Boolean(resumeParam && check?.id === resumeParam && !check.verified),
    // One definition of the gate (web/src/lib/linked-session.ts): it fails CLOSED on a pending or
    // failed lookup, so no PTY is ever spawned on a row we could not prove is an ordinary session.
    blocked: linkedResumeBlockedFor(check, resumeParam),
    // The gate blocks on a pending lookup, but "blocked" is two different screens: until the
    // answer lands there is no notice to show, so covering the canvas with the verdict panel would
    // flash an empty overlay with a lone "New chat" on every resume.
    pending: Boolean(resumeParam && check?.id !== resumeParam),
    overlay: linkedResumeOverlay(check, resumeParam),
    readOnly: linkedResumeReadonly(check, resumeParam),
    agent: linkedResumeReadonly(check, resumeParam) ? (check?.agent ?? null) : null,
  };
}
