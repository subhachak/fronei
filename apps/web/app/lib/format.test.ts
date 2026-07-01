import { describe, expect, it } from 'vitest'
import { appTimestampMs, formatAppDateTime, formatAppTime } from './format'

describe('appTimestampMs', () => {
  it('treats timezone-less API timestamps as UTC', () => {
    expect(appTimestampMs('2026-07-01T03:00:00')).toBe(Date.parse('2026-07-01T03:00:00Z'))
  })

  it('preserves explicit timezone offsets', () => {
    expect(appTimestampMs('2026-07-01T03:00:00-04:00')).toBe(Date.parse('2026-07-01T03:00:00-04:00'))
  })

  it('formats app times in Eastern time', () => {
    expect(formatAppTime('2026-07-01T03:00:00Z')).toMatch(/11:00/)
    expect(formatAppDateTime('2026-07-01T03:00:00Z')).toMatch(/11:00/)
  })
})
