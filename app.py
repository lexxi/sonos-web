from flask import Flask, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3
import threading
import os
import re
import requests
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import soco
from mutagen import File as MutagenFile

app = Flask(__name__)

# Hinter Apache/Gunicorn die ursprünglichen Host-/Proto-Header verwenden.
# Wichtig für request.host_url und damit für die Media-URLs, die Sonos erhält.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "library.db"

CACHE_DIR = BASE_DIR / "media" / "cache"
LIBRARY_DIR = Path("/mnt/sonos-music")

MEDIA_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
}

library_scan_lock = threading.Lock()

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                album TEXT,
                track_number TEXT,
                duration REAL,
                file_size INTEGER,
                mtime REAL,
                scanned_at TEXT NOT NULL,
                UNIQUE(source, path)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_filename ON tracks(filename)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist
            ON playlist_tracks(playlist_id, position)
        """)

        conn.commit()
    finally:
        conn.close()


def discover_sonos():
    devices = soco.discover(timeout=5)
    return list(devices) if devices else []


def find_sonos_by_ip(ip):
    for device in discover_sonos():
        if device.ip_address == ip:
            return device
    return None


def get_coordinator(device):
    try:
        group = device.group
        if group and group.coordinator:
            return group.coordinator
    except Exception:
        pass

    return device


def get_server_url():
    return request.host_url.rstrip("/")


def parse_youtube_duration(value):
    if not value:
        return None

    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T"
        r"(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+)S)?",
        value
    )

    if not match:
        return None

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)

    return (
        days * 86400
        + hours * 3600
        + minutes * 60
        + seconds
    )


def first_tag(tags, keys):
    if not tags:
        return None

    for key in keys:
        value = tags.get(key)

        if not value:
            continue

        if isinstance(value, (list, tuple)):
            if not value:
                continue
            value = value[0]

        value = str(value).strip()
        if value:
            return value

    return None


def read_tags(path):
    result = {
        "title": path.stem,
        "artist": None,
        "album": None,
        "track_number": None,
        "duration": None,
    }

    try:
        audio = MutagenFile(path, easy=True)

        if audio is None:
            return result

        tags = getattr(audio, "tags", None)

        result["title"] = first_tag(tags, ["title"]) or path.stem
        result["artist"] = first_tag(tags, ["artist", "albumartist"])
        result["album"] = first_tag(tags, ["album"])
        result["track_number"] = first_tag(tags, ["tracknumber"])

        info = getattr(audio, "info", None)
        if info is not None:
            length = getattr(info, "length", None)
            if length is not None:
                result["duration"] = round(float(length), 2)

    except Exception:
        pass

    return result


def scan_source(source_name, base_dir):
    base_dir = base_dir.resolve()

    if not base_dir.exists():
        return {
            "source": source_name,
            "found": 0,
            "indexed": 0,
            "unchanged": 0,
            "errors": 0,
            "missing": True,
        }

    conn = get_db()
    found = 0
    indexed = 0
    unchanged = 0
    errors = 0
    scanned_at = datetime.now(timezone.utc).isoformat()

    try:
        for path in base_dir.rglob("*"):
            try:
                if not path.is_file():
                    continue

                if path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue

                found += 1
                relative_path = str(path.relative_to(base_dir))
                stat = path.stat()

                existing = conn.execute(
                    """
                    SELECT id, mtime, file_size
                    FROM tracks
                    WHERE source = ? AND path = ?
                    """,
                    (source_name, relative_path),
                ).fetchone()

                if (
                    existing
                    and existing["mtime"] == stat.st_mtime
                    and existing["file_size"] == stat.st_size
                ):
                    conn.execute(
                        "UPDATE tracks SET scanned_at = ? WHERE id = ?",
                        (scanned_at, existing["id"]),
                    )
                    unchanged += 1
                else:
                    tags = read_tags(path)

                    conn.execute(
                        """
                        INSERT INTO tracks (
                            source, path, filename, title, artist, album,
                            track_number, duration, file_size, mtime, scanned_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source, path)
                        DO UPDATE SET
                            filename = excluded.filename,
                            title = excluded.title,
                            artist = excluded.artist,
                            album = excluded.album,
                            track_number = excluded.track_number,
                            duration = excluded.duration,
                            file_size = excluded.file_size,
                            mtime = excluded.mtime,
                            scanned_at = excluded.scanned_at
                        """,
                        (
                            source_name,
                            relative_path,
                            path.name,
                            tags["title"],
                            tags["artist"],
                            tags["album"],
                            tags["track_number"],
                            tags["duration"],
                            stat.st_size,
                            stat.st_mtime,
                            scanned_at,
                        ),
                    )
                    indexed += 1

                if found % 100 == 0:
                    conn.commit()

            except (PermissionError, OSError, sqlite3.Error):
                errors += 1

        conn.execute(
            """
            DELETE FROM tracks
            WHERE source = ?
              AND scanned_at <> ?
            """,
            (source_name, scanned_at),
        )
        conn.commit()

    finally:
        conn.close()

    return {
        "source": source_name,
        "found": found,
        "indexed": indexed,
        "unchanged": unchanged,
        "errors": errors,
        "missing": False,
    }


def scan_library():
    return [
        scan_source("nas", LIBRARY_DIR),
        scan_source("cache", CACHE_DIR),
    ]


def resolve_media_path(source, relative_path):
    if source == "nas":
        base_dir = LIBRARY_DIR
        media_prefix = "/media/library/"
    elif source == "cache":
        base_dir = CACHE_DIR
        media_prefix = "/media/cache/"
    else:
        raise ValueError("Unbekannte Quelle")

    base_resolved = base_dir.resolve()
    path = (base_dir / relative_path).resolve()

    try:
        path.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError("Ungültiger Dateipfad") from exc

    return path, media_prefix


@app.route("/")
def index():
    return """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sonos Web</title>

    <style>
        :root {
            --bg: #f3f5f7;
            --panel: #ffffff;
            --panel-soft: #f8fafc;
            --text: #172033;
            --muted: #667085;
            --border: #dde3ea;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --danger: #b42318;
            --danger-bg: #fff1f0;
            --shadow: 0 8px 24px rgba(16, 24, 40, 0.06);
            --radius: 14px;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont,
                "Segoe UI", Arial, sans-serif;
        }

        .app-shell {
            width: min(1440px, calc(100% - 28px));
            margin: 0 auto;
            padding: 18px 0 34px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 18px 20px;
            margin-bottom: 16px;
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
        }

        .brand h1 {
            margin: 0;
            font-size: 25px;
            line-height: 1.15;
        }

        .brand-sub {
            margin-top: 4px;
            color: var(--muted);
            font-size: 13px;
        }

        .content-grid {
            display: grid;
            grid-template-columns: repeat(12, minmax(0, 1fr));
            gap: 16px;
        }

        .section {
            grid-column: span 12;
            margin: 0;
            padding: 18px;
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
        }

        .section:nth-of-type(1),
        .section:nth-of-type(6) {
            grid-column: span 12;
        }

        .section:nth-of-type(2),
        .section:nth-of-type(3) {
            grid-column: span 6;
        }

        .section:nth-of-type(4),
        .section:nth-of-type(5) {
            grid-column: span 6;
        }

        .section:nth-of-type(7) {
            grid-column: span 12;
        }

        h2 {
            margin: 0 0 14px;
            font-size: 18px;
        }

        h3 {
            margin: 16px 0 10px;
            font-size: 15px;
        }

        button {
            appearance: none;
            border: 1px solid var(--border);
            border-radius: 9px;
            background: #fff;
            color: var(--text);
            padding: 8px 11px;
            margin: 3px;
            font: inherit;
            cursor: pointer;
            transition: background .15s ease, border-color .15s ease, transform .05s ease;
        }

        button:hover {
            background: #f8fafc;
            border-color: #b9c2ce;
        }

        button:active {
            transform: translateY(1px);
        }

        button.primary {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }

        button.primary:hover {
            background: var(--accent-hover);
            border-color: var(--accent-hover);
        }

        button.danger {
            color: var(--danger);
            background: var(--danger-bg);
            border-color: #f2c7c3;
        }

        input,
        select {
            width: 100%;
            max-width: 100%;
            min-height: 38px;
            padding: 8px 10px;
            margin-bottom: 8px;
            border: 1px solid var(--border);
            border-radius: 9px;
            background: #fff;
            color: var(--text);
            font: inherit;
        }

        input:focus,
        select:focus {
            outline: 2px solid rgba(37, 99, 235, .18);
            border-color: var(--accent);
        }

        input[type="range"] {
            min-height: auto;
            accent-color: var(--accent);
        }

        pre {
            margin: 0;
            padding: 13px;
            max-height: 280px;
            overflow: auto;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            border-radius: 10px;
            background: #101828;
            color: #e6edf5;
            font-size: 12px;
        }

        .track,
        .playlist-track,
        .tree-track,
        .file-track {
            border-bottom: 1px solid #edf0f3;
        }

        .track {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 9px 4px;
        }

        .track-main,
        .playlist-track-main,
        .tree-track-main,
        .file-track-main {
            flex: 1;
            min-width: 0;
        }

        .track-title,
        .playlist-track-title {
            font-weight: 650;
        }

        .track-sub,
        .playlist-track-sub,
        .tree-track-sub,
        .track-meta,
        .youtube-sub {
            color: var(--muted);
            font-size: 12px;
            overflow-wrap: anywhere;
        }

        .source {
            display: inline-block;
            margin-left: 7px;
            padding: 2px 6px;
            border: 1px solid #c8d0da;
            border-radius: 999px;
            color: var(--muted);
            font-size: 10px;
            font-weight: 600;
        }

        .tree-artist,
        .tree-album {
            margin: 4px 0;
        }

        .tree-artist > summary,
        .tree-album > summary,
        .file-node > summary {
            cursor: pointer;
            user-select: none;
            padding: 7px 5px;
            border-radius: 7px;
        }

        .tree-artist > summary:hover,
        .tree-album > summary:hover,
        .file-node > summary:hover {
            background: var(--panel-soft);
        }

        .tree-artist > summary {
            font-weight: 700;
            font-size: 16px;
        }

        .tree-album {
            margin-left: 16px;
        }

        .tree-album > summary {
            font-weight: 650;
        }

        .tree-track {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-left: 34px;
            padding: 6px 4px;
        }

        .tree-count {
            color: var(--muted);
            font-weight: 400;
            font-size: 12px;
            margin-left: 6px;
        }

        .file-node {
            margin-left: 10px;
        }

        .file-track {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-left: 24px;
            padding: 6px 3px;
        }

        .player-row {
            display: grid;
            grid-template-columns: 28px minmax(150px, 1fr) minmax(180px, 2fr) 58px;
            align-items: center;
            gap: 10px;
            padding: 10px;
            margin-bottom: 8px;
            border: 1px solid var(--border);
            border-radius: 10px;
            background: var(--panel-soft);
        }

        .player-name {
            font-weight: 700;
        }

        .player-volume {
            width: 100%;
            margin: 0;
        }

        .group-card {
            padding: 10px 12px;
            margin: 8px 0;
            border: 1px solid var(--border);
            border-radius: 10px;
            background: var(--panel-soft);
        }

        .group-title {
            font-weight: 700;
            margin-bottom: 6px;
        }

        .group-members {
            color: var(--muted);
        }

        .group-member {
            display: inline-block;
            margin: 2px 5px 2px 0;
            padding: 3px 7px;
            border: 1px solid #ccd3dc;
            border-radius: 999px;
            font-size: 11px;
            background: white;
        }

        .group-coordinator {
            font-weight: 700;
        }

        .playlist-track {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 7px 0;
        }

        .playlist-position {
            width: 34px;
            text-align: right;
            color: var(--muted);
        }

        .now-playing {
            margin-top: 14px;
            padding: 14px;
            border: 1px solid #cdd9ee;
            border-radius: 11px;
            background: #f6f9ff;
        }

        .now-playing-title {
            font-weight: 700;
            margin-bottom: 4px;
        }

        .now-playing-sub {
            color: var(--muted);
            font-size: 13px;
        }

        .youtube-result {
            display: grid;
            grid-template-columns: 120px minmax(0, 1fr);
            gap: 12px;
            align-items: center;
            padding: 11px 0;
            border-bottom: 1px solid #edf0f3;
        }

        .youtube-thumb {
            width: 120px;
            max-width: 100%;
            border-radius: 8px;
            display: block;
        }

        .youtube-main {
            min-width: 0;
        }

        .youtube-title {
            font-weight: 700;
        }

        .youtube-actions {
            display: flex;
            gap: 5px;
            flex-wrap: wrap;
            margin-top: 6px;
        }

        .status-section {
            opacity: .82;
        }

        @media (max-width: 1000px) {
            .section:nth-of-type(n) {
                grid-column: span 12;
            }
        }

        @media (max-width: 700px) {
            .app-shell {
                width: min(100% - 16px, 1440px);
                padding-top: 8px;
            }

            .topbar {
                padding: 14px;
                align-items: flex-start;
                flex-direction: column;
            }

            .section {
                padding: 14px;
            }

            .player-row {
                grid-template-columns: 28px 1fr;
            }

            .player-row .player-volume,
            .player-row .player-volume-value {
                grid-column: 2;
            }

            .youtube-result {
                grid-template-columns: 88px minmax(0, 1fr);
            }

            .youtube-thumb {
                width: 88px;
            }

            button {
                min-height: 38px;
            }
        }
    </style>
