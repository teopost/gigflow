---
description: Implementa una issue di GitHub etichettata ready, dal codice al deploy
argument-hint: "[numero della issue — vuoto: prende la prima ready]"
allowed-tools: Bash(gh issue:*), Bash(gh label:*), Bash(gh api:*), Bash(git:*), Bash(docker compose:*), Bash(make:*), Bash(curl:*)
---

Implementa la issue **$ARGUMENTS** del repo `teopost/gigflow`.

Se non c'e' nessun numero, prendi la prima della lista:
`gh issue list --label ready --state open --json number,title --jq 'sort_by(.number) | .[0]'`
Se la lista e' vuota, dillo e fermati: non c'e' niente di approvato da fare.

## Le etichette sono il permesso, non un promemoria

`ready` la mette Stefano, e vuol dire "questa si puo' fare". E' l'unico posto
dove decide cosa entra nell'app: se la togli o la aggiri, il processo non
serve piu' a niente.

- niente etichetta → l'idea e' ancora sua, **non toccarla**
- `ready` → puoi partire
- `in-progress` → la metti tu quando inizi
- `to-test` → la metti tu quando hai deployato, e la palla torna a lui

**Non chiudere mai la issue.** Chiudere vuol dire "funziona", e quello lo puo'
dire solo chi l'ha provata sul telefono. Per la stessa ragione il messaggio di
commit scrive `Issue #N` e **mai** `Closes #N`: la parola magica chiuderebbe
la issue da sola al push, scavalcando il collaudo.

## I passi

1. **Leggi tutto**: `gh issue view N --comments`. I commenti spesso cambiano la
   richiesta piu' del titolo.
2. **Controlla l'etichetta.** Senza `ready`, fermati e chiedi: e' una scelta
   sua, non una formalita' da sbrigare.
3. **Se la richiesta e' ambigua, chiedi prima di scrivere codice.** Una
   domanda adesso costa meno di un deploy da rifare.
4. `gh issue edit N --add-label in-progress --remove-label ready`
5. Implementa. Le convenzioni della casa valgono tutte: un nome di classe CSS
   non si riusa, le schede restano bozze finche' non si conferma, i compensi
   si proiettano da `gigs.fee`.
6. **Commit su `main`** (niente branch, salvo modifiche grosse: si deploya da
   `main`, una PR aggiungerebbe solo un merge prima di poter provare).
   Messaggio in italiano, nello stile del repo — cosa cambia e perche' — e in
   fondo, su una riga sua: `Issue #N`
7. `git push` — senza, la issue su GitHub non vede mai il commit.
8. **Deploy**: `docker compose up -d --build`. Non e' opzionale e non basta
   `restart`: `static/` e' dentro l'immagine, senza `--build` continueresti a
   servire i file di ieri.
9. **Prendi build e impronta** dall'app appena riavviata:
   `curl -s http://localhost:8765/api/version`
   (`build` e' il nome leggibile, `version` e' l'impronta dei file statici).
10. **Commenta sulla issue** con `gh issue comment N --body '...'`:
    - la riga `build <build> · <version>`, che e' quella che gli serve per
      sapere se sul telefono sta guardando la versione giusta o quella vecchia
    - una o due righe su cosa e' cambiato
    - **cosa toccare per verificarlo**, partendo dalla scheda
11. `gh issue edit N --add-label to-test --remove-label in-progress`
12. Nella risposta in chat ripeti build e impronta: il commento sulla issue
    resta scritto, ma lui sta leggendo qui.

Se ti blocchi a meta' — il deploy non parte, la richiesta si rivela un'altra
cosa — **lascia `in-progress`**, commenta sulla issue dove sei arrivato e
dillo. Un'etichetta che mente e' peggio di un lavoro non finito.
