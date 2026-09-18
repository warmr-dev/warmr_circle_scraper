import type { LlmProvider } from './types'

// Model menu for the settings screen. Anthropic prices are from the claude-api
// skill table (2026-06-24); OpenAI prices are list prices when this was written.
export const MODEL_CHOICES: Record<LlmProvider, Array<{ id: string; label: string }>> = {
  anthropic: [
    { id: 'claude-opus-5', label: 'Claude Opus 5 — $5 / $25 за 1M токенов (вход / выход)' },
    { id: 'claude-sonnet-5', label: 'Claude Sonnet 5 — $2 / $10' },
    { id: 'claude-haiku-4-5', label: 'Claude Haiku 4.5 — $1 / $5' }
  ],
  openai: [
    { id: 'gpt-4o-mini', label: 'gpt-4o-mini — $0.15 / $0.60 (уже работал в проекте)' },
    { id: 'gpt-4.1-mini', label: 'gpt-4.1-mini — $0.40 / $1.60' },
    { id: 'gpt-5-mini', label: 'gpt-5-mini — $0.25 / $2' }
  ]
}

export const DEFAULT_MODEL: Record<LlmProvider, string> = {
  anthropic: 'claude-opus-5',
  openai: 'gpt-4o-mini'
}