</head>

<body>

<div class="app-shell">
    <header class="topbar">
        <div class="brand">
            <h1>Sonos Web</h1>
            <div class="brand-sub">Lokale Library, Playlists und Sonos-Steuerung</div>
        </div>
        <div>
            <button class="primary" onclick="scanSonos()">Sonos aktualisieren</button>
            <button onclick="refreshPlayback()">Status aktualisieren</button>
        </div>
    </header>

    <main class="content-grid">

<div class="section">
    <h2>Sonos</h2>

    <button onclick="scanSonos()">Sonos suchen</button>
    <button onclick="createGroup()">Gruppe bilden</button>
    <button onclick="ungroupSelected()">Gruppe auflösen</button>

    <div id="players" style="margin-top: 15px;"></div>

    <div style="margin-top: 20px;">
        <h3>Aktuelle Gruppen</h3>
        <div id="groups"></div>
    </div>

    <div style="margin-top: 20px;">
        <label for="groupVolume"><strong>Gruppenlautstärke</strong></label><br>
        <input
            id="groupVolume"
            type="range"
            min="0"
            max="100"
            value="25"
            style="max-width: 500px;"
            oninput="document.getElementById('groupVolumeValue').textContent = this.value"
        >
        <span id="groupVolumeValue">25</span> %
        <button onclick="setGroupVolume()">Setzen</button>
    </div>
</div>

<div class="section">
    <h2>Musikbibliothek</h2>

    <button class="primary" onclick="scanLibrary()">Library aktualisieren</button>
    <button id="treeTagButton" onclick="showTagTree()">Artist/Album-Baum</button>
    <button id="treeFileButton" onclick="showFileSystemTree()">Dateisystem-Baum</button>
    <button id="treeHideButton" onclick="hideLibraryTree()">Baum ausblenden</button>

    <br><br>

    <input
        id="search"
        type="text"
        placeholder="Artist, Titel, Album oder Datei suchen..."
        oninput="searchLibrary()"
    >

    <div id="library"></div>
</div>

<div class="section">
    <h2>Playlisten</h2>

    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <input id="newPlaylistName" type="text" placeholder="Neue Playlist..." style="max-width:320px;">
        <button class="primary" onclick="createPlaylist()">Playlist anlegen</button>
    </div>

    <div style="margin-top:15px;">
        <select id="playlistSelect" onchange="loadPlaylist()">
            <option value="">-- Playlist auswählen --</option>
        </select>
        <button class="primary" onclick="playPlaylist()">Playlist abspielen</button>
        <button class="danger" onclick="deletePlaylist()">Playlist löschen</button>
    </div>

    <div id="playlistTracks" style="margin-top:15px;"></div>
</div>


<div class="section">
    <h2>YouTube-Suche</h2>

    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <input
            id="youtubeSearch"
            type="text"
            placeholder="YouTube durchsuchen..."
            style="max-width:600px;"
            onkeydown="if(event.key === 'Enter') searchYouTube()"
        >
        <button class="primary" onclick="searchYouTube()">Suchen</button>
    </div>

    <div id="youtubeResults" style="margin-top:15px;"></div>
</div>


<div class="section">
    <h2>HTTP Audio URL</h2>

    <input
        id="audioUrl"
        type="text"
        placeholder="https://..."
    >

    <button onclick="playUrl()">Play URL</button>
</div>

<div class="section">
    <h2>Steuerung</h2>

    <button onclick="previousPlayback()">Previous</button>
    <button onclick="pausePlayback()">Pause</button>
    <button class="primary" onclick="resumePlayback()">Play</button>
    <button onclick="nextPlayback()">Next</button>
    <button class="danger" onclick="stopPlayback()">Stop</button>

    <button onclick="setShuffle(true)">Shuffle an</button>
    <button onclick="setShuffle(false)">Shuffle aus</button>
    <button onclick="refreshPlayback()">Status aktualisieren</button>

    <div id="nowPlaying" class="now-playing">
        Kein Player ausgewählt.
    </div>
</div>

<div class="section status-section">
    <h2>Status / Debug</h2>
    <pre id="result">Bereit.</pre>
</div>
    </main>
</div>

<script>

function showResult(data) {
    document.getElementById("result").textContent =
        JSON.stringify(data, null, 2);
}


function getSelectedPlayers() {
    return Array.from(
        document.querySelectorAll(".player-select:checked")
    ).map(
        checkbox => checkbox.value
    );
}


function getPrimaryPlayer() {
    const selected = getSelectedPlayers();

    if (selected.length === 0) {
        return null;
    }

    return selected[0];
}


let libraryTreeVisible = false;
let libraryTreeMode = null;

let playbackRefreshTimer = null;
let playbackRefreshRunning = false;
const PLAYBACK_REFRESH_INTERVAL_MS = 2500;


function hideLibraryTree() {
    const container =
        document.getElementById("library");

    container.innerHTML = "";
    libraryTreeVisible = false;
    libraryTreeMode = null;
}


async function showTagTree() {
    await loadLibrary("tags");
}


