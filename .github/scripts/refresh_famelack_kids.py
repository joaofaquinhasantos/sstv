import json
import re
import subprocess
import urllib.request
from pathlib import Path

PLAYLIST = Path("sstv.m3u")
FAMELACK_KIDS = "https://raw.githubusercontent.com/famelack/famelack-data/main/tv/raw/categories/kids.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"

# Priority requested for SSTV: French -> Thai -> English. Famelack currently exposes
# no Thai-language/country Kids records, so Thai remains handled by the existing
# Thailand/Kids updater. Spanish/Portuguese are included only for major branded feeds
# that Famelack exposes as directly reusable HLS.
WANTED = [
    # French
    {"language": "French", "key": "fr-cartoonito-tom-jerry", "name": "Cartoonito France: Tom Et Jerry"},
    {"language": "French", "key": "fr-mr-bean-anime", "name": "Banijay Mr Bean Animé"},

    # English - Boomerang / Cartoon Network / Cartoonito
    {"language": "English", "key": "en-boomerang-uk", "name": "Boomerang UK"},
    {"language": "English", "key": "en-boomerang-best", "name": "Boomerang: Best Cartoons Ever"},
    {"language": "English", "key": "en-cn-africa", "name": "Cartoon Network Africa"},
    {"language": "English", "key": "en-cn-asia", "name": "Cartoon Network Asia"},
    {"language": "English", "key": "en-cn-regular-show", "name": "Cartoon Network: Regular Show"},
    {"language": "English", "key": "en-cn-steven-universe", "name": "Cartoon Network: Steven Universe"},
    {"language": "English", "key": "en-cartoonito-baby-looney", "name": "Cartoonito: Baby Looney Tunes"},

    # English - Disney Channel
    {"language": "English", "key": "en-disney-big-city-greens", "name": "Disney Channel: Big City Greens"},
    {"language": "English", "key": "en-disney-gravity-falls", "name": "Disney Channel: Gravity Falls"},
    {"language": "English", "key": "en-disney-phineas-ferb", "name": "Disney Channel: Phineas and Ferb"},
    {"language": "English", "key": "en-disney-stuck-middle", "name": "Disney Channel: Stuck in the Middle"},
    {"language": "English", "key": "en-disney-wizards", "name": "Disney Channel: Wizards of Waverly"},

    # English - Disney Junior
    {"language": "English", "key": "en-disneyjr-bluey", "name": "Disney Junior: Bluey"},
    {"language": "English", "key": "en-disneyjr-spidey", "name": "Disney Junior: Marvel's Spidey and his Amazing Friends"},
    {"language": "English", "key": "en-disneyjr-mickey", "name": "Disney Junior: Mickey Mouse Clubhouse"},
    {"language": "English", "key": "en-disneyjr-minnie", "name": "Disney Junior: Minnie's Bow-Toons"},
    {"language": "English", "key": "en-disneyjr-cars", "name": "Disney Junior: Pixar Cars Shorts"},
    {"language": "English", "key": "en-disneyjr-superkitties", "name": "Disney Junior: SuperKitties"},
    {"language": "English", "key": "en-disneyjr-winnie", "name": "Disney Junior: Winnie the Pooh"},
    {"language": "English", "key": "en-bluey-official", "name": "Bluey - Official Channel"},

    # Major direct HLS branded feeds exposed by Famelack
    {"language": "Spanish", "key": "es-disneyjr-latam-south", "name": "Disney Jr. Latin America South"},
    {"language": "Portuguese", "key": "pt-nickelodeon", "name": "Nickelodeon"},
]

LANG_COUNTRY_FALLBACK = {
    "French": "FR",
    "Thai": "TH",
    "English": "US",
    "Spanish": "AR",
    "Portuguese": "BR",
}


def fetch_text(url, timeout=20, max_bytes=2_000_000):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, application/json, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.geturl(), response.status, response.read(max_bytes).decode("utf-8", "ignore")


def healthy_hls(url):
    try:
        final, status, body = fetch_text(url, timeout=12, max_bytes=128_000)
        if not 200 <= status < 400:
            return False, final, f"HTTP {status}"
        low = body.lower()
        if "<html" in low and "#extm3u" not in low:
            return False, final, "HTML response"
        if "#extm3u" not in low:
            return False, final, "not HLS"
        return True, final, "ok"
    except Exception as exc:
        return False, url, f"{type(exc).__name__}: {exc}"


def youtube_watch_url(embed_url):
    match = re.search(r"/(?:embed|shorts)/([A-Za-z0-9_-]{6,})", embed_url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return embed_url.replace("youtube-nocookie.com", "youtube.com")


def resolve_youtube_hls(embed_url):
    watch_url = youtube_watch_url(embed_url)
    errors = []
    # Anonymous clients only. No account cookies or authentication are used.
    for client in ("web_embedded", "web_safari"):
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout", "20",
            "--extractor-args", f"youtube:player_client={client}",
            "-f", "best[protocol^=m3u8]/best",
            "-g",
            watch_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=50, check=False)
        except Exception as exc:
            errors.append(f"{client}: {type(exc).__name__}: {exc}")
            continue

        if result.returncode != 0:
            lines = (result.stderr or result.stdout or "yt-dlp failed").strip().splitlines()
            errors.append(f"{client}: {lines[-1] if lines else 'yt-dlp failed'}")
            continue

        urls = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")]
        urls.sort(key=lambda u: (0 if ("m3u8" in u.lower() or "/manifest/hls" in u.lower()) else 1))
        for url in urls:
            ok, final, reason = healthy_hls(url)
            if ok:
                return url, f"ok:{client}"
        errors.append(f"{client}: no working HLS manifest")

    return None, " | ".join(errors[-2:]) if errors else "no working HLS manifest"


