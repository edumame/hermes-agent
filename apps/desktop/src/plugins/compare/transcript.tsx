/**
 * A saved comparison inside a chat. The backend stores the run as one
 * assistant turn typed `display_kind: "compare"` with the structured rows in
 * its metadata; this renders that turn as side-by-side cards (the same read
 * the Compare page gives) instead of the markdown fallback, and puts a
 * "Continue with this model" action on every answered card: it branches a
 * child chat off this one, seeded with the prompt and that model's answer
 * and pinned to that model, then opens it.
 */

import {
  Button,
  cn,
  Codicon,
  CopyButton,
  GlyphSpinner,
  host,
  type MessageKindProps,
  queryClient,
  Streamdown,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  branchFromComparison,
  candidateLabel,
  type CompareRunResult,
  formatCost,
  formatElapsed,
  parseCompareTranscript
} from './api'
import { type CompareText, useCompareText } from './i18n'

function ResultCard({
  c,
  onBranch,
  branching,
  result
}: {
  branching: boolean
  c: CompareText
  onBranch: () => void
  result: CompareRunResult
}) {
  const label = candidateLabel({ model: result.model, provider: result.provider })

  const detail = result.ok
    ? c.stats(formatElapsed(result.elapsed_s), result.input_tokens, result.output_tokens, formatCost(result.cost_usd, c.costUnknown))
    : `${c.failed}: ${result.error ?? ''}`

  return (
    <section
      className="flex min-h-0 w-[min(26rem,80vw)] shrink-0 snap-start flex-col overflow-hidden rounded-lg border border-(--ui-stroke-secondary)/50 bg-(--ui-bg-secondary)"
      data-testid="compare-transcript-card"
    >
      <header className="flex shrink-0 items-start gap-2 border-b border-(--ui-stroke-secondary)/50 px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-foreground" title={label}>
            {label}
          </div>
          <div
            className={cn('truncate text-[0.6875rem] tabular-nums', result.ok ? 'text-(--ui-text-tertiary)' : 'text-destructive')}
            title={detail}
          >
            {detail}
          </div>
        </div>
        {result.ok && (
          <Tip label={c.copy}>
            <CopyButton buttonSize="icon-xs" label={c.copy} text={result.text} />
          </Tip>
        )}
      </header>
      <div className="max-h-96 min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {result.ok ? (
          <div className="compare-answer text-sm text-foreground" data-selectable-text="true">
            <Streamdown controls={false} mode="static" parseIncompleteMarkdown={false}>
              {result.text}
            </Streamdown>
          </div>
        ) : (
          <p className="text-sm text-destructive">{result.error}</p>
        )}
      </div>
      {result.ok && (
        <footer className="flex shrink-0 justify-end border-t border-(--ui-stroke-secondary)/50 px-2 py-1.5">
          <Button disabled={branching} onClick={onBranch} size="xs" type="button" variant="secondary">
            {branching ? <GlyphSpinner ariaLabel={c.branching} /> : <Codicon name="git-branch" size="0.75rem" />}
            {branching ? c.branching : c.branchWith}
          </Button>
        </footer>
      )}
    </section>
  )
}

export function CompareTranscript({ metadata, text }: MessageKindProps) {
  const c = useCompareText()
  const parentStoredId = useValue(host.state.focusedStoredSessionId)
  const [branching, setBranching] = useState<null | string>(null)
  const data = parseCompareTranscript(metadata)

  if (!data) {
    // A payload this build cannot read: the markdown the turn also carries.
    return (
      <Streamdown controls={false} mode="static" parseIncompleteMarkdown={false}>
        {text}
      </Streamdown>
    )
  }

  const branch = async (result: CompareRunResult) => {
    if (!parentStoredId) {
      host.notify({ kind: 'error', message: c.branchNoSession })

      return
    }

    const key = `${result.provider}:${result.model}`
    setBranching(key)

    try {
      const child = await branchFromComparison(parentStoredId, data.prompt, result)

      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      await host.openSession(child.stored_session_id, { intent: 'in-place' })
    } catch (error: unknown) {
      host.notify({ kind: 'error', message: c.branchFailed(error instanceof Error ? error.message : String(error)) })
    } finally {
      setBranching(current => (current === key ? null : current))
    }
  }

  const answered = data.results.filter(r => r.ok).length

  return (
    <div className="flex flex-col gap-2" data-testid="compare-transcript">
      <div className="flex items-center gap-1.5 text-xs text-(--ui-text-tertiary)">
        <Codicon name="split-horizontal" size="0.8rem" />
        {c.transcriptTitle(data.results.length, answered)}
      </div>
      <div className="flex snap-x gap-3 overflow-x-auto pb-1">
        {data.results.map(result => {
          const key = `${result.provider}:${result.model}`

          return (
            <ResultCard
              branching={branching === key}
              c={c}
              key={key}
              onBranch={() => void branch(result)}
              result={result}
            />
          )
        })}
      </div>
    </div>
  )
}
