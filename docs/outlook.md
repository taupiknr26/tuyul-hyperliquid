# Feature Enhancement: Scenario Outlook (XAU/USD Scalping Dashboard)

**Status:** Draft v2 (open questions resolved)
**Owner:** TBD
**Target user:** Intraday gold scalper (Jakarta, WIB / UTC+7)
**Last updated:** 2026-10-12

## Change log (v2)

| # | Decision | Impact |
|---|---|---|
| 1 | **Add multiple sources** | New multi-source registry, cross-checking and confluence logic (section 8) |
| 2 | **Live price/chart already exists** | Module reuses the existing live price feed. No new price widget or chart link |
| 3 | **No push notifications** | Removed from scope |
| 4 | **Users can edit scenarios and add their own levels** | New "My Version" layer (section 6) |
| 5 | **History and outcome review in first release** | Moved into Phase 1 (section 7) |

---

## 1. Overview

Add a **Scenario Outlook** module to the web dashboard. It shows a short, level-based, session-by-session view of gold (XAU/USD): context, key levels, and if/then trade scenarios with invalidation.

It is a **scenario outlook, not news**. It states what could happen under defined conditions and when the view is wrong. It never states a single certain prediction.

## 2. Goals and Non-Goals

### Goals
- Give a scalper a fast, scannable brief before each session (Asia, London, New York).
- Show clear **validity**: when the outlook was generated and when it expires.
- Make every level and claim **traceable to one or more sources**, and show how many sources agree.
- Surface the **event risk window** (CPI, PPI, Fed speeches) in WIB.
- Let the user **adapt** the outlook with their own levels and scenario edits.
- Let the user **review outcomes** of past outlooks.

### Non-Goals
- No automatic order execution.
- No guaranteed signals or win-rate claims.
- No long-form news articles.
- No new live price widget or chart (the dashboard already has one).
- No push notifications.

## 3. User Stories

1. As a scalper, I want a brief before each session so I know the bias and key levels.
2. As a scalper, I want to see when the brief expires so I don't trade on a stale view.
3. As a scalper, I want event times in WIB so I can avoid the news spike.
4. As a scalper, I want each scenario to have an invalidation level so I know when to exit.
5. As a user, I want to see the sources behind every number, and whether several sources agree.
6. As a scalper, I want to add my own levels and edit scenario entries, stops and targets to fit my style.
7. As a user, I want to review past outlooks and see which scenarios triggered, hit targets or were invalidated.

## 4. Session Schedule (WIB)

| Session | Brief generated | Session opens (approx.) | Valid until |
|---|---|---|---|
| Asia | 06:45 | 07:00 | London brief (13:45) |
| London | 13:45 | 14:00 | New York brief (20:15) |
| New York | 20:15 | 20:30 | Next Asia brief (06:45) |

- Runs Monday to Friday.
- London and New York open times shift with daylight saving. Store session times in config, not hard-coded.
- A brief is also **invalidated early** by a high-impact data release (see section 9).

## 5. UI Components

### 5.1 Header bar
- Title: `Scenario Outlook: XAU/USD`
- Session badge: `Asia` / `London` / `New York`
- Generated-at timestamp (WIB) and a **validity countdown**
- Status chip: `Fresh` (green), `Aging` (amber, past 75% of validity), `Stale` (red, expired)
- Label: `Scenario outlook, not news`
- Toggle: `Generated` / `My Version`

### 5.2 Context card
- Current price and session change: **read from the existing live price component** (no new feed)
- Bias: `Bullish` / `Neutral` / `Bearish` / `Mixed`, with one-line reason. `Mixed` appears when sources disagree
- Top 3 drivers (Fed, USD/yields, geopolitics, oil), each with one short line and source badges

### 5.3 Key levels panel
- Table of 2-3 resistance and 2-3 support levels
- Columns: level, role, timeframe, **source count**, distance from price
- **Confluence badge** when 2+ independent sources agree on a level (within tolerance, see section 8)
- `Single source` tag when only one source supports a level
- `Mine` tag for user-added levels
- Highlight the level nearest to current price
- Optional overlay on the existing chart, if the chart supports it

### 5.4 Scenario cards (three cards)

| Card | Content |
|---|---|
| **A. Rejection** | Trigger, idea, entry zone, stop, target(s), invalidation |
| **B. Breakout** | Trigger, retest condition, target(s), invalidation |
| **C. Breakdown** | Trigger, retest condition, target(s), invalidation |

