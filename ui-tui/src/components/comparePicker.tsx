import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useEffect, useMemo, useState } from 'react'

import { filterCompareChoices, MAX_COMPARE_CANDIDATES, preselectedChoices, toggleMember } from '../domain/compare.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { CompareOptionsResponse } from '../gatewayTypes.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

import { OverlayHint, windowItems } from './overlayControls.js'
import { chipRowProps, clampOverlayWidth } from './overlayPrimitives.js'

const VISIBLE = 12
const MIN_WIDTH = 40
const MAX_WIDTH = 90
const PROMPT_PREVIEW = 60

/**
 * Multi-select model checklist for a bare `/compare <prompt>`. Pulls every row from
 * `compare.options` (the session's current route first, then each model of every provider
 * configured in this profile), narrows them as you type, toggles with Space and hands the checked
 * `provider:model` labels to `onPick`. The last comparison's models (`selected`) start checked, so
 * Enter alone repeats that set. The interactive sibling of `/compare --models a:b,c:d <prompt>`.
 */
export function ComparePicker({ gw, maxWidth, onCancel, onPick, prompt, sessionId, t }: ComparePickerProps) {
  const [choices, setChoices] = useState<string[]>([])
  const [current, setCurrent] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [filter, setFilter] = useState('')
  const [idx, setIdx] = useState(0)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  const { stdout } = useStdout()
  const preferredWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, (stdout?.columns ?? 80) - 6))
  const width = clampOverlayWidth(preferredWidth, maxWidth)

  useEffect(() => {
    gw.request<CompareOptionsResponse>('compare.options', { session_id: sessionId ?? '' })
      .then(r => {
        const rows = r?.choices ?? []
        setChoices(rows)
        setCurrent(r?.current ?? '')
        setSelected(preselectedChoices(rows, r?.selected ?? []))
        setErr('')
      })
      .catch((e: unknown) => setErr(rpcErrorMessage(e)))
      .finally(() => setLoading(false))
  }, [gw, sessionId])

  const visible = useMemo(() => filterCompareChoices(choices, filter), [choices, filter])
  const cursor = Math.min(idx, Math.max(0, visible.length - 1))

  const toggle = (label: string | undefined) => {
    if (label === undefined) {
      return
    }

    setErr('')
    setSelected(s => {
      if (!s.has(label) && s.size >= MAX_COMPARE_CANDIDATES) {
        setErr(`at most ${MAX_COMPARE_CANDIDATES} models per comparison`)

        return s
      }

      return toggleMember(s, label)
    })
  }

  const setQuery = (next: string) => {
    setFilter(next)
    setIdx(0)
    setErr('')
  }

  useInput((input, key) => {
    if (key.escape) {
      return onCancel()
    }

    if (loading) {
      return
    }

    if (key.upArrow) {
      return setIdx(Math.max(0, cursor - 1))
    }

    if (key.downArrow) {
      return setIdx(Math.min(Math.max(0, visible.length - 1), cursor + 1))
    }

    if (key.return) {
      const labels = choices.filter(c => selected.has(c))

      if (!labels.length) {
        return setErr('pick at least one model (Space toggles a row)')
      }

      return onPick(labels)
    }

    if (input === ' ') {
      return toggle(visible[cursor])
    }

    if (key.backspace || key.delete) {
      return setQuery(filter.slice(0, -1))
    }

    if (input === '\u0015') {
      return setQuery('')
    }

    if (input && !key.ctrl && !key.meta) {
      return setQuery(filter + input)
    }
  })

  if (loading) {
    return <Text color={t.color.muted}>loading models…</Text>
  }

  if (!choices.length) {
    return (
      <Box flexDirection="column" width={width}>
        <Text bold color={t.color.accent}>
          Compare models
        </Text>
        <Text color={t.color.label}>
          {err ? `error: ${err}` : 'no models available — add provider credentials (hermes model) or pass --models'}
        </Text>
        <OverlayHint t={t}>Esc cancel</OverlayHint>
      </Box>
    )
  }

  const { items, offset } = windowItems(visible, cursor, VISIBLE)
  const preview = prompt.length > PROMPT_PREVIEW ? `${prompt.slice(0, PROMPT_PREVIEW - 1)}…` : prompt

  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent}>
        Compare models
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        prompt: {preview}
      </Text>

      <Text color={t.color.muted} wrap="truncate-end">
        {selected.size} of {choices.length} selected · up to {MAX_COMPARE_CANDIDATES}
        {filter ? ` · filter: ${filter}` : ' · type to filter'}
      </Text>

      {offset > 0 && <Text color={t.color.muted}> ↑ {offset} more</Text>}

      {visible.length === 0 && <Text color={t.color.label}> no model matches "{filter}"</Text>}

      {items.map((label, i) => {
        const n = offset + i
        const at = n === cursor
        const checked = selected.has(label)

        return (
          <Text color={t.color.muted} {...chipRowProps(t, at)} key={label} wrap="truncate-end">
            {at ? '▸ ' : '  '}
            {checked ? '[x] ' : '[ ] '}
            {label}
            {label === current ? <Text color={at ? t.color.accent : t.color.muted}> (current)</Text> : null}
          </Text>
        )
      })}

      {offset + VISIBLE < visible.length && (
        <Text color={t.color.muted}> ↓ {visible.length - offset - VISIBLE} more</Text>
      )}

      {err ? <Text color={t.color.label}>{err}</Text> : null}

      <OverlayHint t={t}>↑/↓ move · Space toggle · type to filter · Enter compare · Esc cancel</OverlayHint>
    </Box>
  )
}

interface ComparePickerProps {
  gw: GatewayClient
  maxWidth?: number
  onCancel: () => void
  onPick: (labels: string[]) => void
  prompt: string
  sessionId: null | string
  t: Theme
}
