/**
 * Compare page data layer: the candidate list (a plugin-owned nanostore
 * mirrored to plugin storage for an instant paint and synced with the
 * profile's saved comparison set through `compare.selection`, so the desktop,
 * the TUI picker and the messaging `/compare` all start from ONE list), the
 * `compare.run` RPC wrapper and the `compare.save` wrapper that turns a
 * finished run into a chat. One run request per candidate — the backend
 * accepts a batch, but the page wants every column to paint the moment its
 * model finishes, and a per-candidate request is the simplest way to get that
 * without a stream.
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

/** The models lined up for the next run. Mirrored to plugin storage (instant
 *  paint, offline fallback); the profile's saved selection on the backend is
 *  the source of truth once it answers (`bindCandidateStorage`). */
export const $candidates = atom<CompareCandidate[]>([])

export const candidateKey = (c: CompareCandidate): string => `${c.provider.toLowerCase()}:${c.model.toLowerCase()}`

export const candidateLabel = (c: CompareCandidate): string => (c.provider ? `${c.provider}: ${c.model}` : c.model)

/** The `provider:model` spelling the backend stores (`agent.model_compare.normalize_candidate_labels`). */
export const candidateWireLabel = (c: CompareCandidate): string => (c.provider ? `${c.provider}:${c.model}` : c.model)

/** A stored `provider:model` label as a candidate. Only the FIRST colon splits:
 *  model ids carry their own (`nvidia/x:free`). No colon → bare model. */
export function parseCandidateLabel(label: string): CompareCandidate | null {
  const text = label.trim()
  const colon = text.indexOf(':')
  const provider = colon >= 0 ? text.slice(0, colon).trim() : ''
  const model = (colon >= 0 ? text.slice(colon + 1) : text).trim()

  return model ? { model, provider } : null
}

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

interface SelectionResponse {
  models?: string[]
}

const sameCandidates = (a: CompareCandidate[], b: CompareCandidate[]): boolean =>
  a.length === b.length && a.every((c, i) => candidateKey(c) === candidateKey(b[i]!))

/** Best-effort write of the list as the profile's saved selection. */
async function pushSelection(list: readonly CompareCandidate[]): Promise<void> {
  try {
    await host.request('compare.selection', { models: list.map(candidateWireLabel) })
  } catch {
    // No socket / older backend: the stored mirror still holds the list.
  }
}

/**
 * Hydrate the list from plugin storage, then keep it in step with the
 * profile's saved comparison set: the backend's list replaces the local one
 * when it answers (and again whenever the active profile changes or the
 * socket reopens), and every local edit is pushed back with
 * `compare.selection`, so the TUI picker and `/compare` start from what the
 * page shows and vice versa. A local edit made while a pull is in flight wins
 * over that pull's answer. Returns the disposer (wire it through
 * `ctx.onDispose`).
 */
