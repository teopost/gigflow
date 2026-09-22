#!/usr/bin/env python3
"""Le serate che erano contatti tornano a essere contatti.

Fino al 15 settembre 2026 registrare un'attivita' su un palco senza
serate aperte ne apriva una, in stato "contattato". E l'import dell'archivio
di partenza ha fatto la stessa cosa in grande: lo stato del CRM vecchio
("Inviata Mail", "Inviato Whatsapp") e' diventato una serata.

Il risultato, misurato prima di scrivere questo script: 221 serate senza
data, senza compenso e senza una riga scritta, tutte in stato "contattato".
Non erano occasioni di suonare: erano mail e messaggi. Il registro dei
contatti era finito dentro le opportunita'.

Qui si rimette ogni cosa al suo posto:

  1. la serata fantasma si cancella — non aveva niente dentro;
  2. al suo posto nasce un'attivita' in uscita, con il canale giusto: il
     testo delle note dell'import si e' tenuto lo stato originale del CRM
     ("Stato lead originale: ...") e da li' si sa se era una mail, un
     whatsapp o tutti e due;
  3. l'attivita' porta la data del file da cui arrivano quei contatti
     (28 agosto 2026): a quel giorno erano gia' partiti, ed e' piu' vero del
     giorno in cui sono stati importati;
  4. il palco che era "inattivo" torna "lead": ci abbiamo scritto e
     non ci hanno risposto, e questo e' esattamente un lead contattato.
     Prospect e Lead restano dove sono — quelli li ha decisi una persona.

Non tocca niente che sia successo davvero: una serata con una data, un
compenso, una nota sull'esito o uno stato diverso da "contattato" non viene
nemmeno guardata. Chi ha gia' un'attivita' registrata non ne riceve una
doppia. Le attivita' appese alla serata cancellata restano sul palco
e perdono solo il legame con il ciclo, come quando si elimina una serata a
mano.

NON e' una migrazione e non gira da sola all'avvio, apposta: cancella righe,
e la stessa regola applicata ogni volta si porterebbe via le serate aperte
di domani. Si lancia a mano, una volta.

Uso:
  python3 serate_fantasma.py            # dice cosa farebbe, senza toccare niente
  python3 serate_fantasma.py --apply    # lo fa, dopo aver messo via una copia del database
"""

import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crm.db")

# Il giorno in cui quei contatti erano gia' partiti: e' la data del file da
# cui sono stati importati (Lead_2026_08_28.xlsx). Mezzogiorno per non farsi
# spostare il giorno dai fusi orari.
DATA_CONTATTO = "2026-08-28T12:00:00+00:00"

# Le serate da guardare: una sola per palco, "contattato", e vuota.
DA_RIPULIRE = """
    SELECT l.id, l.name, l.city, l.status,
           (SELECT g.id FROM gigs g WHERE g.location_id = l.id) AS gig_id,
           (SELECT COUNT(*) FROM notes n WHERE n.location_id = l.id
              AND n.kind IS NOT NULL AND n.kind != 'nota') AS attivita,
           (SELECT group_concat(n.text, ' ||| ') FROM notes n
              WHERE n.location_id = l.id) AS note
    FROM locations l
    WHERE l.deleted_at IS NULL
      AND (SELECT COUNT(*) FROM gigs g WHERE g.location_id = l.id) = 1
      AND EXISTS(SELECT 1 FROM gigs g WHERE g.location_id = l.id
                   AND g.status = 'contattato'
                   AND g.gig_date IS NULL
                   AND g.fee IS NULL
                   AND TRIM(COALESCE(g.outcome_note, '')) = '')
    ORDER BY l.name COLLATE NOCASE
"""

STATO_ORIGINALE = re.compile(r'Stato lead originale:\s*"([^"]+)"')

# Come li avevate cercati, tradotto nelle attivita' di oggi.
TESTI = {
    "email": "Email inviata · dall’archivio di partenza",
    "messaggio": "Messaggio inviato · dall’archivio di partenza",
}


def canali(note):
    """Da "Stato lead originale" alle attivita' da scrivere. Senza quella
    riga resta un messaggio solo: che il contatto ci sia stato lo dice la
    serata che stiamo cancellando, come sia partito non lo sa piu' nessuno."""
    trovato = STATO_ORIGINALE.search(note or "")
    if not trovato:
        return ["messaggio"]
    testo = trovato.group(1).lower()
    if "sia mail che whatsapp" in testo:
        return ["email", "messaggio"]
    if "whatsapp" in testo:
        return ["messaggio"]
    if "mail" in testo:
        return ["email"]
    return ["messaggio"]


def main():
    applica = "--apply" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    righe = conn.execute(DA_RIPULIRE).fetchall()

    if not righe:
        print("Niente da ripulire: nessuna serata rimasta a fare da registro dei contatti.")
        return

    conti = {"email": 0, "messaggio": 0, "gia_segnate": 0, "a_lead": 0}
    piano = []
    for r in righe:
        da_scrivere = [] if r["attivita"] else canali(r["note"])
        for k in da_scrivere:
            conti[k] += 1
        if r["attivita"]:
            conti["gia_segnate"] += 1
        if r["status"] == "inattivo":
            conti["a_lead"] += 1
        piano.append((r, da_scrivere))

    print("%d serate fantasma da cancellare.\n" % len(righe))
    print("  attivita' che nascono:")
    print("    %4d email inviate" % conti["email"])
    print("    %4d messaggi inviati" % conti["messaggio"])
    print("    %4d palchi hanno gia' un'attivita' segnata: nessun doppione"
          % conti["gia_segnate"])
    print("  palchi che tornano lead: %d (gli altri %d restano come sono)"
          % (conti["a_lead"], len(righe) - conti["a_lead"]))
    print("  data delle attivita': %s\n" % DATA_CONTATTO[:10])

    print("  primi dieci, per farsi un'idea:")
    for r, da_scrivere in piano[:10]:
        print("    %-34s %-22s %-10s -> %s" % (
            (r["name"] or "senza nome")[:34], (r["city"] or "—")[:22],
            r["status"],
            ", ".join(da_scrivere) or "(ha gia' un'attivita')"))

    if not applica:
        print("\nProva a vuoto: non ho toccato niente. Rilancia con --apply per farlo davvero.")
        return

    copia = DB_PATH + ".bak.pre-fantasmi." + datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(DB_PATH, copia)
    print("\nCopia del database in %s" % os.path.basename(copia))

    ts = datetime.now(timezone.utc).isoformat()
    scritte = cancellate = spostati = 0
    with conn:
        for r, da_scrivere in piano:
            for kind in da_scrivere:
                conn.execute(
                    "INSERT INTO notes (location_id, gig_id, kind, direction, text, created_at) "
                    "VALUES (?, NULL, ?, 'noi', ?, ?)",
                    (r["id"], kind, TESTI[kind], DATA_CONTATTO),
                )
                scritte += 1
            # Le attivita' appese alla serata restano sul palco.
            conn.execute("UPDATE notes SET gig_id = NULL WHERE gig_id = ?", (r["gig_id"],))
            conn.execute("DELETE FROM gigs WHERE id = ?", (r["gig_id"],))
            cancellate += 1
            if r["status"] == "inattivo":
                conn.execute(
                    "UPDATE locations SET status = 'lead', updated_at = ? WHERE id = ?",
                    (ts, r["id"]),
                )
                spostati += 1

    print("Fatto: %d serate cancellate, %d attivita' scritte, %d palchi tornati lead."
          % (cancellate, scritte, spostati))


if __name__ == "__main__":
    main()
