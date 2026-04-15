from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SCHEMA_PATH = BASE_DIR / "sql" / "sondedb.sql"
USERS_SCHEMA_PATH = BASE_DIR / "sql" / "users.sql"
SCHEMA_READY = False
SCHEMA_LOCK = threading.Lock()


def load_env_file(path: Path) -> None:
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
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ENV_PATH)


def get_setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def get_setting_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


CONFIG = {
    "server_host": get_setting("SERVER_HOST", "0.0.0.0"),
    "server_port": get_setting_int("SERVER_PORT", 8080),
    "db_host": get_setting("DB_HOST", "10.1.40.51"),
    "db_port": get_setting_int("DB_PORT", 3306),
    "db_name": get_setting("DB_NAME", "sondedb"),
    "db_user": get_setting("DB_USER", "sondedb"),
    "db_password": os.getenv("DB_PASSWORD", ""),
    "api_token": os.getenv("API_TOKEN", ""),
    "dashboard_measure_limit": get_setting_int("DASHBOARD_MEASURES_LIMIT", 250),
    "dashboard_alert_limit": get_setting_int("DASHBOARD_ALERTS_LIMIT", 100),
    "dashboard_reseau_limit": get_setting_int("DASHBOARD_RESEAUX_LIMIT", 100),
    "dashboard_intervention_limit": get_setting_int("DASHBOARD_INTERVENTIONS_LIMIT", 50),
}




# ── Gestion des sessions ──────────────────────────────────────────────────────
# token (str) → username (str)
SESSIONS: dict[str, str] = {}
SESSION_COOKIE = "sondedb_session"

