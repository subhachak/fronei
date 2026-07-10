import { describe, expect, it } from 'vitest'
import { buildConfidenceCues, buildStalenessWarning, eventChips, plainCommentaryForEvent } from './commentary'
import type { AgentResult, ProgressEvent } from '../types'

function result(overrides: Partial<AgentResult> = {}): AgentResult {
  return {
    turn_id: 'turn_1',
    answer: 'Answer',
    route: 'research',
    ...overrides,
  }
}

function progressEvent(overrides: Partial<ProgressEvent> = {}): ProgressEvent {
  return {
    stage: 'search_worker',
    message: 'Searching the web...',
    ...overrides,
  }
}

describe('buildConfidenceCues', () => {
  it('adds a claims-verified cue when quality_signals has claims', () => {
    const cues = buildConfidenceCues([], result({
      quality_signals: { verified_claim_count: 3, unsupported_claim_count: 1 },
    }))

    expect(cues).toContain('3 of 4 claims source-verified')
  })

  it('omits the claims cue when there are no claims to report', () => {
    const cues = buildConfidenceCues([], result({
      quality_signals: { verified_claim_count: 0, unsupported_claim_count: 0 },
    }))

    expect(cues.some(cue => cue.includes('claims source-verified'))).toBe(false)
  })

  it('omits the claims cue when quality_signals is absent', () => {
    const cues = buildConfidenceCues([], result())

    expect(cues.some(cue => cue.includes('claims source-verified'))).toBe(false)
  })
})

describe('buildStalenessWarning', () => {
  it('returns a warning when evidence is flagged stale', () => {
    const warning = buildStalenessWarning(result({
      quality_signals: { has_stale_evidence: true },
    }))

    expect(warning).toMatch(/outdated/i)
  })

  it('returns null when evidence is not stale', () => {
    expect(buildStalenessWarning(result({ quality_signals: { has_stale_evidence: false } }))).toBeNull()
  })

  it('returns null when there is no result', () => {
    expect(buildStalenessWarning(null)).toBeNull()
  })

  it('returns null when there are no quality_signals at all', () => {
    expect(buildStalenessWarning(result())).toBeNull()
  })
})

describe('plainCommentaryForEvent', () => {
  it('surfaces the literal search query for a search_worker event', () => {
    const commentary = plainCommentaryForEvent(progressEvent({
      stage: 'search_worker',
      data: { query: 'FIFA World Cup match schedule 2026-07-11' },
    }))

    expect(commentary).toContain('FIFA World Cup match schedule 2026-07-11')
  })

  it('truncates a long query rather than showing it in full', () => {
    const longQuery = 'a'.repeat(200)
    const commentary = plainCommentaryForEvent(progressEvent({
      stage: 'search_worker',
      data: { query: longQuery },
    }))

    expect(commentary).not.toContain(longQuery)
    expect(commentary?.length ?? 0).toBeLessThan(longQuery.length)
  })

  it('falls back to a generic message when a search_worker event has no query', () => {
    const commentary = plainCommentaryForEvent(progressEvent({ stage: 'search_worker', data: {} }))

    expect(commentary).toBe('I’m checking the web for current information.')
  })
})

describe('eventChips', () => {
  it('surfaces a truncated query chip for a search_worker event', () => {
    const chips = eventChips(progressEvent({
      stage: 'search_worker',
      data: { query: 'javah section 3-5 then by', worker_index: 2, source_count: 11 },
    }))

    expect(chips[0]).toMatch(/^query: /)
    expect(chips[0]).toContain('javah section 3-5 then by')
  })

  it('truncates a long query chip instead of overflowing it', () => {
    const longQuery = 'b'.repeat(200)
    const chips = eventChips(progressEvent({ stage: 'search_worker', data: { query: longQuery } }))

    expect(chips[0].length).toBeLessThan(longQuery.length)
  })

  it('omits the query chip entirely when no query is present', () => {
    const chips = eventChips(progressEvent({ stage: 'search_worker', data: { worker_index: 1 } }))

    expect(chips.some(chip => chip.startsWith('query:'))).toBe(false)
  })
})
