# Revive Restore Resell — GitHub Pages Deploy

Internal item-dashboard host. Static HTML rendered per-SKU, served via GitHub Pages.

## Repo layout

```
Revive-Restore-Resell/
├── index.html              # Landing
├── 404.html                # Fallback
├── robots.txt              # Blocks all crawlers (incl. GPTBot, ClaudeBot, Google-Extended, etc.)
├── items/
│   └── Tops-1331.html      # One file per SKU
└── photos/
    └── Tops-1331/          # One folder per SKU
        ├── Tops-1331-garment.jpg
        ├── Tops-1331-brand-tag.jpg
        ├── Tops-1331-care-tag.jpg
        ├── Tops-1331-detail.jpg
        └── Tops-1331-archive-01..05.jpg
```

URL pattern: `https://vmonagon.github.io/Revive-Restore-Resell/items/<SKU>.html`

## Deploy steps (one-time)

1. Clone the repo locally: `git clone git@github.com:vmonagon/Revive-Restore-Resell.git`
2. Copy this folder's contents into the clone (root level).
3. `git add . && git commit -m "Phase A proof: Tops-1331" && git push`
4. Repo → Settings → Pages:
   - Source: Deploy from a branch
   - Branch: `main` / `/ (root)`
   - Save
5. Wait 1–2 minutes for first build. URL goes live.
6. Confirm `robots.txt` is served: `https://vmonagon.github.io/Revive-Restore-Resell/robots.txt`

## Privacy notes

- **Pages on Pro plan is publicly accessible** even with a private source repo. `robots.txt` blocks well-behaved crawlers but does NOT block direct URL access. Anyone who guesses or is given a SKU URL can view it.
- For Phase B: consider obfuscating SKU paths with a token (`items/Tops-1331-<token>.html`) or stripping COG/walk-away/consignor data from the published HTML.

## Phase B requirements

This Phase A proof is one file, one SKU, photos in repo. For Phase B (5K active SKUs):

1. **Photo hosting separation.** 5K × 9 × ~4 MB = ~180 GB. GitHub repo soft cap is 5 GB. Photos must move to Cloudflare R2 or Bunny.net before this scales. Update HTML photo paths to absolute CDN URLs.
2. **Commit/push automation.** The render skill needs git auth to push without operator action. Options: GitHub App with limited scope, a deploy bot's PAT in a vaulted env, or run the render skill on a self-hosted runner that has push rights.
3. **Confirmation_Merge_Agent.** Handcuff JSONs land in `_Shared/Handcuffs/Confirmations/inbox/`. Merge agent reads, applies to source phase2.json `_confirmations` block, archives.

## Versions

- v0.5 (gh-pages) · 2026-05-10 · Tops-1331 only