def verify_password(stored_hash: str, password: str) -> bool:
    """Vérifie un mot de passe PBKDF2:sha256."""
    try:
        algo, params = stored_hash.split("$", 1)
        # format: pbkdf2:sha256:iterations$salt_hex$hash_hex
        parts = stored_hash.split("$")
        # parts[0] = "pbkdf2:sha256:200000", parts[1] = salt_hex, parts[2] = hash_hex
        prefix = parts[0]  # "pbkdf2:sha256:200000"
        salt_hex = parts[1]
        hash_hex = parts[2]
        _, hash_name, iterations_str = prefix.split(":")
        iterations = int(iterations_str)
        salt = binascii.unhexlify(salt_hex)
        expected = binascii.unhexlify(hash_hex)
        dk = hashlib.pbkdf2_hmac(hash_name, password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def get_session_user(cookie_header: str) -> str | None:
    """Extrait le nom d'utilisateur depuis le cookie de session."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(SESSION_COOKIE + "="):
            token = part[len(SESSION_COOKIE) + 1:]
            return SESSIONS.get(token)
    return None


def check_user_credentials(username: str, password: str) -> bool:
    """Vérifie les identifiants dans la table users."""
    ensure_schema()
    connection = get_connection(with_database=True)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT password_hash FROM users WHERE username = %s LIMIT 1",
                (username,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()

    if not row:
        return False
    return verify_password(row["password_hash"], password)

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


def normalize_timestamp(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return now_sql()

    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
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
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def level_alias(value: Any) -> str:
    raw = normalize_text(value, "info").lower()
    aliases = {
        "critical": "critical",
        "critique": "critical",
        "high": "critical",
        "warning": "warning",
        "warn": "warning",
        "medium": "warning",
        "info": "info",
        "low": "info",
        "ok": "ok",
    }
    return aliases.get(raw, "info")


def severity_alias(value: Any) -> str:
    raw = normalize_text(value, "medium").lower()
    aliases = {
        "critical": "critical",
        "high": "high",
        "warning": "medium",
        "warn": "medium",
        "medium": "medium",
        "info": "low",
        "low": "low",
        "ok": "low",
    }
    return aliases.get(raw, "medium")


def list_from_payload(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def require_pymysql():
    try:
        import pymysql  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Le module PyMySQL est manquant. Lance `python3 -m pip install -r requirements.txt`."
        ) from exc
    return pymysql


def get_connection(with_database: bool = True):
    pymysql = require_pymysql()
    params = {
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


def iter_sql_statements(sql_script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []

    for raw_line in sql_script.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(raw_line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []

    if current:
        statement = "\n".join(current).strip().rstrip(";").strip()
        if statement:
            statements.append(statement)

    return statements


def apply_schema(connection) -> None:
    if not SCHEMA_PATH.exists():
        raise RuntimeError(f"Fichier de schéma introuvable: {SCHEMA_PATH}")

    statements = iter_sql_statements(SCHEMA_PATH.read_text(encoding="utf-8"))
    cursor = connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
        connection.commit()
    finally:
        cursor.close()


def ensure_schema() -> None:
    global SCHEMA_READY
    if SCHEMA_READY:
        return

    with SCHEMA_LOCK:
        if SCHEMA_READY:
            return

        try:
            connection = get_connection(with_database=True)
        except Exception:
            connection = get_connection(with_database=False)
            try:
                database_name = CONFIG["db_name"].replace("`", "")
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                    connection.commit()
                finally:
                    cursor.close()
            finally:
                connection.close()
            connection = get_connection(with_database=True)

        try:
            apply_schema(connection)
        finally:
            connection.close()

        # Applique le schéma users si présent
        if USERS_SCHEMA_PATH.exists():
            stmts2 = iter_sql_statements(USERS_SCHEMA_PATH.read_text(encoding="utf-8"))
            conn2 = get_connection(with_database=True)
            try:
                cur2 = conn2.cursor()
                try:
                    for s in stmts2:
                        try:
                            cur2.execute(s)
                        except Exception:
                            pass
                    conn2.commit()
                finally:
                    cur2.close()
            finally:
                conn2.close()
        SCHEMA_READY = True


def query_all(cursor, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


def build_dashboard_payload() -> dict[str, Any]:
    ensure_schema()
    connection = get_connection(with_database=True)
    try:
        cursor = connection.cursor()
        try:
            sondes = query_all(
                cursor,
                """
                SELECT
                    id,
                    name AS nom,
                    location AS localisation,
                    DATE_FORMAT(deploy_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS date_deploiement
                FROM probes
                ORDER BY name ASC
                """,
            )
            alertes = query_all(
                cursor,
                """
                SELECT
                    id,
                    probe_id AS id_sonde,
                    alert_type AS type_alerte,
                    description,
                    CASE
                        WHEN severity = 'critical' THEN 'critical'
                        WHEN severity = 'high' THEN 'critical'
                        WHEN severity = 'medium' THEN 'warning'
                        ELSE 'info'
                    END AS niveau,
                    DATE_FORMAT(timestamp, '%%Y-%%m-%%d %%H:%%i:%%s') AS horodatage
                FROM alerts
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (CONFIG["dashboard_alert_limit"],),
            )
            interventions = query_all(
                cursor,
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
                (CONFIG["dashboard_intervention_limit"],),
            )
            mesures = query_all(
                cursor,
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
                (CONFIG["dashboard_measure_limit"],),
            )
            reseaux = query_all(
                cursor,
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
                (CONFIG["dashboard_reseau_limit"],),
            )
        finally:
            cursor.close()
    finally:
        connection.close()

    return {
        "alertes": alertes,
        "interventions": interventions,
        "mesures": mesures,
        "reseaux": reseaux,
        "sondes": sondes,
        "meta": {
            "source": "mysql",
            "updated_at": now_sql(),
            "db_host": CONFIG["db_host"],
            "db_name": CONFIG["db_name"],
        },
    }


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
    rows = list_from_payload(payload, "measurements", "mesures")
    normalized: list[dict[str, Any]] = []

    for row in rows:
        ssid = normalize_text(pick_value(row, "ssid", "name"), "<hidden>")
        bssid = normalize_bssid(pick_value(row, "bssid", "mac"))
        if not bssid and ssid == "<hidden>":
            continue
        normalized.append(
            {
                "ssid": ssid,
                "bssid": bssid or "00:00:00:00:00:00",
                "rssi": normalize_int(pick_value(row, "rssi", "signal"), -100),
                "canal": normalize_int(pick_value(row, "canal", "channel"), 0),
                "horodatage": normalize_timestamp(
                    pick_value(row, "horodatage", "detected_at", "timestamp")
                ),
            }
        )

    return normalized


def normalize_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list_from_payload(payload, "alerts", "alertes")
    normalized: list[dict[str, Any]] = []

    for row in rows:
        alert_type = normalize_text(pick_value(row, "type_alerte", "type"))
        description = normalize_text(row.get("description"))
        if not alert_type and not description:
            continue
        normalized.append(
            {
                "alert_type": alert_type or "WiFi anomaly",
                "description": description or "ESP32 probe alert.",
                "severity": severity_alias(pick_value(row, "severity", "niveau", "level")),
                "timestamp": normalize_timestamp(
                    pick_value(row, "timestamp", "horodatage", "detected_at")
                ),
            }
        )

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
            (
                probe["id"],
                probe["name"],
                probe["location"],
                probe["deploy_date"],
            ),
        )
        return int(probe["id"])

    cursor.execute(
        """
        SELECT id
        FROM probes
        WHERE name = %s AND location = %s
        LIMIT 1
        """,
        (probe["name"], probe["location"]),
    )
    row = cursor.fetchone()
    if row:
        cursor.execute(
            """
            UPDATE probes
            SET deploy_date = %s
            WHERE id = %s
            """,
            (probe["deploy_date"], row["id"]),
        )
        return int(row["id"])

    cursor.execute(
        """
        INSERT INTO probes (name, location, deploy_date)
        VALUES (%s, %s, %s)
        """,
        (probe["name"], probe["location"], probe["deploy_date"]),
    )
    return int(cursor.lastrowid)


