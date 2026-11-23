# Layz Add On — BitChord FLAC Sources

A small Python/Flask HTTP addon for BitChord. It searches Internet Archive and, when configured, Jamendo for licensed FLAC streams.

## Audio quality and catalogue scope

- Searches release years **2008–2026** by default. The API also accepts narrower `year_from` and `year_to` values within that range.
- Returns actual FLAC streams only. It does not convert MP3/AAC/Opus into FLAC.
- **Lossless** returns eligible FLAC streams.
- **Hi-Res** is strict: a file must have verified FLAC STREAMINFO of at least **24-bit and 96 kHz**. Unknown metadata is not labelled Hi-Res.
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
