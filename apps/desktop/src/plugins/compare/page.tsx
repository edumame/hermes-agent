/**
 * The Compare page — mounted at `/compare` (a ROUTES_AREA contribution) in the
 * workspace pane. A workbench: pick models through the SAME catalog menu the
 * composer uses (a selection ADDS a column instead of switching a session),
 * write one prompt, and read every answer side by side as each model finishes.
 * Every column is a stateless `compare.run` request — no live session is
 * touched — and a finished run is saved as its own chat (`compare.save`), so
 * comparisons outlive the page and can be reopened or continued from the
 * sidebar like any conversation.
 */

import {
  Button,
  cn,
  Codicon,
  CopyButton,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
  EmptyState,
  GlyphSpinner,
  host,
  Kbd,
  ModelCatalogMenu,
  ModelMenuCloseContext,
  type ModelMenuController,
  queryClient,
  Streamdown,
  Textarea,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'

import {
  $candidates,
  addCandidate,
  candidateKey,
  candidateLabel,
  clearCandidates,
  type CompareCandidate,
  type CompareRunResult,
  failedRow,
  formatCost,
  formatElapsed,
  removeCandidate,
  runCandidate,
  saveComparison,
  type SavedComparison
} from './api'
import { type CompareText, useCompareText } from './i18n'

type ColumnState =
  { status: 'done'; result: CompareRunResult } | { status: 'error'; message: string } | { status: 'running' }

/** Where the run is on its way to becoming a chat: `pending` until every
 *  column settled, `skipped` when no model answered (nothing worth a sidebar
 *  row), else the saved chat or why the save failed. */
type SaveState =
  | { status: 'failed'; message: string }
  | { status: 'pending' }
  | { status: 'saved'; chat: SavedComparison }
  | { status: 'skipped' }

interface RunState {
  columns: Record<string, ColumnState>
  /** Snapshot of the candidates the run was launched with, so removing a
   *  model afterwards never drops a column mid-read. */
  candidates: CompareCandidate[]
  prompt: string
  saved: SaveState
  startedAt: number
}

/** Seconds since `startedAt`, ticking once a second while `active`. A slow
 *  model (a reasoning model can take minutes) must look alive, not stuck. */
function useElapsedSeconds(startedAt: number, active: boolean): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) {
      return
    }

    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(timer)
  }, [active, startedAt])

  return Math.max(0, Math.floor((now - startedAt) / 1000))
}

/** "Add model" — the shared catalog menu driven by an ADD controller: a pick
 *  appends a column and closes; nothing is ever written to a session. */
