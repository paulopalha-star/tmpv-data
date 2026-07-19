#!/usr/bin/env python3
"""refresh_weather.py — build weather_cache.json for the TMPV CDN.

Bible §14.9 · Weather overlay. One request per unique destination (centroid of
its Active hotels), throttled to ~1 call/sec. Writes to the sibling repo
`/Users/paulopalha/projetos/tmpv-data/data/weather_cache.json`.

Run manually before releases:

    python3 refresh_weather.py

Requires OPENWEATHER_API_KEY in .env (or process env).

Does NOT commit or push — you review the file, then handle git yourself.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ── paths ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
DEST_CSV = ROOT / "data" / "destinations.csv"
TMPV_DATA_DIR = Path(os.getenv("TMPV_DATA_DIR", "/Users/paulopalha/projetos/tmpv-data"))
HOTELS_JSON = TMPV_DATA_DIR / "data" / "hotels.json"
OUT_JSON = TMPV_DATA_DIR / "data" / "weather_cache.json"

# ── env ─────────────────────────────────────────────────────────────────
def load_dotenv(path: Path) -> None:
    """Minimal .env loader — no external deps. Handles KEY=VALUE lines,
    strips surrounding quotes, ignores blanks and #-comments. Does not
    override values already set in the process environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_dotenv(ROOT / ".env")
API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
if not API_KEY:
    sys.exit("OPENWEATHER_API_KEY not set (check .env).")

# ── constants ───────────────────────────────────────────────────────────
API_URL = "https://api.openweathermap.org/data/2.5/weather"
REQUEST_TIMEOUT = 20  # seconds
CALL_INTERVAL_S = 1.1  # keep under 60/min limit


# ── domain helpers (Bible §14.9) ────────────────────────────────────────
# TMPV View Index — nova fórmula (3 eixos, cascata). Substitui a antiga
# média ponderada 50/35/15 (que saturava porque visibility_m == 10000 na
# esmagadora maioria dos destinos). A nova cascata:
#   1. base = céu (nublado tira menos de noite — luzes da cidade compensam)
#   2. − precipitação (chuva/trovoada; neve não penaliza — é bonita)
#   3. × factor de visibilidade (corta, não soma — sem ver landmark, não há vista)

def _sky_base(clouds_pct, is_day):
    """Base da nota, só a partir da % de céu tapado.
    Dia: limpo 92 → tapado 72. Noite: limpo 92 → tapado 80 (a nuvem não tira
    todas as luzes da cidade, mas tapa mais do que o valor generoso inicial)."""
    c = max(0.0, min(100.0, float(clouds_pct or 0))) / 100.0
    drop = 20.0 if is_day else 12.0
    return 92.0 - drop * c


def _precip_penalty(condition_id):
    """O que cai do céu. NEVE NÃO PENALIZA (neve bonita é vista; nevão mau já é
    apanhado pela visibilidade). Nevoeiro/neblina idem — entram pela visibilidade."""
    cid = int(condition_id or 800)
    if 200 <= cid <= 232: return 28.0   # trovoada
    if 300 <= cid <= 321: return 8.0    # chuvisco
    if 500 <= cid <= 501: return 12.0   # chuva leve/moderada
    if 502 <= cid <= 531: return 25.0   # chuva forte e acima
    return 0.0                          # neve, nevoeiro, nuvens, limpo


def _visibility_factor(visibility_m):
    """A visibilidade NÃO dá pontos — corta. Queda acentuada abaixo dos 10 km:
    se o landmark está a 7 km e só se vê a 5 km, não há vista nenhuma."""
    v = float(visibility_m if visibility_m is not None else 10000)
    pts = [(0, 0.0), (1000, 0.05), (3000, 0.25), (5000, 0.5), (8000, 0.9), (10000, 1.0)]
    if v >= 10000: return 1.0
    for i in range(1, len(pts)):
        x0, y0 = pts[i-1]; x1, y1 = pts[i]
        if v <= x1:
            return y0 + (y1 - y0) * ((v - x0) / (x1 - x0))
    return 1.0


def compute_view_index(clouds_pct, condition_id, visibility_m, sunrise, sunset, now_ts):
    is_day = bool(sunrise and sunset and sunrise <= now_ts <= sunset)
    base = _sky_base(clouds_pct, is_day)
    base -= _precip_penalty(condition_id)
    score = max(0.0, base) * _visibility_factor(visibility_m)
    return int(round(max(0.0, min(100.0, score))))


def rating_for(index):
    """Palavras que cabem na frase 'the conditions for enjoying a view are ___'.
    Escala alinhada com a fórmula: limpo dia ≈ 92, tapado seco ≈ 72.
    Perfect exige céu limpo + boa visibilidade + sem chuva (nada menos)."""
    if index >= 88: return "Perfect"
    if index >= 80: return "Beautiful"
    if index >= 65: return "Good"
    if index >= 45: return "Fair"
    if index >= 25: return "Limited"
    return "Obscured"


