#!/usr/bin/env python3
"""Geocodifica i palchi che hanno un indirizzo/città ma non ancora
coordinate, usando Nominatim (OpenStreetMap, gratuito, nessuna chiave API).

Le regole della ricerca — quali domande fare, come riconoscere la risposta
giusta, la pausa fra una richiesta e l'altra — stanno in app.py e basta:
questo script è solo il giro in blocco da riga di comando. Quando qui c'era
una seconda copia le due si erano già divise, e la copia di qui era rimasta
indietro di un difetto.

Uso:
  python3 geocode_venues.py            # geocodifica solo chi non ha ancora lat/lng
  python3 geocode_venues.py --all      # ricalcola anche chi ha già lat/lng
  python3 geocode_venues.py --dry-run  # dice cosa farebbe, senza scrivere
"""

import sys

import app as piazze_app


def main():
    argomenti = sys.argv[1:]
    force_all = "--all" in argomenti
    prova = "--dry-run" in argomenti

    conn = piazze_app.get_conn()
    where = "(address IS NOT NULL AND address != '') OR (city IS NOT NULL AND city != '')"
    if not force_all:
        where = f"lat IS NULL AND ({where})"
    rows = conn.execute(
        f"SELECT id, name, address, city, lat, lng FROM locations WHERE {where}"
    ).fetchall()
    print(f"Palchi da geocodificare: {len(rows)}" + (" (prova, non scrivo)" if prova else ""))

    # La cache è di tutto il giro: molti palchi condividono la città,
    # e senza cache sarebbe una richiesta a testa per la stessa risposta.
    cache = {}
    precisi, approssimati, incerti, falliti = 0, 0, 0, []
    for i, r in enumerate(rows, 1):
        domande, domanda_citta, nomi = piazze_app.geo_candidates(r["name"], r["address"], r["city"])
        etichetta = f"{r['name']} (id {r['id']})"
        if not domande:
            continue
        punto = piazze_app.geo_best(domande, domanda_citta, nomi, cache)
        if not punto:
            falliti.append(etichetta)
            print(f"  [{i}/{len(rows)}] NON TROVATO \"{etichetta}\"")
            continue
        lat, lng, precisione = punto
        if precisione != "preciso" and piazze_app.geo_citta_incerta(r["city"]):
            precisione = "centro incerto"
        if precisione == "preciso":
            precisi += 1
        elif precisione == "centro incerto":
            incerti += 1
        else:
            approssimati += 1
        if not prova:
            conn.execute(
                "UPDATE locations SET lat = ?, lng = ?, updated_at = ? WHERE id = ?",
                (lat, lng, piazze_app.now_iso(), r["id"]),
            )
            conn.commit()
        print(f"  [{i}/{len(rows)}] {precisione.upper()} \"{etichetta}\" -> {lat:.5f}, {lng:.5f}")

    conn.close()

    print(f"\nFatto. Precisi: {precisi}. Solo centro città: {approssimati}. "
          f"Centro di una città ambigua: {incerti}. Non trovati: {len(falliti)}.")
    if incerti:
        print("I 'centro incerto' sono città che possono essere più comuni (o che non sono")
        print("comuni italiani): scrivi la provincia fra parentesi, tipo «Misano (RN)», e rilancia.")
    if falliti:
        print("Palchi senza risultato (da controllare/inserire a mano):")
        for etichetta in falliti:
            print(f"  - {etichetta}")


if __name__ == "__main__":
    main()
