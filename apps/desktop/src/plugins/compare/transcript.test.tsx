import type * as PluginSdk from '@hermes/plugin-sdk'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { COMPARE_LOCALES } from './i18n'
import { CompareTranscript } from './transcript'

const request = vi.fn()
const openSession = vi.fn(async (..._args: unknown[]) => {})
const notify = vi.fn()
const invalidateQueries = vi.fn(async (..._args: unknown[]) => {})

// A settable stand-in for host.state.focusedStoredSessionId (a readonly atom
// in the real SDK). Hoisted: the mock factory below runs before imports.
const { $focusedStoredSessionId } = vi.hoisted(() => {
  let value: null | string = 'compare-chat-1'
  const listeners = new Set<(next: null | string) => void>()

  const listen = (listener: (next: null | string) => void) => {
    listeners.add(listener)

    return () => void listeners.delete(listener)
  }

  return {
    $focusedStoredSessionId: {
      get: () => value,
      listen,
      set: (next: null | string) => {
        value = next
        listeners.forEach(listener => listener(next))
      },
      subscribe: (listener: (next: null | string) => void) => {
        listener(value)

        return listen(listener)
      },
      get value() {
        return value
      }
    }
  }
})

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof PluginSdk>()

  return {
    ...sdk,
    host: {
      ...sdk.host,
      notify: (...args: unknown[]) => notify(...args),
      openSession: (...args: unknown[]) => openSession(...args),
      request: (...args: unknown[]) => request(...args),
      state: { ...sdk.host.state, focusedStoredSessionId: $focusedStoredSessionId }
    },
    queryClient: { ...sdk.queryClient, invalidateQueries: (...args: unknown[]) => invalidateQueries(...args) }
  }
})

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(async () => ({ providers: [] })),
  setApiRequestProfile: vi.fn()
}))

const metadata = {
  kind: 'compare',
  prompt: 'why is the sky blue?',
  results: [
    { cost_usd: 0.002, elapsed_s: 1.5, input_tokens: 10, label: 'p: fast', model: 'fast', ok: true, output_tokens: 4, provider: 'p', text: 'Rayleigh scattering.' },
    { elapsed_s: 0.2, error: 'rate limited', input_tokens: 0, label: 'p: down', model: 'down', ok: false, output_tokens: 0, provider: 'p', text: '' }
  ]
}

let disposeLocales = () => {}

beforeEach(() => {
  disposeLocales = registerPluginLocales('compare', COMPARE_LOCALES)
  $focusedStoredSessionId.set('compare-chat-1')
})

afterEach(() => {
  cleanup()
  disposeLocales()
  vi.clearAllMocks()
})

describe('compare transcript', () => {
  it('paints one card per compared model and offers a branch only on answered ones', () => {
    render(<CompareTranscript messageId="m1" metadata={metadata} text="## fallback" />)

    expect(screen.getByText('Compared 2 models · 1 answered')).toBeTruthy()
    expect(screen.getAllByTestId('compare-transcript-card')).toHaveLength(2)
    expect(screen.getByText('Rayleigh scattering.')).toBeTruthy()
    expect(screen.getByText('Failed: rate limited')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Continue with this model' })).toHaveLength(1)
  })

  it('branches a child chat pinned to the chosen model and opens it', async () => {
    request.mockResolvedValueOnce({ model: 'fast', parent_session_id: 'compare-chat-1', provider: 'p', stored_session_id: 'child-1', title: 'fast · why is the sky blue?' })
    render(<CompareTranscript messageId="m1" metadata={metadata} text="" />)

    fireEvent.click(screen.getByRole('button', { name: 'Continue with this model' }))

    expect(request).toHaveBeenCalledWith('compare.branch', {
      prompt: 'why is the sky blue?',
      result: metadata.results[0],
      session_id: 'compare-chat-1'
    })
    await waitFor(() => expect(openSession).toHaveBeenCalledWith('child-1', { intent: 'in-place' }))
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['sessions'] })
    expect(notify).not.toHaveBeenCalled()
  })

  it('reports a failed branch and never navigates', async () => {
    request.mockRejectedValueOnce(new Error('compare chat not found'))
    render(<CompareTranscript messageId="m1" metadata={metadata} text="" />)

    fireEvent.click(screen.getByRole('button', { name: 'Continue with this model' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'error', message: 'Could not branch: compare chat not found' }))
    expect(openSession).not.toHaveBeenCalled()
  })

  it('refuses to branch without an open chat to branch from', () => {
    $focusedStoredSessionId.set(null)
    render(<CompareTranscript messageId="m1" metadata={metadata} text="" />)

    fireEvent.click(screen.getByRole('button', { name: 'Continue with this model' }))

    expect(request).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith({ kind: 'error', message: 'Open the chat before branching from it' })
  })

  it('falls back to the markdown when the payload is not one it understands', () => {
    render(<CompareTranscript messageId="m1" metadata={{ kind: 'something-else' }} text="plain fallback" />)

    expect(screen.getByText('plain fallback')).toBeTruthy()
    expect(screen.queryByTestId('compare-transcript')).toBeNull()
  })
})
