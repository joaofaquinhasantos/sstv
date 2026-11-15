import concurrent.futures
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE = Path("sstv.m3u")
OUTPUT = Path("sstv-qatar.m3u")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"
CTX = ssl.create_default_context()

WEBPAGE_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be",
    "dailymotion.com", "www.dailymotion.com",
    "lcp.fr", "www.lcp.fr",
    "publicsenat.fr", "www.publicsenat.fr",
}
EXPLICIT_SKIP_NAMES = {"Tiny Pop [Geo]", "POP [Geo]", "Channel 7 HD", "Channel 8"}


def fetch(url, headers=None, timeout=12, max_bytes=160_000):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.geturl(), r.status, dict(r.headers), r.read(max_bytes)


def first_uri(m3u, base):
    for raw in m3u.splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            return urllib.parse.urljoin(base, s)
    return None


def deep_hls(url, headers):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in WEBPAGE_HOSTS:
        return False, "web page URL"

    try:
        final, status, rh, body = fetch(url, headers=headers)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not 200 <= status < 400:
        return False, f"HTTP {status}"

    decoded = body.decode("utf-8", "ignore")
    if "#extm3u" not in decoded.lower():
        return False, "not HLS"

    child = first_uri(decoded, final)
    if not child:
        return True, "manifest reachable"

    try:
        f2, s2, h2, b2 = fetch(child, headers=headers, timeout=10, max_bytes=120_000)
    except Exception as exc:
        return False, f"child {type(exc).__name__}: {exc}"
    if not 200 <= s2 < 400:
        return False, f"child HTTP {s2}"

    d2 = b2.decode("utf-8", "ignore")
    if "#extm3u" in d2.lower():
        segment = first_uri(d2, f2)
        if segment:
            try:
                _, s3, _, b3 = fetch(segment, headers=headers, timeout=10, max_bytes=4096)
            except Exception as exc:
                return False, f"segment {type(exc).__name__}: {exc}"
            if not 200 <= s3 < 400 or not b3:
                return False, f"segment HTTP {s3}"
    return True, "HLS + media reachable"


def parse_entries(text):
    lines = text.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("#EXTINF"):
            i += 1
            continue
        extinf = lines[i]
        group_match = re.search(r'group-title="([^"]+)"', extinf)
        group = group_match.group(1) if group_match else "(none)"
        name = extinf.rsplit(",", 1)[-1].strip()
        options = []
        headers = {}
        url = None
        j = i + 1
        while j < len(lines) and not lines[j].startswith("#EXTINF"):
            s = lines[j].strip()
            low = s.lower()
            if low.startswith("#extvlcopt:http-user-agent="):
                options.append(s)
                headers["User-Agent"] = s.split("=", 1)[1].strip()
            elif low.startswith("#extvlcopt:http-referrer="):
                options.append(s)
                headers["Referer"] = s.split("=", 1)[1].strip()
            elif low.startswith("#extvlcopt:http-origin="):
                options.append(s)
                headers["Origin"] = s.split("=", 1)[1].strip()
            elif s.startswith("http://") or s.startswith("https://"):
                url = s
                break
            j += 1
        if url:
            entries.append({
                "group": group,
                "name": name,
                "extinf": extinf,
                "options": options,
                "headers": headers,
                "url": url,
            })
        i += 1
    return entries


def corrected_block(entry):
    extinf = entry["extinf"]
    name = entry["name"]
    url = entry["url"]
    if entry["group"] == "KIDS | Portuguese" and "/nicktoons/" in url.lower():
        extinf = re.sub(r'tvg-id="[^"]*"', 'tvg-id="Nicktoons.br"', extinf, count=1)
        extinf = re.sub(r',[^,]+$', ',NickToons Brasil', extinf)
        name = "NickToons Brasil"
    return [extinf] + entry["options"] + [url], name


def main():
    source_text = SOURCE.read_text(encoding="utf-8")
    header = next((x for x in source_text.splitlines() if x.startswith("#EXTM3U")), "#EXTM3U")
    entries = parse_entries(source_text)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(deep_hls, e["url"], e["headers"]): idx
            for idx, e in enumerate(entries)
            if e["name"] not in EXPLICIT_SKIP_NAMES
        }
        for fut in concurrent.futures.as_completed(futures):
            idx = futures[fut]
            ok, reason = fut.result()
            results[idx] = (ok, reason)

    kept = []
    for idx, entry in enumerate(entries):
        if entry["name"] in EXPLICIT_SKIP_NAMES:
            print(f"QATAR SKIP: {entry['group']} | {entry['name']} | explicit geo label")
            continue
        ok, reason = results.get(idx, (False, "not tested"))
        if not ok:
            print(f"QATAR FAIL: {entry['group']} | {entry['name']} | {reason}")
            continue
        block, corrected_name = corrected_block(entry)
        kept.append((entry["group"], corrected_name, block))
        print(f"QATAR OK: {entry['group']} | {corrected_name}")

    if len(kept) < 25:
        raise RuntimeError(f"Refusing to overwrite Qatar playlist: only {len(kept)} channels passed")

    groups = []
    for group, _, _ in kept:
        if group not in groups:
            groups.append(group)

    out = [
        header,
        "#PLAYLIST:SSTV Qatar - stable HLS",
        "# NOTE: Auto-built from sstv.m3u using deep HLS manifest/segment checks.",
        "# NOTE: Webpage URLs, direct MPEG-TS relays and explicitly geo-labelled entries are excluded.",
        "# NOTE: Qatar residential playback still depends on the viewer IP and CDN policy.",
        "",
    ]
    for group in groups:
        out.append(f"# === {group.upper()} ===")
        for g, _, block in kept:
            if g == group:
                out.extend(block)
        out.append("")

    new_text = "\n".join(out).rstrip() + "\n"
    old_text = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if new_text != old_text:
        OUTPUT.write_text(new_text, encoding="utf-8")

    print(f"QATAR SUMMARY: {len(kept)} channels")
    for group in groups:
        names = [name for g, name, _ in kept if g == group]
        print(f"  {group}: {len(names)}")


if __name__ == "__main__":
    main()
