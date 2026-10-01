import Link from "next/link";
import { listPublished } from "@/lib/queries/published";
import { Disclaimer } from "@/components/Blocks";

/**
 * The index. Lists only published tickers, because the view it reads contains only those —
 * a draft cannot appear here even by accident.
 */
export default async function HomePage() {
  const rows = await listPublished();

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-8">
        <h1 className="text-3xl font-bold text-slate-900">QuantumEdge</h1>
        <p className="mt-2 max-w-prose text-slate-700">
          Tra cứu bất kỳ cổ phiếu nào — kể cả những mã không ai phân tích.
        </p>
      </header>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-slate-300 bg-slate-50 p-6">
          <p className="text-slate-700">Chưa có mã nào được công bố.</p>
          <p className="mt-2 font-mono text-xs text-slate-500">
            qe factsheet build → qe review approve &lt;MÃ&gt; --as &lt;người duyệt&gt; → qe
            review publish &lt;MÃ&gt; --as &lt;người duyệt&gt;
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200">
          {rows.map((row) => {
            const flags = row.payload.flags ?? [];
            const active = flags.filter((f) => f.state === "active").length;
            const unknown = flags.filter((f) => f.state === "unknown").length;
            return (
              <li key={row.symbol}>
                <Link
                  href={`/co-phieu/${row.symbol.toLowerCase()}`}
                  className="flex flex-wrap items-baseline justify-between gap-3 p-4 hover:bg-slate-50"
                >
                  <span>
                    <span className="text-lg font-semibold text-slate-900">{row.symbol}</span>
                    <span className="ml-2 text-sm text-slate-600">{row.short_name}</span>
                    <span className="ml-2 text-xs text-slate-400">
                      {row.sector_name_vi ?? "—"}
                    </span>
                  </span>
                  <span className="flex items-center gap-2 text-xs">
                    {active > 0 ? (
                      <span className="rounded bg-red-100 px-2 py-0.5 text-red-800">
                        {active} cờ đang có
                      </span>
                    ) : null}
                    {unknown > 0 ? (
                      <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-800">
                        {unknown} chưa đánh giá được
                      </span>
                    ) : null}
                    <span className="font-mono text-slate-400">{row.as_of_date}</span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      <Disclaimer />
    </main>
  );
}
