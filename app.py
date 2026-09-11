#!/usr/bin/env python3
"""GigFlow — gestionale locale per i posti dove far suonare la band.

Server autonomo (solo libreria standard) con database SQLite.
Avvio:  python3 app.py [porta]
"""

import base64
import html
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import socket
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, unquote, quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "crm.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
PHOTOS_DIR = os.path.join(BASE_DIR, "data", "photos")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_EXT_CONTENT_TYPE = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp", "gif": "image/gif",
}

# --- login con Google (opzionale) -------------------------------------
# Se GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET non sono impostate, il login è
# disattivato e l'app si comporta come prima (nessuna autenticazione).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
# Chi puo' modificare i template che precaricano le band nuove. Non e' un
# ruolo dentro l'app come Leader: e' chi amministra questa installazione,
# quindi vive nel file .env e non nel database.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def is_admin(email):
    return bool(email) and (email or "").strip().lower() in ADMIN_EMAILS


SESSION_COOKIE = "session_id"
STATE_COOKIE = "oauth_state"
SESSION_TTL_DAYS = 30
INVITE_COOKIE = "invite_token"
# Il link di invito viene passato a mano (WhatsApp) e ne basta uno nuovo a
# ogni giro: tre ore bastano per mandarlo e farlo aprire, e sono poche
# abbastanza da non aver bisogno di revocarlo se finisce dove non doveva.
INVITE_TTL_HOURS = 3
# Quanto tempo ha chi apre il link per completare il giro su Google.
INVITE_COOKIE_TTL_SECONDS = 600
# secrets.token_urlsafe() non produce mai un punto, quindi separa le due
# parti dello stato senza possibilita' di equivoci.
STATE_INVITE_SEP = "."

# Tabelle i cui dati appartengono a una band e non devono mai attraversare i
# confini del workspace. notes e photos non sono qui: seguono la location.
WORKSPACE_SCOPED_TABLES = [
    "locations", "art_directors", "bands",
    "wa_templates", "venue_types", "venue_categories",
]
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
PUBLIC_PATHS = {
    "/login", "/auth/google", "/auth/google/callback", "/logout",
    # il manifest e il service worker devono restare raggiungibili senza
    # sessione: il sistema Android che genera l'app installata (WebAPK) li
    # legge senza le credenziali dell'utente, altrimenti installa solo una
    # scorciatoia al sito invece dell'app vera (icona generica, barra degli
    # indirizzi visibile).
    "/manifest.json", "/sw.js",
    # /join/<token> e' pubblico per forza: chi apre il link non ha ancora una
    # sessione, ed e' proprio il link a dargli il diritto di entrare.
}


def invite_from_state(state):
    """Il token di invito che era stato agganciato allo stato di OAuth."""
    if not state or STATE_INVITE_SEP not in state:
        return None
    return state.split(STATE_INVITE_SEP, 1)[1] or None


def auth_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

LOCATION_FIELDS = [
    "name", "type", "category", "address", "city", "lat", "lng",
    "contact_name", "phone", "email", "website", "capacity", "genre",
    "art_director_id", "status", "next_contact_date", "planning_note", "favorite",
    "owner_email",
]
ART_DIRECTOR_FIELDS = ["name", "phone", "email", "notes"]
BAND_FIELDS = ["name", "facebook", "followers", "base", "contact", "gigs_count", "notes"]

STATUS_VALUES = {
    "da_contattare", "contattato", "trattativa",
    "confermato", "suonato", "rifiutato",
}

# Gli stessi sei stati, ma applicati alla singola serata invece che al
# palcoscenico: e' quello che permette di ripartire da zero ogni stagione
# senza cancellare com'e' andata l'anno prima.
GIG_FIELDS = ["season", "status", "gig_date", "fee", "outcome_note"]

# Dopo "suonato" o "rifiutato" su quella stagione non c'e' piu' niente da
# fare: la serata si chiude e la prossima nasce come riga nuova.
CLOSING_STATUSES = {"suonato", "rifiutato"}

GIG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEASON_YEAR_RE = re.compile(r"(\d{4})")

# Il tipo di attivita' fatta sul palcoscenico. "nota" e' il default e copre
# tutto quello che si scriveva prima che le attivita' avessero un tipo.
NOTE_KINDS = {"nota", "visita", "chiamata", "messaggio", "email"}
NOTE_KIND_LABELS = {
    "visita": "Passato dal locale",
    "chiamata": "Telefonata",
    "messaggio": "Messaggio inviato",
    "email": "Email inviata",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS art_directors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT,
            address TEXT,
            city TEXT,
            lat REAL,
            lng REAL,
            phone TEXT,
            email TEXT,
            website TEXT,
            capacity INTEGER,
            genre TEXT,
            art_director_id INTEGER REFERENCES art_directors(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'da_contattare',
            next_contact_date TEXT,
            planning_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            facebook TEXT,
            followers INTEGER,
            base TEXT,
            contact TEXT,
            gigs_count INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS venue_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS venue_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS my_bands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            genre TEXT,
            city TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wa_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_profiles (
            email TEXT PRIMARY KEY,
            name TEXT,
            picture TEXT,
            artist_name TEXT,
            genre TEXT,
            city TEXT,
            band_roles TEXT,
            last_seen_at TEXT,
            profile_completed_at TEXT,
            onboarded_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            genre TEXT,
            city TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            invited_by TEXT,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, email)
        );

        CREATE TABLE IF NOT EXISTS invites (
            token TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            created_by TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT
        );

        CREATE TABLE IF NOT EXISTS app_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            message TEXT,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gigs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            season TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'da_contattare',
            gig_date TEXT,
            fee REAL,
            outcome_note TEXT,
            closed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gigs_location ON gigs(location_id, season);
        CREATE INDEX IF NOT EXISTS idx_gigs_date ON gigs(gig_date);
        CREATE INDEX IF NOT EXISTS idx_templates_kind ON app_templates(kind, position);
        CREATE INDEX IF NOT EXISTS idx_members_email ON workspace_members(email);
        CREATE INDEX IF NOT EXISTS idx_invites_workspace ON invites(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_locations_status ON locations(status);
        CREATE INDEX IF NOT EXISTS idx_photos_location ON photos(location_id);
        CREATE INDEX IF NOT EXISTS idx_locations_next_contact ON locations(next_contact_date);
        CREATE INDEX IF NOT EXISTS idx_notes_location ON notes(location_id);
        """
    )
    migrate_schema(conn)
    seed_app_templates(conn)
    # Le tipologie di default non sono piu' globali: nascono con il workspace,
    # dentro create_workspace.
    conn.commit()
    conn.close()


def migrate_schema(conn):
    """Aggiunge colonne introdotte dopo la creazione iniziale del DB, se mancanti."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(locations)").fetchall()}
    if "contact_name" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN contact_name TEXT")
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN deleted_at TEXT")
    if "favorite" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    if "category" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN category TEXT")
    if "owner_email" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN owner_email TEXT")

    my_band_cols = {row["name"] for row in conn.execute("PRAGMA table_info(my_bands)").fetchall()}
    if "genre" not in my_band_cols:
        conn.execute("ALTER TABLE my_bands ADD COLUMN genre TEXT")
    if "city" not in my_band_cols:
        conn.execute("ALTER TABLE my_bands ADD COLUMN city TEXT")

    for table in WORKSPACE_SCOPED_TABLES:
        table_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "workspace_id" not in table_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER")
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_workspace ON {table}(workspace_id)"
            )

    note_cols = {row["name"] for row in conn.execute("PRAGMA table_info(notes)").fetchall()}
    if "kind" not in note_cols:
        conn.execute("ALTER TABLE notes ADD COLUMN kind TEXT")
    if "gig_id" not in note_cols:
        # Senza REFERENCES: la nota resta appesa al palcoscenico anche se la
        # serata viene cancellata, il legame col ciclo e' un in piu'.
        conn.execute("ALTER TABLE notes ADD COLUMN gig_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_gig ON notes(gig_id)")

    profile_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)").fetchall()}
    if "active_workspace_id" not in profile_cols:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN active_workspace_id INTEGER")
    if "band_roles" not in profile_cols:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN band_roles TEXT")
    if "profile_completed_at" not in profile_cols:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_completed_at TEXT")
        # Il modulo di benvenuto serve a chi arriva adesso: a chi usa gia'
        # l'app comparirebbe come un fastidio a sorpresa. Si segnano tutti
        # come gia' passati di li'.
        conn.execute(
            "UPDATE user_profiles SET profile_completed_at = COALESCE(onboarded_at, created_at) "
            "WHERE profile_completed_at IS NULL"
        )

    if "last_seen_at" not in profile_cols:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN last_seen_at TEXT")
        # Chi era gia' dentro non e' mai stato visto: il dato piu' vicino al
        # vero e' l'ultimo login, che e' scritto sulla sessione.
        conn.execute(
            "UPDATE user_profiles SET last_seen_at = ("
            "SELECT MAX(s.created_at) FROM sessions s WHERE s.email = user_profiles.email"
            ") WHERE last_seen_at IS NULL"
        )

    # "owner" era il nome interno del primo giro; il ruolo si chiama Leader.
    conn.execute("UPDATE workspace_members SET role = 'leader' WHERE role = 'owner'")

    migrate_to_workspaces(conn)
    migrate_to_gigs(conn)

    # Un palcoscenico rimasto senza serate non e' un errore — vuol dire che
    # nessuna stagione e' ancora aperta — ma il suo stato deve dirlo. Le
    # versioni prima di questa lasciavano appeso il valore vecchio quando si
    # eliminava l'ultima serata: qui si ripara, ed e' idempotente.
    conn.execute(
        "UPDATE locations SET status = 'da_contattare', updated_at = ? "
        "WHERE status != 'da_contattare' "
        "AND NOT EXISTS (SELECT 1 FROM gigs g WHERE g.location_id = locations.id)",
        (now_iso(),),
    )

    # Svuotare il promemoria scriveva stringa vuota invece di NULL: due modi
    # di dire "nessuna data" che le query devono distinguere. Qui restano in
    # uno solo, ed e' idempotente.
    conn.execute("UPDATE locations SET next_contact_date = NULL WHERE next_contact_date = ''")


def migrate_to_gigs(conn):
    """Porta la storia esistente dentro le serate. Prima di questa versione lo
    stato della trattativa viveva sul palcoscenico, quindi ogni palcoscenico
    aveva un solo ciclo: quello in corso. Diventa la sua prima serata, e le
    successive nascono quando si riparte per una stagione nuova.

    Gira una volta sola ed e' additiva come quella dei workspace: nessuna
    DROP, locations.status non viene toccata — resta la copia da cui elenchi
    e filtri leggono gia' oggi.
    """
    if conn.execute("SELECT id FROM gigs LIMIT 1").fetchone():
        return
    # Anche i palcoscenici archiviati: se vengono ripristinati la loro storia
    # deve essere ancora li'.
    rows = conn.execute("SELECT id, status, created_at FROM locations").fetchall()
    if not rows:
        return
    season = current_season()
    ts = now_iso()
    conn.executemany(
        "INSERT INTO gigs (location_id, season, status, closed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                r["id"],
                season,
                r["status"] or "da_contattare",
                ts if (r["status"] or "") in CLOSING_STATUSES else None,
                r["created_at"] or ts,
                ts,
            )
            for r in rows
        ],
    )


def migrate_to_workspaces(conn):
    """Porta dentro dei workspace i dati nati quando l'app aveva un solo
    dataset globale. Gira una volta sola: al riavvio successivo esiste gia'
    almeno un workspace e la funzione esce subito.

    E' deliberatamente additiva — nessuna DROP, nessuna colonna riscritta —
    cosi' tornare al branch main lascia l'app funzionante sullo stesso file.
    """
    if conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone():
        return

    # my_bands e' gia' l'elenco dei gruppi in cui si suona: e' esattamente il
    # contenitore che serve, quindi ogni riga diventa un workspace.
    seeds = [
        (r["name"], r["genre"], r["city"])
        for r in conn.execute("SELECT name, genre, city FROM my_bands ORDER BY id ASC").fetchall()
    ]
    has_locations = conn.execute("SELECT id FROM locations LIMIT 1").fetchone() is not None
    if not seeds:
        if not has_locations:
            return  # database vuoto: niente da adottare
        seeds = [("La mia band", None, None)]

    ts = now_iso()
    emails = [r["email"] for r in conn.execute("SELECT email FROM user_profiles").fetchall()]
    owner_row = conn.execute(
        "SELECT owner_email FROM locations WHERE owner_email IS NOT NULL "
        "GROUP BY owner_email ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    created_by = owner_row["owner_email"] if owner_row else (emails[0] if emails else None)

    ws_ids = []
    for name, genre, city in seeds:
        cur = conn.execute(
            "INSERT INTO workspaces (name, genre, city, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, genre, city, created_by, ts, ts),
        )
        ws_ids.append(cur.lastrowid)

    # Fino a ieri chiunque fosse loggato vedeva tutti i dati: rendere tutti
    # membri di tutti i workspace, e tutti owner, riproduce esattamente i
    # permessi che ognuno ha oggi. Nessuno perde accesso alla migrazione.
    for ws_id in ws_ids:
        for email in emails:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_members "
                "(workspace_id, email, role, joined_at) VALUES (?, ?, ?, ?)",
                (ws_id, email, "leader", ts),
            )

    # Tutti i dati sciolti finiscono nel primo workspace: gli altri nascono
    # vuoti, che e' il comportamento giusto per una band appena aggiunta.
    primary = ws_ids[0]
    for table in WORKSPACE_SCOPED_TABLES:
        conn.execute(f"UPDATE {table} SET workspace_id = ? WHERE workspace_id IS NULL", (primary,))

    # I workspace oltre al primo nascono vuoti dalla migrazione, quindi non
    # hanno passato da create_workspace: le tipologie di default vanno messe
    # qui, altrimenti si ritrovano l'elenco dei tipi vuoto.
    for ws_id, (seed_name, seed_genre, _city) in zip(ws_ids, seeds):
        seed_workspace_defaults(conn, ws_id, seed_name, seed_genre)

    conn.execute(
        "UPDATE user_profiles SET active_workspace_id = ? WHERE active_workspace_id IS NULL",
        (primary,),
    )


