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
  COMPARE_RPC_TIMEOUT_MS,
  failedRow,
  formatCost,
  formatElapsed,
  MAX_CANDIDATES,
  parseCandidateLabel,
  removeCandidate,
  runCandidate,
  saveComparison
} from './api'

function memoryStorage(seed: [string, unknown][] = []) {
  const store = new Map<string, unknown>(seed)

  return {
    get: <T>(key: string, fallback: T): T => (store.has(key) ? (store.get(key) as T) : fallback),
    remove: (key: string) => void store.delete(key),
    set: (key: string, value: unknown) => void store.set(key, value),
    store
  }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

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
    const storage = memoryStorage([
      ['candidates', [{ model: 'a', provider: 'p' }, { nope: true }, 'x', { model: 'b', provider: '' }]]
    ])

    const dispose = bindCandidateStorage(storage)

    expect($candidates.get()).toEqual([
      { model: 'a', provider: 'p' },
      { model: 'b', provider: '' }
    ])

    addCandidate({ model: 'c', provider: 'q' })

    expect(storage.store.get('candidates')).toEqual([
      { model: 'a', provider: 'p' },
      { model: 'b', provider: '' },
      { model: 'c', provider: 'q' }
    ])

    dispose()
  })

  it('parses stored provider:model labels, splitting on the first colon only', () => {
    expect(parseCandidateLabel('openrouter:nvidia/x:free')).toEqual({ model: 'nvidia/x:free', provider: 'openrouter' })
    expect(parseCandidateLabel('  gpt-5 ')).toEqual({ model: 'gpt-5', provider: '' })
    expect(parseCandidateLabel(':')).toBeNull()
    expect(parseCandidateLabel('')).toBeNull()
  })
})

describe('profile selection sync', () => {
  it("takes the profile's saved set over the stored mirror and pushes local edits back", async () => {
    request.mockResolvedValueOnce({ models: ['openrouter:anthropic/claude-sonnet-5', 'openrouter:z-ai/glm-5.3'] })
    const storage = memoryStorage([['candidates', [{ model: 'stale', provider: 'p' }]]])

    const dispose = bindCandidateStorage(storage)

    expect($candidates.get()).toEqual([{ model: 'stale', provider: 'p' }]) // instant paint
    expect(request).toHaveBeenCalledWith('compare.selection')
    await flush()
    expect($candidates.get()).toEqual([
      { model: 'anthropic/claude-sonnet-5', provider: 'openrouter' },
      { model: 'z-ai/glm-5.3', provider: 'openrouter' }
    ])
    // Applying the backend's answer is not an edit: nothing is echoed back.
    expect(request).toHaveBeenCalledTimes(1)
    expect(storage.store.get('candidates')).toEqual($candidates.get())

    request.mockResolvedValueOnce({ models: [] })
    removeCandidate({ model: 'z-ai/glm-5.3', provider: 'openrouter' })

    expect(request).toHaveBeenLastCalledWith('compare.selection', { models: ['openrouter:anthropic/claude-sonnet-5'] })

    dispose()
  })

  it('seeds an empty profile selection from the page instead of wiping it', async () => {
    request.mockResolvedValueOnce({ models: [] })
    const dispose = bindCandidateStorage(memoryStorage([['candidates', [{ model: 'a', provider: 'p' }]]]))

    await flush()
    expect($candidates.get()).toEqual([{ model: 'a', provider: 'p' }])
    expect(request).toHaveBeenLastCalledWith('compare.selection', { models: ['p:a'] })

    dispose()
  })

  it('lets an edit made during the pull win over the pull', async () => {
    let answer: (value: unknown) => void = () => {}
    request.mockImplementationOnce(() => new Promise(resolve => (answer = resolve)))
    request.mockResolvedValue({})
    const dispose = bindCandidateStorage(memoryStorage())

    addCandidate({ model: 'mine', provider: 'p' })
    answer({ models: ['q:theirs'] })
    await flush()

    expect($candidates.get()).toEqual([{ model: 'mine', provider: 'p' }])

    dispose()
  })

  it('keeps the stored mirror when the backend cannot answer', async () => {
    request.mockRejectedValueOnce(new Error('Hermes gateway unavailable'))
    const dispose = bindCandidateStorage(memoryStorage([['candidates', [{ model: 'a', provider: 'p' }]]]))

    await flush()
    expect($candidates.get()).toEqual([{ model: 'a', provider: 'p' }])

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
    // Bounded above the backend's per-candidate budget, not by the socket's
    // generic 30s deadline: a slow model is its own failed column, never
    // "request timed out after 30s: compare.run".
    expect(request).toHaveBeenCalledWith(
      'compare.run',
      {
        candidates: [{ model: 'm', provider: 'p' }],
        prompt: 'q',
        system: 'terse'
      },
      COMPARE_RPC_TIMEOUT_MS
    )
    expect(COMPARE_RPC_TIMEOUT_MS).toBeGreaterThan(180_000)
  })

  it('throws when the backend returns nothing', async () => {
    request.mockResolvedValueOnce({ prompt: 'q', results: [] })

    await expect(runCandidate('q', { model: 'm', provider: 'p' })).rejects.toThrow('no result')
  })
})

describe('saveComparison', () => {
  it('hands the prompt and the result rows to compare.save and returns the chat', async () => {
    request.mockResolvedValueOnce({ message_count: 2, stored_session_id: 'sess-1', title: 'Compare: q' })
    const rows = [failedRow({ model: 'm', provider: 'p' }, 'socket closed')]

    const saved = await saveComparison('q', rows)

    expect(saved).toEqual({ message_count: 2, stored_session_id: 'sess-1', title: 'Compare: q' })
    expect(request).toHaveBeenCalledWith('compare.save', { prompt: 'q', results: rows })
  })

  it('throws when the backend returns no session', async () => {
    request.mockResolvedValueOnce({})

    await expect(saveComparison('q', [])).rejects.toThrow('no session')
  })

  it('synthesizes a failed row for a request that never reached the backend', () => {
    expect(failedRow({ model: 'm', provider: 'p' }, 'boom')).toEqual({
      elapsed_s: 0,
      error: 'boom',
      input_tokens: 0,
      label: 'p: m',
      model: 'm',
      ok: false,
      output_tokens: 0,
      provider: 'p',
      text: ''
    })
  })
})
