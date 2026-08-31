import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RefreshButton } from './ui'

describe('RefreshButton', () => {
  afterEach(() => cleanup())

  it('refetches when clicked', async () => {
    const onRefresh = vi.fn(async () => ({}))
    render(<RefreshButton onRefresh={onRefresh} />)

    await act(async () => {
      screen.getByRole('button').click()
    })
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('says it is working, then says when it finished', async () => {
    // Every one of these buttons was wired to a real refetch and none of them
    // said so. With data that usually has not changed between clicks, a working
    // refresh was indistinguishable from a dead button.
    let release: () => void = () => {}
    const onRefresh = vi.fn(() => new Promise<void>(resolve => { release = resolve }))
    render(<RefreshButton onRefresh={onRefresh} />)

    expect(screen.queryByText(/Updated/)).toBeNull()

    act(() => { screen.getByRole('button').click() })
    await waitFor(() => expect(screen.getByText(/Refreshing/)).toBeTruthy())
    expect(screen.getByRole('button')).toHaveProperty('disabled', true)

    await act(async () => { release() })
    await waitFor(() => expect(screen.getByText(/^Updated /)).toBeTruthy())
    expect(screen.getByRole('button')).toHaveProperty('disabled', false)
  })

  it('does not claim an update when the refresh failed', async () => {
    const onRefresh = vi.fn(async () => { throw new Error('network') })
    render(<RefreshButton onRefresh={onRefresh} />)

    await act(async () => {
      try {
        screen.getByRole('button').click()
      } catch {
        /* the caller surfaces the error; the button only reports success */
      }
    })

    await waitFor(() => expect(screen.getByRole('button')).toHaveProperty('disabled', false))
    expect(screen.queryByText(/Updated/)).toBeNull()
  })
})
