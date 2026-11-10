# MeloShelf design system · v0.2.0

The source of visual direction is the user-provided `Meloshelf.html`. v0.2.0 extends—not replaces—the supplied design language.

- Typography: Bricolage Grotesque for headings, Figtree for reading, system fallback when offline.
- Light theme: rose-pale background `#f7f1f0`, white surfaces, rose accent `#c0405a`.
- Dark theme: charcoal-plum background `#130f11`, dusk surfaces, pink accent `#f0768c`.
- Circular artist avatars and vinyl-inspired geometric album covers are preserved.
- New: workspace badge, task status banner, acoustic identification page, candid status labels, scanner dialog, paged track list and live match-review controls.
- The online recognition design is explicitly a **proposals** flow, not an automated album picker.
- Desktop: sidebar and 4-column summary. Under 860px, the navigation becomes a horizontal scrolling bar. At 420px, the layout has no body horizontal overflow in the rendered test.
- Theme toggle retained. Animations respect `prefers-reduced-motion`.

## Validation boundaries

`design-preview/index.html` includes sample data with the label "DESIGN PREVIEW" when opened standalone. When served by the Python application it reads live data through the authenticated loopback API. The Safety page shows placeholders labeled as *not enabled*, not fabricated checks.

Preview files: `design-preview/home-preview.png`, `identify-preview.png`, `mobile-preview.png`.
