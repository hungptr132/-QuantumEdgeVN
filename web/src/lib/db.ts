import postgres from "postgres";

/**
 * The single database handle for the web app.
 *
 * It connects as `qe_web`, which holds exactly one privilege in the entire database:
 * SELECT on the `published_ticker_page` view. It cannot read `fact_sheet`, `ticker`, or
 * any other base table. That is the publication gate, and it is enforced by Postgres
 * rather than by remembering to write a WHERE clause — a query against a draft raises a
 * permission error instead of quietly leaking one.
 *
 * Cached on globalThis because `next dev` re-evaluates modules on every hot reload, and
 * a fresh pool per reload exhausts connections within a few saves.
 */

declare global {
  var __qeSql: ReturnType<typeof postgres> | undefined;
}

function connect() {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error(
      "DATABASE_URL is not set. Copy web/.env.local.example to web/.env.local and fill in " +
        "the qe_web password from the repo-root .env.",
    );
  }
  return postgres(url, {
    max: 5,
    idle_timeout: 20,
    types: {
      // Dates and timestamps come back as strings, not JS Date objects.
      //
      // Two reasons. A Date cannot be rendered by React at all - it throws "Objects are
      // not valid as a React child", which is how this was found. And more importantly,
      // converting one back to text would put a formatting decision in the page, which is
      // precisely what this architecture keeps out of the web app: every displayed value
      // is formatted once, in the pipeline.
      //
      // OIDs: 1082 date, 1083 time, 1114 timestamp, 1184 timestamptz.
      dateAsText: {
        to: 1184,
        from: [1082, 1083, 1114, 1184],
        serialize: (value: string) => value,
        parse: (value: string) => value,
      },
    },
  });
}

export const sql = globalThis.__qeSql ?? connect();

if (process.env.NODE_ENV !== "production") {
  globalThis.__qeSql = sql;
}
