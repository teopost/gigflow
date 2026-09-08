SERVICE := gigflow
IMAGE   := gigflow

.PHONY: help build up down restart rebuild logs ps shell clean import geocode

help:
	@echo "make build     - costruisce l'immagine Docker"
	@echo "make up        - avvia il container in background"
	@echo "make down      - ferma e rimuove il container"
	@echo "make restart   - riavvia il container"
	@echo "make rebuild   - ricostruisce l'immagine da zero (senza cache) e riavvia"
	@echo "make logs      - segue i log del container"
	@echo "make ps        - mostra lo stato del container"
	@echo "make shell     - apre una shell dentro il container"
	@echo "make clean     - ferma il container e rimuove container + immagine (i dati in data/ restano intatti)"
	@echo "make import    - rilancia lo script di importazione Excel dentro il container"
	@echo "make geocode   - rilancia la geocodifica dei palcoscenici dentro il container"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

rebuild:
	docker compose build --no-cache
	docker compose up -d

logs:
	docker compose logs -f

ps:
	docker compose ps

shell:
	docker compose exec $(SERVICE) sh

clean:
	docker compose down --rmi local

import:
	docker compose exec $(SERVICE) python3 import_excel.py

geocode:
	docker compose exec $(SERVICE) python3 geocode_venues.py
