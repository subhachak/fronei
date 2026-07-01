'use client'

import { CheckCircle2, ChevronsLeft, ChevronsRight, Folder, Library, Loader2, Moon, Settings2, Shield, Sparkles, Sun, UserCog, type LucideIcon } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useAgent } from '../hooks/useAgent'
import { useTheme } from '../hooks/useTheme'
import { AdminShell } from '../admin/components/AdminShell'
import { brandAsset } from '../lib/brand'
import { clamp } from '../lib/format'
import { engineEventsCopyText } from '../lib/commentary'
import { Composer } from './Composer'
import { ContextPanel } from './ContextPanel'
import { LibraryPanel } from './LibraryPanel'
import { ProfileView } from './ProfileView'
import { PausedApprovalCard } from './PausedApprovalCard'
import { Timeline } from './Timeline'
import { Badge } from './ui/Card'
import { Button } from './ui/Button'
import { CopyButton } from './ui/CopyButton'
import { Modal } from './ui/Modal'
import { Sheet } from './ui/Sheet'

const MIN_LEFT_RAIL_WIDTH = 240
const MAX_LEFT_RAIL_WIDTH = 420
const MIN_COMPOSER_HEIGHT = 152
const MAX_COMPOSER_HEIGHT = 340
const LEFT_RAIL_COLLAPSED_KEY = 'agent-shell:left-rail-collapsed'

function readStoredCollapsedState(key: string) {
  if (typeof window === 'undefined') return false
  try {
    return localStorage.getItem(key) === 'true'
  } catch {
    return false
  }
}

function writeStoredCollapsedState(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? 'true' : 'false')
  } catch {
    /* ignore */
  }
}

