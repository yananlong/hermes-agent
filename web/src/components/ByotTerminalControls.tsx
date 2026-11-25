import { useEffect, useState } from "react";
import { Button } from "@nous-research/ui/ui/components/button";
import { listByotSessions, type ByotSession } from "@/lib/byot";

interface ByotTerminalControlsProps {
  isActive: boolean;
  currentId: string | null;
  onSelect: (id: string | null) => void;
}

export function ByotTerminalControls({ isActive, currentId, onSelect }: ByotTerminalControlsProps) {
  const [sessions, setSessions] = useState<ByotSession[]>([]);
  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;
    const load = async () => {
      try {
        const result = await listByotSessions();
        if (!cancelled) setSessions(result);
      } catch {
        // Older servers do not expose the optional terminal inventory.
        if (!cancelled) setSessions([]);
      }
    };
    void load();
    const timer = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [isActive, currentId]);

  if (!sessions.length && !currentId) return null;
  const active = sessions.find((session) => session.id === currentId);
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Terminal selection">
      <span>{currentId ? `External terminal: ${active?.name ?? currentId}` : "External terminals:"}</span>
      {sessions.map((session) => (
        <Button key={session.id} size="sm" outlined disabled={session.id === currentId}
          onClick={() => onSelect(session.id)} aria-label={`Attach external terminal ${session.name}`}
          title={`${session.shell ?? "terminal"} — ${session.cwd ?? ""}`}>
          {session.name}
        </Button>
      ))}
      {currentId && (
        <Button size="sm" outlined onClick={() => onSelect(null)}
          aria-label="Detach external terminal and use the embedded one">
          Use embedded terminal
        </Button>
      )}
    </div>
  );
}