export function bindCandidateStorage(storage: PluginStorage): () => void {
  const stored = storage.get<unknown>(STORAGE_KEY, [])

  if (Array.isArray(stored)) {
    $candidates.set(stored.filter(isCandidate).slice(0, MAX_CANDIDATES))
  }

  // Bumped on every LOCAL edit; a pull only applies if nothing changed meanwhile.
  let edits = 0
  let applyingRemote = false

  const pull = async () => {
    const epoch = edits

    try {
      const response = await host.request<SelectionResponse>('compare.selection')

      const remote = (response?.models ?? [])
        .map(parseCandidateLabel)
        .filter((c): c is CompareCandidate => c !== null)
        .slice(0, MAX_CANDIDATES)

      const local = $candidates.get()

      if (epoch !== edits || sameCandidates(remote, local)) {
        return
      }

      if (remote.length === 0 && local.length > 0) {
        // Nothing saved for this profile yet: the page's list seeds it rather than being wiped.
        void pushSelection(local)

        return
      }

      applyingRemote = true
      $candidates.set(remote)
    } catch {
      // No socket yet / older backend without the method: the stored mirror stands.
    } finally {
      applyingRemote = false
    }
  }

  // `listen`, not `subscribe`: the latter fires with the current value at bind,
  // which would push the stored mirror over the profile's saved set before the
  // first pull could read it.
  const unsubscribe = $candidates.listen(value => {
    storage.set(STORAGE_KEY, value)

    if (applyingRemote) {
      return
    }

    edits += 1
    void pushSelection(value)
  })

  const unsubscribeProfile = host.state.profile.listen(() => void pull())

  const unsubscribeGateway = host.state.gateway.listen(state => {
    if (state === 'open') {
      void pull()
    }
  })

  void pull()

  return () => {
    unsubscribe()
    unsubscribeProfile()
    unsubscribeGateway()
  }
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

/** The backend gives each candidate 180s (`compare.run`'s default `timeout`);
 *  the RPC wait leaves headroom above it so a slow model is reported as its
 *  own failed column, not as a client-side "request timed out after 30s"
 *  from the socket's generic deadline. Same budget the TUI's `/compare` uses. */
export const COMPARE_RPC_TIMEOUT_MS = 200_000

/** Ask the backend for ONE candidate's answer. Resolves the result row (the
 *  backend reports provider failures as `ok: false` rows, not RPC errors), and
 *  throws only when the request itself fails. */
export async function runCandidate(
  prompt: string,
  candidate: CompareCandidate,
  options: RunOptions = {}
): Promise<CompareRunResult> {
  const response = await host.request<CompareRunResponse>(
    'compare.run',
    {
      candidates: [{ model: candidate.model, provider: candidate.provider }],
      prompt,
      ...(options.system ? { system: options.system } : {}),
      ...(options.maxTokens ? { max_tokens: options.maxTokens } : {})
    },
    COMPARE_RPC_TIMEOUT_MS
  )

  const row = response.results?.[0]

  if (!row) {
    throw new Error('compare.run returned no result')
  }

  return row
}

/** The chat a finished comparison was saved as (`compare.save`). */
export interface SavedComparison {
  stored_session_id: string
  title: string
}

/** A column whose request itself failed (no backend row) as the failed row the
 *  saved transcript records for it, so the chat shows every model that was asked. */
export function failedRow(candidate: CompareCandidate, message: string): CompareRunResult {
  return {
    elapsed_s: 0,
    error: message,
    input_tokens: 0,
    label: candidateLabel(candidate),
    model: candidate.model,
    ok: false,
    output_tokens: 0,
    provider: candidate.provider,
    text: ''
  }
}

/** Persist a finished run as a chat in the active profile's session store —
 *  the prompt as the user turn, every answer (candidate order) as one
 *  assistant turn — titled `Compare: <prompt>`. The row is an ordinary stored
 *  session: the sidebar lists it and opening it resumes it like any chat. */
export async function saveComparison(prompt: string, results: CompareRunResult[]): Promise<SavedComparison> {
  const saved = await host.request<SavedComparison>('compare.save', { prompt, results })

  if (!saved?.stored_session_id) {
    throw new Error('compare.save returned no session')
  }

  return saved
}

/** The child chat `compare.branch` minted off one compared model. */
export interface BranchedChat {
  model: string
  parent_session_id: string
  provider: string
  stored_session_id: string
  title: string
}

/** Branch a saved comparison off ONE of its models: a child chat of the
 *  compare chat, seeded with the prompt and that model's answer and pinned to
 *  that model, so the conversation continues on it as if it had been asked
 *  alone. Returns the stored id to open. */
export async function branchFromComparison(
  parentStoredSessionId: string,
  prompt: string,
  result: CompareRunResult
): Promise<BranchedChat> {
  const branched = await host.request<BranchedChat>('compare.branch', {
    prompt,
    result,
    session_id: parentStoredSessionId
  })

  if (!branched?.stored_session_id) {
    throw new Error('compare.branch returned no session')
  }

  return branched
}

/** The structured rows a saved comparison turn carries (`display_metadata`
 *  of a `display_kind: "compare"` message), or null when the payload is not
 *  one this build understands. */
export interface CompareTranscriptData {
  prompt: string
  results: CompareRunResult[]
}

export function parseCompareTranscript(metadata: Record<string, unknown> | undefined): CompareTranscriptData | null {
  if (!metadata || metadata.kind !== 'compare' || !Array.isArray(metadata.results)) {
    return null
  }

  const results = metadata.results.filter(
    (row): row is CompareRunResult =>
      typeof row === 'object' && row !== null && typeof (row as CompareRunResult).model === 'string'
  )

  return results.length ? { prompt: typeof metadata.prompt === 'string' ? metadata.prompt : '', results } : null
}
