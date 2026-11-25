import { useCallback, useState } from "react";
import { readStoredWorkspace, writeStoredWorkspace } from "@/lib/chat-workspaces";

/** The picker selects the workspace for the next fresh chat, per profile. */
export function useProfileWorkspace(profile: string) {
  const [workspace, setWorkspace] = useState(() => readStoredWorkspace(profile));
  const [owner, setOwner] = useState(profile);
  if (owner !== profile) {
    setOwner(profile);
    setWorkspace(readStoredWorkspace(profile));
  }
  const select = useCallback((next: string) => {
    writeStoredWorkspace(profile, next);
    setWorkspace(next);
  }, [profile]);
  return [workspace, select] as const;
}
