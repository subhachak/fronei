'use client'

import { CheckCircle2, ChevronsLeft, ChevronsRight, Library, Loader2, Moon, PanelRight, Sparkles, Sun } from 'lucide-react'
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useAgentV3 } from '../hooks/useAgentV3'
import { useTheme } from '../hooks/useTheme'
import { clamp } from '../lib/format'
import { Composer } from './Composer'
import { ContextPanel } from './ContextPanel'
import { LibraryPanel } from './LibraryPanel'
import { Timeline } from './Timeline'
import { Badge } from './ui/Card'
import { Button } from './ui/Button'
import { Sheet } from './ui/Sheet'

const MIN_LEFT_RAIL_WIDTH = 240
const MAX_LEFT_RAIL_WIDTH = 420
const MIN_RIGHT_RAIL_WIDTH = 280
const MAX_RIGHT_RAIL_WIDTH = 480
const MIN_COMPOSER_HEIGHT = 152
const MAX_COMPOSER_HEIGHT = 340

export function AgentShell() {
  const agent = useAgentV3()
  const { theme, toggleTheme } = useTheme()

  const [librarySheetOpen, setLibrarySheetOpen] = useState(false)
  const [contextSheetOpen, setContextSheetOpen] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [leftRailWidth, setLeftRailWidth] = useState(280)
  const [rightRailWidth, setRightRailWidth] = useState(340)
  const [composerHeight, setComposerHeight] = useState(168)
  const [leftRailCollapsed, setLeftRailCollapsed] = useState(false)
  const [rightRailCollapsed, setRightRailCollapsed] = useState(false)
  const [uploadSource, setUploadSource] = useState<'composer' | 'profile'>('profile')
  const templateUploadRef = useRef<HTMLInputElement | null>(null)
  const attachFileRef = useRef<HTMLInputElement | null>(null)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [agent.visibleTurns.length, agent.running, agent.result?.turn_id, agent.events.length])

  function openTemplateUpload(source: 'composer' | 'profile') {
    setUploadSource(source)
    templateUploadRef.current?.click()
  }

  function openAttachFile() {
    attachFileRef.current?.click()
  }

  function beginHorizontalResize(kind: 'left' | 'right', event: ReactPointerEvent) {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = kind === 'left' ? leftRailWidth : rightRailWidth
    const onMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX
      if (kind === 'left') setLeftRailWidth(clamp(startWidth + delta, MIN_LEFT_RAIL_WIDTH, MAX_LEFT_RAIL_WIDTH))
      else setRightRailWidth(clamp(startWidth - delta, MIN_RIGHT_RAIL_WIDTH, MAX_RIGHT_RAIL_WIDTH))
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
      activeWorkspaceId={agent.activeWorkspace?.id || null}
      activeConversationId={agent.activeConversation?.id || null}
      onCreateWorkspace={agent.createWorkspace}
      onDeleteWorkspace={agent.deleteWorkspace}
      onCreateConversation={agent.createConversation}
      onDeleteConversation={agent.deleteConversation}
      onSelectConversation={(workspaceId, conversationId) => {
        void agent.selectConversation(workspaceId, conversationId)
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
    />
  )

  const contextContent = (
    <ContextPanel
      result={agent.result}
      events={agent.events}
      sources={agent.sources}
      latestArtifact={agent.latestArtifact}
      activeConversation={agent.activeConversation}
      currentMessage={agent.running ? agent.activeRunMessage || agent.message : agent.message}
      downloadArtifact={agent.downloadArtifact}
      traceOpen={traceOpen}
      setTraceOpen={setTraceOpen}
      copiedKey={agent.copiedKey}
      onCopyText={agent.copyText}
      templates={agent.templates}
      templatesLoaded={agent.templatesLoaded}
      templateStatus={uploadSource === 'profile' ? agent.templateStatus : ''}
      templateError={agent.templateError}
      templateDeleteId={agent.templateDeleteId}
      onUploadTemplate={() => openTemplateUpload('profile')}
      onRefreshTemplates={agent.refreshTemplates}
      onRequestDeleteTemplate={agent.setTemplateDeleteId}
      onCancelDeleteTemplate={() => agent.setTemplateDeleteId(null)}
      onDeleteTemplate={agent.deleteTemplate}
    />
  )

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
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900">
              <Sparkles size={15} />
            </span>
            <div>
              <p className="text-[10px] font-bold uppercase tracking-wider text-neutral-400">Fronei Studio</p>
              <p className="text-[13px] font-bold text-neutral-900 dark:text-neutral-50">Agent v3</p>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
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
              onClick={() => setContextSheetOpen(true)}
              aria-label="Open context"
              className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
            >
              <PanelRight size={15} />
            </button>
          </div>
        </div>
      </header>

      <div
        className="flex min-h-0 flex-1 overflow-hidden md:grid"
        style={{ gridTemplateColumns: `${leftRailCollapsed ? 56 : leftRailWidth}px minmax(0, 1fr) ${rightRailCollapsed ? 56 : rightRailWidth}px` }}
      >
        {/* Desktop library rail */}
        <aside className="relative hidden flex-col overflow-hidden border-r border-neutral-200 bg-neutral-50/60 dark:border-neutral-800 dark:bg-neutral-900/40 md:flex">
          {leftRailCollapsed ? (
            <CollapsedRailButton label="Library" icon={Library} onClick={() => setLeftRailCollapsed(false)} />
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
                onPointerDown={event => beginHorizontalResize('left', event)}
                className="absolute inset-y-0 right-[-5px] z-10 w-[10px] cursor-col-resize hover:bg-neutral-900/5 dark:hover:bg-white/5"
              />
            </>
          )}
        </aside>

        {/* Work pane */}
        <section className="flex min-h-0 flex-col overflow-hidden bg-white dark:bg-neutral-950">
          <header className="hidden flex-shrink-0 border-b border-neutral-200 bg-white/95 px-8 py-5 backdrop-blur md:block dark:border-neutral-800 dark:bg-neutral-950/95">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Research and work-product studio</p>
                <h2 className="mt-0.5 text-2xl font-bold text-neutral-900 dark:text-neutral-50">Workbench</h2>
              </div>
              <div className="flex items-center gap-3">
                {agent.result && (
                  <Badge tone="neutral">{agent.result.route} · {agent.result.latency_ms ?? 0}ms</Badge>
                )}
                <Badge tone={agent.running ? 'success' : 'neutral'}>
                  {agent.running ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                  {agent.running ? 'Working' : 'Ready'}
                </Badge>
                <Button
                  variant="outline"
                  size="icon-sm"
                  onClick={toggleTheme}
                  aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
                  title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
                  className="rounded-full"
                >
                  {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
                </Button>
              </div>
            </div>
          </header>

          <div ref={chatScrollRef} className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 sm:px-6 md:px-8 md:py-6">
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
              turns={agent.visibleTurns}
              events={agent.activeEvents}
              running={agent.running}
              copiedKey={agent.copiedKey}
              onCopyText={agent.copyText}
              downloadArtifact={agent.downloadArtifact}
              onFollowUp={option => void agent.run(option)}
            />
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
        </section>

        {/* Desktop context rail */}
        <aside className="relative hidden flex-col overflow-hidden border-l border-neutral-200 bg-neutral-50/60 dark:border-neutral-800 dark:bg-neutral-900/40 md:flex">
          {rightRailCollapsed ? (
            <CollapsedRailButton label="Context" icon={PanelRight} onClick={() => setRightRailCollapsed(false)} />
          ) : (
            <>
              <div
                role="separator"
                aria-label="Resize context rail"
                onPointerDown={event => beginHorizontalResize('right', event)}
                className="absolute inset-y-0 left-[-5px] z-10 w-[10px] cursor-col-resize hover:bg-neutral-900/5 dark:hover:bg-white/5"
              />
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => setRightRailCollapsed(true)}
                aria-label="Collapse context"
                title="Collapse context"
                className="absolute right-2 top-2 rounded-full text-neutral-400"
              >
                <ChevronsRight size={14} />
              </Button>
              <div className="flex-1 overflow-hidden px-4 py-5">{contextContent}</div>
            </>
          )}
        </aside>
      </div>

      {/* Mobile sheets */}
      <Sheet open={librarySheetOpen} onClose={() => setLibrarySheetOpen(false)} side="left" title="Studio">
        {libraryContent}
      </Sheet>
      <Sheet open={contextSheetOpen} onClose={() => setContextSheetOpen(false)} side="right" title="Context">
        {contextContent}
      </Sheet>
    </div>
  )
}

function CollapsedRailButton({ label, icon: Icon, onClick }: { label: string; icon: typeof Library; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Expand ${label}`}
      title={`Expand ${label}`}
      className="m-2 grid min-h-[120px] w-9 place-items-center gap-2 rounded-lg border border-neutral-200 bg-white text-[11px] font-bold text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-300"
      style={{ writingMode: 'vertical-rl' }}
    >
      <Icon size={16} className="rotate-90" />
      <span>{label}</span>
    </button>
  )
}