- The active scenario is highlighted when price meets its trigger.
- A scenario flips to `Invalidated` when its invalidation level is hit.
- Show risk-reward ratio when entry, stop and target are all defined.
- Each card has an **Edit** button (see 5.7).

### 5.5 Event risk timeline
- Today's events in WIB, with impact level (low / medium / high)
- A shaded **no-trade window** around high-impact events (default: 15 min before, 15 min after, user-adjustable)
- Countdown to the next high-impact event
- Event times cross-checked across sources

### 5.6 Risk notes
- Liquidity (holidays, session overlap)
- Spread/slippage warning near news
- Position sizing reminder

### 5.7 My Version: user edits and custom levels
- **Add level:** price, type (support / resistance), optional note. Appears in the levels panel with a `Mine` tag
- **Edit scenario:** trigger text, entry zone, stop, targets, invalidation
- **Generated stays untouched.** Edits are saved as a separate "My Version" layer; the user can switch between `Generated` and `My Version`
- **Reset** button restores the generated values per scenario
- **Validation applies** to edits (stop and targets on the correct side of entry; invalidation present). Invalid edits show an inline error and cannot be saved
- A card edited by the user shows an `Edited` badge
- When a new brief arrives, user-added levels carry over only if they are still within a configurable distance of price (default: $50). Others move to an "Archived levels" list. Edited scenarios are not carried over; they stay attached to the old brief

### 5.8 History and outcome review
- List of past outlooks by date and session, filterable
- Per scenario outcome: `Not triggered`, `Triggered, hit target`, `Triggered, hit invalidation`, `Triggered, expired open`
- Outcome is computed from the existing price feed, using each scenario's stored trigger, target and invalidation. User-edited scenarios are tracked separately
- Summary view: counts per outcome by session, by scenario type, and by week
- Optional note field per brief (personal journal)
- Clear caption on the summary: `Past outcomes do not predict future results`

### 5.9 Sources and disclaimer footer
- Source list with links, source type and fetch time
- Fixed line: `Not financial advice. Trading gold with leverage carries a high risk of loss.`

## 6. Data Model

```json
{
  "id": "2026-10-12-newyork",
  "instrument": "XAUUSD",
  "session": "newyork",
  "generatedAt": "2026-10-12T13:15:00Z",
  "validUntil": "2026-10-12T23:45:00Z",
  "status": "fresh",
  "bias": {
    "direction": "neutral",
    "agreement": "mixed",
    "reason": "Stalling at 4H resistance; hawkish Fed caps upside.",
    "sources": ["src1", "src2"]
  },
  "drivers": [
    { "label": "Fed", "text": "Markets price a likely December hike.", "sources": ["src1", "src3"] }
  ],
  "levels": [
    {
      "id": "lvl-1",
      "price": 4220,
      "type": "resistance",
      "role": "Fib 23.6% + 100 SMA (4H)",
      "origin": "generated",
      "sources": ["src1", "src2"],
      "confluence": true
    }
  ],
  "scenarios": [
    {
      "key": "A",
      "name": "Rejection",
      "trigger": "Rejection candle at resistance zone on 1-5m chart",
      "direction": "short",
      "entryZone": [4215, 4220],
      "stop": 4222,
      "targets": [4200],
      "invalidation": "5m close above 4220",
      "state": "watching",
      "outcome": null
    }
  ],
  "events": [
    {
      "name": "US CPI",
      "timeUtc": "2026-10-14T12:30:00Z",
      "impact": "high",
      "sources": ["src3", "src4"],
      "noTradeWindowMin": { "before": 15, "after": 15 }
    }
  ],
  "riskNotes": ["Thin liquidity during US holiday"],
  "sources": [
    {
      "id": "src1",
      "title": "FXStreet gold analysis",
      "type": "technical",
      "url": "https://www.fxstreet-id.com/markets/commodities/metals/gold",
      "fetchedAt": "2026-10-12T13:10:00Z"
    }
  ],
  "disclaimer": "Not financial advice."
}
```

### User layer (separate table/collection)

```json
{
  "userId": "u_123",
  "outlookId": "2026-10-12-newyork",
  "customLevels": [
    { "id": "u-lvl-1", "price": 4150, "type": "support", "note": "Yesterday's low" }
  ],
  "scenarioEdits": [
    { "key": "A", "entryZone": [4212, 4218], "stop": 4221, "targets": [4195], "editedAt": "2026-10-12T13:30:00Z" }
  ],
  "journalNote": ""
}
```

