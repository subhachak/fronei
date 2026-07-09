import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('next/font/google', () => ({
  IBM_Plex_Serif: () => ({ variable: '--font-marketing-serif' }),
  IBM_Plex_Sans: () => ({ variable: '--font-marketing-sans' }),
  IBM_Plex_Mono: () => ({ variable: '--font-marketing-mono' }),
}))

vi.mock('./lib/auth', () => ({
  useFroneiAuth: () => ({ isLoaded: true, isSignedIn: false }),
}))

import MarketingHomePage from './page'

describe('MarketingHomePage (route: /)', () => {
  it('renders the marketing hero and nav rather than the agent shell', () => {
    render(<MarketingHomePage />)

    expect(screen.getByRole('heading', { name: /Every claim, traced/i })).toBeTruthy()
    expect(screen.getAllByText(/Request access/i).length).toBeGreaterThan(0)
    expect(screen.queryByPlaceholderText('Give Fronei a task...')).toBeNull()
  })
})
