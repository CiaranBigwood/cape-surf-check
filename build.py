"""Build the Cape Surf Check site.

  python build.py                         writes _site/: index.html, forecast.json, manifest, service worker, icons
  python build.py --no-fetch              reuses _site/forecast.json instead of calling Open-Meteo
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
LEAFLET_CSS = HERE / "leaflet.min.css"
STATIC = ["manifest.webmanifest"]
ASSETS = HERE / "assets"
CHUNK = 25
MARINE = "https://marine-api.open-meteo.com/v1/marine"
WEATHER = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "cape-surf-check (personal surf forecast page)"}

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
    return spots


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
<meta name="description" content="Where to surf in the Western Cape: ratings, wind, swell, tides and water temperature for 103 spots, with a 9-day outlook.">
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
<meta property="og:description" content="Where to surf in the Western Cape this week: ratings, wind, tides and water temperature for 103 spots.">
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

    cfg = {"publicUrl": public_url, "repoUrl": a.repo_url.strip()}  # no build time: keeps index.html, and so the offline cache, unchanged between data-only builds
    head_part, body_part = page_parts(spots, cfg, "null")
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
        h, b = page_parts(spots, cfg, snap)
        pathlib.Path(a.artifact).write_text(h + b, encoding="utf-8")
        print(f"wrote {a.artifact} ({pathlib.Path(a.artifact).stat().st_size / 1024:.0f} KB)", flush=True)


if __name__ == "__main__":
    main()
