# Interface languages

One JSON file per language, with the same keys as `en.json`:

| Code | Language |
|------|----------|
| `en` | English (source text; matches the words in `index.html`) |
| `fr` | Français |
| `es` | Español |
| `pt` | Português |
| `ja` | 日本語 |
| `zh-Hans` | 简体中文 |
| `ko` | 한국어 |
| `hi` | हिन्दी |

## Review status

The English catalog is the source. The other seven were drafted as part of development and **have not been reviewed
by native speakers**. Treat them as a working first version: wording, tone and register may need correction, and
corrections are welcome. Tests check structure only (same keys, same placeholders, no blanks, bounded length); they
cannot judge translation quality.

## Changing text

1. Edit the page text and `en.json` together (a test fails if they differ).
2. Update the other files; a missing key is a test failure, and at run time the page falls back to English.
3. Rebuild the embedded copy: `python -I scripts/build_assets.py ... --i18n-dir src/meloshelf/presentation/web/i18n`
   (the page embeds all catalogs so it works offline; a test fails if the embedded copy is stale).
