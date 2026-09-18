import Anthropic from '@anthropic-ai/sdk'
import { betaZodOutputFormat } from '@anthropic-ai/sdk/helpers/beta/zod'
import type { z } from 'zod'
import type { LlmProvider } from '../../shared/types'

export interface LlmConfig {
  provider: LlmProvider
  model: string
  apiKey: string
}

export interface LlmUsage {
  inputTokens: number
  outputTokens: number
  usd: number
  priced: boolean
}

export interface LlmJsonResult<T> {
  data: T
  usage: LlmUsage
  model: string
}

export class LlmRefusal extends Error {}

/** USD per 1M tokens [input, output]. Anthropic rates from the claude-api
 * skill table (2026-06-24); OpenAI rates are list prices as known when this was
 * written. Unknown models are priced at a deliberately high placeholder so the
 * daily budget errs on the safe side. */
const PRICES: Record<string, [number, number]> = {
  'claude-fable-5-1': [10, 50],
  'claude-opus-5': [5, 25],
  'claude-sonnet-5': [2, 10],
  'claude-haiku-4-5': [1, 5],
  'gpt-4o-mini': [0.15, 0.6],
  'gpt-4.1-nano': [0.1, 0.4],
  'gpt-4.1-mini': [0.4, 1.6],
  'gpt-4.1': [2, 8],
  'gpt-5-nano': [0.05, 0.4],
  'gpt-5-mini': [0.25, 2],
  'gpt-5': [1.25, 10]
}
const UNKNOWN_PRICE: [number, number] = [5, 25]

export function priceFor(model: string): { rates: [number, number]; known: boolean } {
  const exact = PRICES[model]
  if (exact) return { rates: exact, known: true }
  const prefix = Object.keys(PRICES)
    .sort((a, b) => b.length - a.length)
    .find((key) => model.startsWith(key))
  return prefix ? { rates: PRICES[prefix]!, known: true } : { rates: UNKNOWN_PRICE, known: false }
}

export function estimateUsd(model: string, inputTokens: number, outputTokens: number): { usd: number; priced: boolean } {
  const { rates, known } = priceFor(model)
  return { usd: (inputTokens * rates[0] + outputTokens * rates[1]) / 1_000_000, priced: known }
}

export { MODEL_CHOICES, DEFAULT_MODEL } from '../../shared/models'

// Models that accept output_config.effort; Haiku 4.5 rejects it.
function anthropicSupportsEffort(model: string): boolean {
  return /^claude-(opus-5|opus-4-[678]|sonnet-5|sonnet-4-6|fable-5|mythos-5)/.test(model)
}

// Server-side refusal fallbacks: the skill asks for them by default on these.
function anthropicWantsFallbacks(model: string): boolean {
  return /^claude-(opus-5|fable-5-1)/.test(model)
}

async function anthropicJson<T>(
  cfg: LlmConfig,
  system: string,
  user: string,
  schema: z.ZodType<T>,
  maxTokens: number,
  signal?: AbortSignal
): Promise<LlmJsonResult<T>> {
  const client = new Anthropic({ apiKey: cfg.apiKey, maxRetries: 2, timeout: 120_000 })
  const fallbacks = anthropicWantsFallbacks(cfg.model)
  const response = await client.beta.messages.parse(
    {
      model: cfg.model,
      max_tokens: maxTokens,
      ...(fallbacks ? { betas: ['server-side-fallback-2026-07-01'], fallbacks: 'default' as const } : {}),
      system,
      messages: [{ role: 'user', content: user }],
      output_config: {
        ...(anthropicSupportsEffort(cfg.model) ? { effort: 'low' as const } : {}),
        format: betaZodOutputFormat(schema)
      }
    },
    { signal }
  )
  const inputTokens = response.usage.input_tokens ?? 0
  const outputTokens = response.usage.output_tokens ?? 0
  const cost = estimateUsd(response.model || cfg.model, inputTokens, outputTokens)
  if (response.stop_reason === 'refusal') {
    throw new LlmRefusal(`model declined (${response.stop_details?.category ?? 'no category'})`)
  }
  if (response.parsed_output == null) {
    throw new Error(`no parsable JSON (stop_reason=${response.stop_reason})`)
  }
  return {
    data: response.parsed_output as T,
    usage: { inputTokens, outputTokens, usd: cost.usd, priced: cost.priced },
    model: response.model || cfg.model
  }
}

function openAiIsReasoningModel(model: string): boolean {
  return /^(gpt-5|o\d)/.test(model)
}

async function openAiJson<T>(
  cfg: LlmConfig,
  system: string,
  user: string,
  schema: z.ZodType<T>,
  jsonSchema: Record<string, unknown>,
  schemaName: string,
  maxTokens: number,
  signal?: AbortSignal
): Promise<LlmJsonResult<T>> {
  const reasoning = openAiIsReasoningModel(cfg.model)
  const body: Record<string, unknown> = {
    model: cfg.model,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user }
    ],
    max_completion_tokens: reasoning ? Math.max(maxTokens, 2000) : maxTokens,
    response_format: { type: 'json_schema', json_schema: { name: schemaName, strict: true, schema: jsonSchema } }
  }
  if (reasoning) body.reasoning_effort = 'low'
  else body.temperature = 0

  let lastError = ''
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${cfg.apiKey}` },
      body: JSON.stringify(body),
      signal
    })
    const payload = (await res.json().catch(() => ({}))) as {
      error?: { message?: string }
      model?: string
      choices?: Array<{ message?: { content?: string | null; refusal?: string | null }; finish_reason?: string }>
      usage?: { prompt_tokens?: number; completion_tokens?: number }
    }
    if (res.status === 429 || res.status >= 500) {
      lastError = `HTTP ${res.status}: ${payload.error?.message ?? ''}`
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
      continue
    }
    if (!res.ok) throw new Error(`OpenAI HTTP ${res.status}: ${payload.error?.message ?? 'unknown error'}`)
    const choice = payload.choices?.[0]
    if (choice?.message?.refusal) throw new LlmRefusal(choice.message.refusal)
    const content = choice?.message?.content
    if (!content) throw new Error(`OpenAI returned no content (finish_reason=${choice?.finish_reason})`)
    const data = schema.parse(JSON.parse(content))
    const inputTokens = payload.usage?.prompt_tokens ?? 0
    const outputTokens = payload.usage?.completion_tokens ?? 0
    const cost = estimateUsd(cfg.model, inputTokens, outputTokens)
    return {
      data,
      usage: { inputTokens, outputTokens, usd: cost.usd, priced: cost.priced },
      model: payload.model || cfg.model
    }
  }
  throw new Error(`OpenAI unavailable after retries (${lastError})`)
}

/**
 * One structured-JSON completion. `jsonSchema` is the strict JSON Schema for
 * OpenAI (all properties required, additionalProperties false); Anthropic
 * derives its own from the zod schema through the SDK helper.
 */
export async function completeJson<T>(
  cfg: LlmConfig,
  args: {
    system: string
    user: string
    schema: z.ZodType<T>
    jsonSchema: Record<string, unknown>
    schemaName: string
    maxTokens?: number
    signal?: AbortSignal
  }
): Promise<LlmJsonResult<T>> {
  if (!cfg.apiKey) throw new Error(`no API key for ${cfg.provider}`)
  if (cfg.provider === 'anthropic') {
    return anthropicJson(cfg, args.system, args.user, args.schema, args.maxTokens ?? 4096, args.signal)
  }
  return openAiJson(cfg, args.system, args.user, args.schema, args.jsonSchema, args.schemaName, args.maxTokens ?? 400, args.signal)
}
