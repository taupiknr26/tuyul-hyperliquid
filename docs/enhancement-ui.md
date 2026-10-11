# Horizon Dashboard: Palette and Theme

Colors are estimated by eye from the screenshot. Use an eyedropper on the original if you need exact hex values.

## Theme

**Dark fintech with neon-lime accents.** The look comes from five things:

- Layered near-black surfaces on a charcoal base.
- One signature gradient (teal → lime → yellow) reused on charts, rings, bars and card outlines.
- A soft sky-blue → pale-yellow glow behind the page header.
- Generous rounding (16-24px on cards, about 4-6px on badges).
- Small, high-contrast, semi-bold white text with muted gray secondary text.

## Color Palette

| Role | Color | Hex |
|---|---|---|
| App background / sidebar | Charcoal | `#141414` |
| Card surface | Dark gray | `#1B1B1B` |
| Inner cards, rows, active nav | Near-black | `#0B0B0B` |
| Borders / dividers | Subtle gray | `#2A2A2A` |
| Primary text | White | `#FFFFFF` |
| Secondary text | Muted gray | `#8C8C8C` |
| Accent (badges, active chip, buttons) | Lime | `#C6EB72` |
| Accent 2 (bars, chart top, toggle) | Yellow | `#E8D94C` |
| Accent 3 (gradient start) | Teal / mint | `#4DD9C6` |
| Success / positive | Green | `#3FB950` |
| Links / sub-labels | Bright blue | `#3A9BFF` |
| Reminder stripe (alt) | Yellow | `#E8D94C` |
| Header glow gradient | Sky → pale yellow | `#C5E4F3` → `#E6DE7C` |
| Credit card gradient | Slate → purple | `#2A2E45` → `#4A3F73` |
| Danger (not shown; suggested) | Coral red | `#FF5C5C` |

**Signature gradient:** `linear-gradient(135deg, #4DD9C6, #C6EB72, #E8D94C)`

## CSS Tokens

```css
:root {
  --bg: #141414;
  --surface: #1b1b1b;
  --surface-deep: #0b0b0b;
  --border: #2a2a2a;
  --text: #ffffff;
  --text-muted: #8c8c8c;
  --lime: #c6eb72;
  --yellow: #e8d94c;
  --teal: #4dd9c6;
  --success: #3fb950;
  --danger: #ff5c5c;
  --link: #3a9bff;
  --gradient-accent: linear-gradient(135deg, var(--teal), var(--lime), var(--yellow));
  --gradient-header: linear-gradient(135deg, #c5e4f3, #e6de7c 70%, transparent);
  --gradient-card: linear-gradient(135deg, #2a2e45, #4a3f73);
  --radius-card: 20px;
  --radius-badge: 5px;
  --font: "Plus Jakarta Sans", "Inter", sans-serif;
}
```

## Styling Tips

- **Stat cards:** 2px gradient border (teal → yellow) on a `--surface-deep` card, with an icon in a rounded dark square.
- **Badges:** lime background with dark text, uppercase, 10-11px.
- **Active nav item:** a darker pill (`--surface-deep`) on the charcoal sidebar, with no accent color.
- **Charts:** a line with an area fill that fades from yellow/lime at the top to transparent at the bottom, with faint gridlines.
- **List rows:** dark rounded rows with a 3px colored left stripe (green or yellow) for status.
- **Segmented buttons (1D/1W/1M):** dark chips, with the active one in lime and dark text.

## Layout Notes

- **Three columns:** left sidebar (navigation, Discover PRO promo card, Settings/Logout at the bottom), a main content area, and a right rail (search/profile bar, card balance, credit card, reminders).
- **Main area, top to bottom:** page title over the header glow, two summary cards (balance with ring chart, investment with bar chart), then "Overview Statistic" (two gain/loss cards beside the portfolio chart), then a "History" table.
- **Typography:** section titles are about 20-24px bold; card labels and body text are 12-14px.