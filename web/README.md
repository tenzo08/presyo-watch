# PresyoWatch dashboard

A static Next.js export. It has no server of its own: every byte it shows is fetched from
the API in the browser.

```bash
npm install
cp .env.example .env.local     # point NEXT_PUBLIC_API_URL at your API
npm run dev                    # http://localhost:3000
npm run typecheck && npm run build
```

The build writes `out/` — plain HTML, CSS and JS. Deploy it to Vercel or Cloudflare Pages
with `out` as the output directory and `NEXT_PUBLIC_API_URL` set at build time. Note that
`NEXT_PUBLIC_*` is baked into the bundle by `next build`, so changing the API URL means a
rebuild, not just an environment edit.

## Why static

The one slow dependency is a Render free instance that sleeps after 15 minutes and takes
30–60 seconds to wake. Server-rendering would move that wait *in front of* the first paint:
a blank page while a server blocks on an API that is still booting. Exporting static files
puts the page on screen immediately and shows the wait as a labelled loading state inside it,
which is both faster to first paint and honest about what is happening.

It also means the deployed artefact is a CDN's worth of files. Every advisory `npm audit`
reports against Next concerns a Next **server** — Image Optimizer, Server Components,
middleware rewrites, Server Actions, custom servers. None of that is built or shipped here;
`.next/standalone` does not exist after a build, and `out/` contains no runtime. `postcss` is
pinned forward in `package.json` because its advisories are the ones that *do* touch a build
over repository CSS.

## Chart colours are validated, not chosen

The categorical series colours come from a validated palette and were checked with a
colour-blindness validator in both light and dark modes. On the adjacent pairlist that line
charts use, worst CVD separation is ΔE 9.1 light / 8.4 dark against a target of 8, and worst
normal-vision separation is 19.6 / 19.3 against a floor of 15.

Three light-mode steps sit below 3:1 contrast against the light surface, which is permitted
only with relief. This ships two kinds: every series is named in a legend, and the whole
chart has a table view.

**Do not reorder or extend `--series-*` in `globals.css` without re-running the validator.**
The ordering is the colour-blind safety mechanism, not decoration.

## What the chart refuses to do

- **Join across a gap.** The source does not publish daily. Missing days are `null` with
  `connectNulls={false}`, so the line breaks. Bridging it would draw a week of prices nobody
  measured — the interpolation the database is careful not to do, done in pixels instead.
- **Start the y-axis at zero.** Prices are compared with each other. A ₱0 origin flattens
  every real movement into a line near the top of the frame. The axis is labelled `₱ per kg`.
- **Use a second y-axis.** Ever. Two scales become two charts.
- **Average across markets.** Butuan's average and Tandag's average are two prices, and
  their mean is nobody's. Each line, and each movers row, is one market.
