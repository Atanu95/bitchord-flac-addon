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
from flask import Flask, jsonify, request

app = Flask(__name__)

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.9.0",
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
                {"label": "High", "value": "high"},
                {"label": "Low", "value": "low"},
            ],
        }
    ],
}

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Layz-Add-On/1.9 BitChord"})

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


def quality_label(sample_rate, bit_depth):
    details = []
    if bit_depth is not None:
        details.append(f"{bit_depth}-bit")
    if sample_rate is not None:
        details.append(f"{sample_rate / 1000:g} kHz")
    hi_res = (bit_depth is not None and bit_depth > 16) or (
        sample_rate is not None and sample_rate > 48000
    )
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
    response = SESSION.get(
        IA_METADATA.format(quote(identifier, safe="")),
        timeout=(3, 7),
    )
    response.raise_for_status()
    data = response.json()
    if not license_is_open(data.get("metadata", {})):
        return None

    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        if name.lower().endswith(".flac"):
            candidates.append((name, item))
    if not candidates:
        return None

    candidates.sort(key=lambda pair: num(pair[1].get("size")) or 0, reverse=True)
    # Inspect only a few files per archive item to limit nested requests.
    candidates = candidates[:2]

    inspected = []
    for _, item in candidates:
        try:
            inspected.append(inspect(identifier, item, probe_header=probe_header))
        except requests.RequestException:
            continue
    if not inspected:
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


def make_track(doc, flac):
    identifier = str(doc["identifier"])
    title = str(doc.get("title") or flac["filename"])
    creator = as_text(doc.get("creator")) or "Internet Archive"
    return {
        "id": identifier,
        "title": title,
        "artist": creator,
        "album": str(doc.get("album") or ""),
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


def search_docs(query, rows):
    fields = ["identifier", "title", "creator", "album", "year", "subject", "licenseurl", "rights"]
    safe_query = query.replace('"', " ").strip()
    # Run a quoted title search first. Use a broad audio search only to add
    # candidates; relevance_score() below rejects unrelated items.
    search_queries = [
        f'(mediatype:audio) AND title:"{safe_query}"',
        f'(mediatype:audio) AND ({safe_query})',
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
            if not license_is_open(doc):
                continue
            identifier = doc.get("identifier")
            if identifier:
                merged.setdefault(str(identifier), doc)
        # Don't make the second request if the title query already found a
        # healthy set of candidates.
        if len(merged) >= min(rows, 12):
            break
    return list(merged.values())


def do_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})

    try:
        limit = min(max(int(request.args.get("limit", "10")), 1), MAX_RESULTS)
    except ValueError:
        limit = MAX_RESULTS

    try:
        docs = search_docs(query, SEARCH_ROWS)
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 502

    ranked = [
        (relevance_score(query, doc), doc)
        for doc in docs
    ]
    ranked = [(score, doc) for score, doc in ranked if score > 0]
    ranked.sort(key=lambda pair: pair[0], reverse=True)

    debug_enabled = request.args.get("debug") == "1"
    debug = {
        "query": query,
        "archiveDocs": len(docs),
        "relevantDocs": len(ranked),
        "candidatesChecked": 0,
        "flacMatches": 0,
        "checkedIdentifiers": [],
        "matchedIdentifiers": [],
    }
    if not ranked:
        payload = {"tracks": []}
        if debug_enabled:
            payload["debug"] = debug
        return jsonify(payload)

    # Inspect more relevant records because many Archive.org audio items do not
    # contain FLAC. During search, trust metadata and skip byte-range FLAC header
    # probes; the stream endpoint can probe the chosen file later if needed.
    candidates = ranked[:MAX_CANDIDATES]
    debug["candidatesChecked"] = len(candidates)
    debug["checkedIdentifiers"] = [str(doc.get("identifier", "")) for _score, doc in candidates]
    tracks_with_score = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(get_flac, str(doc["identifier"]), False): (score, doc)
            for score, doc in candidates
        }
        for future in as_completed(futures):
            score, doc = futures[future]
            try:
                flac = future.result()
            except Exception:
                continue
            if flac:
                tracks_with_score.append((score, doc, flac))

    tracks_with_score.sort(
        key=lambda entry: (
            entry[0],
            int((entry[2].get("bitDepth") or 0) > 16),
            entry[2].get("bitDepth") or 0,
            entry[2].get("sampleRate") or 0,
            entry[2].get("size") or 0,
        ),
        reverse=True,
    )

    tracks = []
    seen_titles = set()
    for _score, doc, flac in tracks_with_score:
        title_key = normalized(doc.get("title", ""))
        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)
        tracks.append(make_track(doc, flac))
        if len(tracks) >= limit:
            break

    debug["flacMatches"] = len(tracks_with_score)
    debug["matchedIdentifiers"] = [str(doc.get("identifier", "")) for _score, doc, _flac in tracks_with_score]
    payload = {"tracks": tracks}
    if debug_enabled:
        payload["debug"] = debug
    return jsonify(payload)


def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400
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
