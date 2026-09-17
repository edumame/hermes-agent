import type { ReactNode } from 'react'

/**
 * MESSAGE KINDS — a typed assistant turn as a contribution area.
 *
 * The backend stamps some persisted turns with a `display_kind` and a
 * structured `display_metadata` (a saved model comparison, say). Core knows
 * how to paint markdown; it does not know what a comparison looks like or
 * what actions belong beside it — the plugin that produced the turn does. So
 * the body of such a turn is contributed: a registration names the kind it
 * renders, and the assistant bubble hands it the turn's metadata instead of
 * painting the markdown fallback. The footer (copy, read aloud, reactions)
 * stays core's, and the markdown stays in the transcript for copy/search.
 *
 * One kind, one renderer: the first enabled registration for a kind wins.
 */

export const MESSAGE_KINDS_AREA = 'message.kinds'

/** Props handed to a message-kind contribution's `render`. */
export interface MessageKindProps {
  /** The assistant-ui message id (not the durable row id). */
  messageId: string
  /** The turn's `display_metadata`, already parsed. */
  metadata: Record<string, unknown>
  /** The markdown the transcript would otherwise paint — the copy/fallback text. */
  text: string
}

/** Payload of a `message.kinds` contribution's `data`. */
export interface MessageKindContribution {
  /** The `display_kind` this renders (`'compare'`). */
  displayKind: string
  /** Renders the turn's body. Mounted as a component inside the contribution
   *  error boundary, so a throw degrades to an inline error, not a dead chat. */
  render: (props: MessageKindProps) => ReactNode
}
