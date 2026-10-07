import { sql } from "@/lib/db";
import { assertFactSheet } from "@/lib/factsheet";
import type { FactSheet } from "@/lib/generated/factsheet.v1";

/**
 * Every query in the app lives here, and every one reads `published_ticker_page`.
 *
 * There is deliberately no query against a base table anywhere in `web/`. The role cannot
 * execute one, so attempting it fails loudly in development rather than shipping.
 */

export type PublishedRow = {
  symbol: string;
  exchange_code: string;
  ticker_status: string;
  short_name: string;
  legal_name_vi: string;
  sector_name_vi: string | null;
  as_of_date: string;
  version: number;
  reviewed_by: string | null;
  reviewed_at: string | null;
  published_at: string | null;
  position_disclosure: string | null;
  payload: FactSheet;
};

export async function listPublished(): Promise<PublishedRow[]> {
  const rows = await sql<PublishedRow[]>`
    SELECT symbol, exchange_code, ticker_status, short_name, legal_name_vi,
           sector_name_vi, as_of_date, version, reviewed_by, reviewed_at,
           published_at, position_disclosure, payload
    FROM published_ticker_page
    ORDER BY symbol
  `;
  // Validate here rather than at render time. `payload` is jsonb, so the declared type is
  // an assertion the compiler cannot check; a schema drift would otherwise surface as a
  // page missing a section, which a reader has no way to notice.
  return rows.map((row) => ({ ...row, payload: assertFactSheet(row.payload, row.symbol) }));
}

/**
 * Returns null for anything not published — an unknown symbol and an unreviewed draft are
 * indistinguishable from here, which is the intended behaviour. The caller turns null into
 * a 404 rather than revealing that a draft exists.
 */
export async function getPublished(symbol: string): Promise<PublishedRow | null> {
  const rows = await sql<PublishedRow[]>`
    SELECT symbol, exchange_code, ticker_status, short_name, legal_name_vi,
           sector_name_vi, as_of_date, version, reviewed_by, reviewed_at,
           published_at, position_disclosure, payload
    FROM published_ticker_page
    WHERE symbol = ${symbol.toUpperCase()}
    LIMIT 1
  `;
  const row = rows[0];
  if (!row) return null;
  return { ...row, payload: assertFactSheet(row.payload, row.symbol) };
}