# --- sessioni di login ---------------------------------------------------

def create_session(conn, email):
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    conn.execute(
        "INSERT INTO sessions (id, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, email, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    return session_id


# Ogni richiesta passa di qui, anche le immagini: senza freno sarebbe una
# scrittura su SQLite per ogni icona caricata. Un minuto di risoluzione e'
# abbastanza per "attivo ora", e la riga viene toccata al massimo una volta
# al minuto per persona.
LAST_SEEN_THROTTLE_SECONDS = 60


def touch_last_seen(conn, email):
    now = datetime.now(timezone.utc)
    soglia = (now - timedelta(seconds=LAST_SEEN_THROTTLE_SECONDS)).isoformat()
    cur = conn.execute(
        "UPDATE user_profiles SET last_seen_at = ? "
        "WHERE email = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
        (now.isoformat(), email, soglia),
    )
    # updated_at resta fermo: essersi fatti vedere non e' una modifica al
    # profilo, e sporcarlo confonderebbe chi guarda quando e' cambiato cosa.
    if cur.rowcount:
        conn.commit()


def get_session_email(conn, session_id):
    if not session_id:
        return None
    row = conn.execute("SELECT email, expires_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires < datetime.now(timezone.utc):
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return None
    touch_last_seen(conn, row["email"])
    return row["email"]


def delete_session(conn, session_id):
    if session_id:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


def google_auth_url(redirect_uri, state):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def google_exchange_code(code, redirect_uri):
    data = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def google_fetch_userinfo(access_token):
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


# --- workspace (le band) e inviti ---------------------------------------

def is_member(conn, workspace_id, email):
    if not workspace_id or not email:
        return False
    row = conn.execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND email = ?",
        (workspace_id, email),
    ).fetchone()
    return row is not None


def member_role(conn, workspace_id, email):
    row = conn.execute(
        "SELECT role FROM workspace_members WHERE workspace_id = ? AND email = ?",
        (workspace_id, email),
    ).fetchone()
    return row["role"] if row else None


def fetch_workspaces_for(conn, email, active_id=None):
    if not email:
        # Login disattivato: si lavora in modo mono-utente su tutto.
        rows = conn.execute("SELECT * FROM workspaces ORDER BY id ASC").fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            d["role"] = "leader"
    else:
        rows = conn.execute(
            "SELECT w.*, m.role FROM workspaces w "
            "JOIN workspace_members m ON m.workspace_id = w.id "
            "WHERE m.email = ? ORDER BY w.name COLLATE NOCASE ASC",
            (email,),
        ).fetchall()
        out = [dict(r) for r in rows]
    for d in out:
        d["venue_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE workspace_id = ? AND deleted_at IS NULL",
            (d["id"],),
        ).fetchone()["n"]
        d["member_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ?", (d["id"],)
        ).fetchone()["n"]
        d["active"] = (d["id"] == active_id)
    return out


def set_active_workspace(conn, email, workspace_id):
    if not email:
        return
    conn.execute(
        "UPDATE user_profiles SET active_workspace_id = ?, updated_at = ? WHERE email = ?",
        (workspace_id, now_iso(), email),
    )
    conn.commit()


def resolve_active_workspace(conn, email):
    """Il workspace su cui l'utente sta lavorando, o None se non ne ha ancora
    nessuno (utente appena iscritto, senza band e senza invito accettato)."""
    if not email:
        row = conn.execute("SELECT id FROM workspaces ORDER BY id ASC LIMIT 1").fetchone()
        return row["id"] if row else None

    row = conn.execute(
        "SELECT active_workspace_id FROM user_profiles WHERE email = ?", (email,)
    ).fetchone()
    active = row["active_workspace_id"] if row else None
    if active and is_member(conn, active, email):
        return active

    # L'ultimo workspace attivo non vale piu' (rimosso dalla band, o non ne ha
    # mai scelto uno): ripiega sul primo di cui e' membro.
    fallback = conn.execute(
        "SELECT workspace_id FROM workspace_members WHERE email = ? ORDER BY joined_at ASC LIMIT 1",
        (email,),
    ).fetchone()
    if not fallback:
        return None
    set_active_workspace(conn, email, fallback["workspace_id"])
    return fallback["workspace_id"]


def create_workspace(conn, email, name, genre=None, city=None):
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "Il nome della band è obbligatorio")
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO workspaces (name, genre, city, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, (genre or "").strip() or None, (city or "").strip() or None, email, ts, ts),
    )
    ws_id = cur.lastrowid
    if email:
        conn.execute(
            "INSERT OR IGNORE INTO workspace_members (workspace_id, email, role, joined_at) "
            "VALUES (?, ?, 'leader', ?)",
            (ws_id, email, ts),
        )
    person = None
    if email:
        row = conn.execute(
            "SELECT name, artist_name FROM user_profiles WHERE email = ?", (email,)
        ).fetchone()
        if row:
            # Il nome di battesimo basta: nel messaggio si presenta una persona.
            person = (row["name"] or "").split(" ")[0] or None
    seed_workspace_defaults(conn, ws_id, name, genre, person)
    conn.commit()
    set_active_workspace(conn, email, ws_id)
    return ws_id


