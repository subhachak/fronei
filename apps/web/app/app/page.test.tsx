import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../components/AgentShell', () => ({
  AgentShell: () => <div data-testid="agent-shell-stub" />,
}))

import AppPage from './page'

describe('AppPage (route: /app)', () => {
  it('renders the agent shell', () => {
    render(<AppPage />)

    expect(screen.getByTestId('agent-shell-stub')).toBeTruthy()
  })
})
