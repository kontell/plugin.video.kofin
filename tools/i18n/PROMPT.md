# Translation brief

This is the contract every `tr/<locale>.json` is written against.
It exists because the sibling add-on's equivalent contract survived only in two commit messages, and could not be reconstructed without git archaeology.

Give this file, plus the worklist from `python3 tools/i18n/classify.py`, to whoever or whatever writes a locale.

## What you are translating

Kofin is a Jellyfin client for Kodi.
The strings are a settings UI (labels and help paragraphs), browse-menu entries, dialogs, and short toast notifications.
The reader is someone configuring a media player on their TV, not a developer.

Translate the English `msgid` into the target language and write it as the value in `tr/<locale>.json`, keyed by the `msgctxt`.
Never alter a `msgid` — it is the lookup key Kodi matches on, and a changed one simply never resolves.

## Register

Use the register Kodi itself uses in that language.
Most of Kodi's own locales address the user informally in the second person — German `du`/`dein`, French `vous` (French Kodi is formal), Spanish `tú`, Dutch `je`.
Match the locale's Kodi convention rather than importing English's.

Use the locale's own typographic quotes when a string quotes a phrase: German `„…“`, French `« … »`, Italian `« … »`, Dutch `'…'`, Japanese `「…」`.
The exception is the four ids that carry escaped quotes (see below), where the quote characters are part of a name being repeated back.

Prefer the term the locale's Kodi UI already uses for shared concepts — *library*, *playback*, *subtitles*, *resume*, *watched*.
A user reads these strings next to Kodi's own.

## Never translate

**Product and feature names**: Jellyfin, Kodi, Kofin, SyncPlay, Quick Connect, Rotten Tomatoes, OMDb, Emby, Trakt.

**Technical tokens**: codec names (AV1, VP9, H264, HEVC, AAC, MP3, Opus, FLAC, DTS, AC3, TrueHD), container and protocol names (HLS, M3U, fMP4, TS), HDR names (HDR10, HDR10+, Dolby Vision, HLG), and resolutions (720p, 1080p, 1440p, 2160p, 4K).

The ids that are *nothing but* such a token are in `PASSTHROUGH` in `classify.py` and never reach you.
The ones that reach you pair a token with real words — "Dolby Vision with HDR10 fallback" — where the token stays and the rest is translated.

**Kodi UI paths**: `Music → Playlists → Kofin` in `#30612` names folders Kodi draws in the user's own language; translate the folder words to whatever that locale's Kodi calls them, and keep `Kofin` and the `→`.

**Kodi setting names**: where a string points the user at one of Kodi's own settings — `Sync Playback to Display` in `#30552` — use the wording that locale's Kodi uses for it, not a literal translation of the English. The user has to find the setting by the name on their screen.

## Rules that break the add-on if you get them wrong

**Format specifiers.** 52 ids contain `%s` or `%d`. Kodi formats these with a plain `%` tuple — there is no positional `%1$s` form anywhere in kofin — so the specifiers must appear the same number of times **and in the same order** as the English.

Nine ids carry more than one, and are the ones that fail silently rather than loudly:

```
#30021  Connection OK — %s (%s)                       server, then detail
#30602  New: %s | Updates: %s | User data: %s         three counts, in that order
#30628  %s episode of %s added to library             count, then show name
#30629  %s episodes of %s added to library            count, then show name
#30716  Download %s item(s) (%s)?                     count, then size
#30771  Download %s item(s), about %s? %s free        count, size, free space
#30774  Delete %s download(s) of %s?                  count, then item name
#30806  Delete all %s download(s), freeing about %s?  count, then size
#30810  Needs %s, only %s free                        required, then available
```

If the target language wants a different word order, rewrite the sentence around the placeholders rather than reordering them.
`#30810` in a language that puts the available space first must still read `%s` = required, `%s` = available.

**Literal percent signs.** Three help strings contain a `%` that is not a placeholder — the `100%` in `#30478`, the `78%` in `#30667`, and the `80%` in `#30739`.
Leave them as a bare `%`. Do not double them to `%%`; these strings are never `%`-formatted.

**Kodi bbcode.** `#30015` is `Enter code [B]%s[/B] in the Jellyfin app or web interface.` The `[B]`/`[/B]` tags stay exactly as they are.

**Escaped quotes.** `#30505`, `#30506`, `#30607` and `#30794` contain `\"` in the PO source. In the JSON you write these as ordinary `"` characters (JSON-escaped as `\"`); the generator re-escapes them for the PO. What matters is that the quotes stay straight ASCII quotes in these four, because they quote a title or a setting name being repeated back verbatim.

**`#30794` must quote `#30618` word for word.** The caveat string names a settings label; if the two are translated differently, the caveat points at a setting that is not on screen under that name. `pocheck.py` enforces this. Translate the pair together.

## Length

Kodi's notification toast fits about 33 characters and scrolls the rest.
The English source was deliberately shortened for this, so a translation that runs long undoes the work.
Aim for the same length rather than the same literal wording — but only where the string really is a toast.

The budget applies to the sync and playback toasts (`#30404`, `#30406`, `#30409`, `#30410`, `#30414`, `#30420`), the download toasts (`#30708`, `#30717`, `#30720`, `#30806`, `#30807`, `#30810`), the new-content lines (`#30624`–`#30636`), and the Play Next / segment-skip toasts (`#30481`–`#30489`).

It does **not** apply to these, which sit in a panel or a dialog with room to be clear:

- `#30411`, `#30412`, `#30413`, `#30600` — the Library tab's read-only status labels (`sync/library.py`, `update_status_strings`). An earlier version of this brief wrongly called them toasts, and locales compressed them for no reason.
- Most of `#30560`–`#30585` — SyncPlay menu entries and dialog bodies. Only `#30574` is a toast.
- `#30808`, `#30809`, `#30812`, `#30814`, `#30815` — settings labels, help, and confirm-dialog bodies.

If you are unsure whether an id is a toast, check its call site in `lib/kofin/` rather than assuming from the number: a settings label that has been squeezed to 33 characters reads worse than the English for no benefit.

Settings **labels** sit in a narrow left column and should stay short; settings **help** has a panel of its own and can breathe.

## The five ids that take a name, not a count

In the new-content notifications, the singular and plural ids of each pair take
different kinds of argument (`lib/kofin/sync/newcontent.py`, `_count_line`).
The plural takes a number; the singular takes the item's title:

```
#30624  %s movie added to library         %s = "Dune"          -> Dune movie added to library
#30626  %s show added to library          %s = "Severance"
#30631  %s music video added to library   %s = the video title
#30633  %s added to music library         %s = the artist name
#30635  %s added to music library         %s = the album title
```

The category noun is deliberate: it is what distinguishes the Dune film from
the Dune album. Keep that information in the translation. If the target language
cannot form the English noun compound around a proper name, use whatever
construction it does use for the same job — a headline colon, an apposition, a
quoted title — as long as the reader still learns what kind of thing arrived.

`#30625`, `#30627`, `#30630`, `#30632`, `#30634` and `#30636` are the count
forms and behave normally.

## Duplicates

Several ids share the same English text on purpose — `#30633` and `#30635` are the artist and album lines of the new-content notification, kept separate so a translator can word them differently.
Translate each on its own terms. Do not assume equal English means equal target.

## Honesty

These are machine translations. Each generated file carries

```
# Note: machine-translated (LLM), pending native review.
```

in its header. That line stays until a native speaker has actually reviewed the file. It is what stops the next reader assuming this has been checked.
