import type { FactSheet } from "@/lib/generated/factsheet.v1";

/**
 * Runtime validation of a fact sheet payload, at the only place it enters the app.
 *
 * Until this existed, `published.ts` declared `payload: FactSheet` and that was a *type
 * assertion*: TypeScript believed it, nothing checked it. The payload is jsonb, so the
 * compiler has no visibility into what the database actually returned.
 *
 * That is a real exposure rather than a theoretical one. `fact_sheet` keeps every version
 * ever built — AGM is on v19 — and the schema has gained fields since the early ones
 * (`comparison`, `history`, `active_since`). Any of those older rows can be published, and
 * a schema change that the generated types do not yet reflect would produce a page that
 * silently omits a section or renders `undefined`.
 *
 * For a product whose whole claim is that a page says exactly what the data supports, a
 * page missing its flags section is worse than no page. So this throws, and a build fails,
 * rather than degrading quietly. A failed deploy is a problem someone notices; a page with
 * a missing section is one a reader never knows to distrust.
 *
 * This is deliberately a structural check and not a field-by-field schema. The payload is
 * written by a pydantic contract with `extra="forbid"` and its JSON Schema generates the
 * types here, so the failure worth catching is **drift between those two**, which shows up
 * in `schema_version` and in whole missing sections. Re-validating every nested figure
 * would mean maintaining a second copy of the contract in a second language, and the copy
 * would be the thing that drifted.
 */

/**
 * The only schema version this build knows how to render.
 *
 * Hardcoded rather than read from the payload: that is the entire point. If a payload ever
 * declares a different version, these components were generated against a different
 * contract and must not pretend otherwise.
 */
export const SUPPORTED_SCHEMA_VERSION = "factsheet/v1";

/** Sections the ticker page renders unconditionally. A missing one is a broken page. */
const REQUIRED_ARRAYS = [
  "headline_metrics",
  "flags",
  "disclosure_gaps",
  "allowed_numbers",
  "news",
] as const;

/** Sections the page renders as objects. */
const REQUIRED_OBJECTS = ["ticker", "company", "liquidity", "lineage"] as const;

export class FactSheetContractError extends Error {
  constructor(symbol: string, detail: string) {
    super(
      `Fact sheet for ${symbol} does not match the contract this build renders: ${detail}. ` +
        `Expected schema_version "${SUPPORTED_SCHEMA_VERSION}". ` +
        `Regenerate the types (npm run gen:types) after a schema change, or rebuild and ` +
        `republish the sheet. Refusing to render a partial page.`,
    );
    this.name = "FactSheetContractError";
  }
}

/**
 * Narrow an unknown payload to a FactSheet, or throw.
 *
 * `symbol` is passed only so the error names the page that failed — with nine tickers in a
 * build, "a sheet is malformed" is not an actionable message.
 */
/**
 * What the guard has actually established, expressed in the type.
 *
 * The generated types mark a section optional whenever the pydantic contract gave it a
 * default, so `news` and `other_metrics` arrive as `T | undefined` even though every
 * payload carries them. Once `REQUIRED_ARRAYS` has been checked at runtime they are not
 * optional any more, and saying so here is what lets a page render them directly instead
 * of writing `?? []` - a fallback that would silently produce an empty section if the
 * guard were ever weakened.
 */
export type CheckedFactSheet = FactSheet &
  Required<Pick<FactSheet, "news" | "other_metrics">>;

export function assertFactSheet(
  payload: unknown,
  symbol: string,
): CheckedFactSheet {
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload)
  ) {
    throw new FactSheetContractError(
      symbol,
      `payload is ${describe(payload)}, not an object`,
    );
  }

  const sheet = payload as Record<string, unknown>;

  const version = sheet.schema_version;
  if (version !== SUPPORTED_SCHEMA_VERSION) {
    throw new FactSheetContractError(
      symbol,
      `schema_version is ${describe(version)}`,
    );
  }

  for (const key of REQUIRED_OBJECTS) {
    const value = sheet[key];
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new FactSheetContractError(
        symbol,
        `"${key}" is ${describe(value)}, not an object`,
      );
    }
  }

  for (const key of REQUIRED_ARRAYS) {
    if (!Array.isArray(sheet[key])) {
      throw new FactSheetContractError(
        symbol,
        `"${key}" is ${describe(sheet[key])}, not an array`,
      );
    }
  }

  // The spec commits the page to leading with five metrics, and the pipeline's QA enforces
  // it. Checking it again here is not redundant: QA runs when a sheet is built, and a sheet
  // published before that check existed would still be servable.
  const headline = sheet.headline_metrics as unknown[];
  if (headline.length < 5) {
    throw new FactSheetContractError(
      symbol,
      `headline_metrics has ${headline.length} entries, fewer than the five the page leads with`,
    );
  }

  // The liquidity framing note travels with Agent 5's numbers so they cannot read as a
  // trading signal by omission. A block without it must not render.
  const liquidity = sheet.liquidity as Record<string, unknown>;
  if (
    typeof liquidity.framing_note_vi !== "string" ||
    liquidity.framing_note_vi.length === 0
  ) {
    throw new FactSheetContractError(
      symbol,
      "the liquidity block has no framing note, which must travel with its figures",
    );
  }
  if (!Array.isArray(liquidity.figures)) {
    throw new FactSheetContractError(
      symbol,
      "liquidity.figures is not an array",
    );
  }

  return sheet as unknown as CheckedFactSheet;
}

function describe(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "absent";
  if (Array.isArray(value)) return "an array";
  if (typeof value === "string") return JSON.stringify(value);
  return typeof value;
}