async function showFileSystemTree() {
    await loadLibrary("files");
}




function renderGroups(groups) {
    const container =
        document.getElementById("groups");

    container.innerHTML = "";

    if (!groups || !groups.length) {
        container.innerHTML =
            "<p>Keine Gruppen gefunden.</p>";
        return;
    }

    for (const group of groups) {
        const card =
            document.createElement("div");

        card.className =
            "group-card";

        const title =
            document.createElement("div");

        title.className =
            "group-title";

        title.textContent =
            "Gruppe: " +
            (group.coordinator_name || group.coordinator_ip || group.uid);

        const members =
            document.createElement("div");

        members.className =
            "group-members";

        for (const member of group.members || []) {
            const tag =
                document.createElement("span");

            tag.className =
                "group-member";

            if (member.is_coordinator) {
                tag.classList.add(
                    "group-coordinator"
                );
            }

            tag.textContent =
                member.name +
                (member.is_coordinator
                    ? " (Koordinator)"
                    : "");

            members.appendChild(tag);
        }

        card.appendChild(title);
        card.appendChild(members);
        container.appendChild(card);
    }
}


async function scanSonos() {
    showResult({
        status: "working",
        message: "Suche Sonos..."
    });

    try {
        const response = await fetch("/api/sonos");
        const data = await response.json();

        const container =
            document.getElementById("players");

        container.innerHTML = "";

        for (const device of data.devices || []) {
            const row =
                document.createElement("div");

            row.className = "player-row";

            const checkbox =
                document.createElement("input");

            checkbox.type = "checkbox";
            checkbox.className = "player-select";
            checkbox.value = device.ip;

            const name =
                document.createElement("div");

            name.className = "player-name";
            name.textContent =
                device.name + " (" + device.ip + ")";

            const slider =
                document.createElement("input");

            slider.type = "range";
            slider.min = "0";
            slider.max = "100";
            slider.value = device.volume ?? 0;
            slider.className = "player-volume";
            slider.dataset.ip = device.ip;

            const value =
                document.createElement("div");

            value.className = "player-volume-value";
            value.textContent =
                (device.volume ?? 0) + " %";

            slider.oninput = () => {
                value.textContent =
                    slider.value + " %";
            };

            slider.onchange = () =>
                setPlayerVolume(
                    device.ip,
                    parseInt(slider.value)
                );

            row.appendChild(checkbox);
            row.appendChild(name);
            row.appendChild(slider);
            row.appendChild(value);

            container.appendChild(row);
        }

        renderGroups(data.groups || []);
        showResult(data);

    } catch (error) {
        showResult({
            status: "error",
            message: error.toString()
        });
    }
}


async function setPlayerVolume(ip, volume) {
    const response = await fetch(
        "/api/volume",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ip: ip,
                volume: volume
            })
        }
    );

    showResult(await response.json());
}


async function setGroupVolume() {
    const ips = getSelectedPlayers();

    if (!ips.length) {
        alert("Bitte mindestens einen Sonos auswählen.");
        return;
    }

    const volume =
        parseInt(
            document.getElementById("groupVolume").value
        );

    const response = await fetch(
        "/api/group/volume",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ips: ips,
                volume: volume
            })
        }
    );

    showResult(await response.json());
    scanSonos();
}


async function createGroup() {
    const ips = getSelectedPlayers();

    if (ips.length < 2) {
        alert("Bitte mindestens zwei Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/group",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ips: ips
            })
        }
    );

    showResult(await response.json());
    scanSonos();
}


async function ungroupSelected() {
    const ips = getSelectedPlayers();

    if (!ips.length) {
        alert("Bitte mindestens einen Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/ungroup",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ips: ips
            })
        }
    );

    showResult(await response.json());
    scanSonos();
}


async function scanLibrary() {
    showResult({
        status: "working",
        message: "Library wird aktualisiert..."
    });

    try {
        const response = await fetch(
            "/api/library/scan",
            {
                method: "POST"
            }
        );

        const data = await response.json();

        showResult(data);

        if (data.status === "ok" && libraryTreeVisible) {
            loadLibrary(libraryTreeMode || "tags");
        }

    } catch (error) {
        showResult({
            status: "error",
            message: error.toString()
        });
    }
}


function formatDuration(duration) {
    if (!duration) {
        return "";
    }

    const minutes = Math.floor(duration / 60);
    const seconds =
        Math.floor(duration % 60)
        .toString()
        .padStart(2, "0");

    return minutes + ":" + seconds;
}


function renderFileTree(tree) {
    const container =
        document.getElementById("library");

    container.innerHTML = "";

    if (!tree || !tree.children || !tree.children.length) {
        container.innerHTML =
            "<p>Keine Titel im Index. Zuerst Library aktualisieren.</p>";
        return;
    }

    function renderNode(node, parent) {
        if (node.type === "folder") {
            const details =
                document.createElement("details");

            details.className =
                "file-node";

            const summary =
                document.createElement("summary");

            summary.textContent =
                node.name;

            if (node.track_count !== undefined) {
                const count =
                    document.createElement("span");

                count.className =
                    "tree-count";

                count.textContent =
                    "(" + node.track_count + ")";

                summary.appendChild(count);
            }

            const addFolderButton =
                document.createElement("button");

            addFolderButton.textContent =
                "+ Ordner";

            addFolderButton.style.marginLeft =
                "10px";

            function collectTrackIds(currentNode, target) {
                if (currentNode.type === "track") {
                    target.push(currentNode.id);
                    return;
                }

                for (const child of currentNode.children || []) {
                    collectTrackIds(child, target);
                }
            }

            addFolderButton.onclick = (event) => {
                event.preventDefault();
                event.stopPropagation();

                const trackIds = [];
                collectTrackIds(node, trackIds);

                addAlbumToSelectedPlaylist(trackIds);
            };

            const playFolderButton =
                document.createElement("button");

            playFolderButton.textContent =
                "Play Ordner";

            playFolderButton.style.marginLeft =
                "6px";

            playFolderButton.onclick = (event) => {
                event.preventDefault();
                event.stopPropagation();

                const trackIds = [];
                collectTrackIds(node, trackIds);

                playTrackList(trackIds);
            };

            summary.appendChild(addFolderButton);
            summary.appendChild(playFolderButton);
            details.appendChild(summary);

            for (const child of node.children || []) {
                renderNode(child, details);
            }

            parent.appendChild(details);
            return;
        }

        const row =
            document.createElement("div");

        row.className =
            "file-track";

        const main =
            document.createElement("div");

        main.className =
            "file-track-main";

        main.textContent =
            node.filename || node.title || "Datei";

        const source =
            document.createElement("span");

        source.className =
            "source";

        source.textContent =
            node.source === "nas"
                ? "NAS"
                : "CACHE";

        main.appendChild(source);

        const duration =
            document.createElement("div");

        duration.className =
            "track-meta";

        duration.textContent =
            formatDuration(node.duration);

        const addButton =
            document.createElement("button");

        addButton.textContent =
            "+ Playlist";

        addButton.onclick = () =>
            addTrackToSelectedPlaylist(node.id);

        const playButton =
            document.createElement("button");

        playButton.textContent =
            "Play";

        playButton.onclick = () =>
            playLocal(node.id);

        row.appendChild(main);
        row.appendChild(duration);
        row.appendChild(addButton);
        row.appendChild(playButton);

        parent.appendChild(row);
    }

    for (const child of tree.children) {
        renderNode(child, container);
    }
}


function showTree(tree) {
    const container = document.getElementById("library");
    container.innerHTML = "";

    if (!tree.length) {
        container.innerHTML =
            "<p>Keine Titel im Index. Zuerst Library aktualisieren.</p>";
        return;
    }

    for (const artist of tree) {
        const artistDetails = document.createElement("details");
        artistDetails.className = "tree-artist";

        const artistSummary = document.createElement("summary");
        artistSummary.textContent = artist.name;

        const artistCount = document.createElement("span");
        artistCount.className = "tree-count";
        artistCount.textContent = "(" + artist.track_count + ")";

        artistSummary.appendChild(artistCount);
        artistDetails.appendChild(artistSummary);

        for (const album of artist.albums) {
            const albumDetails = document.createElement("details");
            albumDetails.className = "tree-album";

            const albumSummary = document.createElement("summary");
            albumSummary.textContent = album.name;

            const albumCount = document.createElement("span");
            albumCount.className = "tree-count";
            albumCount.textContent = "(" + album.tracks.length + ")";

            const albumAddButton = document.createElement("button");
            albumAddButton.textContent = "+ Album";
            albumAddButton.style.marginLeft = "10px";
            albumAddButton.onclick = (event) => {
                event.preventDefault();
                event.stopPropagation();
                addAlbumToSelectedPlaylist(
                    album.tracks.map(track => track.id)
                );
            };

            albumSummary.appendChild(albumCount);
            albumSummary.appendChild(albumAddButton);
            albumDetails.appendChild(albumSummary);

            for (const track of album.tracks) {
                const row = document.createElement("div");
                row.className = "tree-track";

                const main = document.createElement("div");
                main.className = "tree-track-main";

                const title = document.createElement("div");
                title.textContent = track.title || track.filename;

                const source = document.createElement("span");
                source.className = "source";
                source.textContent =
                    track.source === "nas"
                        ? "NAS"
                        : "CACHE";

                title.appendChild(source);

                const sub = document.createElement("div");
                sub.className = "tree-track-sub";
                sub.textContent = track.path || "";

                main.appendChild(title);
                main.appendChild(sub);

                const duration = document.createElement("div");
                duration.className = "track-meta";
                duration.textContent = formatDuration(track.duration);

                const addButton = document.createElement("button");
                addButton.textContent = "+ Playlist";
                addButton.onclick = () =>
                    addTrackToSelectedPlaylist(track.id);

                const button = document.createElement("button");
                button.textContent = "Play";
                button.onclick = () => playLocal(track.id);

                row.appendChild(main);
                row.appendChild(duration);
                row.appendChild(addButton);
                row.appendChild(button);

                albumDetails.appendChild(row);
            }

            artistDetails.appendChild(albumDetails);
        }

        container.appendChild(artistDetails);
    }
}


