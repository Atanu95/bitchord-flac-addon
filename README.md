# Layz Add On — BitChord FLAC Sources

Current version: **1.15.3**

A small Python/Flask HTTP addon for BitChord. It searches Internet Archive and, when configured, Jamendo for licensed FLAC streams.

## Audio quality and catalogue scope

- Searches release years **2008–2026** by default for All Music and Bollywood. The **English (All Genres)** preset searches **2000–2026** to include early-2000s artists. The API accepts narrower `year_from` and `year_to` values within the supported 2000–2026 range.
- Returns actual FLAC streams only. It does not convert MP3/AAC/Opus into FLAC.
- **Lossless FLAC (any resolution)** returns eligible FLAC streams. Both quality modes keep all eligible FLAC results; Best Available ranks verified Hi-Res first.
- **Best Available** prefers files with verified FLAC STREAMINFO of at least **24-bit and 96 kHz**, but falls back to ordinary FLAC when no verified Hi-Res result is available. It never converts a lossy file into FLAC.
- Internet Archive items must pass the addon's explicit public-domain/Creative Commons licence check.
- Jamendo tracks must have an explicit recognised Creative Commons licence URL and a release date inside the requested year range.

This cannot guarantee every commercial song from 2008–2026. It can only return music present in these catalogues and available under the relevant licence.

## Sources

### Internet Archive

The addon searches Internet Archive audio metadata and only returns items with FLAC files that pass the configured public-domain/Creative Commons licence check.

### Jamendo (optional)

Jamendo's official API supports the FLAC stream format in the `audio` field, track searches, release-date filtering, and Creative Commons licence metadata.

To enable Jamendo, create a Read Only application in the Jamendo Developer Portal and add its Client ID to your host's environment:

```text
JAMENDO_CLIENT_ID=your_application_client_id
```

For Render:
1. Open the `bitchord-flac-addon` web service.
2. Open **Environment**.
3. Add `JAMENDO_CLIENT_ID` with your application's Client ID.
4. Save changes and allow Render to redeploy.

Do not put the Client ID or Client Secret into source code, and do not commit credentials. If a credential was exposed publicly, replace it in the Jamendo Developer Portal before using it.

Jamendo use in this application is intended for **non-commercial use** under Jamendo's API terms. The addon returns the Jamendo track page, artist page, and licence URL for attribution. Respect each track's licence, credit the artist and Jamendo, and link back to the track page. Do not use the Jamendo API integration for offline downloads or persistent media caching.

Jamendo is optional: if `JAMENDO_CLIENT_ID` is not configured, the Internet Archive source continues to work.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export JAMENDO_CLIENT_ID="your_application_client_id"
python server.py
```

Test:
- `http://127.0.0.1:8080/manifest.json`
- `http://127.0.0.1:8080/health`
- `http://127.0.0.1:8080/search?q=electronic&source=all&quality=lossless&debug=1`
- `http://127.0.0.1:8080/search?q=ambient&source=jamendo&quality=hires&debug=1`
- `http://127.0.0.1:8080/stream?id=jamendo-TRACK_ID`

## Search API

`GET /search?q=<query>&limit=10&source=all&quality=lossless&year_from=2008&year_to=2026&debug=1`

Parameters:
- `q`: required search text.
- `limit`: maximum number of tracks to return (1–10).
- `source`: `all` (default), `archive`, or `jamendo`.
- `quality`: `lossless` (default) or `hires`.
- `year_from`, `year_to`: release-year bounds, limited to 2008–2026.
- `debug=1`: includes source counts, licence rejections, FLAC matches and quality diagnostics.

Jamendo search results use IDs prefixed with `jamendo-`. The stream endpoint resolves these IDs through the Jamendo tracks API and rechecks licence/date eligibility before returning the stream.

## Deployment

Use:

```bash
gunicorn --bind 0.0.0.0:$PORT server:app
```

## BitChord

Add the addon root URL or `manifest.json` in BitChord's custom source settings:

```text
https://bitchord-flac-addon-d9sw.onrender.com/
```

## Important

This is a real FLAC-source project, not a lossy-to-FLAC converter. YouTube Music's standard audio streams are not turned into lossless or Hi-Res files by this addon.

Jamendo API use is subject to Jamendo's API terms and each track's Creative Commons licence. Internet Archive items remain subject to their individual rights metadata.

## Bollywood discovery (2008–2026)

The manifest includes **Bollywood (2008–2026)** and **English (All Genres, 2000–2026)** genre presets. English searches support artist names such as Pitbull, Akon, Sean Paul, and The Weeknd. The addon also corrects the common “Weekand” spelling to “The Weeknd”. It first searches with the `Bollywood` genre term; if no relevant tracks are found, it retries using the original artist/song query. Internet Archive also receives both query forms. Jamendo results are deduplicated and must match the requested title or artist. Results still come only from configured FLAC catalogues and must pass their source-specific licence checks; this is not a complete commercial Bollywood catalogue. English mode searches the same licensed FLAC catalogues, not commercial-label catalogues, so it cannot guarantee mainstream recordings. Searches for Pitbull, Akon, Sean Paul, and The Weeknd require the credited artist name to match (including known Pitbull/Akon name variants and featuring credits), preventing unrelated artists from being returned just because the search word appears in a title.

For broader discovery of commercial Bollywood music, use the official YouTube Music search redirect:

```text
GET /youtube-music-search?q=<song-or-artist>&genre=bollywood
```

This endpoint redirects to YouTube Music's official search page. It does not expose or download YouTube audio and is not a lossless/FLAC stream. YouTube Music playback must happen in the official YouTube Music app/site.

## English-language artist search

Choose **English (All Genres, 2000–2026)** and search an artist or track, for example `Pitbull`, `Akon`, `Sean Paul`, or `The Weeknd`. The preset searches the artist/title query first, then tries an English-qualified variant. Results remain limited to eligible FLAC files in Internet Archive and Jamendo and their licence checks. Mainstream commercial recordings may not be available in these free catalogues.

## Amazon Music subscriber search

For commercial Bollywood catalogue discovery, the addon provides an external redirect to Amazon Music India's official search page:

```text
GET /amazon-music-search?q=<song-or-artist>&genre=bollywood
```

This opens Amazon Music's own catalogue for playback under the user's Amazon Music subscription. It is a discovery shortcut only: Amazon Music streaming is not exposed as a reusable FLAC URL, and its protected Ultra HD playback is not routed through this addon's `/stream` endpoint.

The Amazon shortcut is deliberately not listed as a FLAC source in the manifest. Internet Archive and Jamendo remain the direct FLAC sources, with their existing licence checks.
