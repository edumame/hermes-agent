import {
  compareResultOk,
  compareSummaryLines,
  dedupeCandidates,
  formatCompareStats,
  parseCompareArgs
} from '../../../domain/compare.js'
import type { CompareRunResponse, CompareRunResult } from '../../../gatewayTypes.js'
import { patchOverlayState } from '../../overlayStore.js'
import type { SlashCommand, SlashRunCtx } from '../types.js'

const USAGE =
  'usage: /compare [--models provider:model,provider:model] <prompt> — send one prompt to several models and show every answer separately. Without --models you pick from a list of models on your configured providers.'

// The server gives each candidate 180s; leave headroom so a slow model is reported as its own
// failure rather than a client-side RPC timeout.
const COMPARE_RPC_TIMEOUT_MS = 200_000

const ANSWER_MAX_CHARS = 3500

const asResult = (label: string, r: CompareRunResponse | null | undefined): CompareRunResult =>
  r?.results?.[0] ?? { error: 'no result', label }

/**
 * Fan `prompt` out to `specs` — one `compare.run` request per model so answers paint as they
 * land — then print the scoreboard. Runs against the TUI's own gateway (not the slash worker):
 * a comparison can outlive the worker's per-command budget, and the session is never touched.
 */
export function runComparison(ctx: SlashRunCtx, prompt: string, specs: readonly string[]): void {
  const { panel, sys } = ctx.transcript
  const labels = dedupeCandidates(specs)

  if (!labels.length) {
    return sys('/compare: no valid models to compare')
  }

  const total = labels.length
  const results: CompareRunResult[] = []

  sys(`🔬 comparing ${total} model${total === 1 ? '' : 's'}: ${labels.join(', ')} — answers appear as each finishes`)

  // Remember the set so the next picker starts from it; best-effort, never blocks the run.
  void ctx.gateway.gw.request('compare.remember', { candidates: labels }).catch(() => undefined)

  const show = (result: CompareRunResult) => {
    results.push(result)
    const title = `🔬 [${results.length}/${total}] ${result.label ?? result.model ?? '?'}`

    if (!compareResultOk(result)) {
      return panel(title, [{ text: `❌ failed: ${result.error || 'empty response'}` }])
    }

    const body = (result.text ?? '').trim()
    const shown = body.length > ANSWER_MAX_CHARS ? `${body.slice(0, ANSWER_MAX_CHARS).trimEnd()}\n\n… (truncated)` : body

    panel(title, [{ text: shown }, { text: formatCompareStats(result) }])
  }

  const requests = labels.map(label =>
    ctx.gateway.gw
      .request<CompareRunResponse>(
        'compare.run',
        { candidates: [label], prompt, session_id: ctx.sid },
        COMPARE_RPC_TIMEOUT_MS
      )
      .then(
        r => show(asResult(label, r)),
        (e: unknown) => show({ error: e instanceof Error ? e.message : String(e), label })
      )
  )

  void Promise.all(requests).then(() => sys(compareSummaryLines(results).join('\n')))
}

export const compareCommands: SlashCommand[] = [
  {
    help: 'send one prompt to several models and show every answer side by side',
    name: 'compare',
    run: (arg, ctx) => {
      const { prompt, specs } = parseCompareArgs(arg)

      if (!prompt) {
        return ctx.transcript.sys(USAGE)
      }

      if (specs.length) {
        return runComparison(ctx, prompt, specs)
      }

      // No --models: open the checklist; the pick re-enters here with explicit labels.
      patchOverlayState({ comparePicker: { onPick: labels => runComparison(ctx, prompt, labels), prompt } })
    },
    usage: '/compare [--models a:b,c:d] <prompt>'
  }
]