function showTracks(tracks) {
    const container = document.getElementById("library");
    container.innerHTML = "";

    if (!tracks.length) {
        container.innerHTML =
            "<p>Keine Titel gefunden.</p>";
        return;
    }

    for (const track of tracks) {
        const div = document.createElement("div");
        div.className = "track";

        const main = document.createElement("div");
        main.className = "track-main";

        const title = document.createElement("div");
        title.className = "track-title";

        const source = document.createElement("span");
        source.className = "source";
        source.textContent =
            track.source === "nas"
                ? "NAS"
                : "CACHE";

        let titleText = "";

        if (track.artist) {
            titleText += track.artist + " – ";
        }

        titleText += track.title || track.filename;

        title.appendChild(
            document.createTextNode(titleText)
        );
        title.appendChild(source);

        const sub = document.createElement("div");
        sub.className = "track-sub";

        let subText = "";

        if (track.album) {
            subText += track.album;
        }

        if (track.path) {
            if (subText) {
                subText += " · ";
            }

            subText += track.path;
        }

        sub.textContent = subText;

        main.appendChild(title);
        main.appendChild(sub);

        const meta = document.createElement("div");
        meta.className = "track-meta";

        if (track.duration) {
            const minutes = Math.floor(track.duration / 60);
            const seconds =
                Math.floor(track.duration % 60)
                .toString()
                .padStart(2, "0");

            meta.textContent = minutes + ":" + seconds;
        }

        const addButton = document.createElement("button");
        addButton.textContent = "+ Playlist";
        addButton.onclick = () =>
            addTrackToSelectedPlaylist(track.id);

        const button = document.createElement("button");
        button.textContent = "Play";
        button.onclick = () => playLocal(track.id);

        div.appendChild(main);
        div.appendChild(meta);
        div.appendChild(addButton);
        div.appendChild(button);

        container.appendChild(div);
    }
}


async function loadLibrary(mode = "tags") {
    try {
        const endpoint =
            mode === "files"
                ? "/api/library/filetree"
                : "/api/library/tree";

        const response =
            await fetch(endpoint);

        const data =
            await response.json();

        if (mode === "files") {
            renderFileTree(data.tree || {});
        } else {
            showTree(data.tree || []);
        }

        libraryTreeVisible = true;
        libraryTreeMode = mode;

        showResult({
            status: data.status,
            mode: mode,
            count: data.count,
            artist_count: data.artist_count,
            album_count: data.album_count
        });

    } catch (error) {
        showResult({
            status: "error",
            message: error.toString()
        });
    }
}


let searchTimer = null;


function searchLibrary() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearchLibrary, 250);
}


async function doSearchLibrary() {
    const search =
        document
        .getElementById("search")
        .value
        .trim();

    if (!search) {
        if (libraryTreeVisible) {
            loadLibrary(libraryTreeMode || "tags");
        } else {
            document.getElementById("library").innerHTML = "";
        }
        return;
    }

    try {
        const response = await fetch(
            "/api/library/search?q=" +
            encodeURIComponent(search)
        );

        const data = await response.json();
        showTracks(data.tracks || []);

    } catch (error) {
        showResult({
            status: "error",
            message: error.toString()
        });
    }
}


async function loadPlaylists() {
    const response = await fetch("/api/playlists");
    const data = await response.json();

    const select = document.getElementById("playlistSelect");
    const current = select.value;

    select.innerHTML =
        '<option value="">-- Playlist auswählen --</option>';

    for (const playlist of data.playlists || []) {
        const option = document.createElement("option");
        option.value = playlist.id;
        option.textContent =
            playlist.name + " (" + playlist.track_count + ")";
        select.appendChild(option);
    }

    if (
        current &&
        Array.from(select.options)
            .some(option => option.value === current)
    ) {
        select.value = current;
    }
}


async function createPlaylist() {
    const input = document.getElementById("newPlaylistName");
    const name = input.value.trim();

    if (!name) {
        alert("Bitte einen Playlist-Namen eingeben.");
        return;
    }

    const response = await fetch(
        "/api/playlists",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name: name})
        }
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        input.value = "";
        await loadPlaylists();
        document.getElementById("playlistSelect").value =
            String(data.playlist.id);
        await loadPlaylist();
    }
}


async function deletePlaylist() {
    const playlistId =
        document.getElementById("playlistSelect").value;

    if (!playlistId) {
        alert("Bitte eine Playlist auswählen.");
        return;
    }

    const response = await fetch(
        "/api/playlists/" + playlistId,
        {method: "DELETE"}
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        document.getElementById("playlistTracks").innerHTML = "";
        await loadPlaylists();
    }
}


async function addTrackToSelectedPlaylist(trackId) {
    const playlistId =
        document.getElementById("playlistSelect").value;

    if (!playlistId) {
        alert("Bitte zuerst eine Playlist auswählen oder anlegen.");
        return;
    }

    const response = await fetch(
        "/api/playlists/" + playlistId + "/tracks",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({track_id: trackId})
        }
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        await loadPlaylists();
        document.getElementById("playlistSelect").value =
            String(playlistId);
        await loadPlaylist();
    }
}


async function removePlaylistTrack(itemId) {
    const playlistId =
        document.getElementById("playlistSelect").value;

    if (!playlistId) {
        return;
    }

    const response = await fetch(
        "/api/playlists/" + playlistId + "/tracks/" + itemId,
        {method: "DELETE"}
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        await loadPlaylists();
        document.getElementById("playlistSelect").value =
            String(playlistId);
        await loadPlaylist();
    }
}


async function loadPlaylist() {
    const playlistId =
        document.getElementById("playlistSelect").value;

    const container =
        document.getElementById("playlistTracks");

    container.innerHTML = "";

    if (!playlistId) {
        return;
    }

    const response = await fetch("/api/playlists/" + playlistId);
    const data = await response.json();

    if (data.status !== "ok") {
        showResult(data);
        return;
    }

    if (!data.tracks.length) {
        container.innerHTML = "<p>Playlist ist leer.</p>";
        return;
    }

    for (const item of data.tracks) {
        const row = document.createElement("div");
        row.className = "playlist-track";

        const position = document.createElement("div");
        position.className = "playlist-position";
        position.textContent = item.position + ".";

        const main = document.createElement("div");
        main.className = "playlist-track-main";

        const title = document.createElement("div");
        title.className = "playlist-track-title";
        title.textContent =
            (item.artist ? item.artist + " – " : "") +
            (item.title || item.filename);

        const sub = document.createElement("div");
        sub.className = "playlist-track-sub";
        sub.textContent =
            (item.album ? item.album + " · " : "") +
            item.path;

        main.appendChild(title);
        main.appendChild(sub);

        const upButton = document.createElement("button");
        upButton.textContent = "↑";
        upButton.title = "Nach oben";
        upButton.onclick = () =>
            movePlaylistTrack(item.item_id, "up");

        const downButton = document.createElement("button");
        downButton.textContent = "↓";
        downButton.title = "Nach unten";
        downButton.onclick = () =>
            movePlaylistTrack(item.item_id, "down");

        const playButton = document.createElement("button");
        playButton.textContent = "Play";
        playButton.onclick = () =>
            playLocal(item.track_id);

        const removeButton = document.createElement("button");
        removeButton.textContent = "Entfernen";
        removeButton.onclick = () =>
            removePlaylistTrack(item.item_id);

        row.appendChild(position);
        row.appendChild(main);
        row.appendChild(upButton);
        row.appendChild(downButton);
        row.appendChild(playButton);
        row.appendChild(removeButton);

        container.appendChild(row);
    }
}


async function playPlaylist() {
    const playlistId =
        document.getElementById("playlistSelect").value;
    const ip = getPrimaryPlayer();

    if (!playlistId) {
        alert("Bitte eine Playlist auswählen.");
        return;
    }

    if (!ip) {
        alert("Bitte mindestens einen Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/playlists/" + playlistId + "/play",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ip: ip})
        }
    );

    showResult(await response.json());
}


async function addAlbumToSelectedPlaylist(trackIds) {
    const playlistId =
        document.getElementById("playlistSelect").value;

    if (!playlistId) {
        alert("Bitte zuerst eine Playlist auswählen oder anlegen.");
        return;
    }

    const response = await fetch(
        "/api/playlists/" + playlistId + "/tracks/bulk",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({track_ids: trackIds})
        }
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        await loadPlaylists();
        document.getElementById("playlistSelect").value =
            String(playlistId);
        await loadPlaylist();
    }
}


