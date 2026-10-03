# SEO and trust backlog (home page)

From the `/jojo-seo` audit of https://tally-connector-1lir.onrender.com/ on 2026-10-02 (playbook
reviewed 2026-09-28). The owner deferred these; nothing here is done yet. The technical basics
already pass: content in the HTML, one H1, title and description, canonical, raster favicon set,
consistent `WebSite` name, robots/sitemap, `noindex` on private routes, Lighthouse 100 x 4.

## High
1. **Show who runs the product.** Add the operating company's legal name, contact email and
   phone, and address to the footer and the `Organization` JSON-LD (`marketing.json_ld`), and add
   `/privacy` and `/terms` pages (what data is collected, the consent model, retention, who to
   contact). Needs the owner's company details. Rationale: people-first content and E-E-A-T for a
   finance product; bank due diligence.
2. **Own a domain.** Buy one, add it as a Render custom domain, set `TC_PUBLIC_URL`, 301 the
   onrender URL to it, then rebuild the connector (`SERVER=` in `connector/build.sh`) and update
   the canonical/sitemap. Links earned on `*.onrender.com` are lost if we move later.

## Medium
3. **Own brand name.** "Tally Connector" is generic and uses Tally Solutions' trademark; it can't win
   its own brand query. Pick a distinct name and keep "works with TallyPrime" as a descriptor.
   2026-10-04: renamed to **TallyVeda**. Distinct enough to own its brand query, but it still
   starts with "Tally", so a trademark check with Tally Solutions is still open.
4. **Square logo.** `Organization.logo` points at the 1200x630 OG banner; add a 512x512 PNG logo
   under `backend/app/static/` and point `logo` at it.
   2026-10-04: `logo` now points at the square `icon-tv-192.png`; a 512x512 version is still to do.
5. **Cold starts.** The free Render instance sleeps; Googlebot can wait 30-60 s. Keep it warm while
   it is first indexed, or move to a paid instance or a static host for the marketing page.
6. **One page per intent, later.** Split out only when there's real content for each: Tally data
   for bank credit assessment, the MSME dashboard, and a standalone cash conversion cycle
   calculator page. No thin variants.
7. **Search engines.** Google Search Console (URL-prefix property, meta-tag verification), submit
   `sitemap.xml`; Bing Webmaster Tools (Copilot AI performance report); add IndexNow on deploy.

## Low / fine as is
- FAQPage JSON-LD earns no rich results since 7 May 2026: keep it, expect no FAQ snippets.
- `llms.txt` is ignored by Google Search: harmless, not an SEO lever.
- AI-answer citations will come from third parties (LinkedIn, fintech/lending directories, CA
  forums, YouTube walkthroughs): off-site work.
