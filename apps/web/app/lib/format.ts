export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

export function formatRelativeTime(value: string): string {
  const timestamp = new Date(value).getTime()
  if (!Number.isFinite(timestamp)) return 'recent'
  const seconds = Math.max(1, Math.round((Date.now() - timestamp) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function humanizeStage(stage: string): string {
  return stage.replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
}

export function titleFromMessage(message: string): string {
  const cleaned = message.replace(/\s+/g, ' ').trim()
  return cleaned.length > 72 ? `${cleaned.slice(0, 72)}...` : cleaned || 'Untitled work'
}

export function draftConversationId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `draft-${random}`
}

export function draftWorkspaceId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `draft-workspace-${random}`
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

export function uniqueWorkspaceName(baseName: string, names: string[]): string {
  const base = baseName.replace(/\s+/g, ' ').trim() || 'New workspace'
  const existing = new Set(names.map(name => name.toLowerCase()))
  if (!existing.has(base.toLowerCase())) return base
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base} ${index}`
    if (!existing.has(candidate.toLowerCase())) return candidate
  }
  return `${base} ${Date.now().toString().slice(-4)}`
}

export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

export function streamErrorMessage(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err || '')
  if (/network|failed to fetch|load failed|terminated|aborted/i.test(message)) {
    return 'The live connection dropped while Fronei was working. The task may still finish on the server; reopen this conversation or retry if it does not appear shortly.'
  }
  return message || 'Unknown Agent v3 error'
}

export async function fallbackCopyText(text: string): Promise<boolean> {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  const trimmed = text.trim()
  if (!trimmed) return false
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(trimmed)
      return true
    } catch {
      return fallbackCopyText(trimmed)
    }
  }
  return fallbackCopyText(trimmed)
}
