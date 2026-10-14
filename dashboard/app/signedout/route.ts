import { NextRequest, NextResponse } from "next/server";

export async function GET(req: NextRequest) {
  // … your existing cookie-clearing code …

  const host = req.headers.get("host") || "";
  const protocol = host.includes("localhost") ? "http" : "https";
  const res = NextResponse.redirect(`${protocol}://${host}/`, 302);

  res.headers.set("Cache-Control", "no-store");
  res.headers.set("Clear-Site-Data", "\"cookies\"");
  return res;
}