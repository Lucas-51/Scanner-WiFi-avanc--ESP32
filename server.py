"""SondeDB — serveur HTTP, API d'ingestion et dashboard.

Architecture en couches:
  * Config      : chargement .env + constantes runtime
  * Database    : connexion MySQL + schéma + requêtes dashboard
  * Auth        : vérification des mots de passe + sessions
  * Ingest      : normalisation et écriture des scans ESP32
  * HTTPHandler : routage et sérialisation JSON
"""

from __future__ import annotations

import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

# ══ Paths ════════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SCHEMA_PATH = BASE_DIR / "sql" / "sondedb.sql"
USERS_SCHEMA_PATH = BASE_DIR / "sql" / "users.sql"

SESSION_COOKIE = "sondedb_session"


# ══ Config .env ══════════════════════════════════════════════════════════════
def load_env_file(path: Path) -> None:
    """Charge un fichier .env minimaliste dans os.environ."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


load_env_file(ENV_PATH)

CONFIG: dict[str, Any] = {
    "server_host": _env_str("SERVER_HOST", "0.0.0.0"),
    "server_port": _env_int("SERVER_PORT", 8080),
    "db_host": _env_str("DB_HOST", "10.1.40.51"),
    "db_port": _env_int("DB_PORT", 3306),
    "db_name": _env_str("DB_NAME", "sondedb"),
    "db_user": _env_str("DB_USER", "sondedb"),
    "db_password": os.getenv("DB_PASSWORD", ""),
    "api_token": os.getenv("API_TOKEN", ""),
    "limit_mesures": _env_int("DASHBOARD_MEASURES_LIMIT", 250),
    "limit_alertes": _env_int("DASHBOARD_ALERTS_LIMIT", 100),
    "limit_reseaux": _env_int("DASHBOARD_RESEAUX_LIMIT", 100),
    "limit_interventions": _env_int("DASHBOARD_INTERVENTIONS_LIMIT", 50),
}


# ══ Helpers généraux ═════════════════════════════════════════════════════════
def now_sql() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def compact_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def normalize_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def normalize_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_bssid(value: Any) -> str:
    text = normalize_text(value).upper()
    hex_only = re.sub(r"[^0-9A-F]", "", text)
    if len(hex_only) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return text


_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)


def normalize_timestamp(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return now_sql()

    for fmt in _TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=0, minute=0, second=0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    if text.endswith("Z"):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    return now_sql()


def pick_value(mapping: dict[str, Any], *keys: str) -> Any:
    """Retourne la première valeur non vide parmi les clés données."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


_LEVEL_ALIASES = {
    "critical": "critical", "critique": "critical", "high": "critical",
    "warning": "warning", "warn": "warning", "medium": "warning",
    "info": "info", "low": "info", "ok": "ok",
}
_SEVERITY_ALIASES = {
    "critical": "critical", "high": "high",
    "warning": "medium", "warn": "medium", "medium": "medium",
    "info": "low", "low": "low", "ok": "low",
}


def level_alias(value: Any) -> str:
    return _LEVEL_ALIASES.get(normalize_text(value, "info").lower(), "info")


def severity_alias(value: Any) -> str:
    return _SEVERITY_ALIASES.get(normalize_text(value, "medium").lower(), "medium")


def list_from_payload(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def parse_cookie(cookie_header: str) -> dict[str, str]:
    """Parse un header Cookie en dict nom→valeur."""
    cookies: dict[str, str] = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


# ══ Base de données ═════════════════════════════════════════════════════════
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _require_pymysql():
    try:
        import pymysql  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Le module PyMySQL est manquant. Lance `python3 -m pip install -r requirements.txt`."
        ) from exc
    return pymysql