def insert_measurements(cursor, probe_id: int, measurements: list[dict[str, Any]]) -> None:
    if not measurements:
        return

    cursor.executemany(
        """
        INSERT INTO measurements (probe_id, ssid, bssid, rssi, channel, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            (probe_id, row["ssid"], row["bssid"], row["rssi"], row["canal"], row["horodatage"])
            for row in measurements
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
        [
            (row["ssid"], row["bssid"], row["canal"], row["horodatage"])
            for row in measurements
        ],
    )


def insert_alerts(cursor, probe_id: int, alerts: list[dict[str, Any]]) -> None:
    if not alerts:
        return

    cursor.executemany(
        """
        INSERT INTO alerts (probe_id, alert_type, description, severity, timestamp)
        VALUES (%s, %s, %s, %s, %s)
        """,
        [
            (probe_id, row["alert_type"], row["description"], row["severity"], row["timestamp"])
            for row in alerts
        ],
    )


def ingest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_schema()
    probe = normalize_probe(payload)
    measurements = normalize_measurements(payload)
    alerts = normalize_alerts(payload)

    connection = get_connection(with_database=True)
    try:
        cursor = connection.cursor()
        try:
            probe_id = resolve_probe_id(cursor, probe)
            insert_measurements(cursor, probe_id, measurements)
            insert_alerts(cursor, probe_id, alerts)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()

    return {
        "status": "ok",
        "probe_id": probe_id,
        "measurements_saved": len(measurements),
        "alerts_saved": len(alerts),
        "server_time": now_sql(),
    }


class SondeDBHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} - "
            f"{fmt % args}"
        )

    def send_json(self, status: int, payload: Any) -> None:
        body = compact_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Token")

    def send_api_error(self, status: int, message: str) -> None:
        self.send_json(status, {"status": "error", "error": message})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        user = get_session_user(self.headers.get("Cookie", ""))

        if parsed.path == "/api/health":
            return self.handle_health()
        if parsed.path == "/api/logout":
            return self.handle_logout()
        if parsed.path == "/login":
            self.path = "/login.html"
            return super().do_GET()
        if parsed.path == "/api/dashboard":
            if not user:
                return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Session expirée. Reconnectez-vous.")
            return self.handle_dashboard()
        if parsed.path == "/":
            if not user:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self.path = "/sondedb_dashboard.html"

        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            return self.handle_login()
        if parsed.path == "/api/ingest":
            return self.handle_ingest()
        self.send_api_error(HTTPStatus.NOT_FOUND, "Point d'entrée API introuvable.")

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

    def authorize_ingest(self, payload: dict[str, Any]) -> bool:
        expected = CONFIG["api_token"]
        if not expected:
            return True

        header_token = normalize_text(self.headers.get("X-API-Token"))
        auth_header = normalize_text(self.headers.get("Authorization"))
        bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""
        payload_token = normalize_text(payload.get("token"))

        return expected in {header_token, bearer_token, payload_token}

    def handle_login(self) -> None:
        length = normalize_int(self.headers.get("Content-Length"), 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            params = parse_qs(raw.decode("utf-8"))
            username = normalize_text(params.get("username", [""])[0])
            password = normalize_text(params.get("password", [""])[0])
        except Exception:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "Paramètres invalides.")

        if not username or not password:
            return self.send_api_error(HTTPStatus.BAD_REQUEST, "Identifiant et mot de passe requis.")

        try:
            ok = check_user_credentials(username, password)
        except Exception as exc:
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        if not ok:
            return self.send_api_error(HTTPStatus.UNAUTHORIZED, "Identifiant ou mot de passe incorrect.")

        token = secrets.token_hex(32)
        SESSIONS[token] = username
        body = compact_json({"status": "ok", "username": username})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        )
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def handle_logout(self) -> None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(SESSION_COOKIE + "="):
                token = part[len(SESSION_COOKIE) + 1:]
                SESSIONS.pop(token, None)
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/login")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0"
        )
        self.end_headers()

    def handle_health(self) -> None:
        try:
            ensure_schema()
            connection = get_connection(with_database=True)
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1 AS ping")
                    cursor.fetchone()
                finally:
                    cursor.close()
            finally:
                connection.close()
        except Exception as exc:
            return self.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))

        self.send_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "database": CONFIG["db_name"],
                "db_host": CONFIG["db_host"],
                "server_time": now_sql(),
            },
        )

    def handle_dashboard(self) -> None:
        try:
            payload = build_dashboard_payload()
        except Exception as exc:
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json(HTTPStatus.OK, payload)

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
            return self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self.send_json(HTTPStatus.CREATED, result)


def main() -> None:
    host = CONFIG["server_host"]
    port = CONFIG["server_port"]
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
