import type { LiquidityBlock } from "@/lib/generated/factsheet.v1";
import { MetricTable } from "@/components/Figures";

/**
 * Agent 5's output, with its framing note rendered as part of the block rather than as
 * small print. The numbers describe how hard a position would be to exit; presenting them
 * without that sentence would let them read as a trading signal by omission.
 */
export function LiquidityPanel({ liquidity }: { liquidity: LiquidityBlock }) {
  // exit_estimates is absent when Agent 5 could not estimate an exit at all - which is
  // the honest result for a stock whose typical session has no turnover.
  const exits = liquidity.exit_estimates ?? [];
  return (
    <section className="mb-8">
      <div className="mb-3 rounded-lg border border-slate-300 bg-slate-100 p-3">
        <p className="max-w-prose text-sm text-slate-700">{liquidity.framing_note_vi}</p>
      </div>

      <MetricTable title="Thanh khoản" figures={liquidity.figures} />

      {exits.length > 0 ? (
        <div className="rounded-lg border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-700">
            Ước tính số phiên cần để thoát vị thế
          </h3>
          {liquidity.participation_rate ? (
            // The assumption is stated with the result, never buried: the estimate is
            // meaningless without knowing what share of a session it assumes you can take.
            <p className="mt-1 text-xs text-slate-500">
              Giả định: không chiếm quá{" "}
              <span className="font-mono">
                {(Number(liquidity.participation_rate) * 100).toFixed(0)}%
              </span>{" "}
              giá trị giao dịch của một phiên tiêu biểu.
            </p>
          ) : null}
          <ul className="mt-3 space-y-1 text-sm tabular-nums">
            {exits.map((e) => (
              <li key={e.position_label} className="flex justify-between gap-4">
                <span className="text-slate-600">{e.position_label.replace("_", " ")}</span>
                <span className="font-semibold text-slate-900">{e.sessions} phiên</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

/**
 * The stamp. Automating the drafting is fine; automating the verifying is what would
 * destroy the trust being sold, so who signed off and when is shown prominently.
 */
export function ReviewStamp({
  reviewedBy,
  reviewedAt,
  publishedAt,
  version,
  positionDisclosure,
}: {
  reviewedBy: string | null;
  reviewedAt: string | null;
  publishedAt: string | null;
  version: number;
  positionDisclosure: string | null;
}) {
  const date = (value: string | null) =>
    value ? new Date(value).toISOString().slice(0, 10) : null;

  return (
    <aside className="mt-10 rounded-lg border border-slate-300 bg-white p-4 text-sm">
      <p className="text-slate-800">
        {reviewedBy ? (
          <>
            Duyệt bởi <span className="font-semibold">{reviewedBy}</span>
            {date(reviewedAt) ? <> ngày {date(reviewedAt)}</> : null}
          </>
        ) : (
          <span className="text-slate-500">Chưa có người duyệt</span>
        )}
        <span className="ml-2 font-mono text-xs text-slate-500">v{version}</span>
      </p>
      {date(publishedAt) ? (
        <p className="mt-1 text-xs text-slate-500">Công bố ngày {date(publishedAt)}</p>
      ) : null}
      <p className="mt-2 text-xs text-slate-600">
        Công bố vị thế cá nhân:{" "}
        {positionDisclosure ? positionDisclosure : "không nắm giữ cổ phiếu này."}
      </p>
    </aside>
  );
}

/**
 * The regulatory boundary, on every page. This product describes; it does not prescribe.
 */
export function Disclaimer() {
  return (
    <footer className="mt-10 border-t border-slate-200 pt-6 text-xs leading-relaxed text-slate-500">
      <p className="max-w-prose">
        Trang này cung cấp thông tin và bối cảnh để tra cứu, kiểm chứng. Đây không phải
        khuyến nghị đầu tư, không phải tư vấn mua bán, và không đưa ra mục tiêu giá. Mọi
        quyết định đầu tư là của riêng bạn.
      </p>
      <p className="mt-2 max-w-prose">
        Số liệu được tính tự động từ dữ liệu công bố, mỗi con số đều dẫn nguồn. Nơi không có
        dữ liệu, trang ghi rõ <span className="italic">không công bố</span> thay vì để trống.
      </p>
    </footer>
  );
}