async function movePlaylistTrack(itemId, direction) {
    const playlistId =
        document.getElementById("playlistSelect").value;

    if (!playlistId) {
        return;
    }

    const response = await fetch(
        "/api/playlists/" +
        playlistId +
        "/tracks/" +
        itemId +
        "/move",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({direction: direction})
        }
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        await loadPlaylist();
    }
}


function startPlaybackAutoRefresh() {
    if (playbackRefreshTimer) {
        return;
    }

    playbackRefreshTimer = setInterval(
        async () => {
            if (playbackRefreshRunning) {
                return;
            }

            const ip = getPrimaryPlayer();

            if (!ip) {
                return;
            }

            playbackRefreshRunning = true;

            try {
                await refreshPlayback(true);
            } finally {
                playbackRefreshRunning = false;
            }
        },
        PLAYBACK_REFRESH_INTERVAL_MS
    );
}


function stopPlaybackAutoRefresh() {
    if (!playbackRefreshTimer) {
        return;
    }

    clearInterval(playbackRefreshTimer);
    playbackRefreshTimer = null;
}


async function refreshPlayback(silent = false) {
    const ip = getPrimaryPlayer();
    const container = document.getElementById("nowPlaying");

    if (!ip) {
        container.textContent = "Kein Player ausgewählt.";
        return;
    }

    try {
        const response = await fetch(
            "/api/playback?ip=" + encodeURIComponent(ip)
        );
        const data = await response.json();

        if (data.status !== "ok") {
            container.textContent =
                data.message || "Status nicht verfügbar.";
            return;
        }

        const track = data.track || {};
        container.innerHTML = "";

        const title = document.createElement("div");
        title.className = "now-playing-title";
        title.textContent =
            track.title || track.uri || "Kein Titel";

        const sub = document.createElement("div");
        sub.className = "now-playing-sub";

        const info = [];

        if (track.artist) {
            info.push(track.artist);
        }

        if (track.album) {
            info.push(track.album);
        }

        if (data.transport_state) {
            info.push(data.transport_state);
        }

        if (
            data.queue_position !== null &&
            data.queue_size !== null
        ) {
            info.push(
                "Queue " +
                data.queue_position +
                "/" +
                data.queue_size
            );
        }

        if (data.play_mode) {
            info.push(data.play_mode);
        }

        sub.textContent = info.join(" · ");

        container.appendChild(title);
        container.appendChild(sub);

    } catch (error) {
        container.textContent = error.toString();
    }
}


async function setShuffle(enabled) {
    const ip = getPrimaryPlayer();

    if (!ip) {
        alert("Bitte zuerst Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/shuffle",
        {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                ip: ip,
                enabled: enabled
            })
        }
    );

    showResult(await response.json());
    refreshPlayback();
}


function previousPlayback() {
    simpleAction("previous");
}


function nextPlayback() {
    simpleAction("next");
}


async function playTrackList(trackIds) {
    const ip = getPrimaryPlayer();

    if (!ip) {
        alert("Bitte mindestens einen Sonos auswählen.");
        return;
    }

    if (!trackIds || !trackIds.length) {
        alert("Keine Titel in diesem Ordner gefunden.");
        return;
    }

    const response = await fetch(
        "/api/play/tracks",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ip: ip,
                track_ids: trackIds
            })
        }
    );

    const data = await response.json();
    showResult(data);

    if (data.status === "ok") {
        refreshPlayback();
    }
}


async function playLocal(trackId) {
    const ip = getPrimaryPlayer();

    if (!ip) {
        alert("Bitte zuerst Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/play/local",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ip: ip,
                track_id: trackId
            })
        }
    );

    showResult(await response.json());
}


async function searchYouTube() {
    const query =
        document.getElementById("youtubeSearch").value.trim();

    const container =
        document.getElementById("youtubeResults");

    if (!query) {
        container.innerHTML = "";
        return;
    }

    container.innerHTML =
        "<p>Suche läuft...</p>";

    try {
        const response = await fetch(
            "/api/youtube/search?q=" +
            encodeURIComponent(query)
        );

        const data = await response.json();

        if (data.status !== "ok") {
            container.innerHTML =
                "<p>" +
                (data.message || "YouTube-Suche fehlgeschlagen.") +
                "</p>";

            showResult(data);
            return;
        }

        renderYouTubeResults(
            data.results || []
        );

        showResult({
            status: data.status,
            query: data.query,
            count: data.count
        });

    } catch (error) {
        container.innerHTML =
            "<p>" + error.toString() + "</p>";

        showResult({
            status: "error",
            message: error.toString()
        });
    }
}


function renderYouTubeResults(results) {
    const container =
        document.getElementById("youtubeResults");

    container.innerHTML = "";

    if (!results.length) {
        container.innerHTML =
            "<p>Keine Ergebnisse gefunden.</p>";
        return;
    }

    for (const item of results) {
        const row =
            document.createElement("div");

        row.className =
            "youtube-result";

        if (item.thumbnail) {
            const image =
                document.createElement("img");

            image.className =
                "youtube-thumb";

            image.src =
                item.thumbnail;

            image.alt =
                item.title || "";

            row.appendChild(image);
        }

        const main =
            document.createElement("div");

        main.className =
            "youtube-main";

        const title =
            document.createElement("div");

        title.className =
            "youtube-title";

        title.textContent =
            item.title || item.video_id;

        const sub =
            document.createElement("div");

        sub.className =
            "youtube-sub";

        const info = [];

        if (item.channel) {
            info.push(item.channel);
        }

        if (item.duration_text) {
            info.push(item.duration_text);
        }

        sub.textContent =
            info.join(" · ");

        const actions =
            document.createElement("div");

        actions.className =
            "youtube-actions";

        const openButton =
            document.createElement("button");

        openButton.textContent =
            "YouTube öffnen";

        openButton.onclick = () =>
            window.open(
                item.youtube_url,
                "_blank",
                "noopener"
            );

        const copyButton =
            document.createElement("button");

        copyButton.textContent =
            "URL kopieren";

        copyButton.onclick = async () => {
            try {
                await navigator.clipboard.writeText(
                    item.youtube_url
                );

                showResult({
                    status: "ok",
                    message: "YouTube-URL kopiert",
                    url: item.youtube_url
                });
            } catch (error) {
                showResult({
                    status: "error",
                    message: error.toString()
                });
            }
        };

        actions.appendChild(openButton);
        actions.appendChild(copyButton);

        main.appendChild(title);
        main.appendChild(sub);
        main.appendChild(actions);

        row.appendChild(main);
        container.appendChild(row);
    }
}


async function playUrl() {
    const ip = getPrimaryPlayer();
    const url =
        document.getElementById("audioUrl").value;

    if (!ip) {
        alert("Bitte zuerst Sonos auswählen.");
        return;
    }

    if (!url) {
        alert("Bitte Audio-URL eingeben.");
        return;
    }

    const response = await fetch(
        "/api/play",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ip: ip,
                url: url
            })
        }
    );

    showResult(await response.json());
}


async function simpleAction(action) {
    const ip = getPrimaryPlayer();

    if (!ip) {
        alert("Bitte zuerst Sonos auswählen.");
        return;
    }

    const response = await fetch(
        "/api/" + action,
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                ip: ip
            })
        }
    );

    showResult(await response.json());
    refreshPlayback();
}


function pausePlayback() {
    simpleAction("pause");
}


function resumePlayback() {
    simpleAction("resume");
}


function stopPlayback() {
    simpleAction("stop");
}


window.addEventListener(
    "load",
    () => {
        scanSonos();
startPlaybackAutoRefresh();
        loadPlaylists();
    }
);

</script>

