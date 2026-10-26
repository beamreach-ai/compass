"use client";
// The count chip beside a tab or a view: red when something in it is urgent, amber
// otherwise, absent at zero. Rendered outside the tab's link, so the link's text stays
// its label.
import React from "react";

export default function InboxCount({ count, urgent = false, title }: { count: number; urgent?: boolean; title?: string }) {
  if (!count) return null;
  return (
    <span
      data-inbox-count={count}
      title={title || `${count} need${count === 1 ? "s" : ""} you`}
      className={`inline-flex min-w-[1.25rem] items-center justify-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none ${
        urgent ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-800"
      }`}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}
