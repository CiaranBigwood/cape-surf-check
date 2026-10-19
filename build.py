"""Build the Cape Surf Check site.

  python build.py                         writes _site/: index.html, forecast.json, manifest, service worker, icons
  python build.py --no-fetch              reuses _site/forecast.json instead of calling Open-Meteo
  python build.py --no-cam-check          uses webcams.json as written, without asking YouTube about the streams
  python build.py --artifact FILE         also writes FILE: one self-contained page with the forecast inside
  --public-url URL                        the site's address, used for share links and link previews
  --repo-url URL                          the GitHub repository, linked in the footer
  --previous URL                          a forecast.json to fall back on if Open-Meteo cannot be reached

Standard library only, so it runs on a bare GitHub Actions runner.
"""
import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
SPOTS_FILE = HERE / "spots.json"
WEBCAMS_FILE = HERE / "webcams.json"
LEAFLET_CSS = HERE / "leaflet.min.css"
STATIC = ["manifest.webmanifest"]
ASSETS = HERE / "assets"
CHUNK = 25
MARINE = "https://marine-api.open-meteo.com/v1/marine"
WEATHER = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "cape-surf-check (personal surf forecast page)"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+1; SOCS=CAI"}  # YouTube serves a cookie wall without these
CAM_FIELDS = {"spots", "name", "by", "youtube", "channel", "match", "embed", "ipcamlive", "player", "still", "page", "note"}

REQUIRED = ["id", "name", "region", "area", "lat", "lon", "break_type", "swell_dir_min", "swell_dir_max",
            "swell_dir_best", "exposure", "size_min_m", "size_max_m", "best_period_s", "wind_offshore_dir",
            "wind_offshore_tolerance", "tide", "skill"]


def js_list(src, name):
    """Read a `const NAME = [...]` array of strings out of the page script, so the build and the page
    always ask Open-Meteo for the same variables."""
    m = re.search(r"const %s = \[(.*?)\];" % name, src, re.S)
    if not m:
        sys.exit(f"template.html: {name} not found")
    return re.findall(r"'([^']+)'", m.group(1))


def js_int(src, name):
    m = re.search(r"const %s = (\d+);" % name, src)
    if not m:
        sys.exit(f"template.html: {name} not found")
    return int(m.group(1))


def load_spots():
    spots = json.loads(SPOTS_FILE.read_text(encoding="utf-8"))
    ids = set()
    for s in spots:
        missing = [k for k in REQUIRED if k not in s]
        if missing:
            sys.exit(f"spots.json: {s.get('id', '?')} is missing {missing}")
        if s["id"] in ids:
            sys.exit(f"spots.json: duplicate id {s['id']}")
        ids.add(s["id"])
        if not (-35.5 < s["lat"] < -30 and 16.5 < s["lon"] < 25):
            sys.exit(f"spots.json: {s['id']} has coordinates outside the Western Cape: {s['lat']}, {s['lon']}")
        a = s.get("access")
        if a is not None and not (isinstance(a, dict) and isinstance(a.get("lat"), (int, float))
                                  and isinstance(a.get("lon"), (int, float)) and -35.5 < a["lat"] < -30 and 16.5 < a["lon"] < 25):
            sys.exit(f"spots.json: {s['id']} has an access point without a valid lat and lon: {a}")
    return spots


