/**
 * Plugin-scoped i18n for the Compare page — bundles shipped under the plugin
 * id via ctx.i18n.register, never touching core en.ts. `useCompareText()`
 * binds the message SHAPE to the plugin translator so components keep typed
 * `c.run` / `c.stats(...)` access (same pattern as the kanban plugin).
 */

import { type PluginLocaleBundles, type PluginTranslate, usePluginI18n } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

type CompareMessages = {
  nav: string
  title: string
  description: string
  openCommand: string
  addModel: string
  clearModels: string
  removeModel: (label: string) => string
  noModels: string
  noModelsHint: string
  promptPlaceholder: string
  run: string
  running: string
  runHint: string
  copy: string
  waiting: string
  elapsed: (seconds: number) => string
  failed: string
  stats: (seconds: string, tokensIn: number, tokensOut: number, cost: string) => string
  costUnknown: string
  duplicate: string
  emptyResults: string
  emptyResultsHint: string
  candidateCount: (n: number) => string
  saving: string
  savedAsChat: string
  openChat: string
  saveFailed: (message: string) => string
  notSaved: string
  transcriptTitle: (models: number, answered: number) => string
  branchWith: string
  branching: string
  branchNoSession: string
  branchFailed: (message: string) => string
}

const en: CompareMessages = {
  nav: 'Compare',
  title: 'Compare models',
  description: 'Send one prompt to several models and read the answers side by side.',
  openCommand: 'Compare: Open',
  addModel: 'Add model',
  clearModels: 'Clear',
  removeModel: (label: string) => `Remove ${label}`,
  noModels: 'No models selected',
  noModelsHint: 'Pick two or more models to compare, then write a prompt.',
  promptPlaceholder: 'Ask every selected model the same thing…',
  run: 'Run',
  running: 'Running…',
  runHint: 'to run',
  copy: 'Copy answer',
  waiting: 'Waiting for the model…',
  elapsed: (seconds: number) => `${seconds}s`,
  failed: 'Failed',
  stats: (seconds: string, tokensIn: number, tokensOut: number, cost: string) =>
    `${seconds}s · ${tokensIn}→${tokensOut} tokens · ${cost}`,
  costUnknown: 'cost n/a',
  duplicate: 'Already in the comparison',
  emptyResults: 'Nothing to compare yet',
  emptyResultsHint: 'Answers appear here, one column per model, as each model finishes.',
  candidateCount: (n: number) => (n === 1 ? '1 model' : `${n} models`),
  saving: 'Saving as chat…',
  savedAsChat: 'Saved as a chat',
  openChat: 'Open chat',
  saveFailed: (message: string) => `Could not save as a chat: ${message}`,
  notSaved: 'Not saved: no model answered',
  transcriptTitle: (models: number, answered: number) =>
    answered === models ? `Compared ${models} models` : `Compared ${models} models · ${answered} answered`,
  branchWith: 'Continue with this model',
  branching: 'Branching…',
  branchNoSession: 'Open the chat before branching from it',
  branchFailed: (message: string) => `Could not branch: ${message}`
}

const ja: CompareMessages = {
  nav: '比較',
  title: 'モデルを比較',
  description: '1つのプロンプトを複数のモデルに送り、回答を並べて読み比べます。',
  openCommand: '比較: 開く',
  addModel: 'モデルを追加',
  clearModels: 'クリア',
  removeModel: (label: string) => `${label} を削除`,
  noModels: 'モデルが選択されていません',
  noModelsHint: '比較するモデルを2つ以上選び、プロンプトを書いてください。',
  promptPlaceholder: '選択したすべてのモデルに同じ質問をする…',
  run: '実行',
  running: '実行中…',
  runHint: 'で実行',
  copy: '回答をコピー',
  waiting: 'モデルの応答を待っています…',
  elapsed: (seconds: number) => `${seconds}秒`,
  failed: '失敗',
  stats: (seconds: string, tokensIn: number, tokensOut: number, cost: string) =>
    `${seconds}秒 · ${tokensIn}→${tokensOut} トークン · ${cost}`,
  costUnknown: 'コスト不明',
  duplicate: 'すでに比較に含まれています',
  emptyResults: 'まだ比較結果はありません',
  emptyResultsHint: '各モデルが完了するごとに、モデルごとの列として回答がここに表示されます。',
  candidateCount: (n: number) => `${n} モデル`,
  saving: 'チャットとして保存中…',
  savedAsChat: 'チャットとして保存しました',
  openChat: 'チャットを開く',
  saveFailed: (message: string) => `チャットとして保存できませんでした: ${message}`,
  notSaved: '保存されていません: 回答したモデルがありません',
  transcriptTitle: (models: number, answered: number) =>
    answered === models ? `${models} モデルを比較` : `${models} モデルを比較 · ${answered} 件が回答`,
  branchWith: 'このモデルで続ける',
  branching: '分岐中…',
  branchNoSession: '分岐する前にチャットを開いてください',
  branchFailed: (message: string) => `分岐できませんでした: ${message}`
}

