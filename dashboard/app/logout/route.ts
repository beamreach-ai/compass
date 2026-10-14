// dashboard/app/logout/route.ts
import { NextRequest, NextResponse } from "next/server";

export async function GET(req: NextRequest) {
  const host = req.headers.get("host") || ""; 
  const protocol = host.includes("localhost") ? "http" : "https";

  // Redirect target after clearing cookies (ALB will bounce this to Cognito)
  const res = NextResponse.redirect(`${protocol}://${host}/cognito-logout`, 302);

    const names = [
      "AWSELBAuthSessionCookie",
      "AWSELBAuthSessionCookie-0",
      "AWSELBAuthSessionCookie-1",
      "AWSELBAuthSessionCookie-2",
      "AWSELBAuthSessionCookie-3",
      "AWSELBAuthNonce",   // canonical
      "AWSALBAuthNonce",   // belt & suspenders in case your ALB uses this variant
    ];

  const base = {
    httpOnly: true,
    secure: true,
    sameSite: "lax" as const,
    maxAge: 0,
    expires: new Date(0),
    path: "/" as const,
  };

  console.log("Logout route: clearing cookies for host", host);

  // Clear cookies with explicit domain for Path=/
  for (const name of names) {
    if (host) {
      res.cookies.set(name, "", { ...base, domain: host });
      console.log(`Cleared cookie ${name} (Domain=${host}, Path=/)`);
    }
  }

  // Prevent any caching of this response
  res.headers.set("Cache-Control", "no-store");
  // Nuclear fail-safe: clear all cookies for this origin
  res.headers.set("Clear-Site-Data", "\"cookies\"");
  return res;
}