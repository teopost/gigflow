# GigFlow — gestionale live per la band.
# Immagine minimale: l'app usa solo la libreria standard di Python,
# quindi non serve installare nessuna dipendenza.

FROM python:3.12-slim

WORKDIR /app

COPY app.py import_excel.py geocode_venues.py ./
COPY static/ ./static/
RUN mkdir -p /app/data

# Niente utente dedicato: /app/data è un bind mount sulla cartella data/ del
# host (vedi compose.yaml), il cui proprietario è l'utente del host, non uno
# scelto qui in build. Con Docker rootless in particolare, un utente fisso
# nel container spesso non corrisponde a chi possiede quei file sul host e
# la scrittura sul database viene rifiutata — restare root nel container
# evita questa intera classe di problemi di permessi.
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/', timeout=3)" || exit 1

CMD ["python3", "app.py", "8765"]