const zh: CompareMessages = {
  nav: '对比',
  title: '对比模型',
  description: '把同一个提示发送给多个模型，并排阅读它们的回答。',
  openCommand: '对比：打开',
  addModel: '添加模型',
  clearModels: '清空',
  removeModel: (label: string) => `移除 ${label}`,
  noModels: '尚未选择模型',
  noModelsHint: '选择两个或更多模型进行对比，然后输入提示。',
  promptPlaceholder: '向所有已选模型提出同一个问题…',
  run: '运行',
  running: '运行中…',
  runHint: '运行',
  copy: '复制回答',
  waiting: '正在等待模型…',
  elapsed: (seconds: number) => `${seconds}秒`,
  failed: '失败',
  stats: (seconds: string, tokensIn: number, tokensOut: number, cost: string) =>
    `${seconds}秒 · ${tokensIn}→${tokensOut} 个 token · ${cost}`,
  costUnknown: '费用未知',
  duplicate: '已在对比中',
  emptyResults: '还没有可对比的内容',
  emptyResultsHint: '每个模型完成后，回答会以一列一个模型的形式显示在这里。',
  candidateCount: (n: number) => `${n} 个模型`,
  saving: '正在保存为对话…',
  savedAsChat: '已保存为对话',
  openChat: '打开对话',
  saveFailed: (message: string) => `无法保存为对话: ${message}`,
  notSaved: '未保存: 没有模型给出回答',
  transcriptTitle: (models: number, answered: number) =>
    answered === models ? `已比较 ${models} 个模型` : `已比较 ${models} 个模型 · ${answered} 个已回答`,
  branchWith: '用此模型继续',
  branching: '正在分支…',
  branchNoSession: '请先打开对话再分支',
  branchFailed: (message: string) => `无法分支: ${message}`
}

const zhHant: CompareMessages = {
  nav: '比較',
  title: '比較模型',
  description: '把同一個提示傳送給多個模型，並排閱讀它們的回答。',
  openCommand: '比較：開啟',
  addModel: '新增模型',
  clearModels: '清除',
  removeModel: (label: string) => `移除 ${label}`,
  noModels: '尚未選擇模型',
  noModelsHint: '選擇兩個或更多模型進行比較，然後輸入提示。',
  promptPlaceholder: '向所有已選模型提出同一個問題…',
  run: '執行',
  running: '執行中…',
  runHint: '執行',
  copy: '複製回答',
  waiting: '正在等待模型…',
  elapsed: (seconds: number) => `${seconds}秒`,
  failed: '失敗',
  stats: (seconds: string, tokensIn: number, tokensOut: number, cost: string) =>
    `${seconds}秒 · ${tokensIn}→${tokensOut} 個 token · ${cost}`,
  costUnknown: '費用未知',
  duplicate: '已在比較中',
  emptyResults: '還沒有可比較的內容',
  emptyResultsHint: '每個模型完成後，回答會以一欄一個模型的形式顯示在這裡。',
  candidateCount: (n: number) => `${n} 個模型`,
  saving: '正在儲存為對話…',
  savedAsChat: '已儲存為對話',
  openChat: '開啟對話',
  saveFailed: (message: string) => `無法儲存為對話: ${message}`,
  notSaved: '未儲存: 沒有模型給出回答',
  transcriptTitle: (models: number, answered: number) =>
    answered === models ? `已比較 ${models} 個模型` : `已比較 ${models} 個模型 · ${answered} 個已回答`,
  branchWith: '用此模型繼續',
  branching: '正在分支…',
  branchNoSession: '請先開啟對話再分支',
  branchFailed: (message: string) => `無法分支: ${message}`
}

export const COMPARE_LOCALES: PluginLocaleBundles = { en, ja, zh, 'zh-hant': zhHant }

type Bound<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => string ? (...args: A) => string : string
}

function bind<T extends object>(t: PluginTranslate, template: T): Bound<T> {
  const out = {} as Record<string, unknown>

  for (const [key, value] of Object.entries(template)) {
    out[key] = typeof value === 'function' ? (...args: unknown[]) => t(key, ...args) : t(key)
  }

  return out as Bound<T>
}

export type CompareText = Bound<CompareMessages>

/** The compare strings for the active locale — one hook every component reads. */
export function useCompareText(): CompareText {
  const t = usePluginI18n('compare')

  return useMemo(() => bind(t, en), [t])
}
