import postgres from 'postgres'

export type Sql = postgres.Sql<Record<string, unknown>>

/**
 * Supabase-safe Postgres client.
 *
 * - Transaction pooler (port 6543) only: session mode (5432) exhausts the
 *   connection cap and hangs, measured in the Python app (database.py). A
 *   5432 pooler URL is rewritten to 6543, same rule as the Python side.
 * - `prepare: false`: the transaction pooler cannot keep prepared statements.
 * - Every datetime column in this schema is `timestamp without time zone`
 *   holding naive UTC. postgres.js would parse those as LOCAL time, so OID
 *   1114 gets its own parser that pins them to UTC. Writes pass JS Dates
 *   (sent as timestamptz) and the server session is UTC, so they land as UTC.
 */
export function normalizeDbUrl(raw: string): string {
  let url = raw.trim().replace(/^["']|["']$/g, '')
  url = url.replace(/^postgresql\+\w+:\/\//, 'postgresql://')
  try {
    const parsed = new URL(url)
    if (parsed.hostname.endsWith('pooler.supabase.com') && parsed.port === '5432') {
      parsed.port = '6543'
      url = parsed.toString()
    }
  } catch {
    // leave as-is; postgres() will report a readable error
  }
  return url
}

export function parseNaiveUtc(value: string): Date {
  return new Date(`${value.replace(' ', 'T')}Z`)
}

export function createSql(rawUrl: string, opts: { max?: number } = {}): Sql {
  return postgres(normalizeDbUrl(rawUrl), {
    prepare: false,
    max: opts.max ?? 4,
    idle_timeout: 30,
    connect_timeout: 12,
    onnotice: () => {},
    connection: { application_name: 'warmr-desktop' },
    types: {
      naiveUtc: {
        to: 1114,
        from: [1114],
        serialize: (x: Date | string) => (x instanceof Date ? x.toISOString() : String(x)),
        parse: (x: string) => parseNaiveUtc(x)
      }
    }
  }) as unknown as Sql
}

/** SQL fragment: current time as naive UTC, matching the column type. */
export const NOW_UTC = "(now() at time zone 'utc')"
