import type { Figure, FlagItem, DisclosureGap } from "@/lib/generated/factsheet.v1";

/**
 * These components render `display` strings and nothing else.
 *
 * No component here formats a number, derives a value, or decides what counts as missing.
 * All of that happened once in the pipeline, which is what makes it impossible for the page
 * to disagree with the data it was given.
 */

export function MetricTable({ title, figures }: { title: string; figures: Figure[] }) {
  if (figures.length === 0) return null;
  return (
    <section className="mb-8">
      <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-500 uppercase">
        {title}
      </h2>
      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full text-sm">
          <tbody>
            {figures.map((f) => (
              <tr key={f.code} className="border-b border-slate-100 last:border-0">
                <th scope="row" className="p-3 text-left align-top font-medium text-slate-700">
                  {f.label_vi}
                  <p className="mt-1 max-w-prose text-xs font-normal text-slate-500">
                    {f.definition_vi}
                  </p>
                </th>
                <td
                  className={`p-3 text-right align-top whitespace-nowrap tabular-nums ${
                    f.available ? "font-semibold text-slate-900" : "text-slate-400 italic"
                  }`}
                >
                  {f.display}
                  {!f.available && f.unavailable_reason ? (
                    <p className="mt-1 text-xs font-normal not-italic text-slate-400">
                      {f.unavailable_reason}
                    </p>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const STATE_STYLE: Record<string, string> = {
  active: "border-red-200 bg-red-50",
  unknown: "border-amber-200 bg-amber-50",
  inactive: "border-slate-200 bg-white",
};

const STATE_BADGE: Record<string, string> = {
  active: "bg-red-100 text-red-800",
  unknown: "bg-amber-100 text-amber-800",
  inactive: "bg-slate-100 text-slate-600",
};

export function FlagList({ flags }: { flags: FlagItem[] }) {
  // Active first, then unknown, then the tested negatives. The pipeline already ordered
  // them; this preserves that order rather than imposing its own.
  return (
    <section className="mb-8">
      <h2 className="mb-3 text-sm font-semibold tracking-wide text-slate-500 uppercase">
        Cờ rủi ro
      </h2>
      <ul className="space-y-3">
        {flags.map((flag) => (
          <li
            key={flag.code}
            className={`rounded-lg border p-4 ${STATE_STYLE[flag.state] ?? STATE_STYLE.inactive}`}
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="font-semibold text-slate-900">{flag.name_vi}</h3>
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${
                  STATE_BADGE[flag.state] ?? STATE_BADGE.inactive
                }`}
              >
                {flag.state_label_vi}
              </span>
            </div>
            <p className="mt-2 max-w-prose text-sm text-slate-700">{flag.explanation_vi}</p>

            {(flag.evidence ?? []).length > 0 ? (
              <div className="mt-3">
                <p className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
                  Căn cứ
                </p>
                <ul className="mt-1 space-y-1">
                  {(flag.evidence ?? []).map((e, i) => (
                    <li key={i} className="font-mono text-xs text-slate-600">
                      {e.source_url ? (
                        <a
                          href={e.source_url}
                          className="underline hover:text-slate-900"
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {e.label}
                        </a>
                      ) : (
                        e.label
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {/* A flag we cannot assess says why. It is never presented as a clean result. */}
            {flag.state === "unknown" && flag.reason ? (
              <p className="mt-2 font-mono text-xs text-amber-700">{flag.reason}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function GapList({ gaps }: { gaps: DisclosureGap[] }) {
  if (gaps.length === 0) return null;
  return (
    <section className="mb-8">
      <h2 className="mb-1 text-sm font-semibold tracking-wide text-slate-500 uppercase">
        Dữ liệu chưa có
      </h2>
      {/* Gaps are content, not an error state. Saying what is missing is part of the value. */}
      <p className="mb-3 max-w-prose text-xs text-slate-500">
        Những mục dưới đây chưa có dữ liệu. Chúng được nêu rõ thay vì để trống.
      </p>
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-slate-50">
        <table className="w-full text-sm">
          <tbody>
            {gaps.map((gap) => (
              <tr key={gap.field} className="border-b border-slate-100 last:border-0">
                <th scope="row" className="p-2.5 text-left font-normal text-slate-700">
                  {gap.label_vi}
                </th>
                <td className="p-2.5 text-right whitespace-nowrap text-slate-400 italic">
                  {gap.display}
                </td>
                <td className="p-2.5 font-mono text-xs text-slate-400">{gap.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