function AddModelMenu({ c }: { c: CompareText }) {
  const [open, setOpen] = useState(false)
  const candidates = useValue($candidates)

  const controller: ModelMenuController = {
    applyPreset: () => {},
    current: { effort: '', fast: false, model: '', provider: '' },
    presetFor: () => ({}),
    select: (model, provider) => {
      if (!addCandidate({ model, provider })) {
        host.notify({ message: c.duplicate })
      }
    },
    setOptions: () => {}
  }

  return (
    <DropdownMenu onOpenChange={setOpen} open={open}>
      <DropdownMenuTrigger asChild>
        <Button size="sm" type="button" variant="secondary">
          <Codicon name="add" size="0.8rem" />
          {c.addModel}
          {candidates.length > 0 && (
            <span className="rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
              {candidates.length}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72 p-0">
        <ModelMenuCloseContext.Provider value={() => setOpen(false)}>
          <ModelCatalogMenu controller={controller} />
        </ModelMenuCloseContext.Provider>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function CandidateChips({ c, disabled }: { c: CompareText; disabled: boolean }) {
  const candidates = useValue($candidates)

  if (candidates.length === 0) {
    return <span className="text-xs text-(--ui-text-tertiary)">{c.noModelsHint}</span>
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {candidates.map(candidate => (
        <span
          className="inline-flex h-6 items-center gap-1 rounded-full bg-(--ui-bg-quaternary) pr-1 pl-2.5 text-xs text-(--ui-text-primary)"
          data-testid="compare-candidate"
          key={candidateKey(candidate)}
        >
          <span className="max-w-64 truncate">{candidateLabel(candidate)}</span>
          <button
            aria-label={c.removeModel(candidateLabel(candidate))}
            className="grid size-4 place-items-center rounded-full text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground disabled:opacity-40"
            disabled={disabled}
            onClick={() => removeCandidate(candidate)}
            type="button"
          >
            <Codicon name="close" size="0.65rem" />
          </button>
        </span>
      ))}
      <Button disabled={disabled} onClick={clearCandidates} size="xs" type="button" variant="ghost">
        {c.clearModels}
      </Button>
    </div>
  )
}

function ColumnHeader({
  c,
  candidate,
  startedAt,
  state
}: {
  c: CompareText
  candidate: CompareCandidate
  startedAt: number
  state?: ColumnState
}) {
  const waiting = !state || state.status === 'running'
  const elapsed = useElapsedSeconds(startedAt, waiting)
  let detail: string
  let tone = 'text-(--ui-text-tertiary)'

  if (waiting) {
    detail = c.waiting
  } else if (state.status === 'error') {
    detail = `${c.failed}: ${state.message}`
    tone = 'text-destructive'
  } else if (!state.result.ok) {
    detail = `${c.failed}: ${state.result.error ?? ''}`
    tone = 'text-destructive'
  } else {
    const r = state.result
    detail = c.stats(formatElapsed(r.elapsed_s), r.input_tokens, r.output_tokens, formatCost(r.cost_usd, c.costUnknown))
  }

  return (
    <header className="flex shrink-0 items-start gap-2 border-b border-(--ui-stroke-secondary)/50 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-semibold text-foreground" title={candidateLabel(candidate)}>
          {candidateLabel(candidate)}
        </div>
        <div className={cn('truncate text-[0.6875rem] tabular-nums', tone)} title={detail}>
          <span>{detail}</span>
          {waiting && <span className="ml-1">{c.elapsed(elapsed)}</span>}
        </div>
      </div>
      {!state || state.status === 'running' ? (
        <GlyphSpinner ariaLabel={c.waiting} className="mt-0.5 shrink-0" />
      ) : (
        state.status === 'done' &&
        state.result.ok && (
          <Tip label={c.copy}>
            <CopyButton buttonSize="icon-xs" label={c.copy} text={state.result.text} />
          </Tip>
        )
      )}
    </header>
  )
}

function ResultColumn({
  c,
  candidate,
  startedAt,
  state
}: {
  c: CompareText
  candidate: CompareCandidate
  startedAt: number
  state?: ColumnState
}) {
  const body =
    state?.status === 'done' && state.result.ok ? (
      <div className="compare-answer text-sm text-foreground" data-selectable-text="true">
        <Streamdown controls={false} mode="static" parseIncompleteMarkdown={false}>
          {state.result.text}
        </Streamdown>
      </div>
    ) : state?.status === 'done' ? (
      <p className="text-sm text-destructive">{state.result.error}</p>
    ) : state?.status === 'error' ? (
      <p className="text-sm text-destructive">{state.message}</p>
    ) : null

  return (
    <section
      className="flex min-h-0 w-[min(28rem,80vw)] shrink-0 snap-start flex-col overflow-hidden rounded-lg border border-(--ui-stroke-secondary)/50 bg-(--ui-bg-secondary)"
      data-testid="compare-column"
    >
      <ColumnHeader c={c} candidate={candidate} startedAt={startedAt} state={state} />
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">{body}</div>
    </section>
  )
}

/** The one-line save status under the composer: spinner while the chat is
 *  being written, then the saved chat with a button that opens it. */
function SaveStatus({ c, state }: { c: CompareText; state: SaveState }) {
  if (state.status === 'pending') {
    return (
      <span className="flex items-center gap-1.5" data-testid="compare-save-status">
        <GlyphSpinner ariaLabel={c.saving} />
        {c.saving}
      </span>
    )
  }

  if (state.status === 'skipped') {
    return <span data-testid="compare-save-status">{c.notSaved}</span>
  }

  if (state.status === 'failed') {
    return (
      <span className="text-destructive" data-testid="compare-save-status">
        {c.saveFailed(state.message)}
      </span>
    )
  }

  return (
    <span className="flex items-center gap-1.5" data-testid="compare-save-status">
      <Codicon name="comment-discussion" size="0.8rem" />
      {c.savedAsChat}
      <Button
        onClick={() => {
          void host.openSession(state.chat.stored_session_id, { intent: 'in-place' }).catch((error: unknown) => {
            host.notify({ message: error instanceof Error ? error.message : String(error) })
          })
        }}
        size="xs"
        type="button"
        variant="ghost"
      >
        {c.openChat}
      </Button>
    </span>
  )
}

export function ComparePage() {
  const c = useCompareText()
  const candidates = useValue($candidates)
  const [prompt, setPrompt] = useState('')
  const [run, setRun] = useState<RunState | null>(null)
  // Increments per launch: a column from a superseded run must never paint
  // over the run the user is now looking at.
  const runId = useRef(0)
  const running = run !== null && Object.values(run.columns).some(col => col.status === 'running')
  const canRun = !running && prompt.trim().length > 0 && candidates.length > 0

  const launch = useCallback(() => {
    const text = prompt.trim()
    const targets = $candidates.get()

    if (!text || targets.length === 0) {
      return
    }

    const id = ++runId.current
    const columns: Record<string, ColumnState> = {}

    for (const candidate of targets) {
      columns[candidateKey(candidate)] = { status: 'running' }
    }

    setRun({ candidates: targets, columns, prompt: text, saved: { status: 'pending' }, startedAt: Date.now() })

    // The rows the saved chat records, keyed like the columns: a backend row
    // for a finished model, a synthesized failed row when the request itself
    // failed, so the transcript names every model that was asked.
    const rows = new Map<string, CompareRunResult>()

    const settle = (candidate: CompareCandidate, state: Exclude<ColumnState, { status: 'running' }>) => {
      if (runId.current !== id) {
        return
      }

      rows.set(candidateKey(candidate), state.status === 'done' ? state.result : failedRow(candidate, state.message))
      setRun(prev => (prev ? { ...prev, columns: { ...prev.columns, [candidateKey(candidate)]: state } } : prev))
    }

    const setSaved = (saved: SaveState) => {
      if (runId.current === id) {
        setRun(prev => (prev ? { ...prev, saved } : prev))
      }
    }

    const requests = targets.map(candidate =>
      runCandidate(text, candidate).then(
        result => settle(candidate, { result, status: 'done' }),
        (error: unknown) =>
          settle(candidate, { message: error instanceof Error ? error.message : String(error), status: 'error' })
      )
    )

    // Every column settled: persist the run as a chat. Only a run somebody
    // answered earns a sidebar row — all-failed runs stay on this page.
    void Promise.all(requests).then(async () => {
      if (runId.current !== id) {
        return
      }

      const results = targets.map(candidate => rows.get(candidateKey(candidate))).filter((r): r is CompareRunResult => !!r)

      if (!results.some(r => r.ok)) {
        setSaved({ status: 'skipped' })

        return
      }

      try {
        const chat = await saveComparison(text, results)
        // The sidebar reads its rows through the shared query cache: tell it a chat appeared.
        void queryClient.invalidateQueries({ queryKey: ['sessions'] })
        setSaved({ chat, status: 'saved' })
      } catch (error: unknown) {
        setSaved({ message: error instanceof Error ? error.message : String(error), status: 'failed' })
      }
    })
  }, [prompt])

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()

      if (canRun) {
        launch()
      }
    }
  }

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-(--ui-surface-background)">
      <header className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2">
        <h1 className="text-sm font-semibold text-foreground">{c.title}</h1>
        <span className="text-xs text-(--ui-text-tertiary)">{c.description}</span>
      </header>

      <div className="flex shrink-0 flex-col gap-2 px-4 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <AddModelMenu c={c} />
          <CandidateChips c={c} disabled={running} />
        </div>
        <div className="flex items-end gap-2">
          <Textarea
            aria-label={c.promptPlaceholder}
            className="max-h-48 flex-1 resize-y"
            onChange={event => setPrompt(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={c.promptPlaceholder}
            rows={3}
            value={prompt}
          />
          <div className="flex flex-col items-end gap-1">
            <Button disabled={!canRun} onClick={launch} size="sm" type="button">
              {running ? <GlyphSpinner ariaLabel={c.running} /> : <Codicon name="play" size="0.8rem" />}
              {running ? c.running : c.run}
            </Button>
            <span className="flex items-center gap-1 text-[0.625rem] text-(--ui-text-tertiary)">
              <Kbd>⌘↵</Kbd>
              {c.runHint}
            </span>
          </div>
        </div>
      </div>

      {run !== null && !running && (
        <div className="flex shrink-0 items-center gap-2 px-4 pb-2 text-xs text-(--ui-text-tertiary)">
          <SaveStatus c={c} state={run.saved} />
        </div>
      )}

      <div className="min-h-0 flex-1 px-4 pb-4">
        {run === null ? (
          <EmptyState className="h-full" description={c.emptyResultsHint} title={c.emptyResults} />
        ) : (
          <div className="flex h-full snap-x gap-3 overflow-x-auto" data-testid="compare-results">
            {run.candidates.map(candidate => (
              <ResultColumn
                c={c}
                candidate={candidate}
                key={candidateKey(candidate)}
                startedAt={run.startedAt}
                state={run.columns[candidateKey(candidate)]}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
