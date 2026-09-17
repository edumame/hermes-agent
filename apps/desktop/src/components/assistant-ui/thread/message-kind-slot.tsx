import { useAuiState } from '@assistant-ui/react'
import { type FC, type ReactNode, useMemo } from 'react'

import { messageContentText } from '@/components/assistant-ui/thread/content'
import { useContributions } from '@/contrib'
import { ContribBoundary, ContribRender } from '@/contrib/react/boundary'
import { MESSAGE_KINDS_AREA, type MessageKindContribution } from '@/lib/message-kinds'

/**
 * The assistant turn's body: a contributed renderer when the turn carries a
 * `display_kind` some plugin claims, else the ordinary parts (`fallback`).
 * Reads only stable metadata off the message — never the streaming text —
 * so it costs nothing per token on a live turn (typed turns are persisted
 * ones; a streaming reply has no kind).
 */
export const MessageKindSlot: FC<{ fallback: ReactNode }> = ({ fallback }) => {
  const displayKind = useAuiState(s => s.message.metadata?.custom?.displayKind as string | undefined)
  const contributions = useContributions(MESSAGE_KINDS_AREA)

  const contribution = displayKind
    ? contributions.find(c => (c.data as MessageKindContribution | undefined)?.displayKind === displayKind)
    : undefined

  const render = (contribution?.data as MessageKindContribution | undefined)?.render

  if (!contribution || !render) {
    return <>{fallback}</>
  }

  return <MessageKindEntry id={contribution.id} render={render} />
}

const MessageKindEntry: FC<{ id: string; render: MessageKindContribution['render'] }> = ({ id, render }) => {
  const messageId = useAuiState(s => s.message.id)
  const metadata = useAuiState(s => s.message.metadata?.custom?.displayMetadata as Record<string, unknown> | undefined)
  const text = useAuiState(s => messageContentText(s.message.content))

  // Stable identity: ContribRender mounts this AS a component.
  const renderBody = useMemo(
    () => () => render({ messageId, metadata: metadata ?? {}, text }),
    [render, messageId, metadata, text]
  )

  return (
    <ContribBoundary id={id} variant="chip">
      <ContribRender render={renderBody} />
    </ContribBoundary>
  )
}