def old_managed_urls(text):
    found = {}
    pattern = re.compile(
        r'#EXTINF:-1[^\n]*tvg-id="SSTVFamelackKids\.([^"\s]+)"[^\n]*\n(?P<url>https?://[^\n]+)',
        re.I,
    )
    for match in pattern.finditer(text):
        found[match.group(1)] = match.group("url").strip()
    return found


def choose_source(record, old_url=None):
    sources = record.get("sources") or {}

    for url in sources.get("streams") or []:
        if ".m3u8" not in url.lower():
            continue
        ok, final, reason = healthy_hls(url)
        if ok:
            return url, "direct-hls"
        print(f"FAMELACK KIDS DIRECT FAILED: {record['name']}: {url} ({reason})")

    for url in sources.get("youtube") or []:
        resolved, reason = resolve_youtube_hls(url)
        if resolved:
            return resolved, "youtube-hls"
        print(f"FAMELACK KIDS YOUTUBE FAILED: {record['name']}: {url} ({reason})")

    if old_url:
        ok, final, reason = healthy_hls(old_url)
        if ok:
            return old_url, "retained-old"
        print(f"FAMELACK KIDS OLD FAILED: {record['name']}: {old_url} ({reason})")

    return None, None


def managed_block(language, blocks):
    tag = language.upper()
    start = f"# --- FAMELACK KIDS {tag} START ---"
    end = f"# --- FAMELACK KIDS {tag} END ---"
    body = "\n".join(blocks)
    return start + "\n" + (body + "\n" if body else "") + end


def replace_or_insert_language(text, language, blocks):
    start = f"# --- FAMELACK KIDS {language.upper()} START ---"
    end = f"# --- FAMELACK KIDS {language.upper()} END ---"
    replacement = managed_block(language, blocks)
    existing = re.compile(re.escape(start) + r"\n.*?" + re.escape(end), re.S)
    if existing.search(text):
        return existing.sub(replacement, text, count=1)

    if not blocks:
        return text

    section_header = f"# === KIDS — {language.upper()} ==="
    if section_header in text:
        return text.replace(section_header, section_header + "\n" + replacement, 1)

    english_header = "# === KIDS — ENGLISH ==="
    if english_header not in text:
        raise RuntimeError("English Kids section missing")
    section = section_header + "\n" + replacement + "\n\n"
    return text.replace(english_header, section + english_header, 1)


def main():
    playlist_text = PLAYLIST.read_text(encoding="utf-8")
    previous = old_managed_urls(playlist_text)

    _, status, raw = fetch_text(FAMELACK_KIDS, timeout=30, max_bytes=5_000_000)
    if not 200 <= status < 400:
        raise RuntimeError(f"Famelack Kids dataset HTTP {status}")
    data = json.loads(raw)
    records = {item.get("name"): item for item in data if item.get("name")}

    languages = ("French", "Thai", "English", "Spanish", "Portuguese")
    by_language = {language: [] for language in languages}
    summary = []

    for wanted in WANTED:
        record = records.get(wanted["name"])
        if not record:
            print(f"::warning::Famelack Kids listing missing: {wanted['name']}")
            continue
        if record.get("isGeoBlocked"):
            print(f"::warning::Famelack Kids known geo-blocked, skipped: {wanted['name']}")
            continue

        url, source_type = choose_source(record, previous.get(wanted["key"]))
        if not url:
            print(f"::warning::No working IPTV-compatible source: {wanted['name']}")
            continue

        country = str(record.get("country") or LANG_COUNTRY_FALLBACK[wanted["language"]]).upper()
        block = (
            f'#EXTINF:-1 tvg-id="SSTVFamelackKids.{wanted["key"]}" '
            f'tvg-country="{country}" group-title="KIDS | {wanted["language"]}",{wanted["name"]}\n'
            f'{url}'
        )
        by_language[wanted["language"]].append(block)
        summary.append((wanted["language"], wanted["name"], source_type))
        print(f"FAMELACK KIDS OK: {wanted['language']} | {wanted['name']} [{source_type}]")

    new_text = playlist_text
    for language in languages:
        marker_exists = f"# --- FAMELACK KIDS {language.upper()} START ---" in new_text
        if by_language[language] or marker_exists:
            new_text = replace_or_insert_language(new_text, language, by_language[language])

    if new_text != playlist_text:
        PLAYLIST.write_text(new_text, encoding="utf-8")

    print("FAMELACK KIDS SUMMARY:")
    for language in languages:
        names = [name for lang, name, _ in summary if lang == language]
        print(f"  {language}: {len(names)}" + (" -> " + ", ".join(names) if names else ""))


if __name__ == "__main__":
    main()
