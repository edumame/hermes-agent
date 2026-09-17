import type * as PluginSdk from '@hermes/plugin-sdk'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as CompareApi from './api'
import { $candidates, clearCandidates, type CompareRunResult } from './api'
import { COMPARE_LOCALES } from './i18n'
import { ComparePage } from './page'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const runCandidate = vi.fn()
const saveComparison = vi.fn()
const openSession = vi.fn(async (..._args: unknown[]) => {})
const invalidateQueries = vi.fn(async (..._args: unknown[]) => {})

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof CompareApi>()),
  runCandidate: (...args: unknown[]) => runCandidate(...args),
  saveComparison: (...args: unknown[]) => saveComparison(...args)
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof PluginSdk>()

  return {
    ...sdk,
    host: { ...sdk.host, notify: vi.fn(), openSession: (...args: unknown[]) => openSession(...args) },
    queryClient: { ...sdk.queryClient, invalidateQueries: (...args: unknown[]) => invalidateQueries(...args) }
  }
})

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(async () => ({ providers: [] })),
  setApiRequestProfile: vi.fn()
}))

function row(model: string, text: string, extra: Partial<CompareRunResult> = {}): CompareRunResult {
  return {
    cost_usd: 0.002,
    elapsed_s: 1.5,
    input_tokens: 10,
    label: `p: ${model}`,
    model,
    ok: true,
    output_tokens: 4,
    provider: 'p',
    text,
    ...extra
  }
}

let disposeLocales = () => {}

beforeEach(() => {
  clearCandidates()
  disposeLocales = registerPluginLocales('compare', COMPARE_LOCALES)
  saveComparison.mockResolvedValue({ stored_session_id: 'sess-1', title: 'Compare: why?' })
})

afterEach(() => {
  cleanup()
  disposeLocales()
  vi.clearAllMocks()
})

