#!/usr/bin/env python3
"""Geocodifica i palcoscenici che hanno un indirizzo/città ma non ancora
coordinate, usando Nominatim (OpenStreetMap, gratuito, nessuna chiave API).

Rispetta la policy di Nominatim: user-agent identificativo, massimo 1
richiesta al secondo, e raggruppa i palcoscenici con la stessa query di
geocodifica per evitare richieste duplicate.

Per ogni palcoscenico si prova prima una ricerca più precisa (nome del
locale + città), che su Nominatim può agganciare il punto esatto del
locale se è mappato su OpenStreetMap; solo se non trova nulla si scende
al indirizzo/città. Il risultato di ogni tentativo viene inoltre
convalidato confrontandolo con il centro della sola città: se il
risultato "specifico" cade a più di MAX_DRIFT_KM dal centro città
dichiarato, viene scartato (evita match sbagliati in un'altra regione,
es. un omonimo altrove) e si usa il centro città come approssimazione
più prudente.

Uso:
  python3 geocode_venues.py            # geocodifica solo chi non ha ancora lat/lng
  python3 geocode_venues.py --all      # ricalcola anche chi ha già lat/lng
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

import app as piazze_app

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PalcosceniciCRM/1.0 (gestionale locale per band, uso personale)"
RATE_LIMIT_SECONDS = 1.1
MAX_DRIFT_KM = 30  # oltre questa distanza dal centro città, il match "specifico" è considerato sospetto

CITY_PROVINCE_RE = re.compile(r"^(.*?)\s*\(([A-Za-z]{2,3})\)\s*$")


def normalize_city(city):
    city = (city or "").strip()
    m = CITY_PROVINCE_RE.match(city)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return city, None


def build_candidates(name, address, city):
    """Ritorna le query da provare in ordine di precisione decrescente,
    più l'ultima usata anche come riferimento per la convalida (solo città)."""
    city_name, province = normalize_city(city)
    location_part = f"{city_name}, {province}" if province else city_name
    name = (name or "").strip()
    address = (address or "").strip()

    candidates = []
    if name and location_part:
        candidates.append(f"{name}, {location_part}, Italia")
    if address and location_part:
        candidates.append(f"{address}, {location_part}, Italia")
    elif address:
        candidates.append(f"{address}, Italia")
    city_query = f"{location_part}, Italia" if location_part else None
    if city_query:
        candidates.append(city_query)

    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered, city_query


def geocode(query):
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "it",
        "addressdetails": 1,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    r = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def geocode_best(candidates, city_query, cache):
    """Prova le query in ordine; scarta un match specifico se troppo lontano
    dal centro città dichiarato. Ritorna (lat, lng, precisione)."""
    city_result = None
    if city_query:
        if city_query not in cache:
            cache[city_query] = geocode(city_query)
            time.sleep(RATE_LIMIT_SECONDS)
        city_result = cache[city_query]

    for query in candidates:
        if query == city_query:
            continue
        if query not in cache:
            cache[query] = geocode(query)
            time.sleep(RATE_LIMIT_SECONDS)
        result = cache[query]
        if not result:
            continue
        if city_result:
            drift = haversine_km(result[0], result[1], city_result[0], city_result[1])
            if drift > MAX_DRIFT_KM:
                print(f"    scartato match sospetto per \"{query}\" ({drift:.0f} km dal centro città)")
                continue
        return result[0], result[1], "precisa"

    if city_result:
        return city_result[0], city_result[1], "centro città (approssimata)"
    return None


def main():
    force_all = "--all" in sys.argv[1:]

    conn = piazze_app.get_conn()
    where = "(address IS NOT NULL AND address != '') OR (city IS NOT NULL AND city != '')"
    if not force_all:
        where = f"lat IS NULL AND ({where})"
    rows = conn.execute(f"SELECT id, name, address, city, lat, lng FROM locations WHERE {where}").fetchall()
    print(f"Palcoscenici da geocodificare: {len(rows)}")

    cache = {}
    geocoded, approx, failed = 0, 0, []
    for i, r in enumerate(rows, 1):
        candidates, city_query = build_candidates(r["name"], r["address"], r["city"])
        if not candidates:
            continue
        result = geocode_best(candidates, city_query, cache)
        label = f"{r['name']} (id {r['id']})"
        if result:
            lat, lng, precision = result
            piazze_app.update_location(conn, r["id"], {"lat": lat, "lng": lng})
            geocoded += 1
            if precision != "precisa":
                approx += 1
            print(f"  [{i}/{len(rows)}] OK {precision} \"{label}\" -> {lat:.4f}, {lng:.4f}")
        else:
            failed.append(label)
            print(f"  [{i}/{len(rows)}] NON TROVATO \"{label}\"")

    conn.close()

    print(f"\nFatto. Geocodificati: {geocoded} (di cui solo a livello di centro città: {approx}). Non trovati: {len(failed)}.")
    if approx:
        print("Le coordinate 'centro città' sono un'approssimazione: per la posizione esatta serve l'indirizzo,")
        print("oppure puoi correggere manualmente lat/lng dalla scheda del palcoscenico nell'app.")
    if failed:
        print("Palcoscenici senza risultato (da controllare/inserire a mano):")
        for label in failed:
            print(f"  - {label}")


if __name__ == "__main__":
    main()
