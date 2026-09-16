import { afterEach, describe, expect, it, vi } from 'vitest'

const request = vi.fn()

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof import('@hermes/plugin-sdk')>()

  return {
    ...sdk,
    host: { ...sdk.host, request: (...args: unknown[]) => request(...args) }
  }
})

import {
  $candidates,
  addCandidate,
  bindCandidateStorage,
  candidateKey,
  candidateLabel,
  clearCandidates,
  formatCost,
  formatElapsed,
  MAX_CANDIDATES,
  removeCandidate,
  runCandidate
} from './api'

afterEach(() => {
  clearCandidates()
  vi.clearAllMocks()
})

describe('candidate list', () => {
  it('adds, dedupes case-insensitively, and removes', () => {
    expect(addCandidate({ model: 'gpt-5', provider: 'openai' })).toBe(true)
    expect(addCandidate({ model: 'GPT-5', provider: 'OpenAI' })).toBe(false)
    expect(addCandidate({ model: '   ', provider: 'openai' })).toBe(false)
    expect(addCandidate({ model: 'claude', provider: 'anthropic' })).toBe(true)
    expect($candidates.get().map(candidateLabel)).toEqual(['openai: gpt-5', 'anthropic: claude'])

    removeCandidate({ model: 'gpt-5', provider: 'OPENAI' })

    expect($candidates.get().map(candidateKey)).toEqual(['anthropic:claude'])
  })

  it('caps the list', () => {
    for (let i = 0; i < MAX_CANDIDATES + 2; i++) {
      addCandidate({ model: `m${i}`, provider: 'p' })
    }

    expect($candidates.get()).toHaveLength(MAX_CANDIDATES)
  })

  it('hydrates from storage, drops junk, and writes back', () => {
    const store = new Map<string, unknown>([
      ['candidates', [{ model: 'a', provider: 'p' }, { nope: true }, 'x', { model: 'b', provider: '' }]]
    ])
    const storage = {
      get: <T>(key: string, fallback: T): T => (store.has(key) ? (store.get(key) as T) : fallback),
      remove: (key: string) => void store.delete(key),
      set: (key: string, value: unknown) => void store.set(key, value)
    }

    const dispose = bindCandidateStorage(storage)

    expect($candidates.get()).toEqual([
      { model: 'a', provider: 'p' },
      { model: 'b', provider: '' }
    ])

    addCandidate({ model: 'c', provider: 'q' })

    expect(store.get('candidates')).toEqual([
      { model: 'a', provider: 'p' },
      { model: 'b', provider: '' },
      { model: 'c', provider: 'q' }
    ])

    dispose()
  })
})

describe('formatting', () => {
  it('formats cost and elapsed like the chat surface', () => {
    expect(formatCost(undefined, 'n/a')).toBe('n/a')
    expect(formatCost(null, 'n/a')).toBe('n/a')
    expect(formatCost(0, 'n/a')).toBe('$0')
    expect(formatCost(0.0004, 'n/a')).toBe('$0.0004')
    expect(formatCost(1.5, 'n/a')).toBe('$1.50')
    expect(formatElapsed(1.234)).toBe('1.2')
    expect(formatElapsed(12.6)).toBe('13')
  })
})

describe('runCandidate', () => {
  it('sends one candidate and unwraps the first result row', async () => {
    const row = {
      elapsed_s: 1,
      input_tokens: 1,
      label: 'p: m',
      model: 'm',
      ok: true,
      output_tokens: 2,
      provider: 'p',
      text: 'hi'
    }
    request.mockResolvedValueOnce({ prompt: 'q', results: [row] })

    const result = await runCandidate('q', { model: 'm', provider: 'p' }, { system: 'terse' })

    expect(result).toEqual(row)
    expect(request).toHaveBeenCalledWith('compare.run', {
      candidates: [{ model: 'm', provider: 'p' }],
      prompt: 'q',
      system: 'terse'
    })
  })

  it('throws when the backend returns nothing', async () => {
    request.mockResolvedValueOnce({ prompt: 'q', results: [] })

    await expect(runCandidate('q', { model: 'm', provider: 'p' })).rejects.toThrow('no result')
  })
})