</body>
</html>
"""


@app.route("/api/sonos")
def sonos_devices():
    try:
        devices = discover_sonos()
        result = []

        groups_map = {}

        for device in sorted(
            devices,
            key=lambda x: x.player_name or ""
        ):
            group = device.group

            group_uid = (
                group.uid
                if group
                else None
            )

            coordinator = (
                group.coordinator
                if group and group.coordinator
                else None
            )

            result.append({
                "name": device.player_name,
                "ip": device.ip_address,
                "uid": device.uid,
                "volume": device.volume,
                "group_uid": group_uid,
                "coordinator": (
                    coordinator.ip_address
                    if coordinator
                    else None
                ),
            })

            if group:
                entry = groups_map.setdefault(
                    group_uid,
                    {
                        "uid": group_uid,
                        "coordinator_ip": (
                            coordinator.ip_address
                            if coordinator
                            else None
                        ),
                        "coordinator_name": (
                            coordinator.player_name
                            if coordinator
                            else None
                        ),
                        "members": [],
                    }
                )

                entry["members"].append({
                    "name": device.player_name,
                    "ip": device.ip_address,
                    "uid": device.uid,
                    "volume": device.volume,
                    "is_coordinator": (
                        coordinator is not None
                        and device.ip_address
                        == coordinator.ip_address
                    ),
                })

        groups = sorted(
            groups_map.values(),
            key=lambda group: (
                group["coordinator_name"] or ""
            ).casefold()
        )

        for group in groups:
            group["members"] = sorted(
                group["members"],
                key=lambda member: (
                    0 if member["is_coordinator"] else 1,
                    member["name"].casefold()
                )
            )

        return jsonify({
            "status": "ok",
            "count": len(result),
            "devices": result,
            "groups": groups,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/library")
def library():
    conn = get_db()

    try:
        tracks = conn.execute(
            """
            SELECT
                id,
                source,
                path,
                filename,
                title,
                artist,
                album,
                track_number,
                duration,
                file_size
            FROM tracks
            ORDER BY
                COALESCE(artist, ''),
                COALESCE(album, ''),
                COALESCE(title, filename)
            LIMIT 1000
            """
        ).fetchall()

        counts = conn.execute(
            """
            SELECT
                source,
                COUNT(*) AS count
            FROM tracks
            GROUP BY source
            """
        ).fetchall()

    finally:
        conn.close()

    count_map = {
        row["source"]: row["count"]
        for row in counts
    }

    return jsonify({
        "status": "ok",
        "count": sum(count_map.values()),
        "nas_count": count_map.get("nas", 0),
        "cache_count": count_map.get("cache", 0),
        "tracks": [dict(row) for row in tracks],
    })


@app.route("/api/library/tree")
def library_tree():
    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                id,
                source,
                path,
                filename,
                title,
                artist,
                album,
                track_number,
                duration
            FROM tracks
            ORDER BY
                COALESCE(NULLIF(TRIM(artist), ''), path),
                COALESCE(NULLIF(TRIM(album), ''), path),
                CASE
                    WHEN track_number GLOB '[0-9]*'
                    THEN CAST(track_number AS INTEGER)
                    ELSE 999999
                END,
                COALESCE(title, filename)
            """
        ).fetchall()
    finally:
        conn.close()

    artists = {}

    for row in rows:
        track = dict(row)
        path = Path(track["path"])
        parts = path.parts

        artist = (track["artist"] or "").strip()
        album = (track["album"] or "").strip()

        if not artist:
            if len(parts) >= 3:
                artist = parts[-3]
            elif len(parts) >= 2:
                artist = parts[-2]
            else:
                artist = "Ohne Artist"

        if not album:
            if len(parts) >= 2:
                album = parts[-2]

                if album == artist and len(parts) >= 3:
                    album = parts[-2]
            else:
                album = "Ohne Album"

        if not album:
            album = "Ohne Album"

        artist_entry = artists.setdefault(
            artist,
            {
                "name": artist,
                "albums": {},
                "track_count": 0,
            }
        )

        album_entry = artist_entry["albums"].setdefault(
            album,
            {
                "name": album,
                "tracks": [],
            }
        )

        album_entry["tracks"].append(track)
        artist_entry["track_count"] += 1

    tree = []

    for artist_name in sorted(
        artists,
        key=lambda value: value.casefold()
    ):
        artist_entry = artists[artist_name]

        albums = [
            artist_entry["albums"][name]
            for name in sorted(
                artist_entry["albums"],
                key=lambda value: value.casefold()
            )
        ]

        tree.append({
            "name": artist_entry["name"],
            "track_count": artist_entry["track_count"],
            "albums": albums,
        })

    album_count = sum(
        len(artist["albums"])
        for artist in tree
    )

    return jsonify({
        "status": "ok",
        "count": len(rows),
        "artist_count": len(tree),
        "album_count": album_count,
        "tree": tree,
    })


@app.route("/api/library/filetree")
def library_filetree():
    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT
                id,
                source,
                path,
                filename,
                title,
                artist,
                album,
                duration
            FROM tracks
            ORDER BY
                source,
                path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    root = {
        "type": "folder",
        "name": "Library",
        "children": [],
        "track_count": 0,
    }

    source_nodes = {}

    for row in rows:
        track = dict(row)
        source = track["source"]

        if source not in source_nodes:
            source_name = (
                "NAS"
                if source == "nas"
                else "CACHE"
            )

            source_node = {
                "type": "folder",
                "name": source_name,
                "children": [],
                "track_count": 0,
            }

            source_nodes[source] = source_node
            root["children"].append(source_node)

        source_node = source_nodes[source]
        source_node["track_count"] += 1
        root["track_count"] += 1

        parts = list(Path(track["path"]).parts)

        if not parts:
            continue

        directories = parts[:-1]
        current = source_node

        for directory in directories:
            folder = None

            for child in current["children"]:
                if (
                    child.get("type") == "folder"
                    and child.get("name") == directory
                ):
                    folder = child
                    break

            if folder is None:
                folder = {
                    "type": "folder",
                    "name": directory,
                    "children": [],
                    "track_count": 0,
                }

                current["children"].append(folder)

            folder["track_count"] += 1
            current = folder

        current["children"].append({
            "type": "track",
            "id": track["id"],
            "source": track["source"],
            "path": track["path"],
            "filename": track["filename"],
            "title": track["title"],
            "artist": track["artist"],
            "album": track["album"],
            "duration": track["duration"],
        })

    def sort_node(node):
        if node.get("type") != "folder":
            return

        node["children"].sort(
            key=lambda child: (
                0 if child.get("type") == "folder" else 1,
                (child.get("name") or child.get("filename") or "").casefold()
            )
        )

        for child in node["children"]:
            sort_node(child)

    sort_node(root)

    return jsonify({
        "status": "ok",
        "count": len(rows),
        "tree": root,
    })


@app.route("/api/library/search")
def library_search():
    search = request.args.get("q", "").strip()

    if not search:
        return library()

    tokens = [
        token
        for token in search.split()
        if token
    ]

    where_parts = []
    params = []

    for token in tokens:
        like = f"%{token}%"

        where_parts.append(
            """
            (
                title LIKE ? COLLATE NOCASE
                OR artist LIKE ? COLLATE NOCASE
                OR album LIKE ? COLLATE NOCASE
                OR filename LIKE ? COLLATE NOCASE
                OR path LIKE ? COLLATE NOCASE
            )
            """
        )

        params.extend([
            like,
            like,
            like,
            like,
            like,
        ])

    where_sql = " AND ".join(where_parts)
    conn = get_db()

    try:
        tracks = conn.execute(
            f"""
            SELECT
                id,
                source,
                path,
                filename,
                title,
                artist,
                album,
                track_number,
                duration,
                file_size
            FROM tracks
            WHERE {where_sql}
            ORDER BY
                COALESCE(artist, ''),
                COALESCE(album, ''),
                COALESCE(title, filename)
            LIMIT 300
            """,
            params,
        ).fetchall()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "query": search,
        "count": len(tracks),
        "tracks": [dict(row) for row in tracks],
    })


@app.route("/api/library/scan", methods=["POST"])
def library_scan():
    if not library_scan_lock.acquire(blocking=False):
        return jsonify({
            "status": "busy",
            "message": "Library-Scan läuft bereits",
        }), 409

    try:
        results = scan_library()

        conn = get_db()

        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM tracks"
            ).fetchone()[0]
        finally:
            conn.close()

        return jsonify({
            "status": "ok",
            "message": "Library aktualisiert",
            "total": total,
            "sources": results,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500

    finally:
        library_scan_lock.release()


@app.route("/api/volume", methods=["POST"])
def set_volume():
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")
    volume = data.get("volume")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    try:
        volume = int(volume)
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "message": "Ungültige Lautstärke",
        }), 400

    volume = max(0, min(100, volume))

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    try:
        device.volume = volume

        return jsonify({
            "status": "ok",
            "device": device.player_name,
            "volume": device.volume,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/group/volume", methods=["POST"])
def set_group_volume():
    data = request.get_json(silent=True) or {}

    ips = data.get("ips") or []
    volume = data.get("volume")

    if not ips:
        return jsonify({
            "status": "error",
            "message": "Keine Player ausgewählt",
        }), 400

    try:
        volume = int(volume)
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "message": "Ungültige Lautstärke",
        }), 400

    volume = max(0, min(100, volume))

    devices = {
        device.ip_address: device
        for device in discover_sonos()
    }

    changed = []
    missing = []

    for ip in ips:
        device = devices.get(ip)

        if not device:
            missing.append(ip)
            continue

        device.volume = volume

        changed.append({
            "ip": ip,
            "name": device.player_name,
            "volume": device.volume,
        })

    return jsonify({
        "status": "ok",
        "volume": volume,
        "changed": changed,
        "missing": missing,
    })


