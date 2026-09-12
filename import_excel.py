#!/usr/bin/env python3
"""Importa palcoscenici e band nel database del CRM da uno o più file Excel.

Riconosce automaticamente il tipo di ogni foglio dall'intestazione:
- colonne "Locale, Citta, Pagina Facebook, Identita, Tipo, ..."   -> palcoscenici
- colonne "Band, Pagina Facebook, Follower, Base, ..."            -> band
- colonne "..., Stato Lead, ..." (export lead da Zoho CRM)        -> palcoscenici

Rilanciabile: salta i record già presenti (stesso nome, confronto case-insensitive).

Uso:
  python3 import_excel.py                  # importa tutti i .xlsx nella cartella dello script
  python3 import_excel.py file1.xlsx ...   # importa solo i file indicati
"""

import glob
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

import app as piazze_app

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

VENUE_TYPE_MAP = {
    "locale": "Locale / club",
    "evento / sagra": "Sagra",
    "spazio pubblico": "Spazio pubblico",
    "da verificare": "Da verificare",
}

LEAD_STATUS_MAP = {
    "non contattato": "lead",
    "inviata mail": "contattato",
    "inviato whatsapp": "contattato",
    "inviata sia mail che whatsapp": "contattato",
    "incontrato": "trattativa",
    "in attesa di data": "confermato",
}


# ---------------------------------------------------------------- lettura xlsx

def col_letters(ref):
    return re.match(r"([A-Z]+)(\d+)", ref).group(1)


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def read_shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    strings = []
    for si in root.findall("m:si", NS):
        strings.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    return strings


def read_sheet_rows(z, sheet_path, shared_strings):
    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for row in root.find("m:sheetData", NS):
        cells = {}
        for c in row.findall("m:c", NS):
            idx = col_index(col_letters(c.get("r")))
            t = c.get("t")
            if t == "inlineStr":
                is_el = c.find("m:is", NS)
                t_el = is_el.find("m:t", NS) if is_el is not None else None
                val = t_el.text if t_el is not None and t_el.text else ""
            elif t == "s":
                v_el = c.find("m:v", NS)
                si = int(v_el.text) if v_el is not None and v_el.text else None
                val = shared_strings[si] if si is not None and si < len(shared_strings) else ""
            else:
                v_el = c.find("m:v", NS)
                val = v_el.text if v_el is not None else ""
            cells[idx] = val
        if cells:
            maxidx = max(cells.keys())
            rows.append([cells.get(i, "") for i in range(1, maxidx + 1)])
    return rows


def read_all_sheets(xlsx_path):
    """Ritorna {nome_foglio: righe} per ogni foglio del file."""
    z = zipfile.ZipFile(xlsx_path)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    def resolve_target(target):
        # I riferimenti possono essere assoluti ("/xl/worksheets/sheet1.xml")
        # o relativi alla cartella "xl/" ("worksheets/sheet1.xml").
        if target.startswith("/"):
            return target.lstrip("/")
        return "xl/" + target

    rid_to_target = {rel.get("Id"): resolve_target(rel.get("Target")) for rel in rels}
    shared_strings = read_shared_strings(z)

    r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    sheets = {}
    for sheet in wb.find("m:sheets", NS):
        name = sheet.get("name")
        path = rid_to_target[sheet.get(r_ns)]
        sheets[name] = read_sheet_rows(z, path, shared_strings)
    return sheets


def clean(v):
    return (v or "").strip()


