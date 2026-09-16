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

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof CompareApi>()),
  runCandidate: (...args: unknown[]) => runCandidate(...args)
}))

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

    resolveSlow(row('slow', 'Slow answer', { elapsed_s: 12.4 }))

    expect(await screen.findByText('Slow answer')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Waiting for the model…')).toBeNull())
    expect((screen.getByRole('button', { name: /^Run$/ }) as HTMLButtonElement).disabled).toBe(false)
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
  })
})