@app.route("/api/group", methods=["POST"])
def create_group():
    data = request.get_json(silent=True) or {}

    ips = data.get("ips") or []

    if len(ips) < 2:
        return jsonify({
            "status": "error",
            "message": "Mindestens zwei Player erforderlich",
        }), 400

    devices = {
        device.ip_address: device
        for device in discover_sonos()
    }

    selected = [
        devices[ip]
        for ip in ips
        if ip in devices
    ]

    if len(selected) < 2:
        return jsonify({
            "status": "error",
            "message": "Zu wenige ausgewählte Player gefunden",
        }), 404

    coordinator = selected[0]

    try:
        for device in selected[1:]:
            if device.ip_address == coordinator.ip_address:
                continue

            device.join(coordinator)

        return jsonify({
            "status": "ok",
            "message": "Gruppe gebildet",
            "coordinator": coordinator.player_name,
            "members": [
                device.player_name
                for device in selected
            ],
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/ungroup", methods=["POST"])
def ungroup():
    data = request.get_json(silent=True) or {}

    ips = data.get("ips") or []

    if not ips:
        return jsonify({
            "status": "error",
            "message": "Keine Player ausgewählt",
        }), 400

    devices = {
        device.ip_address: device
        for device in discover_sonos()
    }

    changed = []
    missing = []

    try:
        for ip in ips:
            device = devices.get(ip)

            if not device:
                missing.append(ip)
                continue

            device.unjoin()
            changed.append(device.player_name)

        return jsonify({
            "status": "ok",
            "message": "Player aus Gruppe gelöst",
            "changed": changed,
            "missing": missing,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/playlists", methods=["GET", "POST"])
def playlists():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()

        if not name:
            return jsonify({
                "status": "error",
                "message": "Playlist-Name fehlt",
            }), 400

        conn = get_db()

        try:
            created_at = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
                (name, created_at),
            )
            conn.commit()

            playlist = {
                "id": cursor.lastrowid,
                "name": name,
                "created_at": created_at,
            }

        except sqlite3.IntegrityError:
            return jsonify({
                "status": "error",
                "message": "Playlist-Name existiert bereits",
            }), 409

        finally:
            conn.close()

        return jsonify({
            "status": "ok",
            "playlist": playlist,
        })

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT
                p.id,
                p.name,
                p.created_at,
                COUNT(pt.id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt
                ON pt.playlist_id = p.id
            GROUP BY p.id, p.name, p.created_at
            ORDER BY p.name COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "playlists": [dict(row) for row in rows],
    })


@app.route("/api/playlists/<int:playlist_id>", methods=["GET", "DELETE"])
def playlist_detail(playlist_id):
    conn = get_db()

    try:
        playlist = conn.execute(
            "SELECT id, name, created_at FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()

        if not playlist:
            return jsonify({
                "status": "error",
                "message": "Playlist nicht gefunden",
            }), 404

        if request.method == "DELETE":
            conn.execute(
                "DELETE FROM playlists WHERE id = ?",
                (playlist_id,),
            )
            conn.commit()

            return jsonify({
                "status": "ok",
                "message": "Playlist gelöscht",
            })

        tracks = conn.execute(
            """
            SELECT
                pt.id AS item_id,
                pt.position,
                t.id AS track_id,
                t.source,
                t.path,
                t.filename,
                t.title,
                t.artist,
                t.album,
                t.duration
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position, pt.id
            """,
            (playlist_id,),
        ).fetchall()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "playlist": dict(playlist),
        "tracks": [dict(row) for row in tracks],
    })


@app.route("/api/playlists/<int:playlist_id>/tracks", methods=["POST"])
def playlist_add_track(playlist_id):
    data = request.get_json(silent=True) or {}
    track_id = data.get("track_id")

    if not track_id:
        return jsonify({
            "status": "error",
            "message": "track_id fehlt",
        }), 400

    conn = get_db()

    try:
        if not conn.execute(
            "SELECT id FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone():
            return jsonify({
                "status": "error",
                "message": "Playlist nicht gefunden",
            }), 404

        if not conn.execute(
            "SELECT id FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone():
            return jsonify({
                "status": "error",
                "message": "Track nicht gefunden",
            }), 404

        next_position = conn.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 1
            FROM playlist_tracks
            WHERE playlist_id = ?
            """,
            (playlist_id,),
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO playlist_tracks (playlist_id, track_id, position)
            VALUES (?, ?, ?)
            """,
            (playlist_id, track_id, next_position),
        )
        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "message": "Titel zur Playlist hinzugefügt",
    })


@app.route(
    "/api/playlists/<int:playlist_id>/tracks/<int:item_id>",
    methods=["DELETE"]
)
def playlist_remove_track(playlist_id, item_id):
    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT id
            FROM playlist_tracks
            WHERE id = ? AND playlist_id = ?
            """,
            (item_id, playlist_id),
        ).fetchone()

        if not row:
            return jsonify({
                "status": "error",
                "message": "Playlist-Eintrag nicht gefunden",
            }), 404

        conn.execute(
            "DELETE FROM playlist_tracks WHERE id = ?",
            (item_id,),
        )

        remaining = conn.execute(
            """
            SELECT id
            FROM playlist_tracks
            WHERE playlist_id = ?
            ORDER BY position, id
            """,
            (playlist_id,),
        ).fetchall()

        for position, row in enumerate(remaining, start=1):
            conn.execute(
                "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                (position, row["id"]),
            )

        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "message": "Titel aus Playlist entfernt",
    })


@app.route(
    "/api/playlists/<int:playlist_id>/tracks/bulk",
    methods=["POST"]
)
def playlist_add_tracks_bulk(playlist_id):
    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids") or []

    try:
        track_ids = [int(track_id) for track_id in track_ids]
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "message": "Ungültige track_ids",
        }), 400

    if not track_ids:
        return jsonify({
            "status": "error",
            "message": "Keine Tracks übergeben",
        }), 400

    conn = get_db()

    try:
        if not conn.execute(
            "SELECT id FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone():
            return jsonify({
                "status": "error",
                "message": "Playlist nicht gefunden",
            }), 404

        next_position = conn.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 1
            FROM playlist_tracks
            WHERE playlist_id = ?
            """,
            (playlist_id,),
        ).fetchone()[0]

        added = 0
        missing = []

        for track_id in track_ids:
            if not conn.execute(
                "SELECT id FROM tracks WHERE id = ?",
                (track_id,),
            ).fetchone():
                missing.append(track_id)
                continue

            conn.execute(
                """
                INSERT INTO playlist_tracks (
                    playlist_id,
                    track_id,
                    position
                )
                VALUES (?, ?, ?)
                """,
                (playlist_id, track_id, next_position),
            )

            next_position += 1
            added += 1

        conn.commit()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "message": "Titel zur Playlist hinzugefügt",
        "added": added,
        "missing": missing,
    })


@app.route(
    "/api/playlists/<int:playlist_id>/tracks/<int:item_id>/move",
    methods=["POST"]
)
def playlist_move_track(playlist_id, item_id):
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")

    if direction not in ("up", "down"):
        return jsonify({
            "status": "error",
            "message": "direction muss up oder down sein",
        }), 400

    conn = get_db()

    try:
        current = conn.execute(
            """
            SELECT id, position
            FROM playlist_tracks
            WHERE id = ?
              AND playlist_id = ?
            """,
            (item_id, playlist_id),
        ).fetchone()

        if not current:
            return jsonify({
                "status": "error",
                "message": "Playlist-Eintrag nicht gefunden",
            }), 404

        if direction == "up":
            other = conn.execute(
                """
                SELECT id, position
                FROM playlist_tracks
                WHERE playlist_id = ?
                  AND position < ?
                ORDER BY position DESC
                LIMIT 1
                """,
                (playlist_id, current["position"]),
            ).fetchone()
        else:
            other = conn.execute(
                """
                SELECT id, position
                FROM playlist_tracks
                WHERE playlist_id = ?
                  AND position > ?
                ORDER BY position ASC
                LIMIT 1
                """,
                (playlist_id, current["position"]),
            ).fetchone()

        if other:
            conn.execute(
                "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                (other["position"], current["id"]),
            )

            conn.execute(
                "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                (current["position"], other["id"]),
            )

            conn.commit()

    finally:
        conn.close()

    return jsonify({
        "status": "ok",
        "message": "Reihenfolge aktualisiert",
    })