export function AgentShell() {
  const agent = useAgent()
  const { theme, toggleTheme } = useTheme()

  const [librarySheetOpen, setLibrarySheetOpen] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [leftRailWidth, setLeftRailWidth] = useState(280)
  const [composerHeight, setComposerHeight] = useState(168)
  const [leftRailCollapsed, setLeftRailCollapsed] = useState(false)
  const [workModalOpen, setWorkModalOpen] = useState(false)
  const [prefsPopoverOpen, setPrefsPopoverOpen] = useState(false)
  const [uploadSource, setUploadSource] = useState<'composer' | 'profile'>('profile')
  const [view, setView] = useState<'chat' | 'profile' | 'admin'>('chat')
  const templateUploadRef = useRef<HTMLInputElement | null>(null)
  const attachFileRef = useRef<HTMLInputElement | null>(null)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)

  // Smooth scroll for structural events: new turn, run start/stop, completion.
  useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [agent.visibleTurns.length, agent.running, agent.result?.turn_id])

  // Instant scroll while streaming — follows content at RAF cadence (60fps max).
  // Uses scrollTop assignment (no smooth easing) so it never fights the smooth-scroll
  // above and gives no perceived lag between text appearing and the view following.
  useEffect(() => {
    if (!agent.running || !chatScrollRef.current) return
    chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight
  }, [agent.liveAnswer, agent.running])

  // useLayoutEffect fires synchronously before the browser paints, correcting the
  // collapsed state from localStorage without a visible flash. useState(false) keeps
  // the SSR-rendered HTML consistent (server has no window → returns false), so
  // React hydrates without a mismatch warning. The layout correction happens before
  // the first painted frame, so the user never sees the expanded→collapsed shift.
  useLayoutEffect(() => {
    setLeftRailCollapsed(readStoredCollapsedState(LEFT_RAIL_COLLAPSED_KEY))
  }, [])

  useEffect(() => {
    writeStoredCollapsedState(LEFT_RAIL_COLLAPSED_KEY, leftRailCollapsed)
  }, [leftRailCollapsed])

  function openTemplateUpload(source: 'composer' | 'profile') {
    setUploadSource(source)
    templateUploadRef.current?.click()
  }

  function openAttachFile() {
    attachFileRef.current?.click()
  }

  function beginHorizontalResize(event: ReactPointerEvent) {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = leftRailWidth
    const onMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX
      setLeftRailWidth(clamp(startWidth + delta, MIN_LEFT_RAIL_WIDTH, MAX_LEFT_RAIL_WIDTH))
    }
    const onUp = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp, { once: true })
  }

  function beginComposerResize(event: ReactPointerEvent) {
    event.preventDefault()
    const startY = event.clientY
    const startHeight = composerHeight
    const onMove = (moveEvent: PointerEvent) => {
      setComposerHeight(clamp(startHeight + startY - moveEvent.clientY, MIN_COMPOSER_HEIGHT, MAX_COMPOSER_HEIGHT))
    }
    const onUp = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp, { once: true })
  }

  const libraryContent = (
    <LibraryPanel
      workspaces={agent.workspaces}
      workspacesLoading={agent.workspacesLoading}
      workspaceAction={agent.workspaceAction}
      activeWorkspaceId={agent.activeWorkspace?.id || null}
      activeConversationId={agent.activeConversation?.id || null}
      onCreateWorkspace={agent.createWorkspace}
      onDeleteWorkspace={agent.deleteWorkspace}
      onCreateConversation={agent.createConversation}
      onDeleteConversation={agent.deleteConversation}
      onSelectConversation={(workspaceId, conversationId) => {
        void agent.selectConversation(workspaceId, conversationId)
        setView('chat')
        setLibrarySheetOpen(false)
      }}
      expandedWorkspaceIds={agent.expandedWorkspaceIds}
      editingWorkspaceId={agent.editingWorkspaceId}
      editingWorkspaceName={agent.editingWorkspaceName}
      onToggleWorkspace={agent.toggleWorkspace}
      onStartEditingWorkspace={agent.startEditingWorkspace}
      onEditingWorkspaceNameChange={agent.setEditingWorkspaceName}
      onSaveWorkspaceName={agent.saveWorkspaceName}
      pendingDelete={agent.pendingDelete}
      onRequestDeleteWorkspace={workspaceId => agent.setPendingDelete({ type: 'workspace', workspaceId })}
      onRequestDeleteConversation={(workspaceId, conversationId) => agent.setPendingDelete({ type: 'conversation', workspaceId, conversationId })}
      onCancelDelete={() => agent.setPendingDelete(null)}
      isAdmin={agent.isAdmin}
      view={view}
      onOpenProfile={() => {
        setView('profile')
        setLibrarySheetOpen(false)
      }}
      onOpenAdmin={() => {
        setView('admin')
        setLibrarySheetOpen(false)
      }}
      theme={theme}
      onToggleTheme={toggleTheme}
    />
  )

  const sharedContextProps = {
    view,
    result: agent.result,
    events: agent.events,
    sources: agent.sources,
    latestArtifact: agent.latestArtifact,
    activeWorkspace: agent.activeWorkspace,
    activeConversation: agent.activeConversation,
    currentMessage: agent.running ? agent.activeRunMessage || agent.message : agent.message,
    downloadArtifact: agent.downloadArtifact,
    traceOpen,
    setTraceOpen,
    copiedKey: agent.copiedKey,
    onCopyText: agent.copyText,
    templates: agent.templates,
    templateStatus: uploadSource === 'profile' ? agent.templateStatus : '',
    templateError: agent.templateError,
    profileSettings: agent.profileSettings,
    onUpdateProfileSettings: agent.updateProfileSettings,
  }
  const workContent = <ContextPanel {...sharedContextProps} section="work" />
  const prefsContent = <ContextPanel {...sharedContextProps} section="prefs" />
  const showConversationPlaceholder = view === 'chat' && !agent.running && (agent.workspacesLoading || agent.conversationLoading) && agent.visibleTurns.length === 0

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <input
        ref={templateUploadRef}
        type="file"
        accept=".pptx"
        className="hidden"
        onChange={event => {
          void agent.uploadTemplate(event.target.files?.[0] ?? null, uploadSource)
          event.target.value = ''
        }}
      />
      <input
        ref={attachFileRef}
        type="file"
        accept={[...agent.supportedAttachmentTypes, 'image/*'].join(',') || undefined}
        className="hidden"
        onChange={event => {
          void agent.attachFile(event.target.files?.[0] ?? null)
          event.target.value = ''
        }}
      />

      {/* Mobile top bar */}
      <header className="flex-shrink-0 border-b border-neutral-200 bg-white/95 px-3 py-2.5 backdrop-blur md:hidden dark:border-neutral-800 dark:bg-neutral-950/95">
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            {view !== 'chat' ? (
              <button
                type="button"
                onClick={() => setView('chat')}
                aria-label="Back to chat"
                className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
              >
                <ChevronsLeft size={15} />
              </button>
            ) : (
              <img src={brandAsset('/fronei-icon.svg')} alt="Fronei" className="h-8 w-8 flex-shrink-0 rounded-lg" />
            )}
            <div className="min-w-0">
              {view === 'profile' ? (
                <p className="text-[13px] font-bold text-neutral-900 dark:text-neutral-50">Profile</p>
              ) : view === 'admin' ? (
                <p className="text-[13px] font-bold text-neutral-900 dark:text-neutral-50">Admin</p>
              ) : (
                <>
                  <p className="truncate text-[13px] font-bold text-neutral-900 dark:text-neutral-50">
                    {agent.activeWorkspace?.name || 'fronei'}
                  </p>
                  {agent.activeConversation && (
                    <p className="truncate text-[10px] text-neutral-400">{agent.activeConversation.title}</p>
                  )}
                </>
              )}
            </div>
          </div>
          <div className="flex flex-shrink-0 items-center gap-1.5">
            {agent.running && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-bold text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400">
                <Loader2 size={12} className="animate-spin" /> Working
              </span>
            )}
            <button
              type="button"
              onClick={toggleTheme}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
            >
              {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
            </button>
            {view === 'chat' && (
              <>
                <button
                  type="button"
                  onClick={() => setLibrarySheetOpen(true)}
                  aria-label="Open library"
                  className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
                >
                  <Library size={15} />
                </button>
                <button
                  type="button"
                  onClick={() => setWorkModalOpen(true)}
                  aria-label="Current work"
                  className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
                >
                  <Sparkles size={15} />
                </button>
                <div style={{ position: 'relative' }}>
                  <button
                    type="button"
                    onClick={() => setPrefsPopoverOpen(v => !v)}
                    aria-label="Quick preferences"
                    className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
                  >
                    <Settings2 size={15} />
                  </button>
                  {prefsPopoverOpen && (
                    <>
                      <button type="button" aria-label="Close" onClick={() => setPrefsPopoverOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} tabIndex={-1} />
                      <div
                        className="rounded-xl border border-neutral-200 bg-white shadow-xl dark:border-neutral-800 dark:bg-neutral-950"
                        style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0, width: 260, zIndex: 50, padding: '16px' }}
                      >
                        <p className="mb-3 text-[11px] font-bold uppercase tracking-wider text-neutral-400">Quick Preferences</p>
                        {prefsContent}
                      </div>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </header>

      <div
        className="flex min-h-0 flex-1 overflow-hidden md:grid"
        style={{ gridTemplateColumns: `${leftRailCollapsed ? 56 : leftRailWidth}px minmax(0, 1fr)` }}
      >
        {/* Desktop library rail */}
        <aside className="relative hidden flex-col overflow-hidden border-r border-neutral-200 bg-neutral-50/60 dark:border-neutral-800 dark:bg-neutral-900/40 md:flex">
          {leftRailCollapsed ? (
            <CollapsedLibraryRail
              isAdmin={agent.isAdmin}
              activeView={view}
              onExpand={() => setLeftRailCollapsed(false)}
              onOpenWorkspaces={() => {
                setView('chat')
                setLeftRailCollapsed(false)
              }}
              onOpenProfile={() => {
                setView('profile')
                setLeftRailCollapsed(false)
              }}
              onOpenAdmin={() => {
                setView('admin')
                setLeftRailCollapsed(false)
              }}
              theme={theme}
              onToggleTheme={toggleTheme}
            />
          ) : (
            <>
              <div className="flex-1 overflow-hidden px-4 py-5">{libraryContent}</div>
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => setLeftRailCollapsed(true)}
                aria-label="Collapse library"
                title="Collapse library"
                className="absolute right-2 top-2 rounded-full text-neutral-400"
              >
                <ChevronsLeft size={14} />
              </Button>
              <div
                role="separator"
                aria-label="Resize library rail"
                onPointerDown={event => beginHorizontalResize(event)}
                className="absolute inset-y-0 right-[-5px] z-10 w-[10px] cursor-col-resize hover:bg-neutral-900/5 dark:hover:bg-white/5"
              />
            </>
          )}
        </aside>

        {/* Work pane */}
        <section className="flex min-h-0 flex-1 flex-col overflow-hidden bg-white dark:bg-neutral-950">
          {view === 'profile' ? (
            <ProfileView onClose={() => setView('chat')} />
          ) : view === 'admin' ? (
            <AdminShell embedded onClose={() => setView('chat')} />
          ) : (
            <>
              <header className="hidden flex-shrink-0 border-b border-neutral-200 bg-white/95 px-8 py-5 backdrop-blur md:block dark:border-neutral-800 dark:bg-neutral-950/95">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Research and work-product studio</p>
                    <h2 className="mt-0.5 text-2xl font-bold text-neutral-900 dark:text-neutral-50">Workbench</h2>
                    <p className="mt-1 max-w-[52rem] truncate text-xs font-semibold text-neutral-400">
                      {agent.activeWorkspace?.name || 'No workspace selected'} / {agent.activeConversation?.title || 'No conversation selected'}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    {agent.result && (
                      <Badge tone="neutral">{agent.result.route} · {agent.result.latency_ms ?? 0}ms</Badge>
                    )}
                    <Badge tone={agent.running ? 'success' : 'neutral'}>
                      {agent.running ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                      {agent.running ? 'Working' : 'Ready'}
                    </Badge>
                    <Button variant="outline" size="icon-sm" onClick={() => setWorkModalOpen(true)} aria-label="Current work" title="Current work" className="rounded-full text-neutral-500">
                      <Sparkles size={14} />
                    </Button>
                    <div style={{ position: 'relative' }}>
                      <Button variant="outline" size="icon-sm" onClick={() => setPrefsPopoverOpen(v => !v)} aria-label="Quick preferences" title="Quick preferences" className="rounded-full text-neutral-500">
                        <Settings2 size={14} />
                      </Button>
                      {prefsPopoverOpen && (
                        <>
                          <button type="button" aria-label="Close" onClick={() => setPrefsPopoverOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} tabIndex={-1} />
                          <div
                            className="rounded-xl border border-neutral-200 bg-white shadow-xl dark:border-neutral-800 dark:bg-neutral-950"
                            style={{ position: 'absolute', top: 'calc(100% + 8px)', right: 0, width: 260, zIndex: 50, padding: '16px' }}
                          >
                            <p className="mb-3 text-[11px] font-bold uppercase tracking-wider text-neutral-400">Quick Preferences</p>
                            {prefsContent}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              </header>

              <div ref={chatScrollRef} className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 sm:px-6 md:px-8 md:py-6">
                {showConversationPlaceholder ? (
                  <ConversationSkeleton />
                ) : (
                  <>
                    {agent.canLoadOlder && (
                      <button
                        type="button"
                        onClick={agent.loadOlderTurns}
                        className="mx-auto rounded-full border border-neutral-200 bg-white px-3.5 py-2 text-xs font-bold text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300"
                      >
                        Load older turns
                      </button>
                    )}
                    <Timeline
                      draftMessage={agent.running ? agent.activeRunMessage || agent.message : agent.message}
                      liveAnswer={agent.liveAnswer}
                      turns={agent.visibleTurns}
                      events={agent.activeEvents}
                      running={agent.running}
                      copiedKey={agent.copiedKey}
                      onCopyText={agent.copyText}
                      downloadArtifact={agent.downloadArtifact}
                      onFollowUp={option => void agent.run(option)}
                      feedbackMap={agent.feedbackMap}
                      onFeedback={agent.submitFeedback}
                      onRetry={message => void agent.run({ label: 'Retry', message })}
                      onEdit={agent.setMessage}
                    />
                    {agent.result?.turn_status === 'paused' && (
                      <PausedApprovalCard
                        result={agent.result}
                        isAdmin={agent.isAdmin}
                        authorizedFetch={agent.authorizedFetch}
                        onResolved={updated => {
                          agent.setTurnState(updated, updated.events || [])
                          if (agent.activeWorkspace?.id && agent.activeConversation?.id) {
                            void agent.selectConversation(agent.activeWorkspace.id, agent.activeConversation.id)
                          }
                        }}
                      />
                    )}
                  </>
                )}
                {agent.error && (
                  <div className="rounded-lg border-l-4 border-red-400 bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-400">
                    {agent.error}
                  </div>
                )}
              </div>

              <div className="relative flex-shrink-0 border-t border-neutral-200 bg-white/95 p-2.5 backdrop-blur [padding-bottom:calc(0.625rem+env(safe-area-inset-bottom))] md:px-8 md:py-4 dark:border-neutral-800 dark:bg-neutral-950/95" style={{ minHeight: composerHeight }}>
                <div
                  role="separator"
                  aria-label="Resize composer"
                  onPointerDown={beginComposerResize}
                  className="absolute inset-x-0 top-[-5px] z-10 hidden h-[10px] cursor-row-resize md:block hover:bg-neutral-900/5 dark:hover:bg-white/5"
                />
                <Composer
                  message={agent.message}
                  setMessage={agent.setMessage}
                  qualityMode={agent.qualityMode}
                  setQualityMode={agent.setQualityMode}
                  outputFormat={agent.outputFormat}
                  setOutputFormat={agent.setOutputFormat}
                  researchLevel={agent.researchLevel}
                  setResearchLevel={agent.setResearchLevel}
                  running={agent.running}
                  canRun={agent.canRun}
                  run={() => void agent.run()}
                  cancel={() => void agent.cancel()}
                  cancelling={agent.cancelling}
                  onUploadTemplate={() => openTemplateUpload('composer')}
                  templates={agent.templates}
                  selectedTemplateId={agent.selectedTemplateExists ? agent.selectedTemplateId : ''}
                  setSelectedTemplateId={agent.setSelectedTemplateId}
                  templateStatus={uploadSource === 'composer' ? agent.templateStatus : ''}
                  isAdmin={agent.isAdmin}
                  modelOverride={agent.modelOverride}
                  setModelOverride={agent.setModelOverride}
                  onAttachFile={openAttachFile}
                  attachedFile={agent.attachedFile}
                  attachingFile={agent.attachingFile}
                  attachmentError={agent.attachmentError}
                  onClearAttachment={agent.clearAttachment}
                />
              </div>
            </>
          )}
        </section>

      </div>

      {/* Mobile library sheet */}
      <Sheet open={librarySheetOpen} onClose={() => setLibrarySheetOpen(false)} side="left" title="Studio">
        {libraryContent}
      </Sheet>

      {/* Center modals — Current Work and Quick Preferences */}
      <Modal
        open={workModalOpen}
        onClose={() => setWorkModalOpen(false)}
        title="Current Work"
        action={
          agent.events.length > 0 ? (
            <CopyButton
              copied={agent.copiedKey === 'events:all'}
              label="Copy full trace"
              onClick={() => void agent.copyText(engineEventsCopyText(agent.events), 'events:all')}
            />
          ) : undefined
        }
      >
        {workContent}
      </Modal>
    </div>
  )
}

function ConversationSkeleton() {
  return (
    <div className="flex flex-1 flex-col gap-6" aria-label="Loading conversation">
      <div className="self-end w-[65%] max-w-[340px] rounded-2xl rounded-br-md bg-neutral-900/10 px-4 py-3 dark:bg-white/10">
        <div className="mb-3 h-3 w-10 animate-pulse rounded bg-neutral-300 dark:bg-neutral-700" />
        <div className="space-y-2">
          <div className="h-3.5 w-full animate-pulse rounded bg-neutral-300 dark:bg-neutral-700" />
          <div className="h-3.5 w-3/4 animate-pulse rounded bg-neutral-300 dark:bg-neutral-700" />
        </div>
      </div>
      <div className="w-full max-w-[860px] rounded-2xl rounded-bl-md border border-neutral-200 bg-white p-4 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-4 flex items-start gap-3">
          <span className="grid h-9 w-9 flex-shrink-0 place-items-center rounded-full bg-neutral-200 dark:bg-neutral-800">
            <Loader2 size={16} className="animate-spin text-neutral-500 dark:text-neutral-400" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="h-3.5 w-16 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
            <div className="mt-2 h-3 w-36 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          </div>
        </div>
        <div className="space-y-2">
          <div className="h-3.5 w-full animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          <div className="h-3.5 w-11/12 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
          <div className="h-3.5 w-3/5 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        </div>
      </div>
    </div>
  )
}

function CollapsedLibraryRail({
  isAdmin,
  activeView,
  onExpand,
  onOpenWorkspaces,
  onOpenProfile,
  onOpenAdmin,
  theme,
  onToggleTheme,
}: {
  isAdmin: boolean
  activeView: 'chat' | 'profile' | 'admin'
  onExpand: () => void
  onOpenWorkspaces: () => void
  onOpenProfile: () => void
  onOpenAdmin: () => void
  theme: 'light' | 'dark'
  onToggleTheme: () => void
}) {
  return (
    <div className="flex h-full flex-col items-center gap-2 px-2 py-3">
      <a
        href="/"
        aria-label="Go to Fronei home"
        title="Go to Fronei home"
        className="grid h-10 w-10 place-items-center rounded-lg border border-neutral-200 bg-white hover:bg-neutral-100 dark:border-neutral-800 dark:bg-neutral-900 dark:hover:bg-neutral-800"
      >
        <img src={brandAsset('/fronei-icon.svg')} alt="" className="h-7 w-7" />
      </a>
      <div className="h-px w-8 bg-neutral-200 dark:bg-neutral-800" />
      <CollapsedIconButton label="Workspaces" icon={Folder} active={activeView === 'chat'} onClick={onOpenWorkspaces} />
      <CollapsedIconButton label="Profile" icon={UserCog} active={activeView === 'profile'} onClick={onOpenProfile} />
      {isAdmin && <CollapsedIconButton label="Admin" icon={Shield} active={activeView === 'admin'} onClick={onOpenAdmin} />}
      <div className="mt-auto flex flex-col gap-2">
        <CollapsedIconButton
          label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          icon={theme === 'dark' ? Sun : Moon}
          onClick={onToggleTheme}
        />
        <CollapsedIconButton label="Expand library" icon={ChevronsRight} onClick={onExpand} />
      </div>
    </div>
  )
}


function CollapsedIconButton({ label, icon: Icon, active = false, onClick }: { label: string; icon: LucideIcon; active?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`grid h-10 w-10 place-items-center rounded-lg border transition-colors ${
        active
          ? 'border-neutral-900 bg-neutral-900 text-white dark:border-white dark:bg-white dark:text-neutral-900'
          : 'border-neutral-200 bg-white text-neutral-500 hover:bg-neutral-100 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400 dark:hover:bg-neutral-800'
      }`}
    >
      <Icon size={17} />
    </button>
  )
}