describe('compare page', () => {
  it('shows the empty state and keeps Run disabled until there is a prompt and a model', async () => {
    render(<ComparePage />)

    expect(screen.getByText('Nothing to compare yet')).toBeTruthy()

    const run = screen.getByRole('button', { name: /^Run$/ })

    expect((run as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText(/Ask every selected model/), { target: { value: 'hi' } })

    expect((run as HTMLButtonElement).disabled).toBe(true)

    $candidates.set([{ model: 'm1', provider: 'p' }])

    await waitFor(() =>
      expect((screen.getByRole('button', { name: /^Run$/ }) as HTMLButtonElement).disabled).toBe(false)
    )
  })

  it('lists candidates as chips and removes one on click', () => {
    $candidates.set([
      { model: 'm1', provider: 'p' },
      { model: 'm2', provider: 'q' }
    ])
    render(<ComparePage />)

    expect(screen.getAllByTestId('compare-candidate')).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: 'Remove p: m1' }))

    expect(screen.getAllByTestId('compare-candidate')).toHaveLength(1)
    expect($candidates.get()).toEqual([{ model: 'm2', provider: 'q' }])
  })

  it('runs every candidate in parallel and paints each column as it resolves', async () => {
    $candidates.set([
      { model: 'slow', provider: 'p' },
      { model: 'fast', provider: 'p' }
    ])

    let resolveSlow: (value: CompareRunResult) => void = () => {}
    runCandidate.mockImplementation((_prompt: string, candidate: { model: string }) =>
      candidate.model === 'fast'
        ? Promise.resolve(row('fast', 'Fast answer'))
        : new Promise<CompareRunResult>(resolve => {
            resolveSlow = resolve
          })
    )
    render(<ComparePage />)

    fireEvent.change(screen.getByPlaceholderText(/Ask every selected model/), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))

    // Both requests fired at once, one per candidate.
    expect(runCandidate).toHaveBeenCalledTimes(2)
    expect(runCandidate).toHaveBeenCalledWith('why?', { model: 'slow', provider: 'p' })
    expect(runCandidate).toHaveBeenCalledWith('why?', { model: 'fast', provider: 'p' })
    expect(screen.getAllByTestId('compare-column')).toHaveLength(2)

    // The fast column paints while the slow one is still waiting.
    expect(await screen.findByText('Fast answer')).toBeTruthy()
    expect(screen.getAllByText('Waiting for the model…').length).toBeGreaterThan(0)
    expect(screen.getByText('1.5s · 10→4 tokens · $0.0020')).toBeTruthy()
    expect((screen.getByRole('button', { name: /Running…/ }) as HTMLButtonElement).disabled).toBe(true)

    // Nothing is saved while a column is still running.
    expect(saveComparison).not.toHaveBeenCalled()

    resolveSlow(row('slow', 'Slow answer', { elapsed_s: 12.4 }))

    expect(await screen.findByText('Slow answer')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Waiting for the model…')).toBeNull())
    expect((screen.getByRole('button', { name: /^Run$/ }) as HTMLButtonElement).disabled).toBe(false)

    // Every column settled: the run is saved as a chat, rows in candidate
    // order, and the sidebar is told a chat appeared.
    expect(await screen.findByText('Saved as a chat')).toBeTruthy()
    expect(saveComparison).toHaveBeenCalledTimes(1)
    expect(saveComparison).toHaveBeenCalledWith('why?', [
      row('slow', 'Slow answer', { elapsed_s: 12.4 }),
      row('fast', 'Fast answer')
    ])
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['sessions'] })

    fireEvent.click(screen.getByRole('button', { name: 'Open chat' }))

    expect(openSession).toHaveBeenCalledWith('sess-1', { intent: 'in-place' })
  })

  it('records a request failure as a failed row in the saved chat', async () => {
    $candidates.set([
      { model: 'ok', provider: 'p' },
      { model: 'broken', provider: 'p' }
    ])
    runCandidate.mockImplementation((_prompt: string, candidate: { model: string }) =>
      candidate.model === 'ok' ? Promise.resolve(row('ok', 'Answer')) : Promise.reject(new Error('socket closed'))
    )
    render(<ComparePage />)

    fireEvent.change(screen.getByPlaceholderText(/Ask every selected model/), { target: { value: 'q' } })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))

    expect(await screen.findByText('Saved as a chat')).toBeTruthy()
    expect(saveComparison).toHaveBeenCalledWith('q', [
      row('ok', 'Answer'),
      {
        elapsed_s: 0,
        error: 'socket closed',
        input_tokens: 0,
        label: 'p: broken',
        model: 'broken',
        ok: false,
        output_tokens: 0,
        provider: 'p',
        text: ''
      }
    ])
  })

  it('shows why a save failed without touching the columns', async () => {
    $candidates.set([{ model: 'm1', provider: 'p' }])
    runCandidate.mockResolvedValue(row('m1', 'Answer'))
    saveComparison.mockRejectedValueOnce(new Error('session store unavailable'))
    render(<ComparePage />)

    fireEvent.change(screen.getByPlaceholderText(/Ask every selected model/), { target: { value: 'q' } })
    fireEvent.click(screen.getByRole('button', { name: /^Run$/ }))

    expect(await screen.findByText('Could not save as a chat: session store unavailable')).toBeTruthy()
    expect(screen.getByText('Answer')).toBeTruthy()
    expect(invalidateQueries).not.toHaveBeenCalled()
  })

  it('renders provider failures and request errors as failed columns', async () => {
    $candidates.set([
      { model: 'down', provider: 'p' },
      { model: 'broken', provider: 'p' }
    ])
    runCandidate.mockImplementation((_prompt: string, candidate: { model: string }) =>
      candidate.model === 'down'
        ? Promise.resolve(row('down', '', { error: 'rate limited', ok: false }))
        : Promise.reject(new Error('socket closed'))
    )
    render(<ComparePage />)

    fireEvent.change(screen.getByPlaceholderText(/Ask every selected model/), { target: { value: 'q' } })
    fireEvent.keyDown(screen.getByPlaceholderText(/Ask every selected model/), { key: 'Enter', metaKey: true })

    expect(await screen.findByText('Failed: rate limited')).toBeTruthy()
    expect(await screen.findByText('Failed: socket closed')).toBeTruthy()

    // No model answered: nothing worth a sidebar row.
    expect(await screen.findByText('Not saved: no model answered')).toBeTruthy()
    expect(saveComparison).not.toHaveBeenCalled()
  })
})
