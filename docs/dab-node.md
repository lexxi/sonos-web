# DAB+ Receiver Node

Stand: November 2026

## Ziel

Ein separater, headless DAB+-Empfänger soll über das Netzwerk Audio bereitstellen, das später von Sonos Web ausgewählt und an Sonos-Player weitergegeben werden kann.

Geplante Architektur:

```text
DAB+ Antenne
   ↓
Raspberry Pi 2 Model B Rev 1.2
   ↓
Nooelec NESDR SMArt v5 / RTL-SDR
   ↓
welle-cli
   ↓
HTTP Audio Stream
   ↓
Sonos Web
   ↓
Sonos
```

## Hardware

Aktueller DAB-Node:

- Raspberry Pi 2 Model B Rev 1.2
- BCM2837, 4 × Cortex-A53, 900 MHz
- Nooelec NESDR SMArt v5
- RTL2832U
- Rafael Micro R820T tuner

Der zuvor getestete Wyse C90LE7 wurde als Plattform verworfen. Der Raspberry Pi ist für den dauerhaften Headless-Betrieb einfacher zu warten und besser reproduzierbar.

## Betriebssystem

Der Pi läuft headless mit systemd.

Aktiver Boot-Target:

```bash
systemctl get-default
```

Erwartet:

```text
multi-user.target
```

SSH ist dauerhaft aktiviert:

```bash
systemctl enable --now ssh
```

Nicht benötigte Desktop-Dienste wurden entfernt bzw. deaktiviert. Der eigentliche X-Server ist entfernt; verbliebene GUI-Bibliotheken werden erst nach erfolgreichem DAB-Test weiter bereinigt.

## Build-Umgebung

Build grundsätzlich als Benutzer `dab`, Paketinstallation und systemweite Installation mit Root-Rechten.

Basiswerkzeuge:

```bash
apt update
apt install \
  build-essential \
  cmake \
  git \
  pkg-config
```

Zusätzliche Abhängigkeiten für welle.io / welle-cli:

```bash
apt install \
  libfftw3-dev \
  libfaad-dev \
  libmpg123-dev \
  libsamplerate0-dev \
  librtlsdr-dev \
  libmp3lame-dev \
  libasound2-dev \
  xxd
```

## welle.io bauen

Als Benutzer `dab`:

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/AlbrechtL/welle.io.git
cd welle.io

mkdir build
cd build

cmake -S .. -B . \
  -DBUILD_WELLE_IO=OFF \
  -DBUILD_WELLE_CLI=ON \
  -DRTLSDR=1

make -j1
```

Auf dem Raspberry Pi 2 wird bewusst `-j1` verwendet, um Speicher- und Lastspitzen zu vermeiden.

## ARMHF / mpg123 Linker-Fix

Auf dem aktuellen Raspberry-Pi-OS-/Debian-ARMHF-Stand wurde `libmpg123 1.32.10` verwendet.

Der Build scheiterte beim Linken mit:

```text
undefined reference to 'mpg123_framebyframe_decode'
```

Die installierte ARMHF-Library exportiert stattdessen u. a.:

```text
mpg123_framebyframe_decode_64
mpg123_framebyframe_decode64
mpg123_framebyframe_next
```

In:

```text
src/backend/dab_decoder.cpp
```

wurde daher:

```cpp
mpg_result = mpg123_framebyframe_decode(handle, nullptr, data, &frame_len);
```

durch:

```cpp
mpg_result = mpg123_framebyframe_decode64(handle, nullptr, data, &frame_len);
```

ersetzt.

Da der zweite Parameter hier `nullptr` ist, entsteht dabei kein `off_t*`-/`int64_t*`-Typkonflikt.

Danach:

```bash
cd ~/src/welle.io/build
make -j1
```

Der Build lief anschließend erfolgreich durch.

Aktuell getestete Version:

```text
welle-cli v2.7-0-g512558d1
```

## Lokalen Patch in Git sichern

Der Patch gehört zum lokalen welle.io-Checkout und sollte als eigener lokaler Commit erhalten bleiben:

```bash
cd ~/src/welle.io

git diff -- src/backend/dab_decoder.cpp
git add src/backend/dab_decoder.cpp
git commit -m "Fix mpg123 frame-by-frame decode on ARMHF"
```

Vor einem späteren Update:

```bash
git status
git log --oneline --decorate -5
git pull --rebase
```

Falls Upstream die Stelle inzwischen selbst korrigiert hat, muss der lokale Commit entsprechend geprüft bzw. verworfen werden.

## Installation

Nach erfolgreichem Build systemweit installieren:

```bash
sudo make install
```

Prüfen:

```bash
/usr/local/bin/welle-cli -v
```

## RTL-SDR Test

Stick prüfen:

```bash
rtl_test -t
```

Bekannter Stick:

```text
Nooelec, NESDR SMArt v5
Found Rafael Micro R820T tuner
```

Die Meldung:

```text
[R82XX] PLL not locked!
```

wird weiter beobachtet. Im Keller war die Empfangslage schlecht; ein endgültiger Empfangstest muss am vorgesehenen Standort mit guter Antenne erfolgen.

## DAB+-Test

Für Kärnten / Dobratsch wurden folgende Blöcke als relevante Testkandidaten verwendet:

- 6A
- 6B
- 6D

Beispiel:

```bash
welle-cli -F rtl_sdr -T -c 6A -g -1
```

Danach entsprechend:

```bash
welle-cli -F rtl_sdr -T -c 6B -g -1
welle-cli -F rtl_sdr -T -c 6D -g -1
```

Bei schlechtem Kellerempfang trat bisher u. a. auf:

```text
Wait for sync
Lost coarse sync
SyncOnPhase failed
```

Das allein wird derzeit nicht als SDR-Defekt gewertet.

## Nächste Schritte

- stabilen Multiplex-Lock am endgültigen Antennenstandort bestätigen
- Senderliste / Ensemble-Informationen auslesen
- einen einzelnen DAB+-Sender dekodieren
- CPU- und RAM-Last auf dem Raspberry Pi 2 messen
- geeigneten Audio-Ausgabepfad festlegen
- HTTP-Audio-Stream bereitstellen
- Stream direkt mit Sonos testen
- kleine API für Senderliste und Tuning bauen
- Sonos Web um DAB+-Auswahl erweitern
- welle-cli / Streaming-Komponenten als systemd-Service betreiben
