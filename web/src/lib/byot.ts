/**
 * BYOT (bring your own terminal) plumbing for the dashboard Chat tab: the list
 * of external terminals currently bridged to the gateway via
 * ``hermes byot`` (`/api/byot/host`). Attaching one makes the Chat tab dial
 * `/api/pty?byot=<id>` and mirror that terminal instead of the embedded one.
 */

import { fetchJSON } from "@/lib/api";

export interface ByotSession {
  id: string;
  name: string;
  shell?: string | null;
  cwd?: string | null;
  pid?: number | null;
  cols?: number | null;
  rows?: number | null;
  viewers: number;
  created_at: number;
}

interface ByotSessionsResponse {
  sessions?: ByotSession[];
}

/** Live external terminals registered on the gateway, oldest first. */
export async function listByotSessions(): Promise<ByotSession[]> {
  const res = await fetchJSON<ByotSessionsResponse>("/api/byot/sessions");
  return res.sessions ?? [];
}

/**
 * The server's one-off control frame announcing which external terminal a
 * `/api/pty?byot=` socket attached to. Real terminal output always arrives
 * as binary frames, so a text frame with this shape is a control frame.
 */
export function parseByotControlMessage(data: string): ByotSession | null {
  try {
    const parsed = JSON.parse(data);
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.type === "byot" &&
      typeof parsed.id === "string" &&
      parsed.id
    ) {
      return parsed as ByotSession;
    }
  } catch {
    /* not JSON — terminal output or an ANSI banner */
  }
  return null;
}
