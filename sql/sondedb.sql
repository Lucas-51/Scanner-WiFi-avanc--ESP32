CREATE DATABASE IF NOT EXISTS sondedb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE sondedb;

CREATE TABLE IF NOT EXISTS probes (
    id INT NOT NULL AUTO_INCREMENT,
    name VARCHAR(50) NOT NULL,
    location VARCHAR(100) NOT NULL,
    deploy_date DATETIME NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    config_pending JSON NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS wifi_networks (
    id INT NOT NULL AUTO_INCREMENT,
    ssid VARCHAR(50) NOT NULL,
    bssid VARCHAR(17) NOT NULL,
    channel INT NOT NULL,
    first_seen DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_bssid (bssid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS measurements (
    id INT NOT NULL AUTO_INCREMENT,
    probe_id INT NOT NULL,
    ssid VARCHAR(50) NOT NULL,
    bssid VARCHAR(17) NOT NULL,
    rssi INT NOT NULL,
    channel INT NOT NULL,
    timestamp DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY fk_meas_probe (probe_id),
    CONSTRAINT fk_meas_probe FOREIGN KEY (probe_id) REFERENCES probes (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS alerts (
    id INT NOT NULL AUTO_INCREMENT,
    probe_id INT NOT NULL,
    alert_type VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    severity ENUM('low', 'medium', 'high', 'critical') NOT NULL DEFAULT 'medium',
    timestamp DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY fk_alert_probe (probe_id),
    CONSTRAINT fk_alert_probe FOREIGN KEY (probe_id) REFERENCES probes (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS interventions (
    id INT NOT NULL AUTO_INCREMENT,
    probe_id INT NOT NULL,
    technician VARCHAR(60) NOT NULL,
    description TEXT NOT NULL,
    intervention_date DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY fk_interv_probe (probe_id),
    CONSTRAINT fk_interv_probe FOREIGN KEY (probe_id) REFERENCES probes (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS users (
    id            INT          NOT NULL AUTO_INCREMENT,
    username      VARCHAR(50)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Compte par défaut  →  login: admin  /  mot de passe: admin1234
-- ⚠️  Changez le mot de passe après la première connexion !
INSERT INTO users (username, password_hash, created_at)
VALUES (
    'admin',
    'pbkdf2:sha256:200000$736f6e646564625f73346c745f763121$4c1274bf640443aa50f7fd2fcfabfd588e875792438620adc10c569135b0ce5b',
    NOW()
)
ON DUPLICATE KEY UPDATE password_hash = VALUES(password_hash);