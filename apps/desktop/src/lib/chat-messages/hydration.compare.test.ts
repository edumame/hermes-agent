import { describe, expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import { toChatMessages } from './hydration'

describe('contributed message kinds', () => {
  it('carries a compare turn as its kind plus parsed metadata, keeping the markdown fallback', () => {
    const metadata = { kind: 'compare', prompt: 'q', results: [{ model: 'm', provider: 'p', ok: true, text: 'one' }] }

    const messages: SessionMessage[] = [
      { role: 'user', content: 'q' },
      {
        role: 'assistant',
        content: '## answers',
        display_kind: 'compare',
        display_metadata: metadata
      }
    ]

    const [, answer] = toChatMessages(messages)

    expect(answer?.displayKind).toBe('compare')
    expect(answer?.displayMetadata).toEqual(metadata)
    expect(answer?.parts.map(part => (part.type === 'text' ? part.text : part.type))).toEqual(['## answers'])
  })

  it('parses metadata an older backend serves as raw JSON text', () => {
    const [answer] = toChatMessages([
      {
        role: 'assistant',
        content: 'x',
        display_kind: 'compare',
        display_metadata: JSON.stringify({ kind: 'compare', prompt: 'q', results: [] })
      }
    ])

    expect(answer?.displayMetadata).toEqual({ kind: 'compare', prompt: 'q', results: [] })
  })

  it("leaves core's own timeline kinds and user turns alone", () => {
    const messages = toChatMessages([
      { role: 'assistant', content: 'model changed', display_kind: 'model_switch' },
      { role: 'user', content: 'hi', display_kind: 'compare' }
    ])

    expect(messages.every(message => message.displayKind === undefined)).toBe(true)
  })
})
