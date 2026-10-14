import { NextRequest, NextResponse } from "next/server";

export async function GET(req: NextRequest) {
  const hostHeader = req.headers.get("host");
  const url = new URL(req.url);
  const host = hostHeader || url.host;
  const protocol =
    host?.includes("localhost") || url.protocol === "http:" ? "http" : "https";

  const startPath = "/google-login/start";
  const startUrl = host ? `${protocol}://${host}${startPath}` : startPath;
  const escapedStartUrl = startUrl
    .replaceAll("&", "&amp;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");

  const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Continue with Google</title>
  <style>
    :root { color-scheme: light; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f4f5;
      color: #18181b;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 460px;
      background: #ffffff;
      border: 1px solid #e4e4e7;
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
    }
    h1 { margin: 0 0 8px; font-size: 28px; line-height: 1.2; }
    p { margin: 0; color: #52525b; line-height: 1.55; }
    .stack { margin-top: 18px; display: grid; gap: 10px; }
    .btn {
      display: block;
      width: 100%;
      box-sizing: border-box;
      text-decoration: none;
      border-radius: 10px;
      padding: 12px 14px;
      text-align: center;
      font-weight: 600;
      border: 1px solid #d4d4d8;
      color: #18181b;
      background: #ffffff;
    }
    .btn.primary {
      border-color: #18181b;
      color: #ffffff;
      background: #18181b;
    }
    #embedded { display: none; }
  </style>
</head>
<body>
  <main class="card">
    <h1>Continue with Google</h1>
    <p id="status">Redirecting to secure Google sign-in...</p>

    <div id="embedded" class="stack">
      <a class="btn primary" href="${escapedStartUrl}" target="_blank" rel="noopener noreferrer">Open secure browser and continue</a>
      <a class="btn" href="${escapedStartUrl}">Try this browser anyway</a>
      <a class="btn" href="/">Back to sign-in options</a>
    </div>
  </main>
  <script>
    (function () {
      var startUrl = ${JSON.stringify(startUrl)};
      var ua = (navigator.userAgent || "").toLowerCase();
      var inAppMarkers = [
        "fban", "fbav", "instagram", "line/", "micromessenger", "snapchat",
        "tiktok", "linkedinapp", "twitter", "x-app", "threads", "pinterest",
        "gsa/", "slack", "; wv", " wv)", "fb_iab"
      ];

      var embedded = inAppMarkers.some(function (m) { return ua.indexOf(m) !== -1; });
      var isIOS = /iphone|ipad|ipod/.test(ua);
      if (!embedded && isIOS) {
        var isSafari = ua.indexOf("safari") !== -1;
        var isKnownStandalone = ua.indexOf("crios") !== -1 || ua.indexOf("fxios") !== -1 || ua.indexOf("edgios") !== -1 || ua.indexOf("oprios") !== -1;
        embedded = !isKnownStandalone && !isSafari;
      }
      if (!embedded && ua.indexOf("android") !== -1) {
        if (ua.indexOf("; wv") !== -1 || ua.indexOf("version/") !== -1) embedded = true;
      }

      if (embedded) {
        var status = document.getElementById("status");
        var embeddedPanel = document.getElementById("embedded");
        if (status) {
          status.textContent = "Google blocks sign-in inside many in-app browsers. Open this page in Safari or Chrome, then continue.";
        }
        if (embeddedPanel) embeddedPanel.style.display = "grid";
        return;
      }

      window.location.replace(startUrl);
    })();
  </script>
</body>
</html>`;

  const response = new NextResponse(html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
    },
  });
  response.headers.set("Cache-Control", "no-store");
  return response;
}
