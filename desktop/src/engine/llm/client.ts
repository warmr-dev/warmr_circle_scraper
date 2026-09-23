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

/**
 * The model could not judge anything right now (network, rate limit, outage,
 * bad key, no credits, unknown model): the item should wait for the next run
 * instead of being decided without the LLM.
 */
export class LlmUnavailableError extends Error {}

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
  // OpenRouter ids carry the vendor and use dots: anthropic/claude-haiku-4.5.
  const bare = model.includes('/') ? model.slice(model.indexOf('/') + 1).replace(/:.*$/, '') : model
  const candidates = [model, bare, bare.replace(/(\d)\.(\d)/g, '$1-$2')]
  for (const candidate of candidates) {
    const exact = PRICES[candidate]
    if (exact) return { rates: exact, known: true }
  }
  const keys = Object.keys(PRICES).sort((a, b) => b.length - a.length)
  for (const candidate of candidates) {
    const prefix = keys.find((key) => candidate.startsWith(key))
    if (prefix) return { rates: PRICES[prefix]!, known: true }
  }
  return { rates: UNKNOWN_PRICE, known: false }
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
  let response
  try {
    response = await client.beta.messages.parse(
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
  } catch (err) {
    if (signal?.aborted) throw err
    const status = err instanceof Anthropic.APIError ? err.status : undefined
    if (err instanceof Anthropic.APIConnectionError || (status != null && (UNAVAILABLE_STATUSES.has(status) || status >= 500))) {
      throw new LlmUnavailableError(`Anthropic: ${err instanceof Error ? err.message : String(err)}`)
    }
    throw err
  }
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

// Statuses that say nothing about the item being judged: bad key, no credits,
// no access, unknown model or no endpoint, timeout, rate limit.
const UNAVAILABLE_STATUSES = new Set([401, 402, 404, 408, 429])

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
    let res: Response
    try {
      res = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${cfg.apiKey}` },
        body: JSON.stringify(body),
        signal
      })
    } catch (err) {
      if (signal?.aborted) throw err
      lastError = err instanceof Error ? err.message : String(err)
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
      continue
    }
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
    if (!res.ok) {
      const message = `OpenAI HTTP ${res.status}: ${payload.error?.message ?? 'unknown error'}`
      if (UNAVAILABLE_STATUSES.has(res.status) || res.status === 403) throw new LlmUnavailableError(message)
      throw new Error(message)
    }
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
  throw new LlmUnavailableError(`OpenAI unavailable after retries (${lastError})`)
}

const OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'

/** Some endpoints wrap JSON in a markdown fence even under a schema. */
function unfence(text: string): string {
  const m = /^\s*```(?:json)?\s*([\s\S]*?)\s*```\s*$/.exec(text)
  return m ? m[1]! : text
}

/**
 * OpenRouter: one key, many vendors, the OpenAI request shape. A strict
 * json_schema plus provider.require_parameters sends the request only to an
 * endpoint that enforces the schema. The price comes back in usage.cost, so
 * the daily budget counts what was actually billed.
 */
async function openRouterJson<T>(
  cfg: LlmConfig,
  system: string,
  user: string,
  schema: z.ZodType<T>,
  jsonSchema: Record<string, unknown>,
  schemaName: string,
  maxTokens: number,
  signal?: AbortSignal
): Promise<LlmJsonResult<T>> {
  const body = {
    model: cfg.model,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user }
    ],
    max_tokens: maxTokens,
    response_format: { type: 'json_schema', json_schema: { name: schemaName, strict: true, schema: jsonSchema } },
    provider: { require_parameters: true }
  }
  let lastError = ''
  for (let attempt = 0; attempt < 3; attempt++) {
    let res: Response
    try {
      res = await fetch(OPENROUTER_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${cfg.apiKey}`,
          'X-OpenRouter-Title': 'Warmr Circle'
        },
        body: JSON.stringify(body),
        signal
      })
    } catch (err) {
      if (signal?.aborted) throw err
      lastError = err instanceof Error ? err.message : String(err)
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
      continue
    }
    const payload = (await res.json().catch(() => ({}))) as {
      error?: { message?: string; code?: number; metadata?: { reasons?: unknown } }
      model?: string
      choices?: Array<{ message?: { content?: string | null; refusal?: string | null }; finish_reason?: string }>
      usage?: { prompt_tokens?: number; completion_tokens?: number; cost?: number | null }
    }
    const message = `OpenRouter HTTP ${res.status}: ${payload.error?.message ?? 'unknown error'}`
    if (res.status === 408 || res.status === 429 || res.status >= 500) {
      lastError = message
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
      continue
    }
    if (!res.ok) {
      // 403 with moderation reasons is about this input; any other 4xx here is
      // the key, the credits or the model, and hits every item alike.
      if (res.status === 403 && payload.error?.metadata?.reasons) throw new Error(message)
      if (UNAVAILABLE_STATUSES.has(res.status) || res.status === 403) {
        const hint =
          res.status === 402
            ? ' (на счёте OpenRouter закончились деньги)'
            : res.status === 401
              ? ' (неверный ключ OpenRouter)'
              : res.status === 404
                ? ' (модель не найдена или не поддерживает строгий JSON)'
                : ''
        throw new LlmUnavailableError(message + hint)
      }
      throw new Error(message)
    }
    const choice = payload.choices?.[0]
    if (!choice && payload.error) {
      // An upstream vendor failed after OpenRouter accepted the request.
      lastError = message
      await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
      continue
    }
    if (choice?.message?.refusal) throw new LlmRefusal(choice.message.refusal)
    const content = choice?.message?.content
    if (!content) throw new Error(`OpenRouter returned no content (finish_reason=${choice?.finish_reason})`)
    const data = schema.parse(JSON.parse(unfence(content)))
    const inputTokens = payload.usage?.prompt_tokens ?? 0
    const outputTokens = payload.usage?.completion_tokens ?? 0
    const model = payload.model || cfg.model
    const billed = typeof payload.usage?.cost === 'number' ? payload.usage.cost : null
    const estimate = estimateUsd(model, inputTokens, outputTokens)
    return {
      data,
      usage: { inputTokens, outputTokens, usd: billed ?? estimate.usd, priced: billed != null || estimate.priced },
      model
    }
  }
  throw new LlmUnavailableError(`OpenRouter unavailable after retries (${lastError})`)
}

/**
 * One structured-JSON completion. `jsonSchema` is the strict JSON Schema for
 * OpenAI and OpenRouter (all properties required, additionalProperties
 * false); Anthropic derives its own from the zod schema through the SDK helper.
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
  if (cfg.provider === 'openrouter') {
    return openRouterJson(cfg, args.system, args.user, args.schema, args.jsonSchema, args.schemaName, args.maxTokens ?? 600, args.signal)
  }
  return openAiJson(cfg, args.system, args.user, args.schema, args.jsonSchema, args.schemaName, args.maxTokens ?? 400, args.signal)
}
