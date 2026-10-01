import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";

import { getPublished, listPublished } from "@/lib/queries/published";
import { MetricTable, FlagList, GapList } from "@/components/Figures";
import { LiquidityPanel, ReviewStamp, Disclaimer } from "@/components/Blocks";

/**
 * The ticker page.
 *
 * Paths come from `generateStaticParams`, which reads the published view — so only
 * published tickers are pre-rendered. Anything else calls `notFound()`, which is the
 * documented Next.js 16 approach (the older `dynamicParams = false` export is rejected when
 * Cache Components is enabled, so it is not used here).
 *
 * The publication gate is enforced three times over: the view filters to published rows,
 * the `qe_web` role holds no privilege on `fact_sheet` at all, and this page 404s on a null
 * result. The first two make a leak impossible; this one makes it tidy.
 */

export async function generateStaticParams() {
  const rows = await listPublished();
  return rows.map((row) => ({ symbol: row.symbol.toLowerCase() }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ symbol: string }>;
}): Promise<Metadata> {
  const { symbol } = await params;
  const row = await getPublished(symbol);
  if (!row) return { title: "Không tìm thấy mã" };
  return {
    title: `${row.symbol} — ${row.short_name}`,
    description: `Tra cứu ${row.symbol}: thanh khoản, cờ rủi ro, và dữ liệu chưa công bố.`,
  };
}

export default async function TickerPage({
  params,
}: {
  // params is a Promise in Next.js 16 and must be awaited.
  params: Promise<{ symbol: string }>;
}) {
  const { symbol } = await params;
  const row = await getPublished(symbol);
  if (!row) notFound();

  const sheet = row.payload;

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <nav className="mb-6 text-sm">
        <Link href="/" className="text-slate-500 underline hover:text-slate-900">
          ← Tất cả mã đã công bố
        </Link>
      </nav>

      <header className="mb-8">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-3xl font-bold text-slate-900">{row.symbol}</h1>
          <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
            {row.exchange_code}
          </span>
          <span className="text-sm text-slate-500">{row.sector_name_vi ?? "—"}</span>
        </div>
        <p className="mt-1 text-lg text-slate-700">{row.legal_name_vi}</p>

        {sheet.company.description_vi ? (
          <p className="mt-3 max-w-prose text-slate-700">{sheet.company.description_vi}</p>
        ) : (
          // The description is NULL by decision: that prose is reserved for a person. It
          // appears in the gap list below rather than being quietly omitted.
          <p className="mt-3 max-w-prose text-sm text-slate-400 italic">
            Mô tả hoạt động kinh doanh chưa được biên soạn.
          </p>
        )}

        <p className="mt-3 font-mono text-xs text-slate-500">
          Dữ liệu tính đến {sheet.as_of_date}
        </p>
      </header>

      <MetricTable title="Chỉ số chính" figures={sheet.headline_metrics} />
      <LiquidityPanel liquidity={sheet.liquidity} />
      <FlagList flags={sheet.flags} />
      <MetricTable title="Chỉ số khác" figures={sheet.other_metrics ?? []} />
      <GapList gaps={sheet.disclosure_gaps} />

      <ReviewStamp
        reviewedBy={row.reviewed_by}
        reviewedAt={row.reviewed_at}
        publishedAt={row.published_at}
        version={row.version}
        positionDisclosure={row.position_disclosure}
      />
      <Disclaimer />
    </main>
  );
}
