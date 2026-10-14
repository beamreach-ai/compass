/** @type {import('next').NextConfig} */
const nextConfig = {
  // The demo-local container bind-mounts the host `dashboard/` directory over
  // /app, so a production build and a `next dev` session share one working
  // directory and would otherwise overwrite each other's `.next` — leaving
  // whichever server is running to serve chunks it never built. Keeping the
  // production output under its own directory lets both coexist.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  // Local development without nginx: `DEV_API_URL=http://127.0.0.1:8001 npm run dev`
  // forwards the API calls the pages make to a locally running engine API.
  async rewrites() {
    const api = process.env.DEV_API_URL;
    return api ? [{ source: "/api/:path*", destination: `${api}/api/:path*` }] : [];
  },
};

module.exports = nextConfig;