def load_webcams(spot_ids):
    """Read webcams.json, the live cams shown on the spot cards. Each cam lists the spots it shows.
    Strict on purpose: a typo here would otherwise only show up as a cam that quietly never works."""
    if not WEBCAMS_FILE.exists():
        return []
    cams = json.loads(WEBCAMS_FILE.read_text(encoding="utf-8"))
    if not isinstance(cams, list):
        sys.exit("webcams.json must be a list of cams")
    for n, c in enumerate(cams, 1):
        if not isinstance(c, dict):
            sys.exit(f"webcams.json item {n} is not an object")
        where = f"webcams.json item {n} ({c.get('name', '?')})"
        extra = sorted(set(c) - CAM_FIELDS)
        if extra:
            sys.exit(f"{where} has unknown fields {extra}; allowed: {sorted(CAM_FIELDS)}")
        missing = [k for k in ("spots", "name", "by") if not c.get(k)]
        if missing:
            sys.exit(f"{where} is missing {missing}")
        bad = sorted(k for k in CAM_FIELDS - {"spots", "embed"} if k in c and not isinstance(c[k], str))
        if bad:
            sys.exit(f"{where}: {bad} must be text")
        if "embed" in c and not isinstance(c["embed"], bool):
            sys.exit(f"{where}: embed must be true or false, not {c['embed']!r}")
        spots = c["spots"]
        if not isinstance(spots, list) or not all(isinstance(s, str) for s in spots) or len(set(spots)) != len(spots):
            sys.exit(f"{where}: spots must be a list of different spot ids")
        unknown = [s for s in spots if s not in spot_ids]
        if unknown:
            sys.exit(f"{where} names spots that are not in spots.json: {unknown}")
        if len([k for k in ("youtube", "ipcamlive", "player") if c.get(k)]) > 1:
            sys.exit(f"{where} has more than one of youtube, ipcamlive and player")
        if not c.get("youtube") and not c.get("page"):
            sys.exit(f"{where} needs a page: the owner's web page for the cam")
        if c.get("youtube") and not re.fullmatch(r"[\w-]{11}", c["youtube"]):
            sys.exit(f"{where}: youtube must be an 11-character video id, not {c['youtube']!r}")
        if bool(c.get("channel")) != bool(c.get("match")):
            sys.exit(f"{where}: give channel and match together, so the build can find the stream when it restarts")
        if c.get("channel") and not c.get("youtube"):
            sys.exit(f"{where}: channel and match only apply to YouTube cams")
        if c.get("channel") and not re.fullmatch(r"@[\w.-]+|channel/UC[\w-]{22}", c["channel"]):
            sys.exit(f"{where}: channel must look like @handle or channel/UC..., not {c['channel']!r}")
        if c.get("ipcamlive") and not re.fullmatch(r"\w+", c["ipcamlive"]):
            sys.exit(f"{where}: ipcamlive must be the camera's alias, not {c['ipcamlive']!r}")
        for k in ("page", "player", "still"):
            if c.get(k) and not c[k].startswith("https://"):
                sys.exit(f"{where}: {k} must be an https:// address")
    return cams


def fetch_text(url, timeout=20, limit=4_000_000):
    """GET a page as text with a hard time limit: the socket timeout alone lets a slow trickle run on for minutes."""
    give_up = time.monotonic() + 2 * timeout
    req = urllib.request.Request(url, headers=BROWSER)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        parts, size = [], 0
        while True:
            if time.monotonic() > give_up:
                raise TimeoutError(f"took longer than {2 * timeout} s")
            b = r.read(65536)
            if not b:
                break
            parts.append(b)
            size += len(b)
            if size > limit:
                raise ValueError(f"answer larger than {limit // 1_000_000} MB")
    return b"".join(parts).decode("utf-8", "replace")


def walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, key)


def dig(o, *keys):
    """o[k1][k2]... or None, without tripping over a missing level or an unexpected type."""
    for k in keys:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def youtube_live_streams(channel):
    """(video id, title) for each stream the channel is broadcasting now, read from its Live tab: [] when it lists
    streams but none is live. None when the page cannot be read or parsed: YouTube changes its markup now and then."""
    try:
        html = fetch_text(f"https://www.youtube.com/{channel}/streams")
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
        data = json.loads(m.group(1)) if m else None
    except (OSError, http.client.HTTPException, ValueError) as e:
        print(f"  {channel}: could not read the channel ({e})", flush=True)
        return None
    items = [v for v in walk(data, "lockupViewModel") if isinstance(v, dict)]  # the current layout
    old = [v for v in walk(data, "videoRenderer") if isinstance(v, dict)]  # the older layout
    if not items and not old:
        print(f"  {channel}: could not read the channel (no streams found on the page)", flush=True)
        return None
    live = {}
    for v in items:
        vid, title = v.get("contentId"), dig(v, "metadata", "lockupMetadataViewModel", "title", "content")
        if "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE" in json.dumps(v) and isinstance(vid, str):
            live.setdefault(vid, title if isinstance(title, str) else "")
    for v in old:
        vid, runs = v.get("videoId"), dig(v, "title", "runs")
        if "BADGE_STYLE_TYPE_LIVE_NOW" in json.dumps(v) and isinstance(vid, str):
            title = "".join(r.get("text", "") for r in runs if isinstance(r, dict)) if isinstance(runs, list) else ""
            live.setdefault(vid, title)
    return [(vid, title) for vid, title in live.items() if re.fullmatch(r"[\w-]{11}", vid)]


