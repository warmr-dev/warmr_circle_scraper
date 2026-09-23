import { z } from 'zod'
import { completeJson, LlmUnavailableError, type LlmConfig, type LlmUsage } from '../llm/client'
import { assessRules, hasText, ICP_CONFIDENT_NO, ICP_CONFIDENT_YES, NO_METADATA_REASON, type IcpInput } from './rules'

export const ICP_VERSION = 'desktop-icp-v1'

export const DEFAULT_ICP_PROFILE = `We are a software development company (web and mobile apps, SaaS, AI and automation, MVPs for startups).
A community FITS when its members are likely to need or commission software work, so its posts may contain someone looking to hire developers, a technical co-founder, a CTO, or a development agency:
- founders, startups, SaaS and indie builders, no-code/product people, agency owners, consultants, e-commerce and online business owners;
- technical communities (coding, engineering, AI/ML) where members also hand off or take client projects.
A community does NOT fit when it is about hobbies, fitness, health, spirituality, relationships, parenting, fandom, or a specific non-tech trade or craft, with no plausible link to commissioning software.`

export const IcpVerdictSchema = z.object({
  fit: z.boolean(),
  score: z.number(),
  confidence: z.number(),
  reason: z.string(),
  signals: z.array(z.string())
})
export type IcpVerdict = z.infer<typeof IcpVerdictSchema>

const ICP_JSON_SCHEMA = {
  type: 'object',
  properties: {
    fit: { type: 'boolean' },
    score: { type: 'number' },
    confidence: { type: 'number' },
    reason: { type: 'string' },
    signals: { type: 'array', items: { type: 'string' } }
  },
  required: ['fit', 'score', 'confidence', 'reason', 'signals'],
  additionalProperties: false
}

export function icpSystemPrompt(profile: string): string {
  return `You rate online communities (mostly on the Circle.so platform) for a company's lead generation.

Who we are and what fits:
${profile.trim() || DEFAULT_ICP_PROFILE}

You get what is known about one community: its name, sometimes a description, the names of its public sections ("spaces"), directory categories, price and how one joins. Judge only from that; section names are strong evidence of what members discuss. If the information is thin, lower your confidence instead of guessing.

Answer with JSON:
- fit: true if monitoring this community's posts is likely to surface people who want to hire software developers or an agency;
- score: 0-100, how promising the community is for that (100 = members constantly commission software);
- confidence: 0.0-1.0, how sure you are given the available information;
- reason: one short sentence in Russian explaining the verdict;
- signals: up to 5 short phrases from the input that drove the decision.`
}

export interface IcpCommunityInput extends IcpInput {
  host: string | null
  priceLabel: string | null
  joinType: string | null
  membersTotal: number | null
}

export function icpUserMessage(input: IcpCommunityInput): string {
  const spaces = input.spaceNames.slice(0, 40).join(' · ')
  return [
    `Name: ${input.name || '(unknown)'}`,
    `Host: ${input.host || '(unknown)'}`,
    `Description: ${input.description ? input.description.slice(0, 1500) : '(none)'}`,
    `Sections: ${spaces || '(unknown)'}`,
    `Directory categories: ${input.goals.length ? input.goals.join(', ') : '(none)'}`,
    `Price: ${input.priceLabel || '(unknown)'}`,
    `Join type: ${input.joinType || '(unknown)'}`,
    `Members (largest public section): ${input.membersTotal ?? "(unknown)"}`
  ].join('\n')
}

export interface IcpSettings {
  useLlm: boolean
  mode: 'all' | 'ambiguous'
  minConfidence: number
  autoApproveLlm: boolean
  profile: string
}

export interface IcpDecision {
  score: number
  flag: boolean
  reasons: string[]
  decidedBy: 'rules' | 'llm'
  ruleScore: number
  llm: (IcpVerdict & { model: string; usage: LlmUsage }) | null
  llmError: string | null
  /** The LLM was out of reach (not this community's fault): judge it again later. */
  llmRetryable: boolean
  skippedLlm: 'no_text' | 'rules_decisive' | 'disabled' | 'budget' | null
}

export const LLM_FIT_NEEDS_REVIEW = 'llm_fit_needs_review'

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

/**
 * Rules first; the LLM judges everything with text (mode "all") or only the
 * ambiguous band (mode "ambiguous"). Rows with no text never reach the LLM:
 * the Python app measured a model answering "fit, 0.9" on literally empty
 * input (WORKLOG 2026-09-17), so they are marked no_metadata instead.
 */
export async function decideIcp(
  input: IcpCommunityInput,
  settings: IcpSettings,
  llm: LlmConfig | null,
  opts: { budgetOk: boolean; signal?: AbortSignal }
): Promise<IcpDecision> {
  if (!hasText(input)) {
    return {
      score: 0,
      flag: false,
      reasons: [NO_METADATA_REASON],
      decidedBy: 'rules',
      ruleScore: 0,
      llm: null,
      llmError: null,
      llmRetryable: false,
      skippedLlm: 'no_text'
    }
  }
  const rules = assessRules(input)
  const rulesDecision = (
    skip: IcpDecision['skippedLlm'],
    extraReason?: string,
    llmError: string | null = null,
    llmRetryable = false
  ): IcpDecision => ({
    score: clamp(rules.score, 0, 100),
    flag: rules.score >= ICP_CONFIDENT_YES,
    reasons: extraReason ? [...rules.reasons, extraReason] : rules.reasons,
    decidedBy: 'rules',
    ruleScore: rules.score,
    llm: null,
    llmError,
    llmRetryable,
    skippedLlm: skip
  })

  const decisive = rules.score >= ICP_CONFIDENT_YES || rules.score <= ICP_CONFIDENT_NO
  if (!settings.useLlm || !llm) return rulesDecision('disabled')
  if (settings.mode === 'ambiguous' && decisive) return rulesDecision('rules_decisive')
  if (!opts.budgetOk) return rulesDecision('budget', 'llm_budget_reached')

  try {
    const result = await completeJson(llm, {
      system: icpSystemPrompt(settings.profile),
      user: icpUserMessage(input),
      schema: IcpVerdictSchema,
      jsonSchema: ICP_JSON_SCHEMA,
      schemaName: 'community_fit',
      signal: opts.signal
    })
    const verdict: IcpVerdict = {
      fit: Boolean(result.data.fit),
      score: Math.round(clamp(Number(result.data.score) || 0, 0, 100)),
      confidence: clamp(Number(result.data.confidence) || 0, 0, 1),
      reason: String(result.data.reason || '').slice(0, 500),
      signals: (result.data.signals || []).map(String).slice(0, 8)
    }
    const confident = verdict.confidence >= settings.minConfidence
    const reasons = [...rules.reasons, ...verdict.signals.map((s) => `llm:${s}`)]
    if (verdict.fit && !confident) reasons.push('low_llm_confidence')
    if (verdict.fit && confident && !settings.autoApproveLlm) reasons.push(LLM_FIT_NEEDS_REVIEW)
    return {
      score: verdict.score,
      flag: verdict.fit && confident && settings.autoApproveLlm,
      reasons,
      decidedBy: 'llm',
      ruleScore: rules.score,
      llm: { ...verdict, model: result.model, usage: result.usage },
      llmError: null,
      llmRetryable: false,
      skippedLlm: null
    }
  } catch (err) {
    if (opts.signal?.aborted) throw err
    const message = err instanceof Error ? err.message : String(err)
    return rulesDecision(null, 'llm_error', message.slice(0, 300), err instanceof LlmUnavailableError)
  }
}
