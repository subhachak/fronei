import { describe, expect, it } from 'vitest'
import { buildConfidenceCues, buildStalenessWarning } from './commentary'
import type { AgentResult } from '../types'

function result(overrides: Partial<AgentResult> = {}): AgentResult {
  return {
    turn_id: 'turn_1',
    answer: 'Answer',
    route: 'research',
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
