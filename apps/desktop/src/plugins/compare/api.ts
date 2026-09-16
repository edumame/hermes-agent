/**
 * Compare page data layer: the candidate list (a plugin-owned nanostore
 * persisted through the plugin storage door) and the `compare.run` RPC
 * wrapper. One request per candidate — the backend accepts a batch, but the
 * page wants every column to paint the moment its model finishes, and a
 * per-candidate request is the simplest way to get that without a stream.
 */

import { atom, host, type PluginStorage } from '@hermes/plugin-sdk'

export interface CompareCandidate {
  model: string
  provider: string
}

/** Wire shape of one `compare.run` result (`agent/model_compare.py::CompareResult.to_dict`). */
export interface CompareRunResult {
  cost_status?: null | string
  cost_usd?: null | number
  elapsed_s: number
  error?: null | string
  input_tokens: number
  label: string
  model: string
  ok: boolean
  output_tokens: number
  provider: string
  text: string
}

interface CompareRunResponse {
  prompt: string
  results: CompareRunResult[]
}

export const MAX_CANDIDATES = 8
const STORAGE_KEY = 'candidates'

/** The models lined up for the next run. Persisted per plugin (global scope:
 *  a comparison set is a workbench, not a per-session fact). */
export const $candidates = atom<CompareCandidate[]>([])

export const candidateKey = (c: CompareCandidate): string => `${c.provider.toLowerCase()}:${c.model.toLowerCase()}`

export const candidateLabel = (c: CompareCandidate): string => (c.provider ? `${c.provider}: ${c.model}` : c.model)

/** Add a candidate unless it is already listed or the cap is reached. Returns
 *  whether the list changed. */
export function addCandidate(next: CompareCandidate): boolean {
  const model = next.model.trim()
  const provider = next.provider.trim()

  if (!model) {
    return false
  }

  const current = $candidates.get()
  const key = candidateKey({ model, provider })

  if (current.length >= MAX_CANDIDATES || current.some(c => candidateKey(c) === key)) {
    return false
  }

  $candidates.set([...current, { model, provider }])

  return true
}

export function removeCandidate(target: CompareCandidate): void {
  const key = candidateKey(target)
  $candidates.set($candidates.get().filter(c => candidateKey(c) !== key))
}

export function clearCandidates(): void {
  $candidates.set([])
}

function isCandidate(value: unknown): value is CompareCandidate {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as CompareCandidate).model === 'string' &&
    typeof (value as CompareCandidate).provider === 'string'
  )
}

/** Hydrate the list from plugin storage and keep it written back. Returns the
 *  subscription disposer (wire it through `ctx.onDispose`). */
export function bindCandidateStorage(storage: PluginStorage): () => void {
  const stored = storage.get<unknown>(STORAGE_KEY, [])

  if (Array.isArray(stored)) {
    $candidates.set(stored.filter(isCandidate).slice(0, MAX_CANDIDATES))
  }

  return $candidates.subscribe(value => storage.set(STORAGE_KEY, value))
}

export const formatCost = (cost: null | number | undefined, unknown: string): string => {
  if (cost === null || cost === undefined) {
    return unknown
  }

  if (cost === 0) {
    return '$0'
  }

  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`
}

export const formatElapsed = (seconds: number): string => (seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1))

export interface RunOptions {
  maxTokens?: number
  system?: string
}

/** Ask the backend for ONE candidate's answer. Resolves the result row (the
 *  backend reports provider failures as `ok: false` rows, not RPC errors), and
 *  throws only when the request itself fails. */
export async function runCandidate(
  prompt: string,
  candidate: CompareCandidate,
  options: RunOptions = {}
): Promise<CompareRunResult> {
  const response = await host.request<CompareRunResponse>('compare.run', {
    candidates: [{ model: candidate.model, provider: candidate.provider }],
    prompt,
    ...(options.system ? { system: options.system } : {}),
    ...(options.maxTokens ? { max_tokens: options.maxTokens } : {})
  })

  const row = response.results?.[0]

  if (!row) {
    throw new Error('compare.run returned no result')
  }

  return row
}
