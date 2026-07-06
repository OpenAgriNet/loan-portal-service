# Amul "OAN-UI" Chat — Style Reference

Extracted from the chat frontend (`OAN-UI`, `amul-dev` branch). Everything here is
ready to paste into a small self-contained portal so it matches the chat UI look.

Stack it came from: React 19 + Vite + Tailwind v4 (CSS-based theme) + shadcn/ui
(`new-york` style, `lucide` icons). Design tokens live in
`src/styles/global.css`; brand colors/fonts are **overridden at runtime** from
`config.json` via `src/hooks/ConfigProvider.tsx` (it does
`document.documentElement.style.setProperty('--<key>', value)`).

> IMPORTANT — two sources of truth. `global.css` ships a GREEN primary
> (`--primary: forest-600 #00bd52`), but at runtime `ConfigProvider` injects
> `config.json.theme.colors`, which sets **`--primary: #F65151` (coral red)** and
> **`--background: #FFE2E2` (soft pink)**. The live app is the coral/pink theme,
> confirmed by hardcoded `#F65151` / `#FFE2E2` all over the chat components
> (bubbles, language dialog, settings, feedback modal). The tokens below use the
> **live coral/pink values**. The green is kept only as an accent.

---

## 1. Color palette

### Brand / live theme (from `config.json` → injected at runtime)
| Role | Hex | Source |
|---|---|---|
| **Primary (brand coral-red)** | `#F65151` | `config.json` `theme.colors.primary`; hardcoded across chat components |
| Primary hover (darker) | `#E23B3B` | derived (primary/90-ish) |
| Primary tint (bg wash) | `#FFE2E2` | `config.json` `theme.colors.background`; user bubble + selected states |
| Primary tint (hover) | `#FFCCCC` | feedback-modal.tsx hover |
| Secondary (pale honey) | `#FEF89E` | `config.json` `theme.colors.secondary` |
| Accent (green) | `#00BD52` | `config.json` `theme.colors.accent`; pulse/active accents |
| Foreground / text | `#3D3D3D` | `config.json` `theme.colors.foreground` |
| User chat bubble bg | `#FFE2E2` | `bubbles/text-bubble.tsx` (NOTE: config `userBubble` is `#bbf7d0` mint but bubbles actually render `#FFE2E2`) |

### Amul logo gradient (the signature brand mark gradient)
| Role | Value | Source |
|---|---|---|
| Brand gradient | `linear-gradient(90deg, #218FFF 0%, #FF1150 100%)` | `.brand-gradient` in `global.css`; also the `AmulLogo.svg` fill. Used on send/mic buttons |
| Brand gradient (soft) | `linear-gradient(90deg, rgba(33,143,255,.25), rgba(255,17,80,.25))` on white | `.brand-gradient-soft` (input tooltip) |
| Blue stop | `#218FFF` | logo/gradient |
| Pink stop | `#FF1150` | logo/gradient |
| Amul wordmark red | `#EC1C24` | `amulText.svg`, `.route-progress` top bar |

### Surfaces & neutrals
| Role | Hex | Source |
|---|---|---|
| Page background | `linear-gradient(180deg, #FFF2F2 0%, #FFFFFF 100%)` | `.layout-gradient` (chat-layout) |
| Card / surface | `#FFFFFF` | `--card` |
| Header bar | `#FFFFFF` + backdrop blur | `core/header` |
| Header border | `#E6E8EC` | header `border-b` |
| Input bar border | `#E3E3E3` | chat-input `border-t` |
| Card/list border | `#F3F4F6` (gray-100) | welcome cards, list items |
| Table head bg | `#F9FAFB` (gray-50) | card-bubble |
| Muted bg | `#F3F4F6` (gray-100) | `--muted` |
| Muted text / secondary text | `#4B5563`–`#6B7280` (gray-600/500) | widespread |

