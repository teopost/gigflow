#!/usr/bin/env python3
"""Geocodifica i palcoscenici che hanno un indirizzo/città ma non ancora
coordinate, usando Nominatim (OpenStreetMap, gratuito, nessuna chiave API).

Rispetta la policy di Nominatim: user-agent identificativo, massimo 1
richiesta al secondo, e raggruppa i palcoscenici con la stessa città per
evitare richieste duplicate.

Uso:  python3 geocode_venues.py
"""

import json
import re
import time
import urllib.parse
import urllib.request

import app as piazze_app

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PalcosceniciCRM/1.0 (gestionale locale per band, uso personale)"
RATE_LIMIT_SECONDS = 1.1

CITY_PROVINCE_RE = re.compile(r"^(.*?)\s*\(([A-Za-z]{2,3})\)\s*$")


def build_query(address, city):
    city = (city or "").strip()
    address = (address or "").strip()
    m = CITY_PROVINCE_RE.match(city)
    if m:
        city_name, province = m.group(1).strip(), m.group(2).strip()
        location_part = f"{city_name}, {province}"
    else:
        location_part = city

    if address and location_part:
        return f"{address}, {location_part}, Italia"
    if address:
        return f"{address}, Italia"
    if location_part:
        return f"{location_part}, Italia"
    return None


def geocode(query):
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "it",
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


def main():
    conn = piazze_app.get_conn()
    rows = conn.execute(
        """
        SELECT id, name, address, city FROM locations
        WHERE lat IS NULL AND (
            (address IS NOT NULL AND address != '') OR
            (city IS NOT NULL AND city != '')
        )
        """
    ).fetchall()
    print(f"Palcoscenici da geocodificare: {len(rows)}")

    # Raggruppa per query di geocodifica identica (stessa città/indirizzo)
    # per evitare di interrogare Nominatim più volte per lo stesso posto.
    groups = {}
    for r in rows:
        query = build_query(r["address"], r["city"])
        if not query:
            continue
        groups.setdefault(query, []).append(r["id"])

    print(f"Query uniche da geocodificare: {len(groups)}")

    geocoded, failed = 0, []
    for i, (query, ids) in enumerate(sorted(groups.items()), 1):
        try:
            result = geocode(query)
        except Exception as e:
            result = None
            print(f"  [{i}/{len(groups)}] ERRORE su \"{query}\": {e}")

        if result:
            lat, lng = result
            for loc_id in ids:
                piazze_app.update_location(conn, loc_id, {"lat": lat, "lng": lng})
            geocoded += len(ids)
            print(f"  [{i}/{len(groups)}] OK  \"{query}\" -> {lat:.4f}, {lng:.4f} ({len(ids)} palcoscenici)")
        else:
            failed.append((query, ids))
            print(f"  [{i}/{len(groups)}] NON TROVATO \"{query}\" ({len(ids)} palcoscenici)")

        if i < len(groups):
            time.sleep(RATE_LIMIT_SECONDS)

    conn.close()

    print(f"\nFatto. Geocodificati: {geocoded}. Non trovati: {sum(len(ids) for _, ids in failed)}.")
    if failed:
        print("Query senza risultato (da controllare/inserire a mano):")
        for query, ids in failed:
            print(f"  - {query}  (id: {ids})")


if __name__ == "__main__":
    main()
