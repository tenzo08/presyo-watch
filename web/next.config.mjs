/**
 * Static export.
 *
 * The dashboard has no server of its own: every byte it needs comes from the API in the
 * browser. Exporting static files means it deploys free to Cloudflare Pages or Vercel with
 * no Node runtime, no serverless functions to cold-start, and nothing between the reader and
 * a CDN. It also keeps the one slow dependency — a Render free instance that sleeps —
 * visibly the API's problem rather than smeared across a server-rendered page that hangs
 * before it paints anything.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  output: "export",
  reactStrictMode: true,
  // Static export has no image optimiser. There are no raster images here anyway.
  images: { unoptimized: true },
  trailingSlash: true,
};

export default nextConfig;