# ── data loading ────────────────────────────────────────────────────────
def load_destination_meta(csv_path: Path) -> dict:
    """Return {destination_name: {"slug": ..., "timezone": ...}} from
    destinations.csv. Two-row header: group titles + column names."""
    meta = {}
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    # rows[0] = group titles, rows[1] = column names; data from rows[2:]
    for r in rows[2:]:
        if len(r) < 12:
            continue
        status = r[6].strip()
        if status != "Published":
            continue
        name = r[3].strip()
        if not name:
            continue
        timezone = r[7].strip()
        page_url = r[11].strip()  # e.g. "/london-hotel-views"
        slug = page_url.strip("/")
        meta[name] = {"slug": slug, "timezone": timezone}
    return meta


def load_hotels(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def group_hotels_by_destination(hotels: list[dict]) -> dict:
    """{destination_name: [(lat, lng), ...]} for Active hotels with valid coords."""
    groups: dict[str, list[tuple[float, float]]] = {}
    for h in hotels:
        if (h.get("Status") or "").strip() != "Active":
            continue
        dest = (h.get("Destination") or "").strip()
        if not dest:
            continue
        try:
            lat = float(str(h.get("Lat", "")).strip())
            lng = float(str(h.get("Lng", "")).strip())
        except (TypeError, ValueError):
            continue
        groups.setdefault(dest, []).append((lat, lng))
    return groups


def centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(coords)
    return (sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n)


# ── OWM call ────────────────────────────────────────────────────────────
def fetch_weather(lat: float, lng: float) -> dict:
    """One /data/2.5/weather call. Raises on non-200 or JSON error."""
    qs = urllib.parse.urlencode({
        "lat": f"{lat:.5f}",
        "lon": f"{lng:.5f}",
        "units": "metric",
        "appid": API_KEY,
    })
    url = f"{API_URL}?{qs}"
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read())


# ── build ───────────────────────────────────────────────────────────────
def build_destination_entry(dest_name: str, coords_list: list, meta: dict) -> dict:
    lat, lng = centroid(coords_list)
    raw = fetch_weather(lat, lng)
    main = raw.get("main") or {}
    weather0 = (raw.get("weather") or [{}])[0]
    sys_ = raw.get("sys") or {}
    clouds = raw.get("clouds") or {}
    visibility_m = raw.get("visibility")
    condition_main = weather0.get("main", "")
    condition_id = weather0.get("id")
    clouds_pct = clouds.get("all")
    sunrise_ts = sys_.get("sunrise")
    sunset_ts  = sys_.get("sunset")
    now_ts = int(time.time())
    score = compute_view_index(clouds_pct, condition_id, visibility_m,
                               sunrise_ts, sunset_ts, now_ts)
    return {
        "temp_c":                main.get("temp"),
        "feels_like_c":          main.get("feels_like"),
        "condition_id":          condition_id,
        "condition_main":        condition_main,
        "condition_description": weather0.get("description"),
        "clouds_pct":            clouds_pct,
        "visibility_m":          visibility_m,
        "sunrise":               sunrise_ts,
        "sunset":                sunset_ts,
        "view_index":            score,
        "view_rating":           rating_for(score),
        "timezone":              (meta.get("timezone") or "") if meta else "",
        "coords": {
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "n_hotels": len(coords_list),
        },
        "fetched_at": now_ts,
    }


def main():
    print(f"→ loading destinations.csv ({DEST_CSV})")
    meta = load_destination_meta(DEST_CSV)
    print(f"  {len(meta)} destinos Published")

    print(f"→ loading hotels.json ({HOTELS_JSON})")
    hotels = load_hotels(HOTELS_JSON)
    groups = group_hotels_by_destination(hotels)
    print(f"  {len(hotels)} hotéis, {len(groups)} destinos únicos c/ Active+coords")

    missing_meta = sorted(g for g in groups if g not in meta)
    if missing_meta:
        print(f"  ⚠ {len(missing_meta)} destinos em hotels.json sem entry em destinations.csv:")
        for g in missing_meta:
            print(f"     · {g!r}")

    entries = {}
    errors = []
    processed = 0
    total = len(groups)
    for i, (dest_name, coords) in enumerate(sorted(groups.items()), 1):
        m = meta.get(dest_name)
        if m is None or not m.get("slug"):
            errors.append((dest_name, "no slug (destination not Published in CSV)"))
            continue
        slug = m["slug"]
        try:
            entry = build_destination_entry(dest_name, coords, m)
            entries[slug] = entry
            processed += 1
            print(f"  [{i:>3}/{total}] {slug:<50} temp={entry['temp_c']}°C  "
                  f"vi={entry['view_index']} ({entry['view_rating']})")
        except Exception as e:
            errors.append((dest_name, str(e)))
            print(f"  [{i:>3}/{total}] {dest_name!r}: ERROR {e}", file=sys.stderr)
        time.sleep(CALL_INTERVAL_S)

    payload = {
        "generated_at": int(time.time()),
        "source": "openweathermap 2.5",
        "destinations": entries,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print()
    print(f"✓ written {OUT_JSON}")
    print(f"  {processed} destinos processados")
    if errors:
        print(f"  {len(errors)} erros:")
        for name, msg in errors:
            print(f"     · {name!r}: {msg}")
    else:
        print("  0 erros")


if __name__ == "__main__":
    main()
