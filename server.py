#!/usr/bin/env python3
"""BitChord Internet Archive FLAC addon.

Searches Internet Archive audio records, keeps only items whose title/creator
match the user's query, and returns direct URLs for files with a .flac suffix.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from flask import Flask, jsonify, redirect, request

app = Flask(__name__)

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.13.0",
    # Keep this declaration only if BitChord requires it for the addon protocol.
    # It is a capability declaration, not an authentication implementation.
    "bitchordAuth": True,
    "resources": ["search", "stream"],
    "settings": [
        {
            "key": "quality",
            "type": "select",
            "default": "lossless",
            "options": [
                {"label": "Lossless", "value": "lossless"},
                {"label": "Hi-Res (24-bit / 96 kHz)", "value": "hires"},
            ],
        },
        {
            "key": "source",
            "type": "select",
            "default": "all",
            "options": [
                {"label": "All FLAC Sources", "value": "all"},
                {"label": "Internet Archive", "value": "archive"},
                {"label": "Jamendo", "value": "jamendo"},
            ],
        },
        {
            "key": "genre",
            "type": "select",
            "default": "all",
            "options": [
                {"label": "All Music", "value": "all"},
                {"label": "Bollywood (2008–2026)", "value": "bollywood"},
            ],
        },
    ],
}

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"
JAMENDO_TRACKS = "https://api.jamendo.com/v3.0/tracks/"
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "").strip()
MIN_RELEASE_YEAR = 2008
MAX_RELEASE_YEAR = 2026
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Layz-Add-On/1.13 BitChord"})

# Keep the free Render instance responsive: don't inspect dozens of archive
# records for every keystroke/search.
SEARCH_ROWS = 40
MAX_CANDIDATES = 12
MAX_RESULTS = 10
REQUEST_TIMEOUT = (3, 7)
# Only items with explicitly identified open/public-domain licensing are accepted.
ALLOWED_LICENSE_HINTS = (
    "creativecommons.org/publicdomain",
    "creativecommons.org/licenses/by/",
    "creativecommons.org/licenses/by-sa/",
    "creativecommons.org/licenses/by-nc/",
    "creativecommons.org/licenses/by-nd/",
    "creativecommons.org/licenses/by-nc-sa/",
    "creativecommons.org/licenses/by-nc-nd/",
)
STOP_WORDS = {"a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "by", "from"}


def num(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def first(data, *keys):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def tokens(value):
    words = normalized(value).split()
    useful = [word for word in words if word not in STOP_WORDS]
    return set(useful or words)


def as_text(value):
    if isinstance(value, list):
        return " ".join(str(part) for part in value if part)
    return str(value or "")


def license_is_open(doc):
    """Accept only explicit public-domain or Creative Commons license hints."""
    values = []
    for key in ("licenseurl", "license", "rights", "copyright"):
        value = doc.get(key)
        if value:
            values.append(as_text(value).lower())
    combined = " ".join(values)
    return any(hint in combined for hint in ALLOWED_LICENSE_HINTS) or any(
        phrase in combined for phrase in ("public domain", "creativecommons", "cc0")
    )


def relevance_score(query, doc):
    """Return 0 for unrelated records; otherwise score title/creator relevance.

    Exact phrase matches are strongest. For multi-word queries, require a
    substantial token match in the title or creator. This prevents arbitrary
    FLAC podcasts from being returned merely because they have a creator.
    """
    q_norm = normalized(query)
    title_norm = normalized(doc.get("title", ""))
    creator_norm = normalized(as_text(doc.get("creator")))
    album_norm = normalized(doc.get("album", ""))

    if not q_norm:
        return 0

    if q_norm == title_norm:
        return 200
    if f" {q_norm} " in f" {title_norm} ":
        return 150
    if q_norm == creator_norm:
        return 140
    if f" {q_norm} " in f" {creator_norm} ":
        return 125

    q_tokens = tokens(query)
    if not q_tokens:
        return 0

    title_tokens = tokens(title_norm)
    creator_tokens = tokens(creator_norm)
    album_tokens = tokens(album_norm)

    title_hits = len(q_tokens & title_tokens) / len(q_tokens)
    creator_hits = len(q_tokens & creator_tokens) / len(q_tokens)
    combined_hits = len(q_tokens & (title_tokens | creator_tokens)) / len(q_tokens)

    # A one-word query must match that word. Multiword queries must match at
    # least 2/3 of meaningful query terms in the title/creator, or 80% across
    # both fields (e.g. artist + song title entered together).
    if len(q_tokens) == 1:
        if max(title_hits, creator_hits) < 1.0:
            return 0
    elif max(title_hits, creator_hits) < (2 / 3) and combined_hits < 0.8:
        return 0

    score = title_hits * 130 + creator_hits * 65 + combined_hits * 15
    if album_tokens and q_tokens & album_tokens:
        score += 5
    return score


def flac_info(url):
    """Read FLAC STREAMINFO from a small byte-range request, if available."""
    response = None
    try:
        response = SESSION.get(
            url,
            headers={"Range": "bytes=0-63"},
            timeout=(3, 3),
            stream=True,
        )
        response.raise_for_status()
        data = response.raw.read(64)
        if len(data) < 26 or data[:4] != b"fLaC":
            return None, None
        block_type = data[4] & 0x7F
        block_length = int.from_bytes(data[5:8], "big")
        if block_type != 0 or block_length < 34:
            return None, None
        packed = int.from_bytes(data[18:26], "big")
        sample_rate = packed >> 44
        bit_depth = ((packed >> 36) & 0x1F) + 1
        if not 1000 <= sample_rate <= 768000:
            sample_rate = None
        if not 4 <= bit_depth <= 32:
            bit_depth = None
        return sample_rate, bit_depth
    except requests.RequestException:
        return None, None
    finally:
        if response is not None:
            response.close()


def quality(_sample_rate, _bit_depth):
    return "LOSSLESS"


def is_hires(sample_rate, bit_depth):
    """True only when both Hi-Res thresholds are verified from metadata/header."""
    return (
        bit_depth is not None
        and bit_depth >= 24
        and sample_rate is not None
        and sample_rate >= 96000
    )


def quality_label(sample_rate, bit_depth):
    details = []
    if bit_depth is not None:
        details.append(f"{bit_depth}-bit")
    if sample_rate is not None:
        details.append(f"{sample_rate / 1000:g} kHz")
    hi_res = is_hires(sample_rate, bit_depth)
    label = "Hi-Res Lossless" if hi_res else "Lossless"
    return label + (f" · {' / '.join(details)}" if details else "")


def inspect(identifier, item, probe_header=True):
    name = str(item.get("name", ""))
    url = (
        f"https://archive.org/download/{quote(identifier, safe='')}/"
        f"{quote(name, safe='')}"
    )
    sample_rate = num(first(item, "sample_rate", "samplerate", "sampleRate"))
    bit_depth = num(first(item, "bit_depth", "bitdepth", "bitDepth"))
    if probe_header and (sample_rate is None or bit_depth is None):
        detected_sr, detected_bd = flac_info(url)
        sample_rate = sample_rate if sample_rate is not None else detected_sr
        bit_depth = bit_depth if bit_depth is not None else detected_bd
    return {
        "url": url,
        "filename": name,
        "size": num(item.get("size")),
        "length": num(item.get("length")),
        "bitrate": num(first(item, "bitrate", "bit_rate", "bitRate")),
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "quality": quality(sample_rate, bit_depth),
    }


def get_flac(identifier, probe_header=True):
    try:
        response = SESSION.get(
            IA_METADATA.format(quote(identifier, safe="")),
            timeout=(3, 7),
        )
        response.raise_for_status()
        data = response.json()

        metadata = data.get("metadata", {})
        if not license_is_open(metadata):
            print(
                f"[FLAC DEBUG] {identifier}: rejected by licence filter; "
                f"license={metadata.get('license')}; "
                f"licenseurl={metadata.get('licenseurl')}; "
                f"rights={metadata.get('rights')}; "
                f"copyright={metadata.get('copyright')}"
            )
            return None

        candidates = []
        for item in data.get("files", []):
            name = str(item.get("name", ""))
            if name.lower().endswith(".flac"):
                candidates.append((name, item))

        if not candidates:
            print(f"[FLAC DEBUG] {identifier}: no FLAC files found")
            return None

        candidates.sort(
            key=lambda pair: num(pair[1].get("size")) or 0,
            reverse=True,
        )
        candidates = candidates[:2]

        inspected = []
        for name, item in candidates:
            try:
                # Read STREAMINFO for each of the two largest FLAC candidates
                # so Hi-Res mode can select a smaller 24-bit/96 kHz alternative.
                result = inspect(identifier, item, probe_header=probe_header)
                if result:
                    inspected.append(result)
                else:
                    print(f"[FLAC DEBUG] {identifier}: inspect returned empty for {name}")
            except Exception as exc:
                print(f"[FLAC DEBUG] {identifier}: inspect failed for {name}: {exc}")

        if not inspected:
            print(f"[FLAC DEBUG] {identifier}: no inspected FLAC passed")
            return None

        inspected.sort(
            key=lambda item: (
                int((item.get("bitDepth") or 0) > 16),
                item.get("bitDepth") or 0,
                item.get("sampleRate") or 0,
                item.get("size") or 0,
            ),
            reverse=True,
        )
        return inspected[0]

    except Exception as exc:
        print(f"[FLAC DEBUG] {identifier}: metadata/request failure: {exc}")
        return None


def make_track(doc, flac):
    identifier = str(doc["identifier"])
    title = str(doc.get("title") or flac["filename"])
    creator = as_text(doc.get("creator")) or "Internet Archive"
    return {
        "id": identifier,
        "title": title,
        "artist": creator,
        "album": str(doc.get("album") or ""),
        "year": num(doc.get("year")),
        "source": "Internet Archive",
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "streamURL": flac["url"],
    }


def jamendo_license_is_allowed(track):
    """Accept only explicit, recognised Creative Commons licences."""
    license_url = str(track.get("license_ccurl") or "").strip().lower()
    allowed = (
        "creativecommons.org/publicdomain/zero/",
        "creativecommons.org/licenses/by/",
        "creativecommons.org/licenses/by-sa/",
        "creativecommons.org/licenses/by-nc/",
        "creativecommons.org/licenses/by-nd/",
        "creativecommons.org/licenses/by-nc-sa/",
        "creativecommons.org/licenses/by-nc-nd/",
    )
    return any(marker in license_url for marker in allowed)


def inspect_jamendo_track(item, year_from, year_to, probe_header=True):
    """Build a BitChord track only from a licensed Jamendo FLAC stream."""
    if not jamendo_license_is_allowed(item):
        return None, "licence"

    releasedate = str(item.get("releasedate") or "")
    year_match = re.match(r"^(\d{4})", releasedate)
    release_year = int(year_match.group(1)) if year_match else None
    if release_year is None or not year_from <= release_year <= year_to:
        return None, "year"

    stream_url = str(item.get("audio") or "").strip()
    if not stream_url.startswith("https://"):
        return None, "stream"

    sample_rate = None
    bit_depth = None
    if probe_header:
        sample_rate, bit_depth = flac_info(stream_url)

    track_id = str(item.get("id") or "").strip()
    if not track_id.isdigit():
        return None, "id"

    artist = str(item.get("artist_name") or "Unknown artist")
    title = str(item.get("name") or "Untitled")
    track_page = str(item.get("shareurl") or f"https://www.jamendo.com/track/{track_id}")
    artist_id = str(item.get("artist_id") or "").strip()
    artist_page = (
        str(item.get("artist_shareurl") or f"https://www.jamendo.com/artist/{artist_id}")
        if artist_id else "https://www.jamendo.com/"
    )
    album_image = str(item.get("album_image") or item.get("image") or "")

    return {
        "id": f"jamendo-{track_id}",
        "title": title,
        "artist": artist,
        "album": str(item.get("album_name") or ""),
        "year": release_year,
        "source": "Jamendo",
        "duration": num(item.get("duration")),
        "artworkURL": album_image,
        "format": "flac",
        "audioQuality": "LOSSLESS",
        "quality": quality_label(sample_rate, bit_depth),
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "bitrate": None,
        "streamURL": stream_url,
        "trackURL": track_page,
        "artistURL": artist_page,
        "licenseURL": str(item.get("license_ccurl") or ""),
        "attribution": f"{artist} / Jamendo",
    }, None


def jamendo_api_request(params):
    if not JAMENDO_CLIENT_ID:
        return None, "JAMENDO_CLIENT_ID is not configured"
    request_params = dict(params)
    request_params["client_id"] = JAMENDO_CLIENT_ID
    request_params["format"] = "json"
    try:
        response = SESSION.get(JAMENDO_TRACKS, params=request_params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[JAMENDO DEBUG] API request failed: {exc}")
        return None, str(exc)

    headers = payload.get("headers", {})
    if headers.get("status") not in (None, "success"):
        message = str(headers.get("error_message") or "Jamendo API returned an error")
        print(f"[JAMENDO DEBUG] API error: {message}")
        return None, message
    return payload.get("results", []), None


def jamendo_search(query, limit, year_from, year_to, probe_header=True):
    """Search Jamendo's documented FLAC stream field, retaining CC-licensed tracks."""
    stats = {
        "configured": bool(JAMENDO_CLIENT_ID),
        "apiCandidates": 0,
        "licenceRejected": 0,
        "yearRejected": 0,
        "missingStream": 0,
        "flacMatches": 0,
        "hiResMatches": 0,
        "error": None,
    }
    if not JAMENDO_CLIENT_ID:
        return [], stats

    params = {
        "search": query,
        "limit": min(max(limit * 2, 12), 30),
        "type": "single albumtrack",
        "datebetween": f"{year_from}-01-01_{year_to}-12-31",
        "audioformat": "flac",
        "include": "licenses",
        "imagesize": 500,
    }
    items, error = jamendo_api_request(params)
    if items is None:
        stats["error"] = error
        return [], stats

    stats["apiCandidates"] = len(items)
    eligible = []
    for item in items:
        if not jamendo_license_is_allowed(item):
            stats["licenceRejected"] += 1
            continue
        releasedate = str(item.get("releasedate") or "")
        year_match = re.match(r"^(\d{4})", releasedate)
        release_year = int(year_match.group(1)) if year_match else None
        if release_year is None or not year_from <= release_year <= year_to:
            stats["yearRejected"] += 1
            continue
        if not str(item.get("audio") or "").startswith("https://"):
            stats["missingStream"] += 1
            continue
        eligible.append(item)

    results = []
    # Probe a few FLAC headers concurrently to keep Render response times bounded.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(inspect_jamendo_track, item, year_from, year_to, probe_header)
            for item in eligible
        ]
        for future in as_completed(futures):
            try:
                checked, reason = future.result()
            except Exception as exc:
                print(f"[JAMENDO DEBUG] Track inspection failed: {exc}")
                continue
            if checked is None:
                if reason == "stream":
                    stats["missingStream"] += 1
                continue
            stats["flacMatches"] += 1
            if is_hires(checked.get("sampleRate"), checked.get("bitDepth")):
                stats["hiResMatches"] += 1
            results.append(checked)
    return results, stats


