---
name: faceless-shorts
description: Plan and write faceless YouTube Shorts (also TikTok/Reels) that drive traffic to TLNetwork's sites — everydaycalculator.dev, tlnetwork.net, everydaydigitalshelf.net. Use when the user asks for Shorts scripts, a content batch or calendar, hooks, titles/descriptions, Canva Shorts templates, or scheduling Shorts. Args (optional) — count, site, series, or topic, e.g. "/faceless-shorts 5 calculator" or "/faceless-shorts power outage".
---

# Faceless Shorts for TLNetwork sites

Produce ready-to-record, faceless vertical Shorts whose job is to send viewers to one specific page on one of the user's sites.

## Parse the request

From the args or message, work out:
- **Count** — default 10 (two weeks at 5/week).
- **Site / series** — default the weekly mix below.
- **Topic** — optional; otherwise pick unused ideas from the idea banks.

Ask nothing unless the request is truly ambiguous; use the defaults.

## The sites (check before writing)

Pages and products change. Before writing, re-check the pages you'll use (Firecrawl scrape with a `query` format, or WebFetch) for current calculators, runtime tables, prices and codes. Never invent a price, spec, runtime or discount code. If a page has no number to cite, pick a different idea or flag it.

### everydaycalculator.dev — series "DO THE MATH"
Free calculators, each at `everydaycalculator.dev/<slug>`:
fuel-cost-per-mile-calculator, gas-mileage-calculator, unit-price-calculator, gas-trip-cost-calculator, percentage-calculator, sales-tax-calculator, hourly-to-salary-calculator, loan-payment-calculator, compound-interest-calculator, dividend-income-calculator, profit-margin-calculator, discount-calculator, bill-split-calculator, electricity-cost-calculator.

Format: relatable question → screen recording of the calculator → one surprising number.

### tlnetwork.net — series "HOW LONG WILL IT RUN?"
Power station runtime guides and product research: `/how-long-will-power-station-run-tv`, `/how-long-will-power-station-run-fan`, `/product-hunter`, `/jackery-homepower-3000`. Features BLUETTI Elite 100 V2 (1,024Wh) and Jackery HomePower 3000 (3,072Wh); runtimes use an 85% usable-capacity allowance. Discount code last seen: `ELITE5OFF` (5% off BLUETTI Elite 100 V2) — re-verify.

Format: outage/camping scenario → animated battery graphic → runtime numbers from the site's own tables.

### everydaydigitalshelf.net — series "BEFORE / AFTER" and "PROMPT OF THE DAY"
Digital downloads sold via Beacons checkout. Last seen: Resume & Cover Letter Kit ($5.99; 3 designs, A4 + US Letter, 24 editable Word docs + PDFs), Business & Side Hustle AI Prompt Kit ($1; 53 prompts), Farm Animals Coloring Book ($1; 10 pages), Cute Animal Coloring Book for Kids ($1).

Format: before/after transformation, or one real prompt from the kit demoed on screen. Seasonal: résumés in Jan/Sep, coloring books before holidays.

## Default weekly mix

| Mon | Tue | Wed | Thu | Fri |
|---|---|---|---|---|
| Do The Math | How Long Will It Run? | Do The Math | Digital Shelf | Do The Math |

## Idea banks (skip ones already made — check the user's earlier batches if available)

- **Do The Math:** $6 coffee → compound interest; $20/hr → yearly; stacked discounts (70%+20% = 76%); space heater monthly cost; road trip gas vs hotel; car loan total interest; Costco unit price check; commute cost per mile; shares needed for $100/mo dividends; restaurant tip + split; sales tax on a big purchase; side-hustle profit margin; real MPG check; "what % raise is that?".
- **How Long Will It Run?:** TV + Wi-Fi overnight; small vs big TV; 500Wh vs 1,024Wh vs 3,072Wh; fan in a summer outage; CPAP/fridge/router (only if the site has the numbers); BLUETTI vs Jackery face-off.
- **Digital Shelf:** resume before/after; cover letter in 60s; one AI prompt that writes a bio / caption / cold email; coloring page flip-through ("rainy day activity for $1").

## Script format (use for every Short)

```
### #N (Day) SERIES — "Hook"
**Page:** <exact URL> · **Length:** 25–40s

| Time | Visual | VO / On-screen text |
|---|---|---|
| 0–2s   | ... | **HOOK — under 10 words, the most interesting moment first** |
| 2–~8s  | ... | stakes / setup |
| ...    | ... | 2–3 value beats, one idea each, cut every 2–3s |
| ...    | ... | payoff — the big number, ZOOMED |
| ...    | URL end card | CTA with a reason to visit, URL spoken aloud ("everyday calculator dot dev") |
| last 2s| ... | loop line that flows back into the hook |

- **On-screen fine print:** (disclaimers, if any)
- **Title:** under 50 chars, curiosity-driven, true
- **Description:** 1–2 lines + site name + "(link on channel)" + disclosures + 3–5 hashtags incl. #shorts
- **Pinned comment:** (optional) a question that invites replies
```

## Rules

- **Do the math yourself.** Compute every calculator result (show inputs in the script) and sanity-check it; numbers on screen must match what the live calculator shows.
- **Links:** Shorts descriptions and comments aren't clickable — CTA points to "link on my channel" (profile links, order: everydaycalculator.dev, tlnetwork.net, everydaydigitalshelf.net) and the Short's Related Video. Suggest a matching long-form video per series.
- **Disclosures:** money topics → "Not financial advice"; tlnetwork.net product content → "Affiliate links — I may earn a commission" on screen and in the description.
- **Avoid repetitive content:** YouTube demonetizes mass-produced, templated uploads. Vary hooks, visuals and angles in every batch; never reuse a script with numbers swapped.
- **AI disclosure:** remind the user to tick "altered or synthetic content" if they use a realistic AI voice or AI visuals.
- **Readability:** captions max 5 words, key words in CAPS, nothing important in the bottom 20% of the frame; write so it works with sound off.
- **Stock:** only B-roll and music the user has rights to (own clips, Canva, Pexels, Pixabay).

## Output

1. Write the batch to a markdown file the user can open (scratchpad or working directory), named `faceless-shorts-batch-<N>.md`, with: schedule header, all scripts, and the production checklist below. Send it with SendUserFile.
2. In chat, give a short table: #, series, hook — plus anything you couldn't verify.

Production checklist to append:
- Record screen captures in a phone-sized vertical browser window
- Vary voice delivery and visuals between videos
- Tick "altered or synthetic content" if using realistic AI voice/visuals
- Set each Short's Related Video to the series' long-form video
- Pin the comment right after publishing
- Cross-post to TikTok + Reels (TikTok bio link is clickable)

## Optional follow-ups (offer, don't do unasked)

- **Canva templates** — Canva `create-design` with format "Your Story" (9:16). Existing: Do The Math template https://canva.link/9wp0li6g4c17e21 (navy + lime). Planned looks: How Long Will It Run? = charcoal + amber, battery/lightning icons; Before/After = cream with coral + teal, split screen, price tag. If credits are exhausted, tell the user and suggest duplicating the existing template.
- **Scheduling** — Metricool: check best posting times, then schedule across YouTube, TikTok and Instagram once the user has the finished video files. Confirm before scheduling anything.
- **Performance** — Metricool analytics to see which series drive views and adjust the mix.
