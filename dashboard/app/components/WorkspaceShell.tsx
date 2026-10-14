"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";

import Sidebar from "./Sidebar";
import { COMPANY_NAME, LOGO_URL, PRODUCT_NAME } from "../lib/edition";

// The sidebar is a fixed column from md up and an off-canvas drawer below it,
// opened from a slim top bar. Attendees open the demo stand from their phones.
export default function WorkspaceShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="workspace-shell mx-auto flex max-w-screen-2xl gap-0 md:gap-6 px-0 md:px-6 lg:px-8">
      <div className="fixed inset-x-0 top-0 z-30 flex h-14 items-center gap-3 border-b bg-white px-4 md:hidden">
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open menu"
          aria-expanded={open}
          className="rounded-md border border-gray-200 bg-white p-2 text-gray-700 hover:bg-gray-50"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
        <Link href="/workspace" className="flex items-center gap-2 text-sm font-semibold">
          {LOGO_URL && <img src={LOGO_URL} alt={COMPANY_NAME} className="h-6 w-auto" />}
          {PRODUCT_NAME}
        </Link>
      </div>

      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/30 md:hidden"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] transform border-r bg-white transition-transform duration-200 md:static md:z-auto md:w-64 md:max-w-none md:flex-shrink-0 md:translate-x-0 ${
          open ? "translate-x-0 shadow-xl" : "-translate-x-full"
        }`}
        aria-label="Sidebar"
      >
        <div className="flex h-full flex-col overflow-y-auto md:sticky md:top-0 md:h-screen">
          <Suspense fallback={<div className="border-b p-6">Loading...</div>}>
            <Sidebar onNavigate={() => setOpen(false)} />
          </Suspense>
        </div>
      </aside>

      <div className="min-w-0 flex-1 pt-14 md:pt-0">{children}</div>
    </div>
  );
}
