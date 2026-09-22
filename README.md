# Sonos Web

Leichtgewichtige Flask-Weboberfläche zur Steuerung von Sonos-Playern und zur Wiedergabe einer lokalen Musikbibliothek.

**Version:** `0.1.0`

## Funktionen

- Sonos-Geräte im lokalen Netzwerk erkennen
- Einzelne Player auswählen und Lautstärke steuern
- Sonos-Gruppen bilden und auflösen
- Wiedergabesteuerung: Play, Pause, Stop, Previous, Next
- Shuffle ein/aus
- automatische Aktualisierung des Wiedergabestatus
- lokale Musikbibliothek über SQLite indexieren
- NAS- und Cache-Dateien verwalten
- zwei Bibliotheksansichten:
  - Artist → Album → Titel
  - Dateisystem-/Ordnerstruktur
- komplette Alben bzw. Ordner zu Playlists hinzufügen
- komplette Ordner direkt in die Sonos-Queue laden und abspielen
- eigene Playlists mit Reihenfolgeverwaltung
- HTTP-Audio-URLs testen
- vorbereitete YouTube-Suchoberfläche
- Betrieb hinter Apache Reverse Proxy und Gunicorn

## Voraussetzungen

- Linux
- Python 3
- Sonos-Geräte im selben Netzwerk
- optional: NAS-Musikbibliothek, z. B. per NFS eingebunden
- Apache2 für den produktiven Reverse-Proxy-Betrieb

## Verzeichnisstruktur

```text
sonos-web/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
├── config/
│   ├── apache-sonos-web.conf.example
│   └── sonos-web.service.example
├── data/
│   └── .gitkeep
└── media/
    └── cache/
        └── .gitkeep
```

## Installation

Repository klonen und virtuelle Umgebung anlegen:

```bash
git clone https://github.com/lexxi/sonos-web.git
cd sonos-web

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Musikbibliothek

Die Anwendung erwartet standardmäßig zwei Quellen:

- lokaler Cache unter `media/cache`
- NAS-Musikbibliothek unter `/mnt/sonos-music`

Die NAS-Freigabe kann beispielsweise per NFS read-only eingebunden werden.

Beispiel für `/etc/fstab`:

```fstab
NAS-IP:/volume1/music /mnt/sonos-music nfs ro,vers=3,_netdev,nofail,x-systemd.automount 0 0
```

Die konkreten Pfade können in `app.py` angepasst werden.

## Entwicklung

Direkter Start:

```bash
source venv/bin/activate
python app.py
```

Der Direktstart bindet nur an `127.0.0.1:5000` und läuft ohne Flask-Debug-Modus.

## Produktion

Empfohlen:

```text
Browser / Sonos
      ↓
Apache2 :8080
      ↓
Gunicorn 127.0.0.1:5000
      ↓
Flask
```

Beispielkonfigurationen liegen unter `config/`.

Gunicorn installieren:

```bash
source venv/bin/activate
pip install gunicorn
```

Service aktivieren:

```bash
sudo cp config/sonos-web.service.example /etc/systemd/system/sonos-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now sonos-web
```

Apache-Site aktivieren:

```bash
sudo cp config/apache-sonos-web.conf.example /etc/apache2/sites-available/sonos-web.conf
sudo a2enmod proxy proxy_http headers
sudo a2ensite sonos-web.conf
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Zusätzlich muss Apache auf Port 8080 lauschen, z. B. in `/etc/apache2/ports.conf`:

```apache
Listen 8080
```

Danach ist die Anwendung beispielsweise unter

```text
http://SERVER-IP:8080/
```

erreichbar.

## Datenbank

Die Anwendung verwendet SQLite. Laufzeitdaten werden nicht ins Git-Repository eingecheckt.

Insbesondere ignoriert werden:

- `data/*.db`
- `data/*.db-wal`
- `data/*.db-shm`
- `media/cache/*`
- `venv/`
- lokale Umgebungs- und Secret-Dateien

## Hinweise zu YouTube

Die Oberfläche enthält vorbereitete Funktionen für die YouTube-Suche. Externe Quellen sollten getrennt von der lokalen Bibliothek behandelt werden. Dauerhaftes lokales Speichern fremder Inhalte sollte nur erfolgen, wenn dies für die jeweilige Quelle und den jeweiligen Inhalt zulässig ist.

## Lizenz

Noch nicht festgelegt.
