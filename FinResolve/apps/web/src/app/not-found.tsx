import Link from "next/link";

export default function NotFound() {
  return (
    <div className="space-y-3">
      <h1 className="text-2xl font-semibold">Không tìm thấy trang</h1>
      <Link href="/demo" className="text-indigo-700 underline">
        Quay lại trang Demo
      </Link>
    </div>
  );
}
