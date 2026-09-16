import { describe, expect, it } from 'vitest'

import {
  compareSummaryLines,
  dedupeCandidates,
  filterCompareChoices,
  formatCompareStats,
  MAX_COMPARE_CANDIDATES,
  parseCompareArgs,
  toggleMember
} from './compare.js'

describe('parseCompareArgs', () => {
  it('returns the whole text as the prompt without --models', () => {
    expect(parseCompareArgs('  explain monads ')).toEqual({ prompt: 'explain monads', specs: [] })
  })

  it('lifts --models out of the prompt wherever it sits', () => {
    expect(parseCompareArgs('--models a:b,c:d explain monads')).toEqual({
      prompt: 'explain monads',
      specs: ['a:b', 'c:d']
    })
    expect(parseCompareArgs('explain --model=a:b monads')).toEqual({ prompt: 'explain monads', specs: ['a:b'] })
  })

  it('drops empty specs and leaves an empty prompt when only the flag was given', () => {
    expect(parseCompareArgs('--models a:b,,')).toEqual({ prompt: '', specs: ['a:b'] })
  })
})

describe('dedupeCandidates', () => {
  it('dedupes case-insensitively, keeps order and caps the fan-out', () => {
    expect(dedupeCandidates(['a:B', ' a:b', '', 'c:d'])).toEqual(['a:B', 'c:d'])
    const many = Array.from({ length: MAX_COMPARE_CANDIDATES + 3 }, (_, i) => `p:m${i}`)
    expect(dedupeCandidates(many)).toHaveLength(MAX_COMPARE_CANDIDATES)
  })
})

describe('toggleMember', () => {
  it('flips membership without mutating the input', () => {
    const base = new Set(['a:1'])
    expect([...toggleMember(base, 'b:2')].sort()).toEqual(['a:1', 'b:2'])
    expect([...toggleMember(base, 'a:1')]).toEqual([])
    expect([...base]).toEqual(['a:1'])
  })
})

describe('filterCompareChoices', () => {
  const rows = ['openrouter:anthropic/claude-sonnet-5', 'anthropic:claude-opus-5', 'openrouter:openai/gpt-5', 'opencode-free:deepseek-v4-flash-free']

  it('returns every row in order for an empty query', () => {
    expect(filterCompareChoices(rows, '')).toEqual(rows)
    expect(filterCompareChoices(rows, '   ')).toEqual(rows)
  })

  it('narrows to fuzzy matches on the provider:model label', () => {
    expect(filterCompareChoices(rows, 'gpt')).toEqual(['openrouter:openai/gpt-5'])
    // Verbatim matches first, then fuzzy subsequence matches.
    expect(filterCompareChoices(rows, 'OPUS')).toEqual(['anthropic:claude-opus-5', 'openrouter:anthropic/claude-sonnet-5'])
    expect(filterCompareChoices(rows, 'zzz')).toEqual([])
  })
})

describe('compare result formatting', () => {
  it('formats stats and the fastest-first summary with failures last', () => {
    const slow = { cost_usd: 0.02, elapsed_s: 3, input_tokens: 5, label: 'a:slow', output_tokens: 9, text: 'x' }
    const fast = { cost_usd: 0.001, elapsed_s: 1, label: 'b:fast', output_tokens: 4, text: 'y' }
    const bad = { elapsed_s: 0.5, error: 'down', label: 'c:bad' }

    expect(formatCompareStats(slow)).toBe('3.0s · 5→9 tokens · $0.02')
    expect(compareSummaryLines([slow, bad, fast])).toEqual([
      '📊 comparison summary',
      '✅ b:fast — 1.0s · 4 out tokens · $0.0010',
      '✅ a:slow — 3.0s · 9 out tokens · $0.02',
      '❌ c:bad — down'
    ])
    expect(compareSummaryLines([])).toEqual(['📊 comparison finished with no results'])
  })
})
