import type { LlmProvider } from './types'

// Model menu for the settings screen, prices per 1M tokens (input / output).
// OpenRouter: live list at openrouter.ai/api/v1/models on 2026-09-18, all with
// structured outputs. Anthropic: the claude-api skill table (2026-06-24).
// OpenAI: list prices when this was written. One community costs about
// 850 input + 130 output tokens.
export const MODEL_CHOICES: Record<LlmProvider, Array<{ id: string; label: string }>> = {
  openrouter: [
    { id: 'anthropic/claude-sonnet-5', label: 'Claude Sonnet 5 — $2 / $10 (≈ $3 за 1000 сообществ)' },
    { id: 'anthropic/claude-haiku-4.5', label: 'Claude Haiku 4.5 — $1 / $5 (≈ $1.5 за 1000)' },
    { id: 'anthropic/claude-opus-5', label: 'Claude Opus 5 — $5 / $25 (≈ $7.5 за 1000)' },
    { id: 'openai/gpt-4o-mini', label: 'gpt-4o-mini — $0.15 / $0.60 (≈ $0.2 за 1000, уже работал в проекте)' },
    { id: 'google/gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash-Lite — $0.10 / $0.40 (≈ $0.15 за 1000)' }
  ],
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
  openrouter: 'anthropic/claude-sonnet-5',
  anthropic: 'claude-opus-5',
  openai: 'gpt-4o-mini'
}

export const PROVIDER_LABEL: Record<LlmProvider, string> = {
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  anthropic: 'Anthropic'
}
