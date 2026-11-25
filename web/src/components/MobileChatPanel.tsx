import { createPortal } from "react-dom";
import { Button } from "@nous-research/ui/ui/components/button";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import { X } from "lucide-react";
import { ChatSidebar } from "@/components/ChatSidebar";
import { ChatSessionList } from "@/components/ChatSessionList";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

interface MobileChatPanelProps {
  isActive: boolean;
  narrow: boolean;
  portalRoot: HTMLElement | null;
  mobilePanelOpen: boolean;
  closeMobilePanel: () => void;
  modelToolsLabel: string;
  channel: string;
  scopedProfile: string;
  startFreshDashboardChat: () => void;
  handleSessionTitleChange: (title: string | null) => void;
  resumeParam: string | null;
  workspaceCwd: string;
  setWorkspaceCwd: (value: string) => void;
}

export function MobileChatPanel({ isActive, narrow, portalRoot, mobilePanelOpen, closeMobilePanel,
  modelToolsLabel, channel, scopedProfile, startFreshDashboardChat, handleSessionTitleChange,
  resumeParam, workspaceCwd, setWorkspaceCwd }: MobileChatPanelProps) {
  const { t } = useI18n();
  if (!isActive || !narrow || !portalRoot) return null;
  return createPortal(
      <>
        {mobilePanelOpen && (
          <Button
            ghost
            aria-label={t.app.closeModelTools}
            onClick={closeMobilePanel}
            className={cn(
              "fixed inset-0 z-[55] p-0 block",
              "bg-black/60",
            )}
          />
        )}

        <div
          id="chat-side-panel"
          role="complementary"
          aria-label={modelToolsLabel}
          className={cn(
            "font-mondwest fixed top-0 right-0 z-[60] flex h-dvh max-h-dvh w-64 min-w-0 flex-col antialiased",
            "border-l border-current/20 text-midground",
            "bg-background-base/95",
            "transition-transform duration-200 ease-out",
            "[background:var(--component-sidebar-background,var(--background-base))]",
            "[clip-path:var(--component-sidebar-clip-path)]",
            "[border-image:var(--component-sidebar-border-image)]",
            mobilePanelOpen
              ? "translate-x-0"
              : "pointer-events-none translate-x-full",
          )}
        >
          <div
            className={cn(
              "flex h-14 shrink-0 items-center justify-between gap-2 border-b border-current/20 px-5",
            )}
          >
            <Typography
              mondwest
              className="text-display font-bold text-[1.125rem] leading-[0.95] tracking-[0.0525rem] text-midground"
            >
              {t.app.modelToolsSheetTitle}
              <br />
              {t.app.modelToolsSheetSubtitle}
            </Typography>

            <Button
              ghost
              size="icon"
              onClick={closeMobilePanel}
              aria-label={t.app.closeModelTools}
              className="text-text-secondary hover:text-midground"
            >
              <X />
            </Button>
          </div>

          <div
            className={cn(
              "min-h-0 flex-1 overflow-y-auto overflow-x-hidden",
              "border-t border-current/10",
            )}
          >
            <div className="border-b border-current/10 px-1 py-2">
              <ChatSidebar
                channel={channel}
                profile={scopedProfile}
                onDashboardNewSessionRequest={startFreshDashboardChat}
                onSessionTitleChange={handleSessionTitleChange}
              />
            </div>
            <ChatSessionList
              activeSessionId={resumeParam}
              isActive={isActive}
              profile={scopedProfile}
              onPicked={closeMobilePanel}
              onNewChat={startFreshDashboardChat}
              workspaceCwd={workspaceCwd}
              onWorkspaceChange={setWorkspaceCwd}
            />
          </div>
        </div>
      </>,
      portalRoot,
    );
}
