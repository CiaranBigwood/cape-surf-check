# Cape Surf Check

Where to surf in the Western Cape: a rating out of 10 for 98 spots, hour by hour, with wind, swell, tides,
water temperature, wetsuit hints and a 9-day outlook. Every spot has a **Directions** button that opens Google
Maps, and spots with a public webcam have a **Live cam** button. Free, no app, no account.

**Open it:** https://ciaranbigwood.github.io/cape-surf-check/

## On your phone

Open the address above, then put it on your home screen so it opens like an app:

- **iPhone:** in Safari, tap the Share button, then **Add to Home Screen**.
- **Android:** in Chrome, tap the ⋮ menu, then **Add to Home screen** or **Install app**.

The page remembers the last forecast, so it still opens at the beach with no signal. The **Share** button
on the page shows the address, a QR code and a button to send it to friends.

## How it stays up to date

A scheduled GitHub Action rebuilds the page every 3 hours with a fresh forecast from
[Open-Meteo](https://open-meteo.com/). Your browser also fetches the forecast directly when the saved one is
more than about 3½ hours old, or when you press **Refresh**. Ratings are worked out in the browser from the
raw forecast and each spot's rules.

## Change a spot

All spot rules live in [`spots.json`](spots.json). Open it on GitHub, click the pencil icon, edit, and
commit. The site rebuilds itself in about a minute. The fields that drive the rating:

| Field | Meaning |
|---|---|
| `lat`, `lon` | The break itself, in decimal degrees |
| `swell_dir_min`, `swell_dir_max`, `swell_dir_best` | Swell directions the spot works on, as the bearing the swell comes **from** (N 0, E 90, S 180, W 270) |
| `exposure` | `exposed`, `semi` or `sheltered` (how much of the open-ocean swell gets in) |
| `size_min_m`, `size_max_m` | Open-ocean swell height at which it starts working, and above which it is too big |
| `best_period_s` | Swell period in seconds at which it is best |
| `wind_offshore_dir`, `wind_offshore_tolerance` | Bearing the offshore wind comes **from**, and how many degrees either side still count |
| `tide` | `low`, `low-mid`, `mid`, `mid-high`, `high` or `all` |
| `skill` | `beginner`, `intermediate`, `advanced` or `expert` |
| `confidence`, `sources` | How well the rules are backed, and where they came from |
| `access` (optional) | Where **Directions** should take you when it is not the break itself: `lat`, `lon` and a short `note`, e.g. the harbour for Dungeons or the car park for a walk-in beach. Without it, Google Maps routes to the break and stops at the nearest road. |

The rest (`description`, `hazards`, `crowd`, `best_season`, `local_notes`, `aka`) is shown on the spot card.

## Live cams

Webcams live in [`webcams.json`](webcams.json), one entry per camera. A camera can show more than one spot. On a
spot card the cams appear in the order they are listed, so put the best one first: one that plays on the page,
then still pictures, then cams that only open on another site.

| Field | Meaning |
|---|---|
| `spots` | The spot ids (from `spots.json`) whose surf the camera actually shows |
| `name`, `by` | A short name for the view (it labels the cam's tab), and who runs the camera (always credited) |
| `youtube`, `channel`, `match` | A YouTube live stream: its video id, the channel (`@handle`), and part of the stream's title |
| `embed` | `false` when the owner does not allow the stream to play inside other sites; it then opens on YouTube |
| `ipcamlive` | An ipcamlive camera alias: shows its latest picture, and plays live on the page unless `embed` is `false` |
| `player` | Any other live player page that may be shown in a frame |
| `still` | A picture that the camera refreshes, for cameras with no live player |
| `page` | The owner's own page for the cam (needed for everything except YouTube) |
| `note` | A short note shown with the cam, e.g. "Free with ads" |

YouTube gives a live stream a new video id every time its owner restarts it. Each build (every 3 hours) looks on
the cam's `channel` for a live stream whose title contains `match` and uses that, and asks YouTube whether the
stream may still be embedded. When nothing matching is live, the cam shows "offline right now" with a link to the
channel's streams. The results go into `cams.json` next to the page, which reads it when it opens; the page itself
carries the cams as `webcams.json` has them, so a hiccup at YouTube never changes the app people have installed.
If YouTube cannot be reached, the build uses `webcams.json` as written; a cam check never stops the forecast going
out. Video only loads when someone opens a cam, so the page stays light on mobile data.

## Build it yourself

```
python build.py            # writes _site/ with a fresh forecast
python -m http.server 8000 --directory _site
```

`--no-fetch` reuses the forecast already in `_site/`, and `--no-cam-check` skips the YouTube checks.

Then open http://localhost:8000. Python 3.9 or later, standard library only.

| File | What it is |
|---|---|
| `template.html` | The page: layout, styles, the rating engine and the app code |
| `spots.json` | The 98 spots and their rules |
| `webcams.json` | The live cams and the spots they show |
| `build.py` | Fetches the forecast and assembles `_site/` |
| `sw.js`, `manifest.webmanifest`, `assets/` | Offline support, home-screen install and icons |
| `.github/workflows/deploy.yml` | Rebuilds and publishes every 3 hours and on every change |

## Credits and limits

- Weather and marine data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0), free for non-commercial use.
- Live cams belong to the people who run them, credited under each cam. They play from YouTube or the owner's
  own player, or open on the owner's site; nothing is recorded or stored here. Directions open Google Maps.
- Map by [Leaflet](https://leafletjs.com/) (BSD-2-Clause, see `LICENSE-leaflet.txt`; its stylesheet is bundled as `leaflet.min.css`) with tiles © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Spot rules were compiled from public spot guides (surf-forecast.com, Wannasurf, Surfline, Wavescape) and the
  shape of the coast. Each spot shows its confidence; `low` means set from geometry only.
- Ratings are a model, not a surf report. Nobody has checked the beach. Rips, sharks, boats and crowds are not
  in the numbers. Check the Shark Spotters flags where they fly, and look before you paddle out.