def get_connection(with_database: bool = True):
    pymysql = _require_pymysql()
    params: dict[str, Any] = {
        "host": CONFIG["db_host"],
        "port": CONFIG["db_port"],
        "user": CONFIG["db_user"],
        "password": CONFIG["db_password"],
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 5,
        "read_timeout": 5,
        "write_timeout": 5,
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if with_database:
        params["database"] = CONFIG["db_name"]
    return pymysql.connect(**params)


@contextlib.contextmanager
def mysql_cursor(with_database: bool = True, commit: bool = False):
    """Context manager: ouvre une connexion + cursor, gère rollback/commit/close."""
    connection = get_connection(with_database=with_database)
    try:
        cursor = connection.cursor()
        try:
            yield cursor
            if commit:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()


def _iter_sql_statements(sql_script: str) -> Iterable[str]:
    current: list[str] = []
    for raw_line in sql_script.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(raw_line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                yield statement
            current = []
    if current:
        statement = "\n".join(current).strip().rstrip(";").strip()
        if statement:
            yield statement


def _run_schema_file(path: Path, *, tolerate_errors: bool = False) -> None:
    if not path.exists():
        if tolerate_errors:
            return
        raise RuntimeError(f"Fichier SQL introuvable: {path}")

    with mysql_cursor(with_database=True, commit=True) as cursor:
        for stmt in _iter_sql_statements(path.read_text(encoding="utf-8")):
            try:
                cursor.execute(stmt)
            except Exception:
                if not tolerate_errors:
                    raise


def _ensure_database_exists() -> None:
    db_name = CONFIG["db_name"].replace("`", "")
    with mysql_cursor(with_database=False, commit=True) as cursor:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )


def _ensure_users_columns() -> None:
    """Ajoute password_hash si la table users existait sans cette colonne."""
    with mysql_cursor(with_database=True, commit=True) as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'password_hash'",
            (CONFIG["db_name"],),
        )
        row = cursor.fetchone()
        if row and row["cnt"] == 0:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NOT NULL DEFAULT '' AFTER username"
            )


def _ensure_probe_columns() -> None:
    """Ajoute is_active et config_pending à probes si manquants."""
    with mysql_cursor(with_database=True, commit=True) as cursor:
        for col, ddl in (
            ("is_active",      "ALTER TABLE probes ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1"),
            ("config_pending", "ALTER TABLE probes ADD COLUMN config_pending JSON NULL"),
        ):
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'probes' AND COLUMN_NAME = %s",
                (CONFIG["db_name"], col),
            )
            if cursor.fetchone()["cnt"] == 0:
                cursor.execute(ddl)