def youtube_embeddable(video_id):
    """True if the video may play inside other sites, False if its owner switched that off, None if it is gone.
    Anything else raises (rate limits, cookie walls, odd answers), and the caller keeps what webcams.json says."""
    watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        body = fetch_text(f"https://www.youtube.com/oembed?format=json&url={watch}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False
        if e.code in (400, 404):
            return None
        raise
    d = json.loads(body)
    if isinstance(d, dict) and (d.get("type") == "video" or "html" in d):
        return True
    raise ValueError("not an oEmbed answer")


def youtube_cam(c, check, channels, deadline):
    """The page fields for one YouTube cam. With check on, use the channel's current stream: keep the configured one
    while it is live, switch when the owner restarted it under a new id, and fall back to a link to the channel's
    streams when nothing matching is live or the video is gone, so an ended recording never shows as "Watch live"."""
    vid, embed, ch, match = c["youtube"], c.get("embed", True), c.get("channel"), c.get("match")
    off = gone = False
    if check and time.time() < deadline:
        if ch and match:
            if ch not in channels:
                channels[ch] = youtube_live_streams(ch)
            live = channels[ch]
            if live is not None:
                hits = [v for v, t in live if match.lower() in t.lower()]
                if len(hits) > 1:
                    print(f"::warning::{c['name']}: {len(hits)} live streams on {ch} match {match!r}; make match more specific", flush=True)
                if hits and vid not in hits:
                    print(f"  {c['name']}: stream restarted, now {hits[0]} (webcams.json has {vid})", flush=True)
                    vid = hits[0]
                elif not hits:
                    print(f"  {c['name']}: {ch} has no live stream matching {match!r} right now", flush=True)
                    off = True
        if not off:
            try:
                ok = youtube_embeddable(vid)
                gone, embed = ok is None, embed if ok is None else ok
            except (OSError, http.client.HTTPException, ValueError) as e:
                print(f"  {c['name']}: could not ask YouTube about {vid} ({e})", flush=True)
            if gone:
                print(f"  {c['name']}: video {vid} is gone", flush=True)
    cam = {}
    if (off or gone) and ch:
        cam["page"], cam["off"] = f"https://www.youtube.com/{ch}/streams", True
        return cam
    cam["page"] = f"https://www.youtube.com/watch?v={vid}"
    if embed and not gone:
        cam["play"] = f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1&mute=1&playsinline=1&rel=0"
        cam["poster"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        if ch:
            cam["more"] = f"https://www.youtube.com/{ch}/streams"
    return cam


def page_cams(cams, check):
    """Turn webcams.json into what the page needs, per spot id. With check on, ask YouTube about each YouTube cam
    (see youtube_cam). Any failure keeps what webcams.json says, so a cam check never stops the forecast going out."""
    by_spot, channels = {}, {}
    deadline = time.time() + 120
    for c in cams:
        cam = {"name": c["name"], "by": c["by"]}
        if c.get("note"):
            cam["note"] = c["note"]
        if c.get("youtube"):
            try:
                cam.update(youtube_cam(c, check, channels, deadline))
            except Exception as e:  # noqa: BLE001 - a surprise from YouTube must not cost the forecast
                print(f"::warning::{c['name']}: cam check failed ({type(e).__name__}: {e}); using webcams.json as written", flush=True)
                cam.update(youtube_cam(c, False, channels, deadline))
        elif c.get("ipcamlive"):
            alias = c["ipcamlive"]
            cam["page"] = c["page"]
            cam["still"] = f"https://ipcamlive.com/player/snapshot.php?alias={alias}"
            if c.get("embed", True):
                cam["play"] = f"https://g2.ipcamlive.com/player/player.php?alias={alias}&autoplay=1"
        else:
            cam["page"] = c["page"]
            if c.get("player"):
                cam["play"] = c["player"]
            if c.get("still"):
                cam["still"] = c["still"]
        for s in c["spots"]:
            by_spot.setdefault(s, []).append(cam)
    return by_spot


def get_json(url, tries=4):
    wait = 20
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(data.get("reason", "Open-Meteo error"))
            return data if isinstance(data, list) else [data]
        except (OSError, http.client.HTTPException, ValueError, RuntimeError) as e:  # covers dropped and cut-off connections too
            code = getattr(e, "code", None)
            if attempt == tries or code in (400, 404):
                raise
            print(f"  request failed ({code or e}); retrying in {wait}s", flush=True)
            time.sleep(wait)
            wait *= 2


def fetch_forecast(spots, m_vars, w_vars, w_daily, days):
    marine, weather = [], []
    for i in range(0, len(spots), CHUNK):
        chunk = spots[i:i + CHUNK]
        lat = ",".join(str(s["lat"]) for s in chunk)
        lon = ",".join(str(s["lon"]) for s in chunk)
        common = {"latitude": lat, "longitude": lon, "timezone": "Africa/Johannesburg", "forecast_days": days}
        m_url = MARINE + "?" + urllib.parse.urlencode(dict(common, hourly=",".join(m_vars)), safe=",")
        w_url = WEATHER + "?" + urllib.parse.urlencode(dict(common, hourly=",".join(w_vars), daily=",".join(w_daily),
                                                             wind_speed_unit="kmh", cell_selection="sea"), safe=",")
        marine += get_json(m_url)
        weather += get_json(w_url)
        print(f"  fetched locations {i + 1}-{i + len(chunk)} of {len(spots)}", flush=True)
        time.sleep(1)
    if len(marine) != len(spots) or len(weather) != len(spots):
        raise RuntimeError(f"location count mismatch: {len(marine)}/{len(weather)} for {len(spots)} spots")

    times = weather[0]["hourly"]["time"]
    days_list = weather[0].get("daily", {}).get("time", [])
    out = {}
    for s, m, w in zip(spots, marine, weather):
        if w["hourly"]["time"] != times:
            raise RuntimeError(f"{s['id']}: weather times differ from the first location")
        index = {t: k for k, t in enumerate(m["hourly"]["time"])}

        def pick(arr):
            return [None if (index.get(t) is None or arr is None) else arr[index[t]] for t in times]

        out[s["id"]] = {
            "m": {v: pick(m["hourly"].get(v)) for v in m_vars},
            "w": {v: w["hourly"].get(v) for v in w_vars},
            "d": {v: w.get("daily", {}).get(v) for v in w_daily},
            "grid": {"lat": m.get("latitude"), "lon": m.get("longitude")},
        }
    with_swell = sum(1 for v in out.values() if any(x is not None for x in v["m"]["swell_wave_height"][:24]))
    if with_swell < 0.8 * len(spots):
        raise RuntimeError(f"only {with_swell} of {len(spots)} spots have swell data for today")
    return {"v": 2, "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "times": times, "days": days_list, "spots": out}


def previous_forecast(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        return data if data.get("v") == 2 and data.get("spots") else None
    except Exception as e:  # noqa: BLE001 - any failure just means there is nothing to fall back on
        print(f"  no previous forecast at {url}: {e}", flush=True)
        return None


def page_parts(spots, site_cfg, snapshot_json):
    html = TEMPLATE.read_text(encoding="utf-8")
    spots_js = json.dumps(spots, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html, n1 = re.subn(r"/\*SPOTS_START\*/.*?/\*SPOTS_END\*/",
                       lambda m: "/*SPOTS_START*/\nconst SPOTS = " + spots_js + ";\n/*SPOTS_END*/", html, count=1, flags=re.S)
    cfg_js = json.dumps(site_cfg, ensure_ascii=False).replace("</", "<\\/")
    html, n2 = re.subn(r"/\*SITE_START\*/.*?/\*SITE_END\*/",
                       lambda m: "/*SITE_START*/\nconst SITE = " + cfg_js + ";\n/*SITE_END*/", html, count=1, flags=re.S)
    if n1 != 1 or n2 != 1:
        sys.exit("template.html: SPOTS or SITE markers not found")
    css = LEAFLET_CSS.read_text(encoding="utf-8") if LEAFLET_CSS.exists() else ""
    html = html.replace("/*LEAFLET_CSS*/", css, 1)
    html = re.sub(r'(<script id="snapshot" type="application/json">).*?(</script>)',
                  lambda m: m.group(1) + snapshot_json + m.group(2), html, count=1, flags=re.S)
    cut = html.index('<header class="top"')
    return html[:cut], html[cut:]


def document(head_part, body_part, public_url):
    og = ""
    if public_url:
        og = (f'<meta property="og:url" content="{public_url}">\n'
              f'<meta property="og:image" content="{public_url}assets/og.png">\n'
              '<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
              '<meta name="twitter:card" content="summary_large_image">\n'
              f'<link rel="canonical" href="{public_url}">\n')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Where to surf in the Western Cape: ratings, wind, swell, tides and water temperature for 98 spots, with a 9-day outlook.">
<meta name="theme-color" content="#0b6f85">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="assets/favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="assets/icon-192.png">
<link rel="apple-touch-icon" href="assets/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Surf Check">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Cape Surf Check">
<meta property="og:title" content="Cape Surf Check">
<meta property="og:description" content="Where to surf in the Western Cape this week: ratings, wind, tides and water temperature for 98 spots.">
{og}<style>html,body{{margin:0}}[hidden]{{display:none!important}}img{{max-width:100%}}</style>
{head_part}<style>@supports (padding: max(0px)) {{ body {{ padding-left: max(clamp(16px, 3vw, 32px), env(safe-area-inset-left)); padding-right: max(clamp(16px, 3vw, 32px), env(safe-area-inset-right)); }} }}</style>
</head>
<body>
{body_part}
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-cam-check", action="store_true")
    ap.add_argument("--artifact")
    ap.add_argument("--public-url", default="")
    ap.add_argument("--repo-url", default="")
    ap.add_argument("--previous", default="")
    a = ap.parse_args()

    public_url = a.public_url.strip()
    if public_url and not public_url.endswith("/"):
        public_url += "/"
    out = (HERE / a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = TEMPLATE.read_text(encoding="utf-8")
    m_vars, w_vars, w_daily = js_list(src, "M_VARS"), js_list(src, "W_VARS"), js_list(src, "W_DAILY")
    days = js_int(src, "DAYS")
    spots = load_spots()
    cams = load_webcams({s["id"] for s in spots})
    print(f"{len(spots)} spots; {len(m_vars)} marine and {len(w_vars) + len(w_daily)} weather variables; {days} days", flush=True)

    forecast = None
    if a.no_fetch:
        f = out / "forecast.json"
        forecast = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        print("  reusing the forecast already in the output folder" if forecast else "  no forecast in the output folder")
    else:
        try:
            print("fetching the forecast ...", flush=True)
            forecast = fetch_forecast(spots, m_vars, w_vars, w_daily, days)
        except Exception as e:  # noqa: BLE001 - fall back rather than publish nothing
            print(f"  forecast fetch failed: {e}", flush=True)
            if a.previous:
                forecast = previous_forecast(a.previous) or (time.sleep(10) or previous_forecast(a.previous))
                if forecast:
                    print(f"::warning::Open-Meteo fetch failed ({e}); republishing the forecast from {forecast['fetchedAt']}", flush=True)
                    age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(forecast["fetchedAt"])).total_seconds() / 3600
                    if age_h > 24 and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
                        sys.exit(f"The forecast is {age_h:.0f} hours old and Open-Meteo still fails ({e}). "
                                 "Failing so GitHub emails the owner; the live site keeps its last deployment.")
            if forecast is None:
                sys.exit("No forecast: Open-Meteo failed and no previous forecast.json could be loaded. Not publishing.")
    fc_json = json.dumps(forecast, separators=(",", ":"), allow_nan=False) if forecast else None
    if fc_json:
        (out / "forecast.json").write_text(fc_json, encoding="utf-8")
        print(f"  forecast.json {len(fc_json) / 1024:.0f} KB, fetched {forecast['fetchedAt']}", flush=True)

    # The page carries the cams exactly as webcams.json has them, so index.html (and with it the offline cache) only
    # changes when that file does. What YouTube says right now goes to cams.json, which the page reads when it opens.
    cams_src = hashlib.sha256(WEBCAMS_FILE.read_bytes()).hexdigest()[:12] if WEBCAMS_FILE.exists() else ""
    by_spot = page_cams(cams, check=False)
    page_spots = [dict(s, cams=by_spot[s["id"]]) if s["id"] in by_spot else s for s in spots]
    print(f"{'checking' if not a.no_cam_check else 'reading'} {len(cams)} webcams ...", flush=True)
    live_cams = page_cams(cams, check=not a.no_cam_check) if not a.no_cam_check else by_spot
    (out / "cams.json").write_text(json.dumps({"v": 1, "src": cams_src, "spots": live_cams}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  {sum(len(v) for v in by_spot.values())} cam links on {len(by_spot)} spots", flush=True)

    cfg = {"publicUrl": public_url, "repoUrl": a.repo_url.strip(), "camsSrc": cams_src}  # no build time: keeps index.html, and so the offline cache, unchanged between data-only builds
    head_part, body_part = page_parts(page_spots, cfg, "null")
    index = document(head_part, body_part, public_url)
    (out / "index.html").write_text(index, encoding="utf-8")

    for name in STATIC:
        shutil.copy2(HERE / name, out / name)
    if ASSETS.exists():
        shutil.copytree(ASSETS, out / "assets", dirs_exist_ok=True)
    version = hashlib.sha256((index + (HERE / "sw.js").read_text(encoding="utf-8")).encode("utf-8")).hexdigest()[:12]
    (out / "sw.js").write_text((HERE / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", version), encoding="utf-8")
    print(f"wrote {out} (index.html {len(index) / 1024:.0f} KB, service worker {version})", flush=True)

    if a.artifact:
        snap = fc_json.replace("</", "<\\/") if fc_json else "null"
        h, b = page_parts(page_spots, cfg, snap)
        pathlib.Path(a.artifact).write_text(h + b, encoding="utf-8")
        print(f"wrote {a.artifact} ({pathlib.Path(a.artifact).stat().st_size / 1024:.0f} KB)", flush=True)


if __name__ == "__main__":
    main()