def get_jamendo_flac(track_id, probe_header=True):
    """Resolve a Jamendo track ID through the official tracks API, not a guessed URL."""
    if not JAMENDO_CLIENT_ID or not str(track_id).isdigit():
        return None
    params = {
        "id": str(track_id),
        "limit": 1,
        "type": "single albumtrack",
        "datebetween": f"{MIN_RELEASE_YEAR}-01-01_{MAX_RELEASE_YEAR}-12-31",
        "audioformat": "flac",
        "include": "licenses",
        "imagesize": 500,
    }
    items, error = jamendo_api_request(params)
    if not items:
        if error:
            print(f"[JAMENDO DEBUG] Track lookup failed for {track_id}: {error}")
        return None
    track, reason = inspect_jamendo_track(
        items[0], MIN_RELEASE_YEAR, MAX_RELEASE_YEAR, probe_header
    )
    if track is None:
        print(f"[JAMENDO DEBUG] Track {track_id} rejected: {reason}")
    return track


def search_docs(query, rows, year_from=MIN_RELEASE_YEAR, year_to=MAX_RELEASE_YEAR):
    fields = ["identifier", "title", "creator", "album", "year", "subject", "licenseurl", "rights"]
    safe_query = query.replace('"', " ").strip()
    year_filter = f"year:[{year_from} TO {year_to}]"
    search_queries = [
        f'(mediatype:audio) AND {year_filter} AND title:"{safe_query}"',
        f'(mediatype:audio) AND {year_filter} AND ({safe_query})',
    ]
    merged = {}
    for ia_query in search_queries:
        response = SESSION.get(
            IA_SEARCH,
            params={
                "q": ia_query,
                "fl[]": fields,
                "rows": rows,
                "page": 1,
                "output": "json",
            },
            timeout=(3, 6),
        )
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
        for doc in docs:
            identifier = doc.get("identifier")
            year = num(doc.get("year"))
            if identifier and year is not None and year_from <= year <= year_to:
                merged.setdefault(str(identifier), doc)
        if len(merged) >= min(rows, 12):
            break
    return list(merged.values())