def norm_name(name):
    """Chiave di confronto per il rilevamento duplicati: ignora maiuscole/minuscole
    e qualsiasi punteggiatura/spaziatura ("Bagno Italia & Giuliana" ==
    "Bagno Italia&Giuliana")."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def row_get(header_index, row, name):
    i = header_index.get(name)
    if i is None or i >= len(row):
        return ""
    return clean(row[i])


# ---------------------------------------------------------------- palcoscenici (elenco deduplicato)

def build_venue_type(tipo_raw, name):
    tipo_raw = tipo_raw.lower().strip()
    mapped = VENUE_TYPE_MAP.get(tipo_raw, tipo_raw.capitalize() if tipo_raw else None)
    if (mapped == "Locale / club" or not mapped) and name.lower().startswith("bagno"):
        return "Bagno / stabilimento balneare"
    return mapped


def import_venues_sheet(conn, rows, existing_names, source_label):
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    created, skipped = 0, 0
    for row in rows[1:]:
        name = row_get(idx, row, "Locale")
        if not name:
            continue
        if norm_name(name) in existing_names:
            skipped += 1
            continue

        citta = row_get(idx, row, "Citta")
        fb = row_get(idx, row, "Pagina Facebook")
        identita = row_get(idx, row, "Identita")
        tipo_raw = row_get(idx, row, "Tipo")
        n_band = row_get(idx, row, "N. band")
        n_date = row_get(idx, row, "N. date")
        band_list = row_get(idx, row, "Band che ci hanno suonato")
        nota = row_get(idx, row, "Nota")

        website = None if fb in ("", "-") else fb
        data = {
            "name": name,
            "city": citta or None,
            "website": website,
            "type": build_venue_type(tipo_raw, name),
            "status": "lead",
        }
        location = piazze_app.create_location(conn, data)

        parts = [f"Importato da \"{source_label}\"."]
        if n_band or n_date:
            parts.append(f"Band note che ci hanno già suonato: {n_band or '0'} (per un totale di {n_date or '0'} date rilevate).")
        if band_list:
            parts.append(f"Nomi: {band_list}.")
        if nota:
            parts.append(f"Nota della ricerca: {nota}")
        if identita == "probabile":
            parts.append("Attenzione: corrispondenza del locale trovata online non certa, da verificare.")
        piazze_app.add_note(conn, location["id"], {"text": " ".join(parts)})

        existing_names.add(norm_name(name))
        created += 1
        print(f"  + importato: {name} ({data['city'] or 'città n.d.'}) — tipo: {data['type']}")
    print(f"  -> creati: {created}, già presenti e saltati: {skipped}")
    return created, skipped


# ---------------------------------------------------------------- band

def import_bands_sheet(conn, rows, existing_names, source_label):
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    created, skipped = 0, 0
    for row in rows[1:]:
        name = row_get(idx, row, "Band")
        if not name:
            continue
        if norm_name(name) in existing_names:
            skipped += 1
            continue

        fb = row_get(idx, row, "Pagina Facebook")
        follower = row_get(idx, row, "Follower")
        base = row_get(idx, row, "Base")
        contatto = row_get(idx, row, "Contatto trovato")
        n_date = row_get(idx, row, "N. date rilevate")

        data = {
            "name": name,
            "facebook": fb or None,
            "followers": follower or None,
            "base": base or None,
            "contact": contatto or None,
            "gigs_count": n_date or None,
            "notes": f"Importato da \"{source_label}\".",
        }
        piazze_app.create_band(conn, data)
        existing_names.add(norm_name(name))
        created += 1
        print(f"  + importata: {name} ({base or 'base n.d.'}) — follower: {follower or 'n.d.'}")
    print(f"  -> create: {created}, già presenti e saltate: {skipped}")
    return created, skipped


# ---------------------------------------------------------------- lead Zoho CRM -> palcoscenici

def build_lead_status(stato_raw):
    return LEAD_STATUS_MAP.get(stato_raw.lower().strip(), "lead")


def import_leads_sheet(conn, rows, existing_names, source_label):
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    created, skipped, backfilled = 0, 0, 0
    for row in rows[1:]:
        name = row_get(idx, row, "Cognome")  # il nome del locale è nel campo "Cognome" del lead
        if not name:
            continue

        nome_referente = row_get(idx, row, "Nome")

        if norm_name(name) in existing_names:
            skipped += 1
            # Il locale è già stato importato in precedenza: se manca ancora
            # il nome del titolare/contatto, lo recuperiamo ora da questo foglio.
            if nome_referente:
                existing = conn.execute(
                    "SELECT id, contact_name FROM locations WHERE LOWER(name) = LOWER(?)", (name,)
                ).fetchone()
                if existing and not existing["contact_name"]:
                    piazze_app.update_location(conn, existing["id"], {"contact_name": nome_referente})
                    backfilled += 1
            continue

        stato_raw = row_get(idx, row, "Stato Lead")
        cellulare = row_get(idx, row, "Cellulare")
        telefono = row_get(idx, row, "Telefono")
        citta = row_get(idx, row, "Città")
        provincia = row_get(idx, row, "Provincia")
        email = row_get(idx, row, "E-mail")
        valutazione = row_get(idx, row, "Valutazione")
        sito = row_get(idx, row, "Sito Web")

        city = f"{citta} ({provincia})" if citta and provincia else (citta or provincia or None)
        phone = cellulare or telefono or None

        data = {
            "name": name,
            "city": city,
            "contact_name": nome_referente or None,
            "phone": phone,
            "email": email or None,
            "website": sito or None,
            "type": build_venue_type("", name),
            "status": build_lead_status(stato_raw),
        }
        location = piazze_app.create_location(conn, data)

        parts = [f"Importato da \"{source_label}\" (lead CRM)."]
        if cellulare and telefono:
            parts.append(f"Telefono fisso: {telefono}.")
        if valutazione:
            parts.append(f"Valutazione/nota CRM originale: {valutazione}.")
        if stato_raw:
            parts.append(f"Stato lead originale: \"{stato_raw}\".")
        piazze_app.add_note(conn, location["id"], {"text": " ".join(parts)})

        existing_names.add(norm_name(name))
        created += 1
        print(f"  + importato: {name} ({city or 'città n.d.'}) — stato: {data['status']}")
    print(f"  -> creati: {created}, già presenti e saltati: {skipped}, titolare recuperato per {backfilled} già esistenti")
    return created, skipped


# ---------------------------------------------------------------- dispatch

def detect_sheet_kind(header):
    header_set = set(header)
    if "Stato Lead" in header_set and "Cognome" in header_set:
        return "leads"
    if {"Locale", "Citta", "Pagina Facebook"}.issubset(header_set):
        return "venues"
    if {"Band", "Pagina Facebook", "Follower"}.issubset(header_set):
        return "bands"
    return None


def process_file(conn, xlsx_path, existing_venue_names, existing_band_names):
    label = os.path.basename(xlsx_path)
    print(f"\n=== {label} ===")
    sheets = read_all_sheets(xlsx_path)
    any_recognized = False
    for sheet_name, rows in sheets.items():
        if not rows or not rows[0]:
            continue
        kind = detect_sheet_kind(rows[0])
        source_label = f"{label}, foglio {sheet_name}"
        if kind == "venues":
            any_recognized = True
            print(f"[{sheet_name}] -> palcoscenici")
            import_venues_sheet(conn, rows, existing_venue_names, source_label)
        elif kind == "bands":
            any_recognized = True
            print(f"[{sheet_name}] -> band")
            import_bands_sheet(conn, rows, existing_band_names, source_label)
        elif kind == "leads":
            any_recognized = True
            print(f"[{sheet_name}] -> palcoscenici (da lead CRM)")
            import_leads_sheet(conn, rows, existing_venue_names, source_label)
        else:
            print(f"[{sheet_name}] foglio non riconosciuto, saltato (colonne: {rows[0]})")
    if not any_recognized:
        print("  Nessun foglio con un formato riconosciuto in questo file.")


def main():
    args = sys.argv[1:]
    if args:
        files = args
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        files = sorted(glob.glob(os.path.join(base_dir, "*.xlsx")))

    piazze_app.init_db()
    conn = piazze_app.get_conn()

    existing_venue_names = {
        norm_name(r["name"])
        for r in conn.execute("SELECT name FROM locations").fetchall()
        if r["name"]
    }
    existing_band_names = {
        norm_name(r["name"])
        for r in conn.execute("SELECT name FROM bands").fetchall()
        if r["name"]
    }

    for path in files:
        process_file(conn, path, existing_venue_names, existing_band_names)

    conn.close()


if __name__ == "__main__":
    main()
