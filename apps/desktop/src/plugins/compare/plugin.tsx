/**
 * Compare — multi-model comparisons as a first-class page: a `/compare`
 * route, a sidebar nav row and a palette command. Pure SDK-consumer work on
 * top of the `compare.run` JSON-RPC method (`tui_gateway/methods_compare.py`);
 * the messaging twin is the `/compare` gateway slash command.
 */

import {
  type HermesPlugin,
  host,
  MESSAGE_KINDS_AREA,
  type MessageKindContribution,
  type MessageKindProps,
  PALETTE_AREA,
  type PaletteContribution,
  type RouteContribution,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution
} from '@hermes/plugin-sdk'

import { bindCandidateStorage } from './api'
import { COMPARE_LOCALES } from './i18n'
import { ComparePage } from './page'
import { CompareTranscript } from './transcript'

export const COMPARE_PATH = '/compare'

const plugin: HermesPlugin = {
  id: 'compare',
  name: 'Compare',
  description: 'Send one prompt to several models and read the answers side by side.',
  register(ctx) {
    ctx.i18n.register(COMPARE_LOCALES)
    ctx.onDispose(bindCandidateStorage(ctx.storage))

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: COMPARE_PATH } satisfies RouteContribution,
        render: () => <ComparePage />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 40,
        data: {
          codicon: 'split-horizontal',
          label: ctx.i18n.t('nav'),
          path: COMPARE_PATH
        } satisfies SidebarNavContribution
      },
      {
        // A saved comparison inside a chat paints as cards with a
        // "continue with this model" action instead of its markdown.
        id: 'transcript',
        area: MESSAGE_KINDS_AREA,
        data: {
          displayKind: 'compare',
          render: (props: MessageKindProps) => <CompareTranscript {...props} />
        } satisfies MessageKindContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'compare.open',
          label: ctx.i18n.t('openCommand'),
          keywords: ['compare', 'models', 'side by side', 'benchmark'],
          run: () => host.navigate(COMPARE_PATH)
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