def build_genre_query(query, genre):
    """Expand a source search for a selected catalogue genre without changing the user's query."""
    clean_query = (query or "").strip()
    if genre == "bollywood" and not re.search(r"\bbollywood\b", clean_query, re.IGNORECASE):
        return f"Bollywood {clean_query}".strip()
    return clean_query


def do_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})

    try:
        limit = min(max(int(request.args.get("limit", "10")), 1), MAX_RESULTS)
        year_from = int(request.args.get("year_from", str(MIN_RELEASE_YEAR)))
        year_to = int(request.args.get("year_to", str(MAX_RELEASE_YEAR)))
    except ValueError:
        return jsonify({"tracks": [], "error": "limit, year_from and year_to must be integers"}), 400

    year_from = max(MIN_RELEASE_YEAR, min(year_from, MAX_RELEASE_YEAR))
    year_to = max(MIN_RELEASE_YEAR, min(year_to, MAX_RELEASE_YEAR))
    if year_from > year_to:
        return jsonify({"tracks": [], "error": "year_from must not exceed year_to"}), 400

    quality_mode = (request.args.get("quality") or "lossless").strip().lower()
    if quality_mode in ("hi-res", "hi_res", "high-res"):
        quality_mode = "hires"
    if quality_mode not in ("lossless", "high", "low", "hires"):
        quality_mode = "lossless"

    genre_mode = (request.args.get("genre") or "all").strip().lower()
    if genre_mode not in ("all", "bollywood"):
        return jsonify({"tracks": [], "error": "genre must be all or bollywood"}), 400
    search_query = build_genre_query(query, genre_mode)

    source_mode = (request.args.get("source") or "all").strip().lower()
    if source_mode not in ("all", "archive", "internet-archive", "internet_archive", "jamendo"):
        return jsonify({"tracks": [], "error": "source must be all, archive or jamendo"}), 400
    use_archive = source_mode in ("all", "archive", "internet-archive", "internet_archive")
    use_jamendo = source_mode in ("all", "jamendo")

    debug_enabled = request.args.get("debug") == "1"
    debug = {
        "query": query,
        "sourceMode": source_mode,
        "qualityMode": quality_mode,
        "genreMode": genre_mode,
        "sourceSearchQuery": search_query,
        "yearFrom": year_from,
        "yearTo": year_to,
        "archiveDocs": 0,
        "relevantDocs": 0,
        "candidatesChecked": 0,
        "archiveFlacMatches": 0,
        "jamendoConfigured": bool(JAMENDO_CLIENT_ID),
        "jamendoApiCandidates": 0,
        "jamendoLicenceRejected": 0,
        "jamendoYearRejected": 0,
        "jamendoMissingStream": 0,
        "jamendoFlacMatches": 0,
        "jamendoHiResMatches": 0,
        "flacMatches": 0,
        "hiResMatches": 0,
        "qualityMatches": 0,
        "qualityFilteredOut": 0,
        "returnedTracks": 0,
        "duplicateTracksSkipped": 0,
        "checkedIdentifiers": [],
        "matchedIdentifiers": [],
    }

    combined = []
    if use_archive:
        try:
            docs = search_docs(search_query, SEARCH_ROWS, year_from, year_to)
        except (requests.RequestException, ValueError) as exc:
            print(f"[ARCHIVE DEBUG] Search request failed: {exc}")
            docs = []
            debug["archiveError"] = str(exc)
        ranked = [
            (relevance_score(query, doc), doc)
            for doc in docs
        ]
        ranked = [(score, doc) for score, doc in ranked if score > 0]
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        debug["archiveDocs"] = len(docs)
        debug["relevantDocs"] = len(ranked)

        candidates = ranked[:MAX_CANDIDATES]
        debug["candidatesChecked"] = len(candidates)
        debug["checkedIdentifiers"] = [
            str(doc.get("identifier", "")) for _score, doc in candidates
        ]
        archive_matches = []
        if candidates:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {
                    pool.submit(get_flac, str(doc["identifier"]), True): (score, doc)
                    for score, doc in candidates
                }
                for future in as_completed(futures):
                    score, doc = futures[future]
                    try:
                        flac = future.result()
                    except Exception as exc:
                        print(f"[ARCHIVE DEBUG] Candidate inspection failed: {exc}")
                        continue
                    if flac:
                        archive_matches.append((score, make_track(doc, flac)))
        debug["archiveFlacMatches"] = len(archive_matches)
        combined.extend(archive_matches)

    if use_jamendo:
        jamendo_tracks, jamendo_stats = jamendo_search(
            search_query, limit, year_from, year_to, probe_header=True
        )
        debug["jamendoConfigured"] = jamendo_stats["configured"]
        debug["jamendoApiCandidates"] = jamendo_stats["apiCandidates"]
        debug["jamendoLicenceRejected"] = jamendo_stats["licenceRejected"]
        debug["jamendoYearRejected"] = jamendo_stats["yearRejected"]
        debug["jamendoMissingStream"] = jamendo_stats["missingStream"]
        debug["jamendoFlacMatches"] = jamendo_stats["flacMatches"]
        debug["jamendoHiResMatches"] = jamendo_stats["hiResMatches"]
        if jamendo_stats.get("error"):
            debug["jamendoError"] = jamendo_stats["error"]
        for track in jamendo_tracks:
            doc_for_score = {
                "title": track.get("title", ""),
                "creator": track.get("artist", ""),
                "album": track.get("album", ""),
            }
            score = relevance_score(query, doc_for_score) or 50
            combined.append((score, track))

    raw_flac_matches = len(combined)
    raw_hires_matches = sum(
        1 for _score, track in combined
        if is_hires(track.get("sampleRate"), track.get("bitDepth"))
    )
    if quality_mode == "hires":
        combined = [
            entry for entry in combined
            if is_hires(entry[1].get("sampleRate"), entry[1].get("bitDepth"))
        ]

    debug["flacMatches"] = raw_flac_matches
    debug["hiResMatches"] = raw_hires_matches
    debug["qualityMatches"] = len(combined)
    debug["qualityFilteredOut"] = raw_flac_matches - len(combined)

    combined.sort(
        key=lambda entry: (
            int((entry[1].get("bitDepth") or 0) >= 24),
            entry[1].get("bitDepth") or 0,
            entry[1].get("sampleRate") or 0,
            entry[0],
        ),
        reverse=True,
    )

    tracks = []
    seen_recordings = set()
    duplicate_tracks_skipped = 0
    for _score, track in combined:
        title_key = normalized(track.get("title", ""))
        artist_key = normalized(track.get("artist", ""))
        recording_key = (
            (title_key, artist_key)
            if title_key and artist_key
            else (str(track.get("id", "")),)
        )
        if recording_key in seen_recordings:
            duplicate_tracks_skipped += 1
            continue
        seen_recordings.add(recording_key)
        tracks.append(track)
        if len(tracks) >= limit:
            break

    debug["returnedTracks"] = len(tracks)
    debug["duplicateTracksSkipped"] = duplicate_tracks_skipped
    debug["tracksWithBitDepth"] = sum(
        1 for track in tracks if track.get("bitDepth") is not None
    )
    debug["tracksWithSampleRate"] = sum(
        1 for track in tracks if track.get("sampleRate") is not None
    )
    debug["hiResTracks"] = sum(
        1 for track in tracks
        if is_hires(track.get("sampleRate"), track.get("bitDepth"))
    )
    debug["matchedIdentifiers"] = [str(track.get("id", "")) for _score, track in combined]

    payload = {"tracks": tracks}
    if debug_enabled:
        payload["debug"] = debug
    return jsonify(payload)