> Values above are **sample data** for layout only. Never ship hard-coded prices or levels. Every generated level must come from fetched sources.

## 7. Phases

### Phase 1: MVP (includes history, per decision 5)
- Context, levels, three scenarios, events, disclaimer
- Multi-source fetching and cross-checking (section 8)
- Three scheduled briefs per weekday
- Validity countdown and status chip
- Reuse of the existing live price feed
- **My Version:** custom levels, scenario edits, reset
- **History and outcome review** with per-scenario outcomes and summary

### Phase 2: Interactivity
- Levels overlay on the existing chart
- Scenario state tracking in real time (watching, triggered, invalidated)
- Event no-trade window shading and countdown
- Journal notes and tagging

### Phase 3: Insight
- Customizable session times and alert rules (in-dashboard only, no push)
- Per-source reliability scoring based on past outcomes
- Multi-instrument support (silver, EUR/USD)
- Export history to CSV

## 8. Multi-Source Design

### 8.1 Source registry
Each source is a config entry, so sources can be added or disabled without code changes.

| Field | Meaning |
|---|---|
| `id` | Unique source id |
| `type` | `technical`, `news`, `calendar`, `macro`, `positioning` |
| `url` / `api` | Endpoint or page |
| `priority` | Order of preference |
| `weight` | Used in agreement scoring |
| `enabled` | On/off switch |
| `license` | Notes on terms of use and attribution |

### 8.2 Candidate sources (confirm terms of use before enabling)

| Need | Candidates |
|---|---|
| Technical levels and gold analysis | FXStreet, other analysis sites with public technical pages |
| Gold news and price context | Kitco, Investing.com, major financial news outlets |
| Economic calendar | Official release calendars (e.g. BLS for CPI/PPI, Federal Reserve for FOMC) plus a calendar aggregator |
| Rate expectations | CME FedWatch |
| Flows and central bank demand | World Gold Council |

Prefer official or licensed APIs over scraping. Check each site's terms and rate limits before automating.

### 8.3 Cross-checking rules
- **Levels:** cluster levels from different sources that fall within a tolerance (default: $5). A cluster with 2+ independent sources becomes a **confluence level**. Its price is the weighted average, and it shows the source count
- **Single-source levels** are kept but tagged `Single source` and ranked lower
- **Bias:** if sources lean opposite ways, set bias to `Mixed` and list both views instead of picking one
- **Events:** the event time must match across at least two sources. If they differ, show the earlier time as a caution and flag `Time unconfirmed`
- **Probabilities** (e.g. rate-hike odds) are shown only when they come from a named source, with its fetch time
- **Failures:** if one source is down, continue with the rest and show `N of M sources available`. If fewer than a minimum number (default: 2) are available for levels, mark the outlook `Low confidence`
- **Attribution:** show source name and link for every claim. Paraphrase source text and do not copy articles

## 9. Backend / Generation Pipeline

1. **Scheduler** triggers at the three session times (Mon-Fri, WIB).
2. **Fetchers** pull data from all enabled sources in parallel.
3. **Reconciler** applies the cross-checking rules in section 8.3.
4. **Generator** produces the outlook JSON from reconciled data.
5. **Validator** checks the output before publishing (section 11).
6. **Store** saves the outlook and keeps history.
7. **Outcome job** runs after each session using the existing price feed to label scenario outcomes.
8. **API** serves outlooks, history and user layers to the dashboard.

### Suggested endpoints
- `GET /api/outlook/latest?instrument=XAUUSD`
- `GET /api/outlook/{id}`
- `GET /api/outlook/history?instrument=XAUUSD&from=&to=`
- `GET /api/outlook/history/summary?from=&to=`
- `GET /api/outlook/{id}/mine` and `PUT /api/outlook/{id}/mine` (user layer)
- `DELETE /api/outlook/{id}/mine/scenarios/{key}` (reset one scenario)

## 10. Early Invalidation and Refresh Rules

- **Scheduled:** new brief at each session start.
- **Event-triggered:** after a high-impact release (CPI, PPI, NFP, FOMC, Fed Chair speech), mark the current outlook `Stale` and generate a refreshed one within a few minutes.
- **Price-triggered:** if price breaks a scenario's invalidation level, mark that scenario `Invalidated`. If both major levels break, mark the whole outlook `Stale`.
- **Manual:** a "Refresh outlook" button, rate-limited.