Neutral scale used = **Tailwind default `gray`** (the components use `gray-50…950`
utilities; the `--gray-*` vars referenced in `global.css` are actually undefined,
so utilities fall back to Tailwind's gray):
`50 #f9fafb · 100 #f3f4f6 · 200 #e5e7eb · 300 #d1d5db · 400 #9ca3af · 500 #6b7280 · 600 #4b5563 · 700 #374151 · 800 #1f2937 · 900 #111827 · 950 #030712`.

### Status / semantic
| Role | Hex | Source |
|---|---|---|
| Danger / destructive | `#C02626` (dark `#F87171`) | `--destructive` |
| Success / green | `#00A651` / `#00BD52` | pulseGreen keyframes / accent |
| Send-ready mint | `#ABFFA9` / `#72FFB3` | chat-input send button, blink-send |
| Recording orange | `#FF5C00` | recording-controls |
| Warning (honey) | `#FEF89E` / `#CEE536` | secondary / honey-400 |
| Focus ring | `#72FFB3` (forest-300) | `--ring` |

Full honey (yellow-green) and forest (green) ramps exist in `global.css` if you
need more chart/accent stops (`--honey-50…950`, `--forest-50…950`).

---

## 2. Typography

- **Applied family:** `"Noto Sans", sans-serif` (`html`/`body` in `global.css`;
  `config.json` `theme.fonts.family = "Noto Sans"`). Loaded from Google Fonts at
  runtime (`fonts.googleapis.com`).
- Also present but **not the applied body font:** Poppins (imported in
  `fonts.css`) and Montserrat `.ttf`s bundled in `src/assets/fonts/` (unused by
  the rendered UI). Header uses a `font-inter` class that isn't defined (no-op).
- **Self-contained caveat:** Noto Sans is a Google-hosted webfont — a portal that
  can't call external hosts should fall back to a system sans stack (below), or
  self-host Noto Sans / the bundled Montserrat `.ttf`s. The look is a clean,
  neutral humanist sans; the system fallback reads nearly identically.
- **Base size:** `0.9rem` (`config.json` fontSizes.base; CSS default fallback `0.95rem`).
- **Scale** (`config.json` `fontSizes`): `xs .7rem · sm .8rem · base .9rem · lg 1.05rem · xl 1.15rem · 2xl 1.4rem · 3xl 1.75rem`.
- **Weights:** 400 body · 500 medium (buttons, headings, nav) · 600 semibold
  (card titles) · 700 bold. Full 100–900 available.
- **Line-height:** body/bubbles `leading-relaxed` (≈1.625); card titles
  `leading-none`; header titles `leading-tight`.
- **Headings:** header/page title `text-lg → text-xl → 24px` responsive, weight 500.

---

## 3. Shape & spacing

- **Radius scale** (`--radius: 0.625rem` = 10px):
  `sm = calc(r-4) 6px · md = calc(r-2) 8px · lg = r 10px · xl = calc(r+4) 14px`.
  Buttons/inputs `rounded-md` (8px). Cards `rounded-xl` (12px). Welcome/list cards
  `rounded-2xl` (16px). **Chat bubbles `20px`** (with one tail corner squared:
  user `rounded-tr-md`, assistant `rounded-tl-md`). Badges/pills/avatars
  `rounded-full`.
- **Shadows:**
  - Card / assistant bubble: `shadow-sm` = `0 1px 2px 0 rgb(0 0 0 / 0.05)`.
  - User bubble (custom): `1.6px 1.6px 9.6px 0px rgba(0,0,0,0.08)` (`#00000014`).
  - Send button: `shadow-md`. Welcome card hover: `shadow-md`.
- **Spacing rhythm:** 4px base (Tailwind). Common: card padding `py-6 px-6`
  (24px); bubbles `px-4 py-3`; buttons `h-9 px-4`; gaps `gap-2/gap-3/gap-4`.
- **Container widths:** chat column `max-w-3xl` (48rem) centered `mx-auto`,
  padded `px-2 sm:px-4`; welcome action list `max-w-2xl`.

---

## 4. Paste-ready CSS token block

```css
:root {
  /* ---- Brand (live coral/pink theme) ---- */
  --primary:            #F65151;
  --primary-hover:      #E23B3B;
  --primary-foreground: #ffffff;
  --primary-tint:       #FFE2E2;   /* washes, user bubble, selected states */
  --primary-tint-hover: #FFCCCC;

  --secondary:            #FEF89E;  /* pale honey */
  --secondary-foreground: #455314;
  --accent:               #00BD52;  /* green accent */
  --accent-foreground:    #003619;

  /* Amul logo gradient (signature brand mark) */
  --brand-grad:      linear-gradient(90deg, #218FFF 0%, #FF1150 100%);
  --brand-grad-soft: linear-gradient(90deg, rgba(33,143,255,.25) 0%, rgba(255,17,80,.25) 100%);
  --brand-blue:      #218FFF;
  --brand-pink:      #FF1150;
  --brand-red:       #EC1C24;      /* Amul wordmark / top progress bar */

  /* ---- Surfaces & text ---- */
  --page-bg:      linear-gradient(180deg, #FFF2F2 0%, #FFFFFF 100%);
  --background:   #FFF2F2;
  --foreground:   #3D3D3D;
  --card:         #ffffff;
  --card-foreground: #111827;
  --surface-header: #ffffff;

  --muted:            #F3F4F6;
  --muted-foreground: #6B7280;

  /* Borders */
  --border:        #E5E7EB;   /* gray-200 */
  --border-subtle: #F3F4F6;   /* gray-100, cards/list items */
  --border-header: #E6E8EC;
  --input-border:  #E3E3E3;

  /* Neutral scale (Tailwind gray) */
  --gray-50:#f9fafb; --gray-100:#f3f4f6; --gray-200:#e5e7eb; --gray-300:#d1d5db;
  --gray-400:#9ca3af; --gray-500:#6b7280; --gray-600:#4b5563; --gray-700:#374151;
  --gray-800:#1f2937; --gray-900:#111827; --gray-950:#030712;

  /* ---- Status ---- */
  --success:     #00A651;
  --send-ready:  #ABFFA9;
  --danger:      #C02626;
  --danger-foreground: #ffffff;
  --warning:     #FEF89E;
  --recording:   #FF5C00;
  --ring:        #72FFB3;   /* focus ring */

  /* ---- Typography ---- */
  --font-family: "Noto Sans", system-ui, -apple-system, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
  --font-size-base: 0.9rem;
  --fs-xs:.7rem; --fs-sm:.8rem; --fs-lg:1.05rem; --fs-xl:1.15rem;
  --fs-2xl:1.4rem; --fs-3xl:1.75rem;
  --fw-regular:400; --fw-medium:500; --fw-semibold:600; --fw-bold:700;

  /* ---- Radius ---- */
  --radius:    0.625rem;              /* 10px */
  --radius-sm: 0.375rem;              /* 6px  */
  --radius-md: 0.5rem;               /* 8px  */
  --radius-lg: 0.625rem;             /* 10px */
  --radius-xl: 0.875rem;             /* 14px */
  --radius-2xl: 1rem;                /* 16px cards */
  --radius-bubble: 20px;
  --radius-full: 9999px;

  /* ---- Shadows ---- */
  --shadow-sm:     0 1px 2px 0 rgb(0 0 0 / 0.05);
  --shadow-md:     0 4px 6px -1px rgb(0 0 0 / 0.10), 0 2px 4px -2px rgb(0 0 0 / 0.10);
  --shadow-bubble: 1.6px 1.6px 9.6px 0px rgba(0,0,0,0.08);

  /* ---- Spacing (4px rhythm) ---- */
  --space-1:.25rem; --space-2:.5rem; --space-3:.75rem; --space-4:1rem;
  --space-6:1.5rem; --space-8:2rem;
  --container: 48rem;   /* max-w-3xl chat column */
}

html, body {
  font-family: var(--font-family);
  font-size: var(--font-size-base);
  color: var(--foreground);
}
body { background: var(--page-bg); background-attachment: fixed; }
```

### Dark mode
The chat FE **does support dark** (`.dark` class + `next-themes`), though the
runtime `config.json` injection only sets the light brand values, so dark is
partial in practice. If you want it, apply on `.dark` / `[data-theme=dark]`:

```css
.dark {
  --background: #030712;  --foreground: #f9fafb;
  --card: #111827;        --card-foreground: #f9fafb;
  --primary: #F65151;     --primary-foreground: #ffffff;   /* brand stays coral */
  --secondary: #CEE536;   --accent: #E2F270;
  --muted: #374151;       --muted-foreground: #f3f4f6;
  --border: #374151;      --border-subtle: #1f2937;
  --danger: #F87171;      --ring: #72FFB3;
}
```
For a small portal, **light-only is fine and matches the primary experience.**

---

## 5. Component snippets

```css
/* ---- Buttons (shadcn: h-9, rounded-md, weight 500) ---- */
.btn {
  display:inline-flex; align-items:center; justify-content:center; gap:.5rem;
  height:2.25rem; padding:0 1rem; border-radius:var(--radius-md);
  font-weight:var(--fw-medium); font-size:.875rem; white-space:nowrap;
  transition:all .15s ease; cursor:pointer; border:1px solid transparent;
}
.btn-primary   { background:var(--primary); color:var(--primary-foreground); }
.btn-primary:hover   { background:var(--primary-hover); }
.btn-secondary { background:var(--secondary); color:var(--secondary-foreground); }
.btn-secondary:hover { filter:brightness(.96); }
.btn-outline   { background:var(--card); border-color:var(--border); color:var(--foreground); box-shadow:var(--shadow-sm); }
.btn-outline:hover   { background:var(--primary-tint); color:var(--primary); }
.btn-ghost:hover     { background:var(--muted); }
.btn:disabled  { opacity:.5; pointer-events:none; }
.btn-lg { height:2.5rem; padding:0 1.5rem; }
.btn-sm { height:2rem;  padding:0 .75rem; }
/* signature gradient action (send/mic) */
.btn-brand { background:var(--brand-grad); color:#fff; border-radius:var(--radius-full); box-shadow:var(--shadow-md); }

/* ---- Text input (h-9, rounded-md, 3px focus ring) ---- */
.input {
  height:2.25rem; width:100%; padding:.25rem .75rem;
  border:1px solid var(--input-border); border-radius:var(--radius-md);
  background:transparent; font-size:1rem; color:var(--foreground);
  box-shadow:var(--shadow-sm); outline:none; transition:color,box-shadow .15s;
}
.input::placeholder { color:var(--muted-foreground); }
.input:focus-visible {
  border-color:var(--ring);
  box-shadow:0 0 0 3px color-mix(in srgb, var(--ring) 50%, transparent);
}

/* ---- Card / list item ---- */
.card {
  background:var(--card); color:var(--card-foreground);
  border:1px solid var(--border-subtle); border-radius:var(--radius-2xl);
  padding:1.5rem; box-shadow:var(--shadow-sm);
}
.list-item {   /* welcome-panel action card */
  display:flex; align-items:center; gap:1rem; width:100%; text-align:left;
  background:var(--card); border:1px solid var(--border-subtle);
  border-radius:var(--radius-2xl); padding:1rem 1.5rem; box-shadow:var(--shadow-sm);
  transition:all .2s ease; cursor:pointer;
}
.list-item:hover { background:var(--gray-50); box-shadow:var(--shadow-md); }

/* ---- Badge / pill / status chip (rounded-full, xs, weight 500) ---- */
.badge {
  display:inline-flex; align-items:center; gap:.25rem; width:fit-content;
  padding:.125rem .5rem; border-radius:var(--radius-full);
  font-size:.75rem; font-weight:var(--fw-medium); border:1px solid transparent;
}
.badge-primary { background:var(--primary); color:#fff; }
.badge-secondary { background:var(--secondary); color:var(--secondary-foreground); }
.badge-success { background:color-mix(in srgb,var(--success) 15%,#fff); color:var(--success); }
.badge-danger  { background:color-mix(in srgb,var(--danger) 12%,#fff);  color:var(--danger); }
.badge-warning { background:var(--warning); color:#455314; }
.badge-outline { background:transparent; border-color:var(--border); color:var(--foreground); }
/* selected/active pill pattern from the FE */
.chip-selected { background:var(--primary-tint); color:var(--gray-900); border-left:3px solid var(--primary); }

/* ---- Top bar / header ---- */
.topbar {
  position:sticky; top:0; z-index:40;
  display:flex; align-items:center; justify-content:space-between;
  padding:1rem .75rem; background:var(--surface-header);
  border-bottom:1px solid var(--border-header);
  -webkit-backdrop-filter:saturate(110%) blur(6px);
  backdrop-filter:saturate(110%) blur(6px);
}
.topbar-title { font-size:1.25rem; font-weight:var(--fw-medium); color:#000; }

/* ---- Chat bubbles (reference for a conversational surface) ---- */
.bubble { max-width:85%; padding:.75rem 1rem; border-radius:var(--radius-bubble); font-size:1rem; line-height:1.625; }
.bubble-user      { margin-left:auto; background:var(--primary-tint); color:#000; border-top-right-radius:var(--radius-md); box-shadow:var(--shadow-bubble); }
.bubble-assistant { background:var(--card); color:var(--card-foreground); border:1px solid var(--border); border-top-left-radius:var(--radius-md); box-shadow:var(--shadow-sm); }

/* ---- Page shell ---- */
.page { min-height:100svh; background:var(--page-bg); background-attachment:fixed; color:var(--foreground); }
.container { max-width:var(--container); margin:0 auto; padding:0 1rem; }
```

---

## 6. Logo / brand mark

Two usable SVGs, both small and safe to inline (self-contained, no external host):

| Asset | Path (in repo) | What it is |
|---|---|---|
| **Amul mark** | `public/AmulLogo.svg` (2.9 KB) | Circular "A" mark with the blue→pink gradient (`#218FFF → #FF1150`). Used as favicon + assistant avatar. **Primary brand mark to embed.** |
| Amul AI wordmark | `src/assets/amulText.svg` (4.3 KB) | "Amul AI" wordmark in red `#EC1C24`. Used in the welcome header. |
| Maharashtra gov logo | `public/maha-logo.svg` (0.8 KB) | Secondary/partner mark. |
| User avatar | `public/user-avatar.svg` (0.8 KB) | Generic user avatar (green). |

**Inline brand mark** (paste directly — this is `AmulLogo.svg`, the circular
gradient "A"):

```html
<svg width="40" height="40" viewBox="0 0 255 255" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Amul">
  <g clip-path="url(#amulClip)">
    <path d="M127.48 254.962C197.886 254.962 254.961 197.886 254.961 127.481C254.961 57.0751 197.886 0 127.48 0C57.0747 0 -0.0004 57.0751 -0.0004 127.481C-0.0004 197.886 57.0747 254.962 127.48 254.962Z" fill="url(#amulGrad)"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M113.533 160.215C106.771 159.677 98.8564 158.255 90.327 155.643L83.2191 173.585L99.1638 183.959C101.277 185.573 104.389 184.42 108.654 180.386L112.304 183.498C104.85 191.643 97.3964 199.904 89.9812 208.049C80.107 199.174 69.0802 190.337 57.477 190.375C45.0286 190.452 33.0797 203.131 45.6818 224.993C45.7586 225.377 43.7992 227.951 42.6465 226.76C32.8492 219.153 29.1223 197.06 36.7297 184.535C41.6092 176.621 51.4065 168.821 66.8902 170.55L73.3449 153.222C67.8891 151.109 61.2039 153.299 53.4428 159.984C56.017 146.114 64.6617 136.855 81.9512 134.319L99.6632 91.5565C90.4038 87.5223 81.567 92.4018 77.2638 100.432C74.5359 100.201 72.6917 99.3176 72.1923 97.3965L90.4038 63.5092C99.0101 60.5508 106.656 60.4739 113.456 62.6639C141.043 71.4623 155.527 118.451 176.352 170.243C178.08 174.738 180.616 175.199 186.379 169.244L190.606 172.74L153.376 210.124L136.278 158.64C136.278 159.37 127.326 161.367 113.495 160.215H113.533ZM113.533 140.082C118.989 140.735 124.791 140.812 129.478 139.66C127.326 130.554 118.221 109.461 113.533 103.429C112.995 102.737 112.534 102.276 112.15 101.969L97.6269 137.239C97.6269 137.239 105.004 139.16 113.572 140.082H113.533Z" fill="#fff"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M229.527 87.2919V91.4797C205.975 94.9376 196.985 103.928 193.527 127.48H189.339C185.881 103.967 176.891 94.9376 153.377 91.4797V87.2919C176.891 83.834 185.881 74.8435 189.339 51.3298H193.527C196.985 74.8435 206.014 83.834 229.527 87.2919Z" fill="#fff"/>
    <path fill-rule="evenodd" clip-rule="evenodd" d="M177.198 46.95V49.2168C164.481 51.0994 159.64 55.9405 157.757 68.6578H155.49C153.607 55.9405 148.766 51.0994 136.088 49.2168V46.95C148.805 45.0673 153.646 40.2263 155.49 27.5474H157.757C159.64 40.2647 164.481 45.1057 177.198 46.95Z" fill="#fff"/>
  </g>
  <defs>
    <linearGradient id="amulGrad" x1="212.813" y1="42.1478" x2="29.968" y2="224.993" gradientUnits="userSpaceOnUse">
      <stop stop-color="#218FFF"/><stop offset="1" stop-color="#FF1150"/>
    </linearGradient>
    <clipPath id="amulClip"><rect width="255" height="255" fill="#fff"/></clipPath>
  </defs>
</svg>
```

**Text-wordmark fallback** (if you prefer no SVG): render "Amul AI" in the brand
red, weight 700 — `<span style="color:#EC1C24;font-weight:700">Amul&nbsp;AI</span>`
— or use the gradient: a bold word with
`background:var(--brand-grad); -webkit-background-clip:text; color:transparent;`.

Favicon: an emoji or the inlined mark above both work; the FE uses `AmulLogo.svg`.

---

## 7. Caveats

- **Font not self-hostable via CDN in a locked-down portal.** Applied font is
  Google-hosted **Noto Sans**. Use the system fallback in `--font-family` above,
  or self-host Noto Sans / the bundled `src/assets/fonts/Montserrat-*.ttf`.
- **Primary color is coral `#F65151`, not the green in `global.css`.** The green
  (`#00BD52`) is a runtime-overridden default; use it only as an accent.
- **`--gray-*` vars in the FE `global.css` are undefined** (a latent bug); real
  neutrals come from Tailwind's default `gray` scale — reproduced in the token
  block so you don't inherit the bug.
- `config.json.theme.colors.userBubble` (`#bbf7d0` mint) is defined but the chat
  bubbles actually render `#FFE2E2` (pink). The tokens follow the rendered value.
- Dark mode exists in CSS but is only partially wired at runtime; light-only is
  the faithful primary experience.