def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400

    if identifier.startswith("jamendo-"):
        track_id = identifier[len("jamendo-"):]
        flac = get_jamendo_flac(track_id, probe_header=True)
        if not flac:
            return jsonify({"error": "No licensed Jamendo FLAC stream found for this item"}), 404
        return jsonify({
            "url": flac["streamURL"],
            "format": "flac",
            "quality": flac.get("quality", "LOSSLESS"),
            "audioQuality": flac.get("audioQuality", "LOSSLESS"),
            "codec": "flac",
            "container": "flac",
            "manifest": "none",
            "encrypted": False,
            "sampleRate": flac.get("sampleRate"),
            "bitDepth": flac.get("bitDepth"),
            "bitrate": flac.get("bitrate"),
            "audioMode": "stereo",
            "source": "Jamendo",
            "trackURL": flac.get("trackURL"),
            "licenseURL": flac.get("licenseURL"),
        })

    try:
        flac = get_flac(identifier)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502
    if not flac:
        return jsonify({"error": "No FLAC file found for this item"}), 404
    return jsonify({
        "url": flac["url"],
        "format": "flac",
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "audioMode": "stereo",
    })


@app.get("/youtube-music-search")
def youtube_music_search():
    """Open the official YouTube Music catalogue for broad Bollywood discovery."""
    query = (request.args.get("q") or "").strip()
    genre = (request.args.get("genre") or "bollywood").strip().lower()
    if not query:
        query = "Bollywood"
    elif genre == "bollywood" and not re.search(r"\bbollywood\b", query, re.IGNORECASE):
        query = f"Bollywood {query}"
    return redirect(
        "https://music.youtube.com/search?q=" + quote(query, safe=""),
        code=302,
    )


@app.get("/")
def root():
    return jsonify(MANIFEST)


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({"ok": True, "name": MANIFEST["name"], "version": MANIFEST["version"]})


@app.get("/search")
def search():
    return do_search()


@app.get("/stream/<path:identifier>")
def stream(identifier):
    return do_stream(identifier)


@app.get("/stream")
def stream_query():
    return do_stream(request.args.get("id"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