def fetch_members(conn, workspace_id):
    rows = conn.execute(
        "SELECT m.email, m.role, m.joined_at, m.invited_by, p.name, p.picture, "
        "p.band_roles, p.last_seen_at "
        "FROM workspace_members m LEFT JOIN user_profiles p ON p.email = m.email "
        "WHERE m.workspace_id = ? ORDER BY m.joined_at ASC",
        (workspace_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# Leader amministra la band, Member lavora sui dati, Slaker li consulta e
# basta. L'ordine conta solo per l'interfaccia.
MEMBER_ROLES = ("leader", "member", "slaker")


def count_leaders(conn, workspace_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ? AND role = 'leader'",
        (workspace_id,),
    ).fetchone()["n"]


def set_member_role(conn, workspace_id, actor_email, target_email, role):
    """Promuove a Leader o riporta a Member. Il Leader e' chi puo' gestire la
    band: inviti, ruoli e rimozioni."""
    if role not in MEMBER_ROLES:
        raise ApiError(400, "Ruolo non valido")
    if not is_member(conn, workspace_id, target_email):
        raise ApiError(404, "Questa persona non fa parte della band")
    if actor_email and member_role(conn, workspace_id, actor_email) != "leader":
        raise ApiError(403, "Solo un Leader può cambiare i ruoli")
    if actor_email and actor_email == target_email:
        raise ApiError(400, "Non puoi cambiare il tuo ruolo")
    current = member_role(conn, workspace_id, target_email)
    if current == role:
        return
    # Senza Leader nessuno potrebbe piu' invitare, cambiare ruoli o rimuovere:
    # la band resterebbe bloccata per sempre.
    if current == "leader" and count_leaders(conn, workspace_id) <= 1:
        raise ApiError(400, "Questo è l'ultimo Leader: promuovine un altro prima di retrocederlo")
    conn.execute(
        "UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND email = ?",
        (role, workspace_id, target_email),
    )
    conn.commit()


def remove_member(conn, workspace_id, actor_email, target_email):
    if not is_member(conn, workspace_id, target_email):
        raise ApiError(404, "Questa persona non fa parte della band")
    if actor_email and member_role(conn, workspace_id, actor_email) != "leader" \
            and actor_email != target_email:
        raise ApiError(403, "Solo un Leader può rimuovere gli altri membri")
    if member_role(conn, workspace_id, target_email) == "leader" \
            and count_leaders(conn, workspace_id) <= 1:
        raise ApiError(400, "Questo è l'ultimo Leader: promuovine un altro prima di rimuoverlo")
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()["n"]
    if remaining <= 1:
        raise ApiError(400, "Non puoi rimuovere l'ultimo membro: la band resterebbe senza nessuno")
    conn.execute(
        "DELETE FROM workspace_members WHERE workspace_id = ? AND email = ?",
        (workspace_id, target_email),
    )
    # Chi resta fuori non deve ritrovarsi puntato a una band che non vede piu'.
    conn.execute(
        "UPDATE user_profiles SET active_workspace_id = NULL "
        "WHERE email = ? AND active_workspace_id = ?",
        (target_email, workspace_id),
    )
    conn.commit()


# --- inviti -------------------------------------------------------------

def invite_to_dict(row, origin=None):
    d = dict(row)
    d["url"] = f"{origin}/join/{d['token']}" if origin else f"/join/{d['token']}"
    d["expired"] = d["expires_at"] < now_iso()
    d["exhausted"] = bool(d["max_uses"]) and d["used_count"] >= d["max_uses"]
    d["revoked"] = bool(d["revoked_at"])
    d["valid"] = not (d["expired"] or d["exhausted"] or d["revoked"])
    return d


# Un invito e' per una persona: il link vale un ingresso e poi e' carta
# straccia. Un link che resta buono dopo essere stato usato e' un link
# che gira su WhatsApp e fa entrare nella band chi non hai invitato tu.
def create_invite(conn, workspace_id, email, max_uses=1):
    ts = datetime.now(timezone.utc)
    # Una band ha un solo link alla volta, ma se ce n'e' gia' uno buono si
    # riusa quello. Prima se ne creava uno a ogni apertura della schermata, e
    # il link appena mandato su WhatsApp moriva nel momento in cui tornavi a
    # guardarlo: chi lo apriva finiva su una pagina di accesso qualsiasi e si
    # registrava senza band. Il link nuovo si fa quando il vecchio e'
    # scaduto, esaurito o annullato.
    esistente = conn.execute(
        "SELECT * FROM invites WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if esistente and invite_to_dict(esistente)["valid"]:
        return esistente
    conn.execute("DELETE FROM invites WHERE workspace_id = ?", (workspace_id,))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO invites (token, workspace_id, created_by, created_at, expires_at, max_uses) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            token, workspace_id, email,
            ts.isoformat(),
            (ts + timedelta(hours=INVITE_TTL_HOURS)).isoformat(),
            max_uses,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    return row


def fetch_invites(conn, workspace_id, origin=None):
    rows = conn.execute(
        "SELECT * FROM invites WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    ).fetchall()
    return [invite_to_dict(r, origin) for r in rows]


def check_invite(conn, token):
    """Restituisce (riga_invito, messaggio_errore). Ogni motivo di rifiuto ha
    il suo messaggio: "non valido" da solo non dice a chi lo riceve se deve
    chiederne un altro o se ha semplicemente aspettato troppo."""
    if not token:
        return None, "Link di invito mancante."
    row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    if not row:
        return None, "Questo link di invito non esiste. Chiedi che te ne mandino uno nuovo."
    if row["revoked_at"]:
        return None, "Questo invito è stato annullato da chi te l'ha mandato."
    if row["expires_at"] < now_iso():
        return None, f"Questo invito è scaduto: i link valgono {INVITE_TTL_HOURS} ore. Chiedine uno nuovo."
    if row["max_uses"] and row["used_count"] >= row["max_uses"]:
        return None, "Questo invito ha già raggiunto il numero massimo di utilizzi."
    return row, None


def accept_invite(conn, token, email):
    """Aggiunge l'utente alla band dell'invito e la rende quella attiva."""
    row, error = check_invite(conn, token)
    if error:
        return None, error
    ws_id = row["workspace_id"]
    if not is_member(conn, ws_id, email):
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, email, role, invited_by, joined_at) "
            "VALUES (?, ?, 'member', ?, ?)",
            (ws_id, email, row["created_by"], now_iso()),
        )
        conn.execute(
            "UPDATE invites SET used_count = used_count + 1 WHERE token = ?", (token,)
        )
    conn.commit()
    set_active_workspace(conn, email, ws_id)
    name_row = conn.execute("SELECT name FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    return (name_row["name"] if name_row else None), None


# --- profilo utente (wizard di benvenuto + dati Google) -----------------

ME_FIELDS = ["name", "artist_name", "genre", "city", "band_roles"]

# Cosa suoni nella band. Sono piu' di uno perche' quasi sempre lo sono:
# chi canta suona anche la chitarra. Lista chiusa e non libera: e'
# l'informazione che si legge a colpo d'occhio nella lista dei membri,
# e venti modi di scrivere "voce" la renderebbero illeggibile.
BAND_ROLES = (
    "Cantante", "Chitarrista", "Bassista", "Batterista",
    "Percussionista", "Tastierista", "Violinista", "Altro",
)


def clean_band_roles(value):
    """Arrivano come lista dall'app; una stringa separata da virgole e'
    accettata lo stesso perche' e' cosi' che stanno nel database."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ApiError(400, "Ruolo nella band non valido")
    out = []
    for item in value:
        label = str(item or "").strip()
        if not label:
            continue
        if label not in BAND_ROLES:
            raise ApiError(400, "Ruolo nella band non valido: " + label)
        if label not in out:
            out.append(label)
    # L'ordine e' quello della lista, non quello in cui si e' toccato:
    # cosi' la stessa persona si legge sempre uguale.
    out.sort(key=BAND_ROLES.index)
    return ", ".join(out) or None


def upsert_profile_from_google(conn, email, name, picture):
    ts = now_iso()
    existing = conn.execute(
        "SELECT email, name FROM user_profiles WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        # La foto arriva sempre da Google, il nome no: chi lo corregge nel
        # proprio profilo se lo vedrebbe tornare indietro al primo accesso.
        # Google lo scrive solo finche' non c'e' niente.
        if (existing["name"] or "").strip():
            conn.execute(
                "UPDATE user_profiles SET picture = ?, updated_at = ? WHERE email = ?",
                (picture, ts, email),
            )
        else:
            conn.execute(
                "UPDATE user_profiles SET name = ?, picture = ?, updated_at = ? WHERE email = ?",
                (name, picture, ts, email),
            )
    else:
        conn.execute(
            "INSERT INTO user_profiles (email, name, picture, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (email, name, picture, ts, ts),
        )
    conn.commit()


def fetch_me(conn, email):
    active = resolve_active_workspace(conn, email)
    workspaces = fetch_workspaces_for(conn, email, active)
    base = {
        "email": email, "name": None, "picture": None,
        "artist_name": None, "genre": None, "city": None, "band_roles": None,
    }
    if not email:
        d = dict(base, onboarded=True)
    else:
        row = conn.execute("SELECT * FROM user_profiles WHERE email = ?", (email,)).fetchone()
        if not row:
            d = dict(base, onboarded=False)
        else:
            d = dict(row)
            d["onboarded"] = bool(d.get("onboarded_at"))
    # Senza login non c'e' un profilo da completare: l'app e' di chi ce l'ha
    # sul computer, e il modulo non avrebbe niente da chiedere.
    d["profile_completed"] = (not email) or bool(d.get("profile_completed_at"))
    d["workspaces"] = workspaces
    d["active_workspace_id"] = active
    d["active_workspace"] = next((w for w in workspaces if w["id"] == active), None)
    # Senza band attiva l'app non ha dati da mostrare: il wizard deve partire
    # anche se il profilo risulta gia' compilato da un giro precedente.
    d["needs_workspace"] = active is None
    # Con il login spento non c'e' un utente da riconoscere: l'app gira in
    # locale per una persona sola, che e' anche l'amministratore.
    d["is_admin"] = (not auth_enabled()) or is_admin(email)
    d["role"] = member_role(conn, active, email) if (active and email) else "leader"
    d["can_write"] = d["role"] != "slaker"
    return d


def update_me(conn, email, body):
    fields = {}
    for f in ME_FIELDS:
        if f not in body:
            continue
        value = body[f]
        if f == "band_roles":
            value = clean_band_roles(value)
        elif f == "name":
            value = (value or "").strip()
            if not value:
                raise ApiError(400, "Il nome è obbligatorio")
        fields[f] = value.strip() if isinstance(value, str) else value
    # Il modulo di benvenuto si chiude una volta sola: da li' in poi il
    # profilo si modifica dalle Impostazioni, non all'avvio.
    if body.get("profile_completed"):
        fields["profile_completed_at"] = now_iso()
    if fields:
        ts = now_iso()
        existing = conn.execute("SELECT email FROM user_profiles WHERE email = ?", (email,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO user_profiles (email, created_at, updated_at) VALUES (?, ?, ?)",
                (email, ts, ts),
            )
        set_clause = ",".join(f"{k} = ?" for k in fields.keys())
        conn.execute(
            f"UPDATE user_profiles SET {set_clause}, updated_at = ? WHERE email = ?",
            list(fields.values()) + [ts, email],
        )
        row = conn.execute(
            "SELECT onboarded_at, artist_name, genre, city FROM user_profiles WHERE email = ?", (email,)
        ).fetchone()
        if row and not row["onboarded_at"] and row["artist_name"] and row["city"]:
            conn.execute("UPDATE user_profiles SET onboarded_at = ? WHERE email = ?", (ts, email))
            conn.commit()
            # Chi finisce il wizard senza aver accettato un invito parte con la
            # propria band vuota: e' il suo primo workspace.
            if resolve_active_workspace(conn, email) is None:
                create_workspace(conn, email, row["artist_name"], row["genre"], row["city"])
        conn.commit()
    return fetch_me(conn, email)


# --- template di partenza per una band nuova ---------------------------
#
# Ricalcati sui dati reali dei Pink Froid: sono l'unico set gia' rodato sul
# campo. Quello che viene inserito qui e' una copia che appartiene alla band
# nuova, quindi ognuno puo' poi cambiarla senza toccare le altre.
#
# Nei testi ci sono due tipi di segnaposto, e la differenza conta:
#   {band}, {genere}, {nome}  vengono sostituiti in automatico con i dati
#                             della band che si sta creando;
#   [fra parentesi quadre]    restano da compilare a mano, perche' sono dati
#                             personali (telefono, email, link ai video) che
#                             non si possono indovinare e che non vanno
#                             ereditati da un'altra band.

DEFAULT_VENUE_TYPES = [
    "Locale / Club / Pub", "Festa di paese", "Sagra",
    "Stabilimento balneare", "Villaggio / resort",
    "Evento privato", "Spazio pubblico", "Bar", "Ristorante",
]

# I Pink Froid non hanno mai usato le categorie e nessun palcoscenico ne ha
# una assegnata: non c'e' nessun set rodato da cui copiare, quindi una band
# nuova parte senza categorie invece che con categorie inventate.
DEFAULT_VENUE_CATEGORIES = []

DEFAULT_WA_TEMPLATES = [
    {
        "name": "Invio materiale",
        "message": (
            "Ciao, mi chiamo {nome} e faccio parte dei {band}{genere}.\n"
            "Se avete in programma di fare musica dal vivo la prossima "
            "stagione possiamo proporre un paio d'ore divertenti.\n"
            "Qui sotto un link dove potrete vedere un collage di video di "
            "spettatori dei nostri concerti.\n\n"
            "[incolla qui il link ai vostri video]\n\n"
            "Per contatti al telefono o via WhatsApp [il tuo numero] o per "
            "e-mail [la tua email].\n"
            "Grazie."
        ),
    },
]


def render_default_text(text, band_name, genre=None, person=None):
    genre_part = f", {genre.strip().lower()}" if (genre or "").strip() else ""
    return (
        text.replace("{band}", band_name or "la nostra band")
            .replace("{genere}", genre_part)
            .replace("{nome}", (person or "").strip() or "[il tuo nome]")
    )


TEMPLATE_KINDS = {
    "venue_type": ("venue_types", False),
    "venue_category": ("venue_categories", False),
    "wa_template": ("wa_templates", True),
}


def seed_app_templates(conn):
    """Porta le costanti qui sopra dentro app_templates, una volta sola.

    Da li' in poi la fonte di verita' e' la tabella, che l'amministratore
    puo' modificare: le costanti restano solo come seme per un'installazione
    nuova.
    """
    if conn.execute("SELECT 1 FROM app_templates LIMIT 1").fetchone():
        return
    ts = now_iso()
    rows = []
    for i, name in enumerate(DEFAULT_VENUE_TYPES):
        rows.append(("venue_type", name, None, i, ts, ts))
    for i, name in enumerate(DEFAULT_VENUE_CATEGORIES):
        rows.append(("venue_category", name, None, i, ts, ts))
    for i, t in enumerate(DEFAULT_WA_TEMPLATES):
        rows.append(("wa_template", t["name"], t["message"], i, ts, ts))
    conn.executemany(
        "INSERT INTO app_templates (kind, name, message, position, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def fetch_templates(conn, kind=None):
    if kind:
        if kind not in TEMPLATE_KINDS:
            raise ApiError(400, "Tipo di template non valido")
        rows = conn.execute(
            "SELECT * FROM app_templates WHERE kind = ? ORDER BY position ASC, id ASC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM app_templates ORDER BY kind ASC, position ASC, id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_template(conn, kind, body):
    if kind not in TEMPLATE_KINDS:
        raise ApiError(400, "Tipo di template non valido")
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Il nome è obbligatorio")
    has_message = TEMPLATE_KINDS[kind][1]
    message = (body.get("message") or "").strip() if has_message else None
    dup = conn.execute(
        "SELECT id FROM app_templates WHERE kind = ? AND LOWER(name) = LOWER(?)", (kind, name)
    ).fetchone()
    if dup:
        raise ApiError(400, "Esiste già un template con questo nome")
    ts = now_iso()
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM app_templates WHERE kind = ?", (kind,)
    ).fetchone()["p"]
    cur = conn.execute(
        "INSERT INTO app_templates (kind, name, message, position, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, name, message, position, ts, ts),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM app_templates WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_template(conn, template_id, body):
    row = conn.execute("SELECT * FROM app_templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        raise ApiError(404, "Template non trovato")
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Il nome è obbligatorio")
    dup = conn.execute(
        "SELECT id FROM app_templates WHERE kind = ? AND LOWER(name) = LOWER(?) AND id != ?",
        (row["kind"], name, template_id),
    ).fetchone()
    if dup:
        raise ApiError(400, "Esiste già un template con questo nome")
    message = row["message"]
    if TEMPLATE_KINDS[row["kind"]][1] and "message" in body:
        message = (body.get("message") or "").strip()
    conn.execute(
        "UPDATE app_templates SET name = ?, message = ?, updated_at = ? WHERE id = ?",
        (name, message, now_iso(), template_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM app_templates WHERE id = ?", (template_id,)).fetchone())


def delete_template(conn, template_id):
    cur = conn.execute("DELETE FROM app_templates WHERE id = ?", (template_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Template non trovato")


def seed_workspace_defaults(conn, ws, band_name=None, genre=None, person=None):
    """Precarica tipologie, categorie e modelli WhatsApp di una band nuova.

    Riempie solo le tabelle vuote per quel workspace, cosi' rieseguirla non
    duplica niente e non sovrascrive quello che l'utente ha gia' cambiato.
    """
    ts = now_iso()

    types = [t["name"] for t in fetch_templates(conn, "venue_type")]
    if types and not conn.execute(
        "SELECT 1 FROM venue_types WHERE workspace_id = ? LIMIT 1", (ws,)
    ).fetchone():
        conn.executemany(
            "INSERT INTO venue_types (name, workspace_id, created_at) VALUES (?, ?, ?)",
            [(name, ws, ts) for name in types],
        )

    categories = [t["name"] for t in fetch_templates(conn, "venue_category")]
    if categories and not conn.execute(
        "SELECT 1 FROM venue_categories WHERE workspace_id = ? LIMIT 1", (ws,)
    ).fetchone():
        conn.executemany(
            "INSERT INTO venue_categories (name, workspace_id, created_at) VALUES (?, ?, ?)",
            [(name, ws, ts) for name in categories],
        )

    messages = fetch_templates(conn, "wa_template")
    if messages and not conn.execute(
        "SELECT 1 FROM wa_templates WHERE workspace_id = ? LIMIT 1", (ws,)
    ).fetchone():
        conn.executemany(
            "INSERT INTO wa_templates (name, message, workspace_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    render_default_text(t["name"], band_name, genre, person),
                    render_default_text(t["message"] or "", band_name, genre, person),
                    ws, ts, ts,
                )
                for t in messages
            ],
        )


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def to_number_or_none(value, kind=float):
    if value is None or value == "":
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Valore numerico non valido")


# Una serata = un tentativo di suonare in quel posto in quella stagione.
# L'ordine e' sempre lo stesso: la stagione piu' recente in cima, e dentro la
# stessa stagione prima le date fissate.
GIG_ORDER = "ORDER BY season DESC, gig_date IS NULL, gig_date DESC, id DESC"


def current_season():
    """La stagione di default e' l'anno: chi pianifica per periodi diversi
    ("Estate 2027") puo' scriverci quello che vuole, e' testo libero."""
    return str(datetime.now(timezone.utc).year)


def gig_to_dict(row):
    d = dict(row)
    d["open"] = d.get("closed_at") is None
    return d


def current_gig_row(conn, loc_id):
    """La serata che conta adesso: quella aperta della stagione piu' recente,
    e se non ce ne sono aperte l'ultima chiusa. E' da qui che il palcoscenico
    prende lo stato mostrato negli elenchi, ed e' a questa che si attaccano le
    attivita' registrate."""
    return conn.execute(
        "SELECT * FROM gigs WHERE location_id = ? "
        "ORDER BY (closed_at IS NULL) DESC, season DESC, id DESC LIMIT 1",
        (loc_id,),
    ).fetchone()


def refresh_location_status(conn, loc_id):
    """locations.status e' una copia: la verita' sta sulla serata in corso.
    Tenerla aggiornata qui vuol dire che elenchi, filtri e badge continuano a
    funzionare esattamente come prima, senza sapere niente delle serate.

    Senza nessuna serata lo stato torna a "da contattare": e' un posto in
    rubrica su cui non e' ancora stata aperta nessuna stagione. Lasciare il
    valore vecchio mostrerebbe "Confermato" su un palcoscenico che non ha
    nessuna serata confermata.
    """
    row = current_gig_row(conn, loc_id)
    conn.execute(
        "UPDATE locations SET status = ?, updated_at = ? WHERE id = ?",
        (row["status"] if row else "da_contattare", now_iso(), loc_id),
    )


def insert_gig(conn, loc_id, season, status, ts=None):
    ts = ts or now_iso()
    cur = conn.execute(
        "INSERT INTO gigs (location_id, season, status, closed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (loc_id, season, status, ts if status in CLOSING_STATUSES else None, ts, ts),
    )
    return cur.lastrowid


def plus_one_year(date_str):
    """Stessa data, anno dopo. Il 29 febbraio scala al 28: meglio una data
    buona che nessuna."""
    try:
        d = date.fromisoformat((date_str or "").strip())
    except ValueError:
        return None
    try:
        return d.replace(year=d.year + 1).isoformat()
    except ValueError:
        return d.replace(year=d.year + 1, day=28).isoformat()


def next_contact_after_gig(conn, loc_id, gig_row):
    """Un locale si ricontatta piu' o meno nello stesso periodo ogni anno.
    La base migliore e' il promemoria che ti eri gia' dato per quel posto;
    se non ce n'e' uno, la data della serata; in mancanza di tutto, oggi."""
    row = conn.execute(
        "SELECT next_contact_date FROM locations WHERE id = ?", (loc_id,)
    ).fetchone()
    base = (row["next_contact_date"] if row else None) or (gig_row["gig_date"] if gig_row else None)
    return plus_one_year(base or date.today().isoformat())


def next_season_for(conn, loc_id):
    """La stagione da aprire: l'anno corrente, o l'anno dopo l'ultima stagione
    gia' usata se si e' andati avanti. Legge le quattro cifre dentro
    l'etichetta, cosi' funziona anche con "Estate 2027"."""
    cur = int(current_season())
    best = cur - 1
    for r in conn.execute("SELECT season FROM gigs WHERE location_id = ?", (loc_id,)).fetchall():
        m = SEASON_YEAR_RE.search(str(r["season"] or ""))
        if m:
            best = max(best, int(m.group(1)))
    return str(max(cur, best + 1))


def set_location_status(conn, loc_id, status):
    """Cambiare lo stato dalla scheda del palcoscenico vuol dire cambiarlo
    sulla serata in corso: se lo scrivessimo solo su locations, la prima
    modifica alla serata lo sovrascriverebbe.

    Ma su una serata CHIUSA non si scrive mai da qui. Una stagione conclusa e'
    storia: rimetterla a "da contattare" per ripartire cancellerebbe il fatto
    che ci hai suonato, che e' esattamente quello che le serate esistono per
    non perdere. Se non c'e' niente di aperto si apre la stagione dopo. Per
    correggere un anno passato si passa dalla sua riga, dove si vede quale.
    """
    row = current_gig_row(conn, loc_id)
    ts = now_iso()
    if row is None or row["closed_at"] is not None:
        insert_gig(conn, loc_id, next_season_for(conn, loc_id), status, ts)
    else:
        conn.execute(
            "UPDATE gigs SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (status, ts if status in CLOSING_STATUSES else None, ts, row["id"]),
        )
    conn.execute(
        "UPDATE locations SET status = ?, updated_at = ? WHERE id = ?", (status, ts, loc_id)
    )


def clean_gig_payload(body, partial):
    data = {}
    for field in GIG_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if field == "status":
            if value and value not in STATUS_VALUES:
                raise ApiError(400, "Stato non valido")
            value = value or "da_contattare"
        elif field == "season":
            value = (value or "").strip()
            if not value:
                raise ApiError(400, "La stagione è obbligatoria")
        elif field == "gig_date":
            value = (value or "").strip() or None
            if value and not GIG_DATE_RE.match(value):
                raise ApiError(400, "Data della serata non valida")
        elif field == "fee":
            value = to_number_or_none(value, float)
        elif isinstance(value, str):
            value = value.strip()
        data[field] = value
    return data


def require_location(conn, ws, loc_id):
    row = conn.execute(
        "SELECT id FROM locations WHERE id = ? AND workspace_id = ?", (loc_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Palcoscenico non trovato")
    return row


def create_gig(conn, ws, loc_id, body):
    require_location(conn, ws, loc_id)
    data = clean_gig_payload(body or {}, partial=False)
    data.setdefault("season", current_season())
    data.setdefault("status", "da_contattare")
    ts = now_iso()
    # Ripartire per una stagione nuova chiude quelle vecchie rimaste in
    # sospeso: se di quella trattativa non se n'e' fatto niente per un anno,
    # aperta non e' piu'. Lo stato resta scritto com'era, la storia non si
    # riscrive.
    conn.execute(
        "UPDATE gigs SET closed_at = ?, updated_at = ? "
        "WHERE location_id = ? AND closed_at IS NULL AND season < ?",
        (ts, ts, loc_id, data["season"]),
    )
    fields = ["location_id"] + list(data.keys()) + ["closed_at", "created_at", "updated_at"]
    values = [loc_id] + list(data.values()) + [
        ts if data["status"] in CLOSING_STATUSES else None, ts, ts,
    ]
    placeholders = ",".join("?" for _ in fields)
    conn.execute(f"INSERT INTO gigs ({','.join(fields)}) VALUES ({placeholders})", values)
    refresh_location_status(conn, loc_id)
    # Aprire una stagione senza promemoria faceva sparire il palcoscenico
    # dall'Agenda: fuori dal riquadro "Riproponi" perche' ormai una trattativa
    # aperta ce l'ha, e fuori da "Da ricontattare" che va a data. Aprire una
    # stagione vuol dire che da adesso li devi richiamare, quindi se un
    # promemoria non c'e' la data e' oggi. Se ce n'era gia' uno non si tocca:
    # "richiamali a febbraio" resta febbraio.
    conn.execute(
        "UPDATE locations SET next_contact_date = ?, updated_at = ? "
        "WHERE id = ? AND COALESCE(next_contact_date, '') = ''",
        (date.today().isoformat(), now_iso(), loc_id),
    )
    conn.commit()
    return fetch_location(conn, ws, loc_id)


def gig_location_id(conn, ws, gig_id):
    row = conn.execute(
        "SELECT g.location_id FROM gigs g JOIN locations l ON l.id = g.location_id "
        "WHERE g.id = ? AND l.workspace_id = ?",
        (gig_id, ws),
    ).fetchone()
    if not row:
        raise ApiError(404, "Serata non trovata")
    return row["location_id"]


def update_gig(conn, ws, gig_id, body):
    """`next_contact_date` non e' un campo della serata ma del palcoscenico:
    si accetta lo stesso qui perche' chiudere una serata e darsi la data per
    ripartire sono una cosa sola, e farne due chiamate lascerebbe la serata
    chiusa senza promemoria se la seconda fallisce."""
    loc_id = gig_location_id(conn, ws, gig_id)
    before = conn.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,)).fetchone()
    data = clean_gig_payload(body, partial=True)
    closing = (
        "status" in data
        and data["status"] in CLOSING_STATUSES
        and before["closed_at"] is None
    )
    if data:
        if "status" in data:
            data["closed_at"] = now_iso() if data["status"] in CLOSING_STATUSES else None
        data["updated_at"] = now_iso()
        set_clause = ",".join(f"{k} = ?" for k in data.keys())
        conn.execute(
            f"UPDATE gigs SET {set_clause} WHERE id = ?", list(data.values()) + [gig_id]
        )
        refresh_location_status(conn, loc_id)

    # Solo sul passaggio a chiusa: correggere il compenso di una serata gia'
    # suonata non deve rispostare il promemoria.
    if "next_contact_date" in body:
        wanted = (body.get("next_contact_date") or "").strip() or None
        if wanted and not GIG_DATE_RE.match(wanted):
            raise ApiError(400, "Data di ricontatto non valida")
        conn.execute(
            "UPDATE locations SET next_contact_date = ?, updated_at = ? WHERE id = ?",
            (wanted, now_iso(), loc_id),
        )
    elif closing:
        # La riga DOPO l'aggiornamento: il pannello salva data e chiusura in
        # una volta sola, quindi guardando quella di prima la data della
        # serata non ci sarebbe ancora e si ripiegherebbe su oggi.
        after = conn.execute("SELECT * FROM gigs WHERE id = ?", (gig_id,)).fetchone()
        conn.execute(
            "UPDATE locations SET next_contact_date = ?, updated_at = ? WHERE id = ?",
            (next_contact_after_gig(conn, loc_id, after or before), now_iso(), loc_id),
        )
    conn.commit()
    return fetch_location(conn, ws, loc_id)


def delete_gig(conn, ws, gig_id):
    loc_id = gig_location_id(conn, ws, gig_id)
    # Le attivita' restano: erano cose fatte davvero, perdono solo il legame
    # con il ciclo che non c'e' piu'.
    conn.execute("UPDATE notes SET gig_id = NULL WHERE gig_id = ?", (gig_id,))
    conn.execute("DELETE FROM gigs WHERE id = ?", (gig_id,))
    refresh_location_status(conn, loc_id)
    conn.commit()
    return fetch_location(conn, ws, loc_id)


def location_to_dict(row, notes_by_location, ad_by_id, photos_by_location=None,
                     gigs_by_location=None):
    d = dict(row)
    ad = ad_by_id.get(d.get("art_director_id"))
    d["art_director_name"] = ad["name"] if ad else None
    d["notes"] = notes_by_location.get(d["id"], [])
    d["photos"] = (photos_by_location or {}).get(d["id"], [])
    gigs = (gigs_by_location or {}).get(d["id"], [])
    d["gigs"] = gigs
    # Quante volte ci hai suonato e in quali stagioni: e' il dato che dice se
    # vale la pena richiamare questo posto, e viene gratis dalle righe.
    played = [g for g in gigs if g["status"] == "suonato"]
    d["gigs_played"] = len(played)
    d["seasons_played"] = sorted({g["season"] for g in played}, reverse=True)
    open_gigs = [g for g in gigs if g["open"]]
    d["current_gig_id"] = open_gigs[0]["id"] if open_gigs else (gigs[0]["id"] if gigs else None)
    return d


def fetch_locations(conn, ws, status=None, search=None, include_deleted=False):
    query = "SELECT * FROM locations"
    clauses = ["workspace_id = ?"]
    params = [ws]
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(city) LIKE ? OR LOWER(type) LIKE ?)")
        like = f"%{search.lower()}%"
        params.extend([like, like, like])
    query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY name COLLATE NOCASE ASC"
    rows = conn.execute(query, params).fetchall()

    ad_rows = conn.execute(
        "SELECT * FROM art_directors WHERE workspace_id = ?", (ws,)
    ).fetchall()
    ad_by_id = {r["id"]: dict(r) for r in ad_rows}

    # notes e photos non hanno workspace_id: seguono la location, quindi si
    # filtrano passando da li'.
    note_rows = conn.execute(
        "SELECT n.* FROM notes n JOIN locations l ON l.id = n.location_id "
        "WHERE l.workspace_id = ? ORDER BY n.created_at ASC", (ws,)
    ).fetchall()
    notes_by_location = {}
    for n in note_rows:
        notes_by_location.setdefault(n["location_id"], []).append(dict(n))

    photo_rows = conn.execute(
        "SELECT p.* FROM photos p JOIN locations l ON l.id = p.location_id "
        "WHERE l.workspace_id = ? ORDER BY p.created_at ASC", (ws,)
    ).fetchall()
    photos_by_location = {}
    for p in photo_rows:
        photos_by_location.setdefault(p["location_id"], []).append(dict(p))

    gig_rows = conn.execute(
        "SELECT g.* FROM gigs g JOIN locations l ON l.id = g.location_id "
        "WHERE l.workspace_id = ? " + GIG_ORDER, (ws,)
    ).fetchall()
    gigs_by_location = {}
    for g in gig_rows:
        gigs_by_location.setdefault(g["location_id"], []).append(gig_to_dict(g))

    return [
        location_to_dict(r, notes_by_location, ad_by_id, photos_by_location, gigs_by_location)
        for r in rows
    ]


def fetch_location(conn, ws, loc_id):
    row = conn.execute(
        "SELECT * FROM locations WHERE id = ? AND workspace_id = ?", (loc_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Palcoscenico non trovato")
    ad_rows = conn.execute("SELECT * FROM art_directors WHERE workspace_id = ?", (ws,)).fetchall()
    ad_by_id = {r["id"]: dict(r) for r in ad_rows}
    note_rows = conn.execute(
        "SELECT * FROM notes WHERE location_id = ? ORDER BY created_at ASC", (loc_id,)
    ).fetchall()
    notes_by_location = {loc_id: [dict(n) for n in note_rows]}
    photo_rows = conn.execute(
        "SELECT * FROM photos WHERE location_id = ? ORDER BY created_at ASC", (loc_id,)
    ).fetchall()
    photos_by_location = {loc_id: [dict(p) for p in photo_rows]}
    gig_rows = conn.execute(
        "SELECT * FROM gigs WHERE location_id = ? " + GIG_ORDER, (loc_id,)
    ).fetchall()
    gigs_by_location = {loc_id: [gig_to_dict(g) for g in gig_rows]}
    return location_to_dict(
        row, notes_by_location, ad_by_id, photos_by_location, gigs_by_location
    )


def clean_location_payload(body, partial):
    data = {}
    for field in LOCATION_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if field == "lat" or field == "lng":
            value = to_number_or_none(value, float)
        elif field == "capacity" or field == "art_director_id":
            value = to_number_or_none(value, int)
        elif field == "status":
            if value and value not in STATUS_VALUES:
                raise ApiError(400, "Stato non valido")
            value = value or "da_contattare"
        elif field == "favorite":
            value = 1 if value else 0
        elif field == "next_contact_date":
            # Vuoto vuol dire "non ricontattarli": si scrive NULL, non "",
            # cosi' e' lo stesso niente con cui nasce un palcoscenico e le
            # query che cercano il promemoria non devono sapere di due vuoti.
            value = (value or "").strip() or None
            if value and not GIG_DATE_RE.match(value):
                raise ApiError(400, "Data di ricontatto non valida")
        elif isinstance(value, str):
            value = value.strip()
        data[field] = value
    return data


def create_location(conn, ws, body, owner_email=None):
    data = clean_location_payload(body, partial=False)
    data.setdefault("name", "")
    data.setdefault("status", "da_contattare")
    data["owner_email"] = owner_email
    data["workspace_id"] = ws
    ts = now_iso()
    fields = list(data.keys()) + ["created_at", "updated_at"]
    values = list(data.values()) + [ts, ts]
    placeholders = ",".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO locations ({','.join(fields)}) VALUES ({placeholders})", values
    )
    # Un palcoscenico nuovo e' un tentativo di serata per la stagione in corso:
    # senza questa riga non ci sarebbe niente su cui registrare la trattativa.
    insert_gig(conn, cur.lastrowid, current_season(), data["status"], ts)
    conn.commit()
    return fetch_location(conn, ws, cur.lastrowid)


def update_location(conn, ws, loc_id, body):
    existing = conn.execute(
        "SELECT id FROM locations WHERE id = ? AND workspace_id = ?", (loc_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Palcoscenico non trovato")
    data = clean_location_payload(body, partial=True)
    # Lo stato non e' un campo del palcoscenico ma della sua serata in corso:
    # chi lo manda qui (la scheda, l'app installata di una versione vecchia)
    # continua a funzionare, ma finisce nel posto giusto.
    status = data.pop("status", None)
    if data:
        data["updated_at"] = now_iso()
        set_clause = ",".join(f"{k} = ?" for k in data.keys())
        conn.execute(
            f"UPDATE locations SET {set_clause} WHERE id = ?", list(data.values()) + [loc_id]
        )
    if status is not None:
        set_location_status(conn, loc_id, status)
    conn.commit()
    return fetch_location(conn, ws, loc_id)


def delete_location(conn, ws, loc_id):
    ts = now_iso()
    cur = conn.execute(
        "UPDATE locations SET deleted_at = ?, updated_at = ? "
        "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
        (ts, ts, loc_id, ws),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Palcoscenico non trovato")


def restore_location(conn, ws, loc_id):
    ts = now_iso()
    cur = conn.execute(
        "UPDATE locations SET deleted_at = NULL, updated_at = ? "
        "WHERE id = ? AND workspace_id = ? AND deleted_at IS NOT NULL",
        (ts, loc_id, ws),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Palcoscenico non trovato")
    return fetch_location(conn, ws, loc_id)


def purge_location(conn, ws, loc_id):
    """Eliminazione definitiva: sparisce il palcoscenico e tutto quello che
    gli sta attaccato. Al contrario dell'archiviazione non e' recuperabile,
    quindi i file delle foto vanno tolti anche dal disco."""
    existing = conn.execute(
        "SELECT id FROM locations WHERE id = ? AND workspace_id = ?", (loc_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Palcoscenico non trovato")
    filenames = [
        r["filename"]
        for r in conn.execute("SELECT filename FROM photos WHERE location_id = ?", (loc_id,)).fetchall()
    ]
    conn.execute("DELETE FROM photos WHERE location_id = ?", (loc_id,))
    conn.execute("DELETE FROM notes WHERE location_id = ?", (loc_id,))
    conn.execute("DELETE FROM gigs WHERE location_id = ?", (loc_id,))
    conn.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    conn.commit()
    for filename in filenames:
        try:
            os.remove(os.path.join(PHOTOS_DIR, filename))
        except OSError:
            pass


def add_note(conn, ws, loc_id, body):
    kind = (body.get("kind") or "nota").strip() or "nota"
    if kind not in NOTE_KINDS:
        raise ApiError(400, "Tipo di attività non valido")
    text = (body.get("text") or "").strip()
    if not text:
        # I pulsanti rapidi registrano l'attivita' con un tocco solo: il testo
        # lo mette l'app, altrimenti registrare una telefonata costerebbe
        # quanto scriverne una nota.
        text = NOTE_KIND_LABELS.get(kind, "")
    if not text:
        raise ApiError(400, "Il testo della nota è obbligatorio")
    require_location(conn, ws, loc_id)
    ts = now_iso()
    gig = current_gig_row(conn, loc_id)
    nota_id = conn.execute(
        "INSERT INTO notes (location_id, gig_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (loc_id, gig["id"] if gig else None, kind, text, ts),
    ).lastrowid
    conn.execute("UPDATE locations SET updated_at = ? WHERE id = ?", (ts, loc_id))
    # Aver contattato il posto e' esattamente cosa distingue "da contattare"
    # da "contattato": avanzarlo qui evita di dover cambiare lo stato a mano
    # ogni volta. Da "contattato" in poi non si tocca piu' niente: dove sia
    # arrivata la trattativa lo sa solo chi la sta portando avanti.
    #
    # Senza nessuna serata l'attivita' ne apre una per la stagione corrente
    # (ci pensa set_location_status): lavorare un posto vuol dire aver
    # cominciato, e la nota appena scritta e' la prova.
    if kind != "nota" and (gig is None or gig["status"] == "da_contattare"):
        set_location_status(conn, loc_id, "contattato")
        if gig is None:
            nuova = current_gig_row(conn, loc_id)
            if nuova is not None:
                conn.execute(
                    "UPDATE notes SET gig_id = ? WHERE id = ?", (nuova["id"], nota_id)
                )
    conn.commit()
    return fetch_location(conn, ws, loc_id)


def delete_note(conn, ws, note_id):
    row = conn.execute(
        "SELECT n.location_id FROM notes n JOIN locations l ON l.id = n.location_id "
        "WHERE n.id = ? AND l.workspace_id = ?", (note_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Nota non trovata")
    loc_id = row["location_id"]
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return fetch_location(conn, ws, loc_id)


DATA_URL_RE = re.compile(r"^data:image/(\w+);base64,(.+)$", re.S)


def add_photo(conn, ws, loc_id, body):
    existing = conn.execute(
        "SELECT id FROM locations WHERE id = ? AND workspace_id = ?", (loc_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Palcoscenico non trovato")

    data_url = body.get("image_base64") or ""
    m = DATA_URL_RE.match(data_url)
    if not m:
        raise ApiError(400, "Immagine non valida")
    ext = m.group(1).lower()
    if ext not in PHOTO_EXT_CONTENT_TYPE:
        ext = "jpg"
    try:
        raw = base64.b64decode(m.group(2))
    except (ValueError, TypeError):
        raise ApiError(400, "Immagine non valida")
    if not raw:
        raise ApiError(400, "Immagine non valida")
    if len(raw) > MAX_PHOTO_BYTES:
        raise ApiError(400, "Immagine troppo grande (massimo 8 MB)")

    os.makedirs(PHOTOS_DIR, exist_ok=True)
    filename = f"{loc_id}_{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(PHOTOS_DIR, filename), "wb") as f:
        f.write(raw)

    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO photos (location_id, filename, created_at) VALUES (?, ?, ?)",
        (loc_id, filename, ts),
    )
    conn.execute("UPDATE locations SET updated_at = ? WHERE id = ?", (ts, loc_id))
    conn.commit()
    row = conn.execute("SELECT * FROM photos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def delete_photo(conn, ws, photo_id):
    row = conn.execute(
        "SELECT p.filename FROM photos p JOIN locations l ON l.id = p.location_id "
        "WHERE p.id = ? AND l.workspace_id = ?", (photo_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Foto non trovata")
    conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    conn.commit()
    try:
        os.remove(os.path.join(PHOTOS_DIR, row["filename"]))
    except OSError:
        pass


def art_director_to_dict(row, counts):
    d = dict(row)
    d["location_count"] = counts.get(d["id"], 0)
    return d


def fetch_art_directors(conn, ws):
    rows = conn.execute(
        "SELECT * FROM art_directors WHERE workspace_id = ? ORDER BY name COLLATE NOCASE ASC", (ws,)
    ).fetchall()
    count_rows = conn.execute(
        "SELECT art_director_id, COUNT(*) AS n FROM locations "
        "WHERE art_director_id IS NOT NULL AND workspace_id = ? GROUP BY art_director_id", (ws,)
    ).fetchall()
    counts = {r["art_director_id"]: r["n"] for r in count_rows}
    return [art_director_to_dict(r, counts) for r in rows]


def clean_art_director_payload(body, partial):
    data = {}
    for field in ART_DIRECTOR_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if isinstance(value, str):
            value = value.strip()
        data[field] = value
    return data


def create_art_director(conn, ws, body):
    data = clean_art_director_payload(body, partial=False)
    data.setdefault("name", "")
    ts = now_iso()
    fields = list(data.keys()) + ["workspace_id", "created_at"]
    values = list(data.values()) + [ws, ts]
    placeholders = ",".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO art_directors ({','.join(fields)}) VALUES ({placeholders})", values
    )
    conn.commit()
    row = conn.execute("SELECT * FROM art_directors WHERE id = ?", (cur.lastrowid,)).fetchone()
    return art_director_to_dict(row, {})


def update_art_director(conn, ws, ad_id, body):
    existing = conn.execute(
        "SELECT id FROM art_directors WHERE id = ? AND workspace_id = ?", (ad_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Art director non trovato")
    data = clean_art_director_payload(body, partial=True)
    if data:
        set_clause = ",".join(f"{k} = ?" for k in data.keys())
        conn.execute(
            f"UPDATE art_directors SET {set_clause} WHERE id = ?",
            list(data.values()) + [ad_id],
        )
        conn.commit()
    counts_row = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE art_director_id = ? AND workspace_id = ?",
        (ad_id, ws),
    ).fetchone()
    row = conn.execute("SELECT * FROM art_directors WHERE id = ?", (ad_id,)).fetchone()
    return art_director_to_dict(row, {ad_id: counts_row["n"]})


def delete_art_director(conn, ws, ad_id):
    cur = conn.execute(
        "DELETE FROM art_directors WHERE id = ? AND workspace_id = ?", (ad_id, ws)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Art director non trovato")


def clean_band_payload(body, partial):
    data = {}
    for field in BAND_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if field in ("followers", "gigs_count"):
            value = to_number_or_none(value, int)
        elif isinstance(value, str):
            value = value.strip()
        data[field] = value
    return data


def fetch_bands(conn, ws):
    rows = conn.execute(
        "SELECT * FROM bands WHERE workspace_id = ? ORDER BY name COLLATE NOCASE ASC", (ws,)
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_band(conn, ws, band_id):
    row = conn.execute(
        "SELECT * FROM bands WHERE id = ? AND workspace_id = ?", (band_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Band non trovata")
    return dict(row)


def create_band(conn, ws, body):
    data = clean_band_payload(body, partial=False)
    data.setdefault("name", "")
    ts = now_iso()
    fields = list(data.keys()) + ["workspace_id", "created_at", "updated_at"]
    values = list(data.values()) + [ws, ts, ts]
    placeholders = ",".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO bands ({','.join(fields)}) VALUES ({placeholders})", values
    )
    conn.commit()
    return fetch_band(conn, ws, cur.lastrowid)


def update_band(conn, ws, band_id, body):
    existing = conn.execute(
        "SELECT id FROM bands WHERE id = ? AND workspace_id = ?", (band_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Band non trovata")
    data = clean_band_payload(body, partial=True)
    if data:
        data["updated_at"] = now_iso()
        set_clause = ",".join(f"{k} = ?" for k in data.keys())
        conn.execute(
            f"UPDATE bands SET {set_clause} WHERE id = ?", list(data.values()) + [band_id]
        )
        conn.commit()
    return fetch_band(conn, ws, band_id)


def delete_band(conn, ws, band_id):
    cur = conn.execute("DELETE FROM bands WHERE id = ? AND workspace_id = ?", (band_id, ws))
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Band non trovata")


# "Le mie band" e i workspace sono la stessa cosa: ogni band in cui suoni e'
# un contenitore di dati separato, con i suoi membri. Le rotte /api/my_bands
# restano quelle di prima per non rompere l'app installata sui telefoni.

def fetch_my_bands(conn, ctx):
    return fetch_workspaces_for(conn, ctx.email, ctx.ws)


def create_my_band(conn, ctx, body):
    ws_id = create_workspace(
        conn, ctx.email, body.get("name"), body.get("genre"), body.get("city")
    )
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    d = dict(row)
    d["role"] = "leader"
    d["venue_count"] = 0
    d["member_count"] = 1 if ctx.email else 0
    d["active"] = True
    return d


def update_my_band(conn, ctx, ws_id, body):
    if ctx.email and not is_member(conn, ws_id, ctx.email):
        raise ApiError(404, "Band non trovata")
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Il nome è obbligatorio")
    conn.execute(
        "UPDATE workspaces SET name = ?, genre = ?, city = ?, updated_at = ? WHERE id = ?",
        (name, (body.get("genre") or "").strip(), (body.get("city") or "").strip(), now_iso(), ws_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    if not row:
        raise ApiError(404, "Band non trovata")
    d = dict(row)
    d["role"] = member_role(conn, ws_id, ctx.email) or "leader"
    d["venue_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE workspace_id = ? AND deleted_at IS NULL", (ws_id,)
    ).fetchone()["n"]
    d["member_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ?", (ws_id,)
    ).fetchone()["n"]
    d["active"] = (ws_id == ctx.ws)
    return d


def delete_my_band(conn, ctx, ws_id):
    """Elimina una band solo se e' vuota. Cancellare a cascata i palcoscenici
    di una band per un tocco sbagliato e' un danno irreversibile: meglio
    obbligare a svuotarla prima."""
    if ctx.email and not is_member(conn, ws_id, ctx.email):
        raise ApiError(404, "Band non trovata")
    if ctx.email and member_role(conn, ws_id, ctx.email) != "leader":
        raise ApiError(403, "Solo un Leader può eliminare la band")
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE workspace_id = ?", (ws_id,)
    ).fetchone()["n"]
    if n:
        noun = "palcoscenico" if n == 1 else "palcoscenici"
        raise ApiError(
            400,
            f"Questa band contiene {n} {noun}: eliminali prima, oppure lascia la band "
            "senza cancellarla.",
        )
    others = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ? AND email != ?",
        (ws_id, ctx.email or ""),
    ).fetchone()["n"]
    if others:
        raise ApiError(400, "Ci sono altri membri in questa band: rimuovili prima di eliminarla")
    conn.execute("DELETE FROM workspace_members WHERE workspace_id = ?", (ws_id,))
    conn.execute("DELETE FROM invites WHERE workspace_id = ?", (ws_id,))
    for table in WORKSPACE_SCOPED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (ws_id,))
    conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
    conn.execute(
        "UPDATE user_profiles SET active_workspace_id = NULL WHERE active_workspace_id = ?",
        (ws_id,),
    )
    conn.commit()


def switch_workspace(conn, ctx, ws_id):
    if ctx.email and not is_member(conn, ws_id, ctx.email):
        raise ApiError(404, "Band non trovata")
    set_active_workspace(conn, ctx.email, ws_id)
    return fetch_workspaces_for(conn, ctx.email, ws_id)


def fetch_wa_templates(conn, ws):
    rows = conn.execute(
        "SELECT * FROM wa_templates WHERE workspace_id = ? ORDER BY id ASC", (ws,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_wa_template(conn, ws, body):
    name = (body.get("name") or "").strip()
    message = (body.get("message") or "").strip()
    if not name:
        raise ApiError(400, "Il nome è obbligatorio")
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO wa_templates (name, message, workspace_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, message, ws, ts, ts),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM wa_templates WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_wa_template(conn, ws, template_id, body):
    existing = conn.execute(
        "SELECT id FROM wa_templates WHERE id = ? AND workspace_id = ?", (template_id, ws)
    ).fetchone()
    if not existing:
        raise ApiError(404, "Modello non trovato")
    name = (body.get("name") or "").strip()
    message = (body.get("message") or "").strip()
    if not name:
        raise ApiError(400, "Il nome è obbligatorio")
    ts = now_iso()
    conn.execute(
        "UPDATE wa_templates SET name = ?, message = ?, updated_at = ? WHERE id = ?",
        (name, message, ts, template_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM wa_templates WHERE id = ?", (template_id,)).fetchone())


def delete_wa_template(conn, ws, template_id):
    cur = conn.execute(
        "DELETE FROM wa_templates WHERE id = ? AND workspace_id = ?", (template_id, ws)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Modello non trovato")


def fetch_venue_types(conn, ws):
    rows = conn.execute(
        "SELECT * FROM venue_types WHERE workspace_id = ? ORDER BY id ASC", (ws,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_venue_type(conn, ws, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Il nome della tipologia è obbligatorio")
    existing = conn.execute(
        "SELECT id FROM venue_types WHERE LOWER(name) = LOWER(?) AND workspace_id = ?", (name, ws)
    ).fetchone()
    if existing:
        raise ApiError(400, "Questa tipologia esiste già")
    cur = conn.execute(
        "INSERT INTO venue_types (name, workspace_id, created_at) VALUES (?, ?, ?)",
        (name, ws, now_iso()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM venue_types WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def update_venue_type(conn, ws, type_id, body):
    row = conn.execute(
        "SELECT name FROM venue_types WHERE id = ? AND workspace_id = ?", (type_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Tipologia non trovata")
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise ApiError(400, "Il nome della tipologia è obbligatorio")
    old_name = row["name"]

    if new_name.lower() != old_name.lower():
        dup = conn.execute(
            "SELECT id FROM venue_types WHERE LOWER(name) = LOWER(?) AND id != ? AND workspace_id = ?",
            (new_name, type_id, ws),
        ).fetchone()
        if dup:
            raise ApiError(400, "Questa tipologia esiste già")

    conn.execute("UPDATE venue_types SET name = ? WHERE id = ?", (new_name, type_id))

    affected = 0
    if new_name != old_name:
        ts = now_iso()
        cur = conn.execute(
            "UPDATE locations SET type = ?, updated_at = ? WHERE type = ? AND workspace_id = ?",
            (new_name, ts, old_name, ws),
        )
        affected = cur.rowcount

    conn.commit()
    updated = dict(conn.execute("SELECT * FROM venue_types WHERE id = ?", (type_id,)).fetchone())
    updated["affected_locations"] = affected
    return updated


def delete_venue_type(conn, ws, type_id):
    row = conn.execute(
        "SELECT name FROM venue_types WHERE id = ? AND workspace_id = ?", (type_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Tipologia non trovata")
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE type = ? AND workspace_id = ?",
        (row["name"], ws),
    ).fetchone()["n"]
    if count > 0:
        noun = "palcoscenico" if count == 1 else "palcoscenici"
        verb = "usa" if count == 1 else "usano"
        raise ApiError(400, f"Impossibile eliminare: {count} {noun} {verb} ancora questa tipologia")
    conn.execute("DELETE FROM venue_types WHERE id = ? AND workspace_id = ?", (type_id, ws))
    conn.commit()


def fetch_venue_categories(conn, ws):
    rows = conn.execute(
        "SELECT * FROM venue_categories WHERE workspace_id = ? ORDER BY id ASC", (ws,)
    ).fetchall()
    return [dict(r) for r in rows]


def create_venue_category(conn, ws, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Il nome della categoria è obbligatorio")
    existing = conn.execute(
        "SELECT id FROM venue_categories WHERE LOWER(name) = LOWER(?) AND workspace_id = ?",
        (name, ws),
    ).fetchone()
    if existing:
        raise ApiError(400, "Questa categoria esiste già")
    cur = conn.execute(
        "INSERT INTO venue_categories (name, workspace_id, created_at) VALUES (?, ?, ?)",
        (name, ws, now_iso()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM venue_categories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def update_venue_category(conn, ws, category_id, body):
    row = conn.execute(
        "SELECT name FROM venue_categories WHERE id = ? AND workspace_id = ?", (category_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Categoria non trovata")
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise ApiError(400, "Il nome della categoria è obbligatorio")
    old_name = row["name"]

    if new_name.lower() != old_name.lower():
        dup = conn.execute(
            "SELECT id FROM venue_categories WHERE LOWER(name) = LOWER(?) AND id != ? "
            "AND workspace_id = ?",
            (new_name, category_id, ws),
        ).fetchone()
        if dup:
            raise ApiError(400, "Questa categoria esiste già")

    conn.execute("UPDATE venue_categories SET name = ? WHERE id = ?", (new_name, category_id))

    affected = 0
    if new_name != old_name:
        ts = now_iso()
        cur = conn.execute(
            "UPDATE locations SET category = ?, updated_at = ? WHERE category = ? AND workspace_id = ?",
            (new_name, ts, old_name, ws),
        )
        affected = cur.rowcount

    conn.commit()
    updated = dict(conn.execute("SELECT * FROM venue_categories WHERE id = ?", (category_id,)).fetchone())
    updated["affected_locations"] = affected
    return updated


def delete_venue_category(conn, ws, category_id):
    row = conn.execute(
        "SELECT name FROM venue_categories WHERE id = ? AND workspace_id = ?", (category_id, ws)
    ).fetchone()
    if not row:
        raise ApiError(404, "Categoria non trovata")
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE category = ? AND workspace_id = ?",
        (row["name"], ws),
    ).fetchone()["n"]
    if count > 0:
        noun = "palcoscenico" if count == 1 else "palcoscenici"
        verb = "usa" if count == 1 else "usano"
        raise ApiError(400, f"Impossibile eliminare: {count} {noun} {verb} ancora questa categoria")
    conn.execute(
        "DELETE FROM venue_categories WHERE id = ? AND workspace_id = ?", (category_id, ws)
    )
    conn.commit()


def list_owners(conn, ws):
    """Chi puo' avere inserito un palcoscenico: i membri della band attiva,
    non piu' chiunque abbia un profilo sul server."""
    rows = conn.execute(
        "SELECT m.email, p.name FROM workspace_members m "
        "LEFT JOIN user_profiles p ON p.email = m.email "
        "WHERE m.workspace_id = ? ORDER BY COALESCE(p.name, m.email) COLLATE NOCASE ASC",
        (ws,),
    ).fetchall()
    return [dict(r) for r in rows]


class RequestContext:
    """Chi sta chiamando e su quale band. Viene costruito una volta sola nel
    dispatch e passato a ogni handler: e' l'unico punto in cui l'identita'
    entra nel layer dati."""

    __slots__ = ("email", "ws", "origin")

    def __init__(self, email, ws, origin):
        self.email = email
        self.ws = ws
        self.origin = origin


def require_ws(ctx):
    if ctx.ws is None:
        raise ApiError(409, "Nessuna band attiva: creane una o accetta un invito")
    return ctx.ws


def _h_list_locations(conn, match, query, body, ctx):
    status = (query.get("status") or [None])[0]
    search = (query.get("search") or [None])[0]
    include_deleted = (query.get("include_deleted") or [None])[0] in ("1", "true")
    return 200, fetch_locations(conn, require_ws(ctx), status, search, include_deleted)


def _h_list_owners(conn, match, query, body, ctx):
    return 200, list_owners(conn, require_ws(ctx))


def _h_get_location(conn, match, query, body, ctx):
    return 200, fetch_location(conn, require_ws(ctx), int(match.group(1)))


def _h_update_location(conn, match, query, body, ctx):
    return 200, update_location(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_location(conn, match, query, body, ctx):
    delete_location(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_purge_location(conn, match, query, body, ctx):
    purge_location(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_restore_location(conn, match, query, body, ctx):
    return 200, restore_location(conn, require_ws(ctx), int(match.group(1)))


def _h_add_note(conn, match, query, body, ctx):
    return 201, add_note(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_note(conn, match, query, body, ctx):
    return 200, delete_note(conn, require_ws(ctx), int(match.group(1)))


def _h_create_gig(conn, match, query, body, ctx):
    return 201, create_gig(conn, require_ws(ctx), int(match.group(1)), body)


def _h_update_gig(conn, match, query, body, ctx):
    return 200, update_gig(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_gig(conn, match, query, body, ctx):
    return 200, delete_gig(conn, require_ws(ctx), int(match.group(1)))


def _h_add_photo(conn, match, query, body, ctx):
    return 201, add_photo(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_photo(conn, match, query, body, ctx):
    delete_photo(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_list_art_directors(conn, match, query, body, ctx):
    return 200, fetch_art_directors(conn, require_ws(ctx))


def _h_create_art_director(conn, match, query, body, ctx):
    return 201, create_art_director(conn, require_ws(ctx), body)


def _h_update_art_director(conn, match, query, body, ctx):
    return 200, update_art_director(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_art_director(conn, match, query, body, ctx):
    delete_art_director(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_list_bands(conn, match, query, body, ctx):
    return 200, fetch_bands(conn, require_ws(ctx))


def _h_create_band(conn, match, query, body, ctx):
    return 201, create_band(conn, require_ws(ctx), body)


def _h_update_band(conn, match, query, body, ctx):
    return 200, update_band(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_band(conn, match, query, body, ctx):
    delete_band(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_list_my_bands(conn, match, query, body, ctx):
    return 200, fetch_my_bands(conn, ctx)


def _h_create_my_band(conn, match, query, body, ctx):
    return 201, create_my_band(conn, ctx, body)


def _h_update_my_band(conn, match, query, body, ctx):
    return 200, update_my_band(conn, ctx, int(match.group(1)), body)


def _h_delete_my_band(conn, match, query, body, ctx):
    delete_my_band(conn, ctx, int(match.group(1)))
    return 204, {}


def _h_switch_workspace(conn, match, query, body, ctx):
    ws_id = body.get("workspace_id")
    if ws_id is None:
        raise ApiError(400, "Manca la band da attivare")
    return 200, switch_workspace(conn, ctx, int(ws_id))


def _h_list_members(conn, match, query, body, ctx):
    return 200, fetch_members(conn, require_ws(ctx))


def _h_set_member_role(conn, match, query, body, ctx):
    set_member_role(conn, require_ws(ctx), ctx.email, unquote(match.group(1)), body.get("role"))
    return 200, fetch_members(conn, ctx.ws)


def _h_remove_member(conn, match, query, body, ctx):
    target = unquote(match.group(1))
    remove_member(conn, require_ws(ctx), ctx.email, target)
    return 200, fetch_members(conn, ctx.ws)


def _h_list_invites(conn, match, query, body, ctx):
    return 200, fetch_invites(conn, require_ws(ctx), ctx.origin)


def _h_create_invite(conn, match, query, body, ctx):
    # Senza indicazioni il link vale per una persona sola.
    max_uses = to_number_or_none(body.get("max_uses"), int) or 1
    row = create_invite(conn, require_ws(ctx), ctx.email, max_uses)
    return 201, invite_to_dict(row, ctx.origin)


def require_writer(conn, ctx):
    """Uno Slaker consulta ma non tocca. Il controllo sta qui, in un punto
    solo attraversato da ogni scrittura: nascondere i pulsanti nell'app non
    fermerebbe una chiamata fatta a mano."""
    if not ctx.email or ctx.ws is None:
        return
    if member_role(conn, ctx.ws, ctx.email) == "slaker":
        raise ApiError(
            403,
            "Sei Slaker in questa band: puoi consultare i dati ma non modificarli",
        )


def require_admin(ctx):
    """L'amministratore e' definito nel .env di questa installazione. Se
    ADMIN_EMAILS e' vuoto non c'e' nessun amministratore: meglio nessuno che
    tutti, perche' queste rotte cambiano cosa ricevono le band di chiunque."""
    if not auth_enabled():
        return
    if not is_admin(ctx.email):
        raise ApiError(403, "Riservato all'amministratore dell'app")


def _h_list_templates(conn, match, query, body, ctx):
    require_admin(ctx)
    return 200, fetch_templates(conn, (query.get("kind") or [None])[0])


def _h_create_template(conn, match, query, body, ctx):
    require_admin(ctx)
    return 201, create_template(conn, (body.get("kind") or "").strip(), body)


def _h_update_template(conn, match, query, body, ctx):
    require_admin(ctx)
    return 200, update_template(conn, int(match.group(1)), body)


def _h_delete_template(conn, match, query, body, ctx):
    require_admin(ctx)
    delete_template(conn, int(match.group(1)))
    return 204, {}


def _h_list_wa_templates(conn, match, query, body, ctx):
    return 200, fetch_wa_templates(conn, require_ws(ctx))


def _h_create_wa_template(conn, match, query, body, ctx):
    return 201, create_wa_template(conn, require_ws(ctx), body)


def _h_update_wa_template(conn, match, query, body, ctx):
    return 200, update_wa_template(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_wa_template(conn, match, query, body, ctx):
    delete_wa_template(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_list_venue_types(conn, match, query, body, ctx):
    return 200, fetch_venue_types(conn, require_ws(ctx))


def _h_create_venue_type(conn, match, query, body, ctx):
    return 201, create_venue_type(conn, require_ws(ctx), body)


def _h_update_venue_type(conn, match, query, body, ctx):
    return 200, update_venue_type(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_venue_type(conn, match, query, body, ctx):
    delete_venue_type(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


def _h_list_venue_categories(conn, match, query, body, ctx):
    return 200, fetch_venue_categories(conn, require_ws(ctx))


def _h_create_venue_category(conn, match, query, body, ctx):
    return 201, create_venue_category(conn, require_ws(ctx), body)


def _h_update_venue_category(conn, match, query, body, ctx):
    return 200, update_venue_category(conn, require_ws(ctx), int(match.group(1)), body)


def _h_delete_venue_category(conn, match, query, body, ctx):
    delete_venue_category(conn, require_ws(ctx), int(match.group(1)))
    return 204, {}


# Scritture che uno Slaker puo' comunque fare: cambiare la band attiva e'
# una preferenza sua, e creare una band nuova non tocca quella in cui e'
# Slaker — nella band nuova sara' Leader.
SLAKER_ALLOWED = {_h_switch_workspace, _h_create_my_band}

ROUTES = [
    ("GET", re.compile(r"^/api/locations$"), _h_list_locations),
    ("GET", re.compile(r"^/api/owners$"), _h_list_owners),
    ("GET", re.compile(r"^/api/locations/(\d+)$"), _h_get_location),
    ("PUT", re.compile(r"^/api/locations/(\d+)$"), _h_update_location),
    ("DELETE", re.compile(r"^/api/locations/(\d+)$"), _h_delete_location),
    ("POST", re.compile(r"^/api/locations/(\d+)/restore$"), _h_restore_location),
    ("DELETE", re.compile(r"^/api/locations/(\d+)/permanent$"), _h_purge_location),
    ("POST", re.compile(r"^/api/locations/(\d+)/notes$"), _h_add_note),
    ("DELETE", re.compile(r"^/api/notes/(\d+)$"), _h_delete_note),
    ("POST", re.compile(r"^/api/locations/(\d+)/gigs$"), _h_create_gig),
    ("PUT", re.compile(r"^/api/gigs/(\d+)$"), _h_update_gig),
    ("DELETE", re.compile(r"^/api/gigs/(\d+)$"), _h_delete_gig),
    ("POST", re.compile(r"^/api/locations/(\d+)/photos$"), _h_add_photo),
    ("DELETE", re.compile(r"^/api/photos/(\d+)$"), _h_delete_photo),
    ("GET", re.compile(r"^/api/art_directors$"), _h_list_art_directors),
    ("POST", re.compile(r"^/api/art_directors$"), _h_create_art_director),
    ("PUT", re.compile(r"^/api/art_directors/(\d+)$"), _h_update_art_director),
    ("DELETE", re.compile(r"^/api/art_directors/(\d+)$"), _h_delete_art_director),
    ("GET", re.compile(r"^/api/bands$"), _h_list_bands),
    ("POST", re.compile(r"^/api/bands$"), _h_create_band),
    ("PUT", re.compile(r"^/api/bands/(\d+)$"), _h_update_band),
    ("DELETE", re.compile(r"^/api/bands/(\d+)$"), _h_delete_band),
    ("GET", re.compile(r"^/api/workspaces$"), _h_list_my_bands),
    ("PUT", re.compile(r"^/api/workspaces/active$"), _h_switch_workspace),
    ("GET", re.compile(r"^/api/workspaces/members$"), _h_list_members),
    ("PUT", re.compile(r"^/api/workspaces/members/(.+)$"), _h_set_member_role),
    ("DELETE", re.compile(r"^/api/workspaces/members/(.+)$"), _h_remove_member),
    ("GET", re.compile(r"^/api/invites$"), _h_list_invites),
    ("POST", re.compile(r"^/api/invites$"), _h_create_invite),
    ("GET", re.compile(r"^/api/my_bands$"), _h_list_my_bands),
    ("POST", re.compile(r"^/api/my_bands$"), _h_create_my_band),
    ("PUT", re.compile(r"^/api/my_bands/(\d+)$"), _h_update_my_band),
    ("DELETE", re.compile(r"^/api/my_bands/(\d+)$"), _h_delete_my_band),
    ("GET", re.compile(r"^/api/wa_templates$"), _h_list_wa_templates),
    ("POST", re.compile(r"^/api/wa_templates$"), _h_create_wa_template),
    ("PUT", re.compile(r"^/api/wa_templates/(\d+)$"), _h_update_wa_template),
    ("DELETE", re.compile(r"^/api/wa_templates/(\d+)$"), _h_delete_wa_template),
    ("GET", re.compile(r"^/api/admin/templates$"), _h_list_templates),
    ("POST", re.compile(r"^/api/admin/templates$"), _h_create_template),
    ("PUT", re.compile(r"^/api/admin/templates/(\d+)$"), _h_update_template),
    ("DELETE", re.compile(r"^/api/admin/templates/(\d+)$"), _h_delete_template),
    ("GET", re.compile(r"^/api/venue_types$"), _h_list_venue_types),
    ("POST", re.compile(r"^/api/venue_types$"), _h_create_venue_type),
    ("PUT", re.compile(r"^/api/venue_types/(\d+)$"), _h_update_venue_type),
    ("DELETE", re.compile(r"^/api/venue_types/(\d+)$"), _h_delete_venue_type),
    ("GET", re.compile(r"^/api/venue_categories$"), _h_list_venue_categories),
    ("POST", re.compile(r"^/api/venue_categories$"), _h_create_venue_category),
    ("PUT", re.compile(r"^/api/venue_categories/(\d+)$"), _h_update_venue_category),
    ("DELETE", re.compile(r"^/api/venue_categories/(\d+)$"), _h_delete_venue_category),
]


LOGIN_PAGE_TEMPLATE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accedi — GigFlow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root{
    --stage-1:#141225; --stage-2:#0a0e17; --stage-3:#050710;
    --amber:#ffb347; --magenta:#ff3cac; --cyan:#28e0ff;
    --ink:#f4f1ea; --ink-dim:#9aa3b4;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{
    margin:0; overflow:hidden; position:relative; min-height:100vh;
    display:flex; align-items:center; justify-content:center;
    background:radial-gradient(120% 90% at 50% 0%, var(--stage-1) 0%, var(--stage-2) 55%, var(--stage-3) 100%);
    color:var(--ink);
    font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }

  .beam{position:absolute;top:-15%;left:50%;width:40vmax;height:150vmax;
    transform-origin:top center;mix-blend-mode:screen;filter:blur(7px);
    opacity:.5;pointer-events:none;}
  .beam.b1{background:conic-gradient(from 0deg, transparent 0deg, var(--amber) 6deg, transparent 12deg);
    animation:sweep1 9s ease-in-out infinite;}
  .beam.b2{background:conic-gradient(from 0deg, transparent 0deg, var(--magenta) 5deg, transparent 10deg);
    animation:sweep2 11s ease-in-out infinite;}
  .beam.b3{background:conic-gradient(from 0deg, transparent 0deg, var(--cyan) 5deg, transparent 10deg);
    animation:sweep3 13s ease-in-out infinite;}
  @keyframes sweep1{0%,100%{transform:translateX(-50%) rotate(-34deg);}50%{transform:translateX(-50%) rotate(-6deg);}}
  @keyframes sweep2{0%,100%{transform:translateX(-50%) rotate(2deg);}50%{transform:translateX(-50%) rotate(30deg);}}
  @keyframes sweep3{0%,100%{transform:translateX(-50%) rotate(-20deg);}50%{transform:translateX(-50%) rotate(16deg);}}

  .spark{position:absolute;bottom:16%;width:3px;height:3px;border-radius:50%;
    background:var(--amber);box-shadow:0 0 6px 2px rgba(255,179,71,.65);
    opacity:0;animation-name:rise;animation-timing-function:linear;animation-iteration-count:infinite;
    pointer-events:none;}
  @keyframes rise{
    0%{opacity:0;transform:translateY(0) scale(1);}
    10%{opacity:.9;} 85%{opacity:.35;}
    100%{opacity:0;transform:translateY(-65vh) scale(.4);}
  }

  .stage-glow{position:absolute;left:50%;bottom:0;transform:translateX(-50%);
    width:95vmax;height:48vh;pointer-events:none;filter:blur(18px);
    background:radial-gradient(ellipse 60% 100% at 50% 100%,
      rgba(255,183,71,.55) 0%, rgba(255,60,172,.28) 40%, transparent 72%);}
  .stage{position:absolute;left:0;right:0;bottom:4vh;height:44vh;pointer-events:none;}
  .stage svg{position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:min(680px,100vw);height:auto;}
  .band-figures{animation:bob 4s ease-in-out infinite;}
  @keyframes bob{0%,100%{transform:translateY(0);}50%{transform:translateY(-4px);}}
  .drumstick{animation:tap .5s ease-in-out infinite;}
  @keyframes tap{0%,100%{transform:rotate(0deg);}50%{transform:rotate(-22deg);}}
  .guitar-neck{animation:strum 2.4s ease-in-out infinite;}
  @keyframes strum{0%,100%{transform:rotate(-25deg);}50%{transform:rotate(-19deg);}}
  .mic-arm{animation:wave 3.2s ease-in-out infinite;}
  @keyframes wave{0%,100%{transform:rotate(0deg);}50%{transform:rotate(-6deg);}}

  .eq{position:absolute;left:0;right:0;bottom:0;height:4.5vh;display:flex;align-items:flex-end;
    gap:3px;padding:0 4px;opacity:.45;pointer-events:none;}
  .eq span{flex:1;background:linear-gradient(to top, var(--amber), transparent);
    animation:eqbar 1.1s ease-in-out infinite;}
  .eq span:nth-child(2n){animation-duration:.8s;background:linear-gradient(to top, var(--magenta), transparent);}
  .eq span:nth-child(3n){animation-duration:1.4s;background:linear-gradient(to top, var(--cyan), transparent);}
  @keyframes eqbar{0%,100%{height:8%;}50%{height:85%;}}

  .vignette{position:absolute;inset:0;
    background:radial-gradient(120% 80% at 50% 100%, transparent 35%, rgba(0,0,0,.7) 100%);
    pointer-events:none;}

  .card{
    position:relative;z-index:2;
    background:rgba(15,17,28,.6);
    backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
    border:1px solid rgba(255,255,255,.09);
    border-radius:20px;padding:40px 30px 32px;max-width:320px;width:calc(100vw - 48px);
    text-align:center;box-shadow:0 24px 70px rgba(0,0,0,.55);
  }
  .card .brand{font-family:"Bebas Neue",sans-serif;font-size:42px;letter-spacing:.05em;margin:0 0 2px;
    background:linear-gradient(90deg,var(--amber),var(--magenta));
    -webkit-background-clip:text;background-clip:text;color:transparent;}
  .card .tagline{color:var(--ink-dim);font-size:13px;margin:0 0 24px;letter-spacing:.02em;}
  .err{color:#ff8a73;font-size:13px;margin:0 0 14px;}
  a.btn{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;color:#1c1c1e;
    text-decoration:none;font-weight:600;font-size:15px;padding:13px 18px;border-radius:12px;
    transition:transform .15s;}
  a.btn:active{transform:scale(.97);}
  a.btn svg{flex:none;}
  @media (prefers-reduced-motion:reduce){
    .beam,.spark,.band-figures,.drumstick,.guitar-neck,.mic-arm,.eq span{animation:none !important;}
  }
</style>
</head>
<body>
  <div class="beam b1"></div>
  <div class="beam b2"></div>
  <div class="beam b3"></div>

  __SPARKS__

  <div class="stage-glow"></div>
  <div class="stage">
    <svg viewBox="0 0 600 220" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <g class="band-figures" fill="#0b0710">
        <g>
          <ellipse cx="95" cy="185" rx="30" ry="26"/>
          <circle cx="58" cy="150" r="12"/>
          <rect x="9" y="118" width="3" height="47" rx="1.5"/>
          <ellipse cx="8" cy="116" rx="22" ry="4"/>
          <rect x="80" y="150" width="34" height="50" rx="14"/>
          <circle cx="97" cy="138" r="13"/>
          <g class="drumstick" style="transform-box:fill-box;transform-origin:0% 100%;">
            <line x1="108" y1="152" x2="140" y2="112" stroke="#05070b" stroke-width="4" stroke-linecap="round"/>
          </g>
        </g>
        <g>
          <line x1="230" y1="205" x2="230" y2="118" stroke="#05070b" stroke-width="3"/>
          <line x1="219" y1="127" x2="241" y2="127" stroke="#05070b" stroke-width="3" stroke-linecap="round"/>
          <rect x="255" y="140" width="32" height="55" rx="14"/>
          <circle cx="271" cy="128" r="13"/>
          <g class="mic-arm" style="transform-box:fill-box;transform-origin:100% 100%;">
            <line x1="283" y1="150" x2="258" y2="113" stroke="#05070b" stroke-width="6" stroke-linecap="round"/>
            <circle cx="256" cy="110" r="6.5"/>
          </g>
        </g>
        <g>
          <rect x="420" y="145" width="32" height="55" rx="14"/>
          <circle cx="436" cy="133" r="13"/>
          <ellipse cx="452" cy="186" rx="27" ry="19"/>
          <g class="guitar-neck" style="transform-box:fill-box;transform-origin:0% 100%;">
            <rect x="468" y="150" width="62" height="6" rx="3"/>
          </g>
        </g>
      </g>
    </svg>
  </div>
  <div class="eq">__EQBARS__</div>
  <div class="vignette"></div>

  <div class="card">
    <div class="brand">GIGFLOW</div>
    <p class="tagline">Il gestionale live della tua band</p>
    __MSG__
    <a class="btn" href="/auth/google">
      <svg width="18" height="18" viewBox="0 0 48 48">
        <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6.1 29.6 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z"/>
        <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.5 16 18.9 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.6 6.1 29.6 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>
        <path fill="#4CAF50" d="M24 44c5.2 0 10.1-2 13.7-5.3l-6.3-5.3C29.4 35.1 26.8 36 24 36c-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z"/>
        <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.2 5.7l6.3 5.3C39.9 37 44 31 44 24c0-1.3-.1-2.7-.4-3.5z"/>
      </svg>
      Accedi con Google
    </a>
  </div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "GigFlowCRM/1.0"

    def log_message(self, fmt, *args):
        pass  # niente log rumoroso in console

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json(404, {"error": "Non trovato"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "JSON non valido")

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _request_origin(self):
        scheme = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("Host", "localhost")
        return f"{scheme}://{host}"

    def _send_redirect(self, location, set_cookie=None, clear_cookie=None, max_age=None):
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header(
                "Set-Cookie",
                f"{set_cookie[0]}={set_cookie[1]}; Path=/; HttpOnly; SameSite=Lax"
                + (f"; Max-Age={max_age}" if max_age else ""),
            )
        if clear_cookie is not None:
            self.send_header("Set-Cookie", f"{clear_cookie}=deleted; Path=/; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _current_email(self, conn):
        if not auth_enabled():
            return None
        return get_session_email(conn, self._cookie(SESSION_COOKIE))

    def _send_login_page(self, error=None):
        msg = f'<p class="err">{html.escape(error)}</p>' if error else ""
        sparks = "".join(
            f'<div class="spark" style="left:{left}%;animation-duration:{dur}s;animation-delay:{delay}s;"></div>'
            for left, dur, delay in [
                (6, 7, 0), (14, 9, 1.4), (23, 6.5, 3.1), (33, 8, .6), (44, 7.5, 2.4),
                (55, 9.5, 4), (64, 6, 1.1), (74, 8.5, 3.6), (85, 7, .2), (93, 9, 2.8),
            ]
        )
        eqbars = "".join(f'<span style="animation-delay:{i * 0.09}s"></span>' for i in range(28))
        body = LOGIN_PAGE_TEMPLATE.replace("__MSG__", msg).replace("__SPARKS__", sparks).replace("__EQBARS__", eqbars)
        body_bytes = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_auth_route(self, method, path, parsed):
        if method == "GET" and path == "/login":
            self._send_login_page()
            return True

        if method == "GET" and path == "/auth/google":
            if not auth_enabled():
                self._send_json(503, {"error": "Login con Google non configurato"})
                return True
            # Il cookie dell'invito e' la strada normale, ma e' anche l'unica
            # cosa che puo' non tornare indietro dal giro su Google (browser
            # che li limitano, app installata che apre il link in un'altra
            # scheda, cookie di terze parti bloccati). Chi lo perdeva si
            # ritrovava registrato senza band, con il wizard che gli chiedeva
            # nome, genere e citta' della band in cui era stato invitato.
            # Lo stato di OAuth invece Google lo restituisce identico: il
            # token viaggia li' dentro, il cookie resta come riserva.
            state = secrets.token_urlsafe(24)
            invito = (parse_qs(parsed.query).get("invite") or [None])[0] or self._cookie(INVITE_COOKIE)
            if invito:
                state = state + STATE_INVITE_SEP + invito
            redirect_uri = self._request_origin() + "/auth/google/callback"
            url = google_auth_url(redirect_uri, state)
            self._send_redirect(url, set_cookie=(STATE_COOKIE, state), max_age=600)
            return True

        if method == "GET" and path == "/auth/google/callback":
            query = parse_qs(parsed.query)
            state = (query.get("state") or [None])[0]
            code = (query.get("code") or [None])[0]
            expected_state = self._cookie(STATE_COOKIE)
            if not state or not expected_state or state != expected_state or not code:
                self._send_login_page(error="Accesso annullato o non valido. Riprova.")
                return True
            try:
                redirect_uri = self._request_origin() + "/auth/google/callback"
                token_data = google_exchange_code(code, redirect_uri)
                userinfo = google_fetch_userinfo(token_data["access_token"])
            except (urllib.error.URLError, KeyError, json.JSONDecodeError):
                self._send_login_page(error="Impossibile completare l'accesso con Google. Riprova.")
                return True
            email = (userinfo.get("email") or "").strip().lower()
            if not email or not userinfo.get("email_verified", True):
                self._send_login_page(error="Google non ha confermato questo indirizzo email.")
                return True
            # Nessuna lista chiusa di indirizzi: l'accesso e' aperto, ma chi
            # entra senza invito trova un'app vuota e non vede i dati di
            # nessun altro. Sono i workspace a fare da confine, non il login.
            invite_token = self._cookie(INVITE_COOKIE) or invite_from_state(state)
            conn = get_conn()
            try:
                upsert_profile_from_google(conn, email, userinfo.get("name"), userinfo.get("picture"))
                session_id = create_session(conn, email)
                destination = "/"
                if invite_token:
                    joined, invite_error = accept_invite(conn, invite_token, email)
                    destination = (
                        "/?joined=" + urlencode({"n": joined})[2:] if joined
                        else "/?invite_error=" + urlencode({"e": invite_error})[2:]
                    )
            finally:
                conn.close()
            self._send_redirect(
                destination,
                set_cookie=(SESSION_COOKIE, session_id),
                max_age=SESSION_TTL_DAYS * 86400,
                clear_cookie=INVITE_COOKIE if invite_token else None,
            )
            return True

        if method == "GET" and path.startswith("/join/"):
            token = path[len("/join/"):]
            conn = get_conn()
            try:
                email = self._current_email(conn)
                row, error = check_invite(conn, token)
                if error:
                    if email:
                        self._send_redirect("/?invite_error=" + urlencode({"e": error})[2:])
                    else:
                        self._send_login_page(error=error)
                    return True
                if email:
                    joined, error = accept_invite(conn, token, email)
                    if error:
                        self._send_redirect("/?invite_error=" + urlencode({"e": error})[2:])
                    else:
                        self._send_redirect("/?joined=" + urlencode({"n": joined})[2:])
                    return True
            finally:
                conn.close()
            # Non ancora loggato: il token non sopravviverebbe al giro su
            # Google, quindi va parcheggiato in un cookie di breve durata e
            # ripreso nel callback.
            # Il token viaggia nell'indirizzo, non solo nel cookie: da qui
            # finisce dentro lo stato di OAuth, che Google restituisce
            # identico. Cosi' l'invito arriva in fondo anche a un browser che
            # i cookie non li tiene. Il cookie resta come seconda strada.
            self._send_redirect(
                "/auth/google?invite=" + quote(token),
                set_cookie=(INVITE_COOKIE, token),
                max_age=INVITE_COOKIE_TTL_SECONDS,
            )
            return True

        if method == "GET" and path == "/logout":
            conn = get_conn()
            try:
                delete_session(conn, self._cookie(SESSION_COOKIE))
            finally:
                conn.close()
            self._send_redirect("/login", clear_cookie=SESSION_COOKIE)
            return True

        return False

    def _handle_me_route(self, method, path):
        if path != "/api/me":
            return False
        conn = get_conn()
        try:
            email = self._current_email(conn)
            if method == "GET":
                self._send_json(200, fetch_me(conn, email))
            elif method == "PUT":
                if not email:
                    self._send_json(401, {"error": "Accesso richiesto"})
                else:
                    self._send_json(200, update_me(conn, email, self._read_json_body()))
            else:
                self._send_json(405, {"error": "Metodo non consentito"})
        finally:
            conn.close()
        return True

    def _handle_create_location_route(self, method, path):
        if method != "POST" or path != "/api/locations":
            return False
        conn = get_conn()
        try:
            owner_email = self._current_email(conn)
            try:
                ws = resolve_active_workspace(conn, owner_email)
                if ws is None:
                    raise ApiError(409, "Nessuna band attiva: creane una o accetta un invito")
                require_writer(conn, RequestContext(owner_email, ws, self._request_origin()))
                payload = create_location(conn, ws, self._read_json_body(), owner_email=owner_email)
                self._send_json(201, payload)
            except ApiError as e:
                self._send_json(e.status, {"error": e.message})
        finally:
            conn.close()
        return True

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path

        if self._handle_auth_route(method, path, parsed):
            return

        if (
            auth_enabled()
            and path not in PUBLIC_PATHS
            and not path.startswith("/icons/")
            and not path.startswith("/join/")
            and path != "/favicon.ico"
        ):
            conn = get_conn()
            try:
                email = self._current_email(conn)
            finally:
                conn.close()
            if not email:
                if path.startswith("/api/"):
                    self._send_json(401, {"error": "Accesso richiesto"})
                else:
                    self._send_redirect("/login")
                return

        if method == "GET" and path in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return

        if method == "GET" and path == "/manifest.json":
            self._send_file(os.path.join(STATIC_DIR, "manifest.json"), "application/manifest+json; charset=utf-8")
            return

        if method == "GET" and path == "/sw.js":
            self._send_file(os.path.join(STATIC_DIR, "sw.js"), "application/javascript; charset=utf-8")
            return

        if method == "GET" and path == "/comuni.json":
            self._send_file(os.path.join(STATIC_DIR, "comuni.json"), "application/json; charset=utf-8")
            return

        if self._handle_me_route(method, path):
            return

        if self._handle_create_location_route(method, path):
            return

        if method == "GET" and (path.startswith("/icons/") or path == "/favicon.ico"):
            rel = "icons/favicon-32.png" if path == "/favicon.ico" else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                self._send_json(404, {"error": "Non trovato"})
                return
            self._send_file(full, "image/png")
            return

        if method == "GET" and path.startswith("/photos/"):
            filename = path[len("/photos/"):]
            full = os.path.normpath(os.path.join(PHOTOS_DIR, filename))
            if not full.startswith(os.path.normpath(PHOTOS_DIR) + os.sep) or not os.path.isfile(full):
                self._send_json(404, {"error": "Non trovato"})
                return
            ext = full.rsplit(".", 1)[-1].lower()
            content_type = PHOTO_EXT_CONTENT_TYPE.get(ext, "application/octet-stream")
            self._send_file(full, content_type)
            return

        for route_method, pattern, fn in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                body = self._read_json_body() if method in ("POST", "PUT") else {}
                conn = get_conn()
                try:
                    email = self._current_email(conn)
                    ctx = RequestContext(
                        email, resolve_active_workspace(conn, email), self._request_origin()
                    )
                    if method != "GET" and fn not in SLAKER_ALLOWED:
                        require_writer(conn, ctx)
                    status, payload = fn(conn, match, parse_qs(parsed.query), body, ctx)
                finally:
                    conn.close()
                self._send_json(status, payload)
            except ApiError as e:
                self._send_json(e.status, {"error": e.message})
            except Exception as e:  # pragma: no cover - safety net
                self._send_json(500, {"error": f"Errore interno: {e}"})
            return

        self._send_json(404, {"error": "Rotta non trovata"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Palcoscenici CRM avviato.")
    print(f"  Su questo computer: http://localhost:{port}")
    print(f"  Da smartphone (stessa Wi-Fi): http://{local_ip()}:{port}")
    print("Premi Ctrl+C per fermare il server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer arrestato.")


if __name__ == "__main__":
    main()
