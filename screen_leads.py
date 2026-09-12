#!/usr/bin/env python3
"""Rimanda a "lead" i palcoscenici che sono "da contattare" per sbaglio.

Fino alla build 260912.2156 ogni palcoscenico nuovo apriva subito una serata
per la stagione in corso: bastava trascrivere un indirizzo perche' l'app lo
dichiarasse da contattare e aprisse un tentativo che nessuno aveva deciso di
fare. Quella riga adesso non c'e' piu' — un palcoscenico nuovo nasce lead e
la serata nasce quando decidi di provarci — ma i palcoscenici gia' in
archivio si portano dietro la serata fantasma di allora.

Qui si ripuliscono: se tutte le serate di un palcoscenico sono ancora "da
contattare" e sono vuote — niente data, niente compenso, niente scritto su
com'e' andata, nessuna attivita' appesa — allora quel tentativo non e' mai
cominciato. Le serate si cancellano e il palcoscenico torna lead.

Non tocca niente che sia successo davvero: le note restano attaccate al
palcoscenico (perdono solo il legame con il ciclo che non c'e' piu', come
quando si elimina una serata dalla scheda), il promemoria di ricontatto
resta dov'e', e un palcoscenico con anche una sola serata vera non viene
nemmeno guardato.

NON e' una migrazione e non gira da sola all'avvio, apposta: la stessa
regola applicata ogni volta cancellerebbe la serata che hai appena aperto e
non hai ancora avuto il tempo di riempire. Si lancia a mano, una volta.

Uso:
  python3 screen_leads.py            # dice cosa farebbe, senza toccare niente
  python3 screen_leads.py --apply    # lo fa, dopo aver messo via una copia del database
"""

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crm.db")

# Una serata "vera" e' una su cui e' successo qualcosa. Basta un segno solo.
SERATA_VERA = """
    EXISTS(SELECT 1 FROM gigs g WHERE g.location_id = l.id AND (
        g.status != 'da_contattare'
        OR g.gig_date IS NOT NULL
        OR g.fee IS NOT NULL
        OR TRIM(COALESCE(g.outcome_note, '')) != ''
        OR EXISTS(SELECT 1 FROM notes n WHERE n.gig_id = g.id)))
"""

DA_SCREMARE = """
    SELECT l.id, l.name, l.city, l.next_contact_date,
           (SELECT COUNT(*) FROM gigs g WHERE g.location_id = l.id) AS serate,
           (SELECT COUNT(*) FROM notes n WHERE n.location_id = l.id
              AND n.kind IS NOT NULL AND n.kind != 'nota') AS attivita
    FROM locations l
    WHERE l.status = 'da_contattare' AND NOT
""" + SERATA_VERA + " ORDER BY l.name COLLATE NOCASE"


def main():
    applica = "--apply" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    righe = conn.execute(DA_SCREMARE).fetchall()

    if not righe:
        print("Niente da scremare: nessun palcoscenico 'da contattare' senza serate vere.")
        return

    print("%d palcoscenici tornano lead:\n" % len(righe))
    for r in righe:
        segni = []
        if r["next_contact_date"]:
            segni.append("promemoria " + r["next_contact_date"])
        if r["attivita"]:
            segni.append("%d attività registrate" % r["attivita"])
        print("  %-38s %-26s %s" % (
            (r["name"] or "senza nome")[:38], (r["city"] or "—")[:26],
            " · ".join(segni)))

    if not applica:
        print("\nProva a vuoto: non ho toccato niente. Rilancia con --apply per farlo davvero.")
        return

    copia = DB_PATH + ".bak.pre-scrematura." + datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(DB_PATH, copia)
    print("\nCopia del database in %s" % os.path.basename(copia))

    ids = [r["id"] for r in righe]
    segna = ",".join("?" for _ in ids)
    ts = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "UPDATE notes SET gig_id = NULL WHERE gig_id IN "
            "(SELECT id FROM gigs WHERE location_id IN (%s))" % segna, ids)
        cur = conn.execute("DELETE FROM gigs WHERE location_id IN (%s)" % segna, ids)
        serate = cur.rowcount
        conn.execute(
            "UPDATE locations SET status = 'lead', updated_at = ? WHERE id IN (%s)" % segna,
            [ts] + ids)
    print("Fatto: %d serate mai cominciate cancellate, %d palcoscenici tornati lead."
          % (serate, len(ids)))


if __name__ == "__main__":
    main()