@app.route("/api/playlists/<int:playlist_id>/play", methods=["POST"])
def playlist_play(playlist_id):
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    conn = get_db()

    try:
        tracks = conn.execute(
            """
            SELECT
                t.source,
                t.path
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position, pt.id
            """,
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()

    if not tracks:
        return jsonify({
            "status": "error",
            "message": "Playlist ist leer",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    try:
        coordinator = get_coordinator(device)

        coordinator.clear_queue()

        added = 0
        skipped = []

        for track in tracks:
            try:
                path, media_prefix = resolve_media_path(
                    track["source"],
                    track["path"]
                )

                if not path.is_file():
                    skipped.append(track["path"])
                    continue

                media_url = (
                    get_server_url()
                    + media_prefix
                    + quote(track["path"], safe="/")
                )

                coordinator.add_uri_to_queue(media_url)
                added += 1

            except Exception:
                skipped.append(track["path"])

        if added == 0:
            return jsonify({
                "status": "error",
                "message": "Kein Playlist-Titel konnte zur Sonos-Queue hinzugefügt werden",
                "skipped": skipped,
            }), 500

        coordinator.play_from_queue(0)

        return jsonify({
            "status": "ok",
            "message": "Playlist gestartet",
            "device": coordinator.player_name,
            "queued": added,
            "skipped": skipped,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/youtube/search")
def youtube_search():
    if not YOUTUBE_API_KEY:
        return jsonify({
            "status": "error",
            "message": "YOUTUBE_API_KEY ist nicht gesetzt",
        }), 500

    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({
            "status": "error",
            "message": "Suchbegriff fehlt",
        }), 400

    try:
        search_response = requests.get(
            YOUTUBE_SEARCH_URL,
            params={
                "key": YOUTUBE_API_KEY,
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": 10,
                "regionCode": "AT",
                "safeSearch": "moderate",
            },
            timeout=15,
        )

        search_response.raise_for_status()
        search_data = search_response.json()

        raw_items = search_data.get("items", [])

        video_ids = [
            item.get("id", {}).get("videoId")
            for item in raw_items
            if item.get("id", {}).get("videoId")
        ]

        durations = {}

        if video_ids:
            videos_response = requests.get(
                YOUTUBE_VIDEOS_URL,
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "contentDetails",
                    "id": ",".join(video_ids),
                },
                timeout=15,
            )

            videos_response.raise_for_status()
            videos_data = videos_response.json()

            for item in videos_data.get("items", []):
                video_id = item.get("id")
                duration_iso = (
                    item.get("contentDetails", {})
                    .get("duration")
                )

                durations[video_id] = parse_youtube_duration(
                    duration_iso
                )

        results = []

        for item in raw_items:
            video_id = (
                item.get("id", {})
                .get("videoId")
            )

            if not video_id:
                continue

            snippet = item.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})

            thumbnail = (
                thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
                or thumbnails.get("high", {}).get("url")
            )

            duration = durations.get(video_id)

            duration_text = None

            if duration is not None:
                hours, remainder = divmod(duration, 3600)
                minutes, seconds = divmod(remainder, 60)

                if hours:
                    duration_text = (
                        f"{hours}:{minutes:02d}:{seconds:02d}"
                    )
                else:
                    duration_text = (
                        f"{minutes}:{seconds:02d}"
                    )

            results.append({
                "video_id": video_id,
                "title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "published_at": snippet.get("publishedAt"),
                "thumbnail": thumbnail,
                "duration": duration,
                "duration_text": duration_text,
                "youtube_url": (
                    "https://www.youtube.com/watch?v="
                    + video_id
                ),
            })

        return jsonify({
            "status": "ok",
            "query": query,
            "count": len(results),
            "results": results,
        })

    except requests.HTTPError as exc:
        response = exc.response
        detail = None

        try:
            detail = response.json()
        except Exception:
            detail = response.text

        return jsonify({
            "status": "error",
            "message": "YouTube API HTTP-Fehler",
            "http_status": response.status_code,
            "detail": detail,
        }), 502

    except requests.RequestException as exc:
        return jsonify({
            "status": "error",
            "message": f"YouTube API nicht erreichbar: {exc}",
        }), 502

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/media/cache/<path:filename>")
def media_cache(filename):
    return send_from_directory(
        CACHE_DIR,
        filename,
        conditional=True
    )


@app.route("/media/library/<path:filename>")
def media_library(filename):
    return send_from_directory(
        LIBRARY_DIR,
        filename,
        conditional=True
    )


@app.route("/api/play/tracks", methods=["POST"])
def play_tracks():
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")
    track_ids = data.get("track_ids") or []

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    try:
        track_ids = [int(track_id) for track_id in track_ids]
    except (TypeError, ValueError):
        return jsonify({
            "status": "error",
            "message": "Ungültige track_ids",
        }), 400

    if not track_ids:
        return jsonify({
            "status": "error",
            "message": "Keine Tracks übergeben",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    coordinator = get_coordinator(device)

    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in track_ids)
        rows = conn.execute(
            f"""
            SELECT id, source, path, title, artist
            FROM tracks
            WHERE id IN ({placeholders})
            """,
            track_ids,
        ).fetchall()
    finally:
        conn.close()

    by_id = {
        row["id"]: row
        for row in rows
    }

    ordered_tracks = [
        by_id[track_id]
        for track_id in track_ids
        if track_id in by_id
    ]

    try:
        coordinator.clear_queue()

        added = 0
        skipped = []

        for track in ordered_tracks:
            try:
                path, media_prefix = resolve_media_path(
                    track["source"],
                    track["path"]
                )

                if not path.is_file():
                    skipped.append({
                        "path": track["path"],
                        "error": "Datei nicht gefunden",
                    })
                    continue

                media_url = (
                    get_server_url()
                    + media_prefix
                    + quote(track["path"], safe="/")
                )

                queue_position = coordinator.add_uri_to_queue(
                    media_url
                )

                added += 1

            except Exception as exc:
                skipped.append({
                    "path": track["path"],
                    "error": str(exc),
                })

        try:
            queue_size = coordinator.queue_size
        except Exception:
            queue_size = None

        if added == 0:
            return jsonify({
                "status": "error",
                "message": "Kein Titel konnte zur Sonos-Queue hinzugefügt werden",
                "requested": len(track_ids),
                "added": added,
                "queue_size": queue_size,
                "skipped": skipped,
            }), 500

        # Sicherstellen, dass weder Shuffle noch Repeat aus einer
        # vorherigen Wiedergabe den Ordnerlauf beeinflussen.
        try:
            coordinator.play_mode = "NORMAL"
        except Exception:
            pass

        coordinator.play_from_queue(0)

        return jsonify({
            "status": "ok",
            "message": "Ordner gestartet",
            "device": coordinator.player_name,
            "requested": len(track_ids),
            "added": added,
            "queue_size": queue_size,
            "skipped": skipped,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/play/local", methods=["POST"])
def play_local():
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")
    track_id = data.get("track_id")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    if not track_id:
        return jsonify({
            "status": "error",
            "message": "track_id fehlt",
        }), 400

    conn = get_db()

    try:
        track = conn.execute(
            "SELECT * FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
    finally:
        conn.close()

    if not track:
        return jsonify({
            "status": "error",
            "message": "Track nicht gefunden",
        }), 404

    try:
        path, media_prefix = resolve_media_path(
            track["source"],
            track["path"]
        )

    except ValueError as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 400

    if not path.is_file():
        return jsonify({
            "status": "error",
            "message": "Datei nicht vorhanden. Library neu scannen.",
        }), 404

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    media_url = (
        get_server_url()
        + media_prefix
        + quote(track["path"], safe="/")
    )

    try:
        device.play_uri(media_url)

        return jsonify({
            "status": "ok",
            "source": track["source"],
            "device": device.player_name,
            "track_id": track["id"],
            "artist": track["artist"],
            "title": track["title"],
            "album": track["album"],
            "path": track["path"],
            "url": media_url,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/play", methods=["POST"])
def play():
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")
    url = data.get("url")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    if not url:
        return jsonify({
            "status": "error",
            "message": "url fehlt",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    try:
        device.play_uri(url)

        return jsonify({
            "status": "ok",
            "source": "url",
            "device": device.player_name,
            "url": url,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/playback")
def playback_status():
    ip = request.args.get("ip")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    coordinator = get_coordinator(device)

    try:
        track = coordinator.get_current_track_info() or {}
        transport = coordinator.get_current_transport_info() or {}

        queue_size = None
        queue_position = None

        try:
            queue_size = coordinator.queue_size
        except Exception:
            pass

        try:
            pos = track.get("playlist_position")
            if pos:
                queue_position = int(pos)
        except Exception:
            pass

        return jsonify({
            "status": "ok",
            "device": coordinator.player_name,
            "transport_state": transport.get(
                "current_transport_state"
            ),
            "play_mode": coordinator.play_mode,
            "queue_size": queue_size,
            "queue_position": queue_position,
            "track": {
                "title": track.get("title"),
                "artist": track.get("artist"),
                "album": track.get("album"),
                "duration": track.get("duration"),
                "position": track.get("position"),
                "uri": track.get("uri"),
            },
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/shuffle", methods=["POST"])
def shuffle():
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")
    enabled = bool(data.get("enabled"))

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    coordinator = get_coordinator(device)

    try:
        coordinator.play_mode = (
            "SHUFFLE_NOREPEAT"
            if enabled
            else "NORMAL"
        )

        return jsonify({
            "status": "ok",
            "device": coordinator.player_name,
            "shuffle": enabled,
            "play_mode": coordinator.play_mode,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


@app.route("/api/previous", methods=["POST"])
def previous():
    return player_command("previous")


@app.route("/api/next", methods=["POST"])
def next_track():
    return player_command("next")


@app.route("/api/pause", methods=["POST"])
def pause():
    return player_command("pause")


@app.route("/api/resume", methods=["POST"])
def resume():
    return player_command("play")


@app.route("/api/stop", methods=["POST"])
def stop():
    return player_command("stop")


def player_command(command):
    data = request.get_json(silent=True) or {}

    ip = data.get("ip")

    if not ip:
        return jsonify({
            "status": "error",
            "message": "ip fehlt",
        }), 400

    device = find_sonos_by_ip(ip)

    if not device:
        return jsonify({
            "status": "error",
            "message": f"Sonos {ip} nicht gefunden",
        }), 404

    coordinator = get_coordinator(device)

    try:
        if command == "play":
            coordinator.play()
        elif command == "pause":
            coordinator.pause()
        elif command == "stop":
            coordinator.stop()
        elif command == "next":
            coordinator.next()
        elif command == "previous":
            coordinator.previous()
        else:
            raise ValueError("Unbekanntes Kommando")

        return jsonify({
            "status": "ok",
            "action": command,
            "device": coordinator.player_name,
        })

    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
        }), 500


if __name__ == "__main__":
    # Nur für einen manuellen Direktstart.
    # Im produktiven Betrieb wird die App über Gunicorn gestartet.
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False,
    )

