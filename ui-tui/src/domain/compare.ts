import { fuzzyRank } from '@hermes/shared/fuzzy'

import type { CompareRunResult } from '../gatewayTypes.js'

/**
 * `/compare [--models a:b,c:d] <prompt>` argument handling for the TUI — the client-side twin of
 * `agent.model_compare.parse_compare_args` so the slash command can decide locally whether to open
 * the model picker or fan out straight away.
 */
const MODELS_FLAG_RE = /(?:^|\s)--models?(?:=|\s+)(\S+)/

/** Fan-out cap, mirrored from `agent.model_compare.MAX_COMPARE_CANDIDATES`. */
export const MAX_COMPARE_CANDIDATES = 8

export interface CompareArgs {
  prompt: string
  specs: string[]
}

export function parseCompareArgs(raw: string): CompareArgs {
  const text = (raw ?? '').trim()
  const match = MODELS_FLAG_RE.exec(text)

  if (!match) {
    return { prompt: text, specs: [] }
  }

  const specs = match[1]!
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)

  const prompt = `${text.slice(0, match.index)} ${text.slice(match.index + match[0].length)}`
    .split(/\s+/)
    .filter(Boolean)
    .join(' ')

  return { prompt, specs }
}

/** Case-insensitive dedupe (order preserved), capped at the fan-out limit. */
export function dedupeCandidates(specs: readonly string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []

  for (const raw of specs) {
    const spec = raw.trim()
    const key = spec.toLowerCase()

    if (!spec || seen.has(key)) {
      continue
    }

    seen.add(key)
    out.push(spec)

    if (out.length >= MAX_COMPARE_CANDIDATES) {
      break
    }
  }

  return out
}

export function formatCompareCost(cost: null | number | undefined): string {
  if (cost === null || cost === undefined) {
    return 'n/a'
  }

  if (cost === 0) {
    return '$0'
  }

  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`
}

const elapsed = (r: CompareRunResult) => `${(r.elapsed_s ?? 0).toFixed(1)}s`

export const compareResultOk = (r: CompareRunResult): boolean => !r.error && Boolean(r.text?.trim())

/** Stats footer for one answer: `1.2s · 10→20 tokens · $0.01`. */
export function formatCompareStats(r: CompareRunResult): string {
  return `${elapsed(r)} · ${r.input_tokens ?? 0}→${r.output_tokens ?? 0} tokens · ${formatCompareCost(r.cost_usd)}`
}

/** Closing scoreboard, fastest first, failures last — same order as the Python summary. */
export function compareSummaryLines(results: readonly CompareRunResult[]): string[] {
  if (!results.length) {
    return ['📊 comparison finished with no results']
  }

  const sorted = [...results].sort((a, b) => {
    const okDiff = Number(!compareResultOk(a)) - Number(!compareResultOk(b))

    return okDiff || (a.elapsed_s ?? 0) - (b.elapsed_s ?? 0)
  })

  return [
    '📊 comparison summary',
    ...sorted.map(r =>
      compareResultOk(r)
        ? `✅ ${r.label ?? r.model ?? '?'} — ${elapsed(r)} · ${r.output_tokens ?? 0} out tokens · ${formatCompareCost(r.cost_usd)}`
        : `❌ ${r.label ?? r.model ?? '?'} — ${r.error || 'failed'}`
    )
  ]
}

/**
 * Checklist rows matching a type-to-filter query; every row when empty. Rows containing the query
 * verbatim come first in list order (typing "opus" must surface the Opus rows, not a scattered
 * subsequence match in a longer label), then the fuzzy matches, best first.
 */
export function filterCompareChoices(choices: readonly string[], query: string): string[] {
  const q = query.trim().toLowerCase()

  if (!q) {
    return [...choices]
  }

  const verbatim = choices.filter(c => c.toLowerCase().includes(q))
  const rest = choices.filter(c => !verbatim.includes(c))

  return [...verbatim, ...fuzzyRank(rest, q, c => c).map(r => r.item)]
}

/** `set` with `item` added when absent, removed when present. */
export function toggleMember<T>(set: ReadonlySet<T>, item: T): ReadonlySet<T> {
  const next = new Set(set)

  if (next.has(item)) {
    next.delete(item)
  } else {
    next.add(item)
  }

  return next
}
