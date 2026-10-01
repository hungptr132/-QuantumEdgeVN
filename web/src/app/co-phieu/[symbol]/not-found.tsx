import Link from "next/link";
/**
 * Shown for any symbol that is not published — which deliberately includes symbols that
 * exist as drafts. Revealing that an unreviewed page exists would partly defeat the review
 * gate, so an unknown ticker and an unpublished one are indistinguishable from here.
 */
export default function NotFound() {
  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold text-slate-900">Chưa có trang cho mã này</h1>
      <p className="mt-3 max-w-prose text-slate-700">
        Mã này chưa được công bố. Một trang chỉ xuất hiện sau khi dữ liệu đã qua kiểm định
        tự động và được một người duyệt.
      </p>
      <p className="mt-6 text-sm">
        <Link href="/" className="text-slate-500 underline hover:text-slate-900">
          ← Xem các mã đã công bố
        </Link>
      </p>
    </main>
  );
}