def ensure_schema() -> None:
    """Initialise la base + schéma principal + schéma users (idempotent)."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return

        # Création de la base si nécessaire
        try:
            get_connection(with_database=True).close()
        except Exception:
            _ensure_database_exists()

        _run_schema_file(SCHEMA_PATH, tolerate_errors=False)
        _run_schema_file(USERS_SCHEMA_PATH, tolerate_errors=True)
        _ensure_users_columns()
        _ensure_probe_columns()
        _SCHEMA_READY = True


# ── Requêtes dashboard ──────────────────────────────────────────────────────
DASHBOARD_QUERIES: dict[str, tuple[str, str]] = {
    "sondes": (
        """
        SELECT
            p.id,
            p.name AS nom,
            p.location AS localisation,
            DATE_FORMAT(p.deploy_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS date_deploiement,
            DATE_FORMAT(MAX(m.timestamp), '%%Y-%%m-%%d %%H:%%i:%%s') AS last_seen,
            p.is_active
        FROM probes p
        LEFT JOIN measurements m ON m.probe_id = p.id
        GROUP BY p.id, p.name, p.location, p.deploy_date, p.is_active
        ORDER BY p.name ASC
        """,
        "",
    ),
    "alertes": (
        """
        SELECT
            id,
            probe_id AS id_sonde,
            alert_type AS type_alerte,
            description,
            CASE
                WHEN severity IN ('critical','high') THEN 'critical'
                WHEN severity = 'medium'             THEN 'warning'
                ELSE 'info'
            END AS niveau,
            DATE_FORMAT(timestamp, '%%Y-%%m-%%d %%H:%%i:%%s') AS horodatage
        FROM alerts
        ORDER BY timestamp DESC
        LIMIT %s
        """,
        "limit_alertes",
    ),
    "interventions": (
        """
        SELECT
            id,
            probe_id AS id_sonde,
            technician AS technicien,
            description,
            DATE_FORMAT(intervention_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS date_intervention
        FROM interventions
        ORDER BY intervention_date DESC
        LIMIT %s
        """,
        "limit_interventions",
    ),
    "mesures": (
        """
        SELECT *
        FROM (
            SELECT
                id,
                probe_id AS id_sonde,
                ssid,
                bssid,
                rssi,
                channel AS canal,
                DATE_FORMAT(timestamp, '%%Y-%%m-%%d %%H:%%i:%%s') AS horodatage
            FROM measurements
            ORDER BY timestamp DESC
            LIMIT %s
        ) AS recent_mesures
        ORDER BY horodatage ASC, id ASC
        """,
        "limit_mesures",
    ),
    "reseaux": (
        """
        SELECT
            id,
            ssid,
            bssid,
            channel AS canal,
            DATE_FORMAT(first_seen, '%%Y-%%m-%%d %%H:%%i:%%s') AS date_detection
        FROM wifi_networks
        ORDER BY first_seen DESC
        LIMIT %s
        """,
        "limit_reseaux",
    ),
}


def build_dashboard_payload() -> dict[str, Any]:
    ensure_schema()
    data: dict[str, list[dict[str, Any]]] = {}

    with mysql_cursor(with_database=True) as cursor:
        for key, (query, limit_key) in DASHBOARD_QUERIES.items():
            params: tuple[Any, ...] = (CONFIG[limit_key],) if limit_key else ()
            cursor.execute(query, params)
            data[key] = list(cursor.fetchall())

    data["meta"] = {
        "source": "mysql",
        "updated_at": now_sql(),
        "db_host": CONFIG["db_host"],
        "db_name": CONFIG["db_name"],
    }
    return data


# ══ Commandes sonde ═════════════════════════════════════════════════════════
def get_probe_sync(probe_id: int) -> dict[str, Any]:
    """Retourne l'état actif + config en attente, puis efface config_pending."""
    ensure_schema()
    with mysql_cursor(with_database=True, commit=True) as cursor:
        cursor.execute(
            "SELECT is_active, config_pending FROM probes WHERE id = %s",
            (probe_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {"active": True, "config": None}

        config = json.loads(row["config_pending"]) if row["config_pending"] else None
        if config:
            cursor.execute("UPDATE probes SET config_pending = NULL WHERE id = %s", (probe_id,))

        return {"active": bool(row["is_active"]), "config": config}


def update_probe_settings(probe_id: int, settings: dict[str, Any]) -> None:
    """Met à jour is_active et/ou config_pending pour une sonde."""
    ensure_schema()
    config: dict[str, str] = {}
    if "name" in settings:
        config["name"] = normalize_text(settings["name"])
    if "location" in settings:
        config["location"] = normalize_text(settings["location"])

    active = settings.get("active")

    with mysql_cursor(with_database=True, commit=True) as cursor:
        if config and active is not None:
            cursor.execute(
                "UPDATE probes SET config_pending = %s, is_active = %s WHERE id = %s",
                (json.dumps(config), 1 if active else 0, probe_id),
            )
        elif config:
            cursor.execute(
                "UPDATE probes SET config_pending = %s WHERE id = %s",
                (json.dumps(config), probe_id),
            )
        elif active is not None:
            cursor.execute(
                "UPDATE probes SET is_active = %s WHERE id = %s",
                (1 if active else 0, probe_id),
            )


# ══ Authentification ════════════════════════════════════════════════════════
# token → username
SESSIONS: dict[str, str] = {}


def verify_password(stored_hash: str, password: str) -> bool:
    """Vérifie un mot de passe PBKDF2:sha256 (format Werkzeug-like)."""
    try:
        parts = stored_hash.split("$")
        prefix = parts[0]  # e.g. "pbkdf2:sha256:200000"
        salt = binascii.unhexlify(parts[1])
        expected = binascii.unhexlify(parts[2])
        _, hash_name, iterations_str = prefix.split(":")
        dk = hashlib.pbkdf2_hmac(hash_name, password.encode("utf-8"), salt, int(iterations_str))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def check_user_credentials(username: str, password: str) -> bool:
    ensure_schema()
    with mysql_cursor(with_database=True) as cursor:
        cursor.execute(
            "SELECT password_hash FROM users WHERE username = %s LIMIT 1",
            (username,),
        )
        row = cursor.fetchone()
    return bool(row) and verify_password(row["password_hash"], password)


def get_session_user(cookie_header: str) -> str | None:
    token = parse_cookie(cookie_header).get(SESSION_COOKIE)
    return SESSIONS.get(token) if token else None


def drop_session(cookie_header: str) -> None:
    token = parse_cookie(cookie_header).get(SESSION_COOKIE)
    if token:
        SESSIONS.pop(token, None)


# ══ Ingestion ESP32 ═════════════════════════════════════════════════════════
def normalize_probe(payload: dict[str, Any]) -> dict[str, Any]:
    probe = payload["probe"] if isinstance(payload.get("probe"), dict) else {}
    probe_id = normalize_int(
        pick_value(probe, "id", "probe_id", "id_sonde")
        or pick_value(payload, "probe_id", "id_sonde", "id"),
        0,
    )
    return {
        "id": probe_id,
        "name": normalize_text(
            pick_value(probe, "name", "nom"),
            f"ESP32-{probe_id if probe_id > 0 else 'NEW'}",
        ),
        "location": normalize_text(
            pick_value(probe, "location", "localisation"),
            "Location to be defined",
        ),
        "deploy_date": normalize_timestamp(
            pick_value(probe, "deploy_date", "date_deploiement", "deployed_at")
            or pick_value(payload, "deploy_date", "date_deploiement")
        ),
    }


def normalize_measurements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in list_from_payload(payload, "measurements", "mesures"):
        ssid = normalize_text(pick_value(row, "ssid", "name"), "<hidden>")
        bssid = normalize_bssid(pick_value(row, "bssid", "mac"))
        if not bssid and ssid == "<hidden>":
            continue
        normalized.append({
            "ssid": ssid,
            "bssid": bssid or "00:00:00:00:00:00",
            "rssi": normalize_int(pick_value(row, "rssi", "signal"), -100),
            "canal": normalize_int(pick_value(row, "canal", "channel"), 0),
            "horodatage": normalize_timestamp(
                pick_value(row, "horodatage", "detected_at", "timestamp")
            ),
        })
    return normalized


def normalize_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in list_from_payload(payload, "alerts", "alertes"):
        alert_type = normalize_text(pick_value(row, "type_alerte", "type"))
        description = normalize_text(row.get("description"))
        if not alert_type and not description:
            continue
        normalized.append({
            "alert_type": alert_type or "WiFi anomaly",
            "description": description or "ESP32 probe alert.",
            "severity": severity_alias(pick_value(row, "severity", "niveau", "level")),
            "timestamp": normalize_timestamp(
                pick_value(row, "timestamp", "horodatage", "detected_at")
            ),
        })
    return normalized


def resolve_probe_id(cursor, probe: dict[str, Any]) -> int:
    if probe["id"] > 0:
        cursor.execute(
            """
            INSERT INTO probes (id, name, location, deploy_date)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                location = VALUES(location),
                deploy_date = VALUES(deploy_date)
            """,
            (probe["id"], probe["name"], probe["location"], probe["deploy_date"]),
        )
        return int(probe["id"])

    cursor.execute(
        "SELECT id FROM probes WHERE name = %s AND location = %s LIMIT 1",
        (probe["name"], probe["location"]),
    )
    row = cursor.fetchone()
    if row:
        cursor.execute(
            "UPDATE probes SET deploy_date = %s WHERE id = %s",
            (probe["deploy_date"], row["id"]),
        )
        return int(row["id"])

    cursor.execute(
        "INSERT INTO probes (name, location, deploy_date) VALUES (%s, %s, %s)",
        (probe["name"], probe["location"], probe["deploy_date"]),
    )
    return int(cursor.lastrowid)


def insert_measurements(cursor, probe_id: int, measurements: list[dict[str, Any]]) -> None:
    if not measurements:
        return
    cursor.executemany(
        "INSERT INTO measurements (probe_id, ssid, bssid, rssi, channel, timestamp) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [
            (probe_id, m["ssid"], m["bssid"], m["rssi"], m["canal"], m["horodatage"])
            for m in measurements
        ],
    )
    cursor.executemany(
        """
        INSERT INTO wifi_networks (ssid, bssid, channel, first_seen)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ssid = VALUES(ssid),
            channel = VALUES(channel)
        """,
        [(m["ssid"], m["bssid"], m["canal"], m["horodatage"]) for m in measurements],
    )


def insert_alerts(cursor, probe_id: int, alerts: list[dict[str, Any]]) -> None:
    if not alerts:
        return
    cursor.executemany(
        "INSERT INTO alerts (probe_id, alert_type, description, severity, timestamp) "
        "VALUES (%s, %s, %s, %s, %s)",
        [
            (probe_id, a["alert_type"], a["description"], a["severity"], a["timestamp"])
            for a in alerts
        ],
    )


def ingest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_schema()
    probe = normalize_probe(payload)
    measurements = normalize_measurements(payload)
    alerts = normalize_alerts(payload)

    with mysql_cursor(with_database=True, commit=True) as cursor:
        probe_id = resolve_probe_id(cursor, probe)
        insert_measurements(cursor, probe_id, measurements)
        insert_alerts(cursor, probe_id, alerts)

    return {
        "status": "ok",
        "probe_id": probe_id,
        "measurements_saved": len(measurements),
        "alerts_saved": len(alerts),
        "server_time": now_sql(),
    }


# ══ Handler HTTP ═════════════════════════════════════════════════════════════
class SondeDBHandler(SimpleHTTPRequestHandler):
    # Sérialisation
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {self.address_string()} - {fmt % args}")

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Token")

    def send_json(self, status: int, payload: Any, *, extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        body = compact_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status: int, message: str) -> None:
        self.send_json(status, {"status": "error", "error": message})

    def redirect(self, location: str, *, extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()

    # Méthodes HTTP
    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        user = get_session_user(self.headers.get("Cookie", ""))

        route = _GET_ROUTES.get(path)
        if route is not None:
            return route(self, user)

        # /api/probe/<id>/sync
        if re.match(r"^/api/probe/\d+/sync$", path):
            return self.handle_probe_sync()

        # Pages statiques protégées / redirection
        if path == "/":
            if not user:
                return self.redirect("/login")
            self.path = "/sondedb_dashboard.html"
        elif path == "/login":
            self.path = "/login.html"

        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        route = _POST_ROUTES.get(path)
        if route is not None:
            return route(self)

        # /api/probe/<id>/settings
        if re.match(r"^/api/probe/\d+/settings$", path):
            return self.handle_probe_settings()

        return self.send_api_error(HTTPStatus.NOT_FOUND, "Point d'entrée API introuvable.")

    # Lecture body
    def read_json_body(self) -> dict[str, Any]:
        length = normalize_int(self.headers.get("Content-Length"), 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Payload JSON invalide.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Le payload doit être un objet JSON.")
        return payload

    def read_form_body(self) -> dict[str, str]:
        length = normalize_int(self.headers.get("Content-Length"), 0)
        raw = self.rfile.read(length) if length > 0 else b""
        params = parse_qs(raw.decode("utf-8"))
        return {k: v[0] if v else "" for k, v in params.items()}

    # ── Routes GET ───────────────────────────────────────────────────────────
    def handle_health(self, _user: str | None) -> None:
        try:
            ensure_schema()
            with mysql_cursor(with_database=True) as cursor:
                cursor.execute("SELECT 1 AS ping")
                cursor.fetchone()
        except Exception as exc:
            return self.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))

        self.send_json(HTTPStatus.OK, {
            "status": "ok",
            "database": CONFIG["db_name"],
            "db_host": CONFIG["db_host"],
            "server_time": now_sql(),
        })

    def handle_dashboard(self, user: str | None) -> None:
        if not user:
            return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Session expirée. Reconnectez-vous.")
        try:
            payload = build_dashboard_payload()
        except Exception as exc:
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json(HTTPStatus.OK, payload)

    def handle_logout(self, _user: str | None) -> None:
        drop_session(self.headers.get("Cookie", ""))
        self.redirect("/login", extra_headers=[
            ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0"),
        ])

    # ── Routes POST ──────────────────────────────────────────────────────────
    def handle_login(self) -> None:
        try:
            form = self.read_form_body()
        except Exception:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "Paramètres invalides.")

        username = normalize_text(form.get("username"))
        password = normalize_text(form.get("password"))
        if not username or not password:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "Identifiant et mot de passe requis.")

        try:
            ok = check_user_credentials(username, password)
        except Exception as exc:
            traceback.print_exc()
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        if not ok:
            return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Identifiant ou mot de passe incorrect.")

        token = secrets.token_hex(32)
        SESSIONS[token] = username
        self.send_json(
            HTTPStatus.OK,
            {"status": "ok", "username": username},
            extra_headers=[
                ("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"),
            ],
        )

    def authorize_ingest(self, payload: dict[str, Any]) -> bool:
        expected = CONFIG["api_token"]
        if not expected:
            return True

        header_token = normalize_text(self.headers.get("X-API-Token"))
        auth_header = normalize_text(self.headers.get("Authorization"))
        bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""
        payload_token = normalize_text(payload.get("token"))

        return expected in {header_token, bearer_token, payload_token}

    def handle_ingest(self) -> None:
        try:
            payload = self.read_json_body()
        except ValueError as exc:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc))

        if not self.authorize_ingest(payload):
            return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Token API invalide.")

        try:
            result = ingest_payload(payload)
        except Exception as exc:
            traceback.print_exc()
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self.send_json(HTTPStatus.CREATED, result)

    def _probe_id_from_path(self) -> int | None:
        m = re.match(r"^/api/probe/(\d+)/", urlparse(self.path).path)
        return int(m.group(1)) if m else None

    def handle_probe_sync(self) -> None:
        """GET /api/probe/<id>/sync — appelé par l'ESP32."""
        probe_id = self._probe_id_from_path()
        if probe_id is None:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "ID sonde invalide.")

        expected = CONFIG["api_token"]
        if expected:
            token = normalize_text(self.headers.get("X-API-Token"))
            auth = normalize_text(self.headers.get("Authorization"))
            bearer = auth.removeprefix("Bearer ").strip() if auth else ""
            if expected not in {token, bearer}:
                return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Token API invalide.")

        try:
            result = get_probe_sync(probe_id)
        except Exception as exc:
            traceback.print_exc()
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json(HTTPStatus.OK, result)

    def handle_probe_settings(self) -> None:
        """POST /api/probe/<id>/settings — appelé par le dashboard."""
        user = get_session_user(self.headers.get("Cookie", ""))
        if not user:
            return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Session expirée.")

        probe_id = self._probe_id_from_path()
        if probe_id is None:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "ID sonde invalide.")

        try:
            body = self.read_json_body()
        except ValueError as exc:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc))

        try:
            update_probe_settings(probe_id, body)
        except Exception as exc:
            traceback.print_exc()
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json(HTTPStatus.OK, {"status": "ok"})


# ── Tables de routage ───────────────────────────────────────────────────────
_GET_ROUTES: dict[str, Callable[[SondeDBHandler, str | None], None]] = {
    "/api/health": SondeDBHandler.handle_health,
    "/api/dashboard": SondeDBHandler.handle_dashboard,
    "/api/logout": SondeDBHandler.handle_logout,
}
_POST_ROUTES: dict[str, Callable[[SondeDBHandler], None]] = {
    "/api/login": SondeDBHandler.handle_login,
    "/api/ingest": SondeDBHandler.handle_ingest,
}


# ══ Entrée principale ════════════════════════════════════════════════════════
def main() -> None:
    host, port = CONFIG["server_host"], CONFIG["server_port"]
    server = ThreadingHTTPServer((host, port), SondeDBHandler)
    print(f"SondeDB API disponible sur http://{host}:{port}")
    print(
        "Configure ensuite l'ESP32 pour publier vers "
        f"http://{CONFIG['db_host']}:{port}/api/ingest ou vers l'hôte qui exécute ce script."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
