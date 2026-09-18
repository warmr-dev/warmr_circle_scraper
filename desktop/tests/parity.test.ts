import { describe, expect, it } from 'vitest'
import fixtures from './fixtures_python.json'
import { contentHash, simhash, stripHtml, redactPii, tiptapText } from '@engine/circle/text'
import { communitySlugForHost, uniqueSlugForHost } from '@engine/discovery/hosts'
import { classifyPayload, priceLabelFallback } from '@engine/circle/probe'
import { assessRules } from '@engine/icp/rules'
import { normalizeComment, normalizePost } from '@engine/stages/scrape'

// Every expected value here was produced by the Python app's own functions
// (tests/fixtures_python.json, generated from circle_leads). Both apps write
// the same rows, so these must match exactly.

describe('parity with the Python app', () => {
  it('content_hash and simhash are bit-identical', () => {
    for (const row of fixtures.hash) {
      expect(contentHash(row.text), row.text).toBe(row.content_hash)
      expect(simhash(row.text), row.text).toBe(row.simhash)
    }
  })

  it('community slugs match (the rule that prevents merging unrelated communities)', () => {
    for (const row of fixtures.slugs) {
      expect(communitySlugForHost(row.host), row.host).toBe(row.community)
      expect(uniqueSlugForHost(row.host), row.host).toBe(row.unique)
    }
  })

  it('strip_html matches', () => {
    for (const row of fixtures.strip_html) {
      expect(stripHtml(row.in as string | null)).toBe(row.out)
    }
  })

  it('PII redaction matches', () => {
    for (const row of fixtures.redact) expect(redactPii(row.in), row.in).toBe(row.out)
  })

  it('tiptap flattening matches', () => {
    expect(tiptapText(fixtures.tiptap.in)).toBe(fixtures.tiptap.out)
  })

  it('join-type classification of communities/current matches', () => {
    for (const row of fixtures.classify) {
      const got = classifyPayload(row.in as Record<string, unknown>)
      expect({ join_type: got.joinType, detail: got.detail }).toEqual(row.out)
    }
  })

  it('price-label fallback matches', () => {
    for (const row of fixtures.price) {
      const got = priceLabelFallback(row.in as string | null)
      expect(got ? { join_type: got.joinType, detail: got.detail } : null).toEqual(row.out)
    }
  })

  it('posts and comments get the same stored key and text as the old worker', () => {
    // scripts/gen_stored_key_fixture.py: Python's fetch_space_posts + triage_records.
    const { input, expected } = fixtures.stored_keys
    const space = { id: '1', name: 'Feed', slug: 'feed', type: 'basic', postsCount: null, isPrivate: null }
    const base = 'https://x.circle.so'
    const got: Array<{ circle_id: string; key: string; content: string; dedup_hash: string }> = []
    for (const record of input.posts) {
      const post = normalizePost(record, space, base, 'member_session')
      if (post && post.content.trim()) got.push({ circle_id: post.circleId, key: post.sourceContentId, content: post.content, dedup_hash: post.dedupHash })
      for (const comment of (input.comments as Record<string, Array<Record<string, unknown>>>)[String(record.id)] ?? []) {
        const c = normalizeComment(comment, String(record.id), base, 'member_session', null)
        if (c) got.push({ circle_id: c.circleId, key: c.sourceContentId, content: c.content, dedup_hash: c.dedupHash })
        for (const reply of (input.replies as Record<string, Array<Record<string, unknown>>>)[String(comment.id)] ?? []) {
          const r = normalizeComment(reply, String(record.id), base, 'member_session', null)
          if (r) got.push({ circle_id: r.circleId, key: r.sourceContentId, content: r.content, dedup_hash: r.dedupHash })
        }
      }
    }
    expect(got).toEqual(expected.map((e) => ({ circle_id: e.circle_id, key: e.key, content: e.content, dedup_hash: e.dedup_hash })))
  })

  it('ICP rule scores match for single-goal inputs', () => {
    for (const row of fixtures.icp) {
      const got = assessRules({
        name: row.name,
        description: row.description,
        spaceNames: [],
        goals: row.goal ? [row.goal] : []
      })
      expect(got.score, String(row.name)).toBe(row.score)
      expect(got.reasons).toEqual(row.reasons)
    }
  })
})