## 11. Validation Rules (Quality Gates)

An outlook cannot publish unless:
- Every level and driver has at least one `source`.
- Levels are tagged `confluence` or `Single source` correctly.
- Every scenario has a **trigger, target and invalidation**.
- Stop and targets are on the correct side of entry for the stated direction.
- `validUntil` is set and later than `generatedAt`.
- No probability figure appears unless it comes from a cited source.
- Disclaimer text and the `Scenario outlook, not news` label are present.

User edits are validated with the same stop/target/invalidation rules.

If data is missing, show `Unavailable`. Do not fill gaps with guesses.

## 12. States and Edge Cases

| State | Behavior |
|---|---|
| Loading | Skeleton cards |
| Fresh | Normal display |
| Aging | Amber chip |
| Stale | Red chip; dim scenario cards; "Outlook expired" |
| Low confidence | Banner: fewer than the minimum sources were available |
| Source down | Continue with remaining sources and show the count |
| All sources down | Show last outlook with a warning and its fetch time |
| Weekend / market closed | Show weekly recap and next-open time |
| Holiday / thin liquidity | Liquidity warning banner |
| User edit invalid | Inline error; edit not saved |
| Carried-over user level far from price | Moved to Archived levels |

## 13. Acceptance Criteria

- [ ] A brief is generated and displayed for each of the three sessions on weekdays.
- [ ] Generated-at time, validity and status are always visible.
- [ ] All times display in WIB.
- [ ] Data comes from multiple sources, configurable in a registry.
- [ ] Levels show a source count, confluence badge, or `Single source` tag.
- [ ] Conflicting sources produce a `Mixed` bias instead of a forced pick.
- [ ] The module reads price from the existing live price feed; no new price widget exists.
- [ ] Each scenario shows trigger, target and invalidation.
- [ ] Users can add custom levels and edit scenarios, with validation.
- [ ] Generated values stay intact and can be restored with Reset.
- [ ] History lists past outlooks with per-scenario outcomes and a summary.
- [ ] Stale outlooks are visibly dimmed and labeled.
- [ ] High-impact events show a no-trade window.
- [ ] Disclaimer, "not news" label and "past outcomes do not predict future results" caption are visible.
- [ ] The layout works on mobile width.
- [ ] Missing data shows `Unavailable`, never an invented number.
- [ ] No push notification code or settings exist.

## 14. Success Metrics

- Share of briefs published on time (target: 99%)
- Share of briefs passing validation on the first run
- Average source count per published level
- Share of levels with confluence (2+ sources)
- Dashboard opens per session
- Share of users who add custom levels or edit scenarios
- User-reported usefulness (thumbs up/down per brief)

## 15. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Stale levels cause bad trades | Validity countdown, event-triggered invalidation |
| Users read it as a signal | "Scenario outlook" label, disclaimer, if/then wording |
| Source terms of use or scraping limits | Prefer official/licensed APIs, store license notes in registry |
| Sources disagree | `Mixed` bias, show both views, confluence scoring |
| Source site changes or goes down | Multi-source fallback, last-known display with warning |
| Invented or wrong numbers | Source-required validation gate |
| Outcome stats misread as a track record | Caption, no win-rate headline, separate user-edited scenarios |
| User edits break risk rules | Same validation as generated scenarios |
| Daylight-saving time drift | Session times in config, tested twice a year |

## 16. Resolved Decisions

| Question | Decision |
|---|---|
| Which sources are approved beyond FXStreet? | Multiple sources, managed in a registry (section 8). Final list to be confirmed against terms of use |
| Show live price directly or link out? | Neither. The dashboard already has a live price and chart; reuse it |
| Push notifications? | Not needed |
| Can users edit scenarios and add levels? | Yes (section 5.7) |
| History/outcome review in the first release? | Yes (section 5.8, Phase 1) |

## 17. Remaining Open Items

1. Final approved source list and who owns the licensing check.
2. Default tolerance for confluence clustering ($5 proposed) and carry-over distance ($50 proposed). Confirm or adjust.
3. Minimum source count before an outlook is labeled `Low confidence` (2 proposed).
4. How long to keep history (e.g. 12 months, or unlimited).

---

*Not financial advice. This document describes a software feature, not a trading recommendation.*