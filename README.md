# iCloud Shared Photos for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

This custom integration adds the **iCloud Shared Photo Library** (*Geteilte Mediathek*), your **Shared Albums** (*Geteilte Alben*) and your **favorites** to the Home Assistant media browser. It uses the existing, already authenticated **Apple iCloud** core integration. You don't enter a second Apple ID and don't go through a second 2FA login.

> The rest of this README is in German.

```
Medien
└── iCloud Shared Photos
    ├── Shared Library          (SharedSync-<UUID>)
    │   ├── Library
    │   └── Favorites           ← ❤️ in der geteilten Mediathek
    ├── Personal Library        (PrimarySync)
    │   ├── Library
    │   └── Favorites
    ├── Shared Albums           (geteilte Alben: Photo Streams + CloudKit)
    │   └── <Albumname> …
    └── iCloud Favorites
        ├── Personal Favorites
        ├── Shared Favorites
        └── All Favorites       (persönlich + geteilt, ohne Duplikate)
```

Bei mehreren iCloud-Konten erscheint oberhalb dieser Struktur zuerst eine Ebene pro Konto. Existieren mehrere Shared Libraries, heißen sie „Shared Library 1“, „Shared Library 2“ usw.

---

## 1. Analyse des bestehenden Codes (Stand HA 2026.9.4 / pyicloud 2.6.5 und 2.7.0)

### Home-Assistant-Core `icloud`

* `homeassistant/components/icloud/__init__.py` legt pro Config Entry ein `IcloudAccount` an und speichert es in **`entry.runtime_data`**.
* `IcloudAccount.api` ist die authentifizierte **`PyiCloudService`**-Instanz (bzw. `None`, solange die Anmeldung fehlschlägt). Die Session-Cookies liegen in `.storage/icloud`.
* `media_source.py` (seit 2026.x im Core) bietet nur zwei Einstiegspunkte an:
  * `api.photos.albums` → die Alben der **persönlichen** Mediathek (`PrimarySync`)
  * `api.photos.shared_streams` → klassische **geteilte Alben** (Photo Streams)

  Die Shared Photo Library taucht dort nicht auf. `Albums → Favorites` ist deshalb nur die Favoritenliste der persönlichen Mediathek.
* Bilder werden über eine eigene `HomeAssistantView` (`requires_auth = True`) chunkweise von der signierten CloudKit-Download-URL an den Client **gestreamt**, nichts wird auf die Platte geschrieben. Diese Integration macht es genauso.
* HA 2026.9 pinnt **`pyicloud==2.6.5`**.

### pyicloud (2.6.5, identisch in 2.7.0)

pyicloud hat inzwischen einen CloudKit-basierten Photos-Service mit **nativer Shared-Library-Unterstützung**:

* `api.photos.libraries` fragt `zones/list` in der **privaten** und in der **geteilten** CloudKit-Datenbank (`com.apple.photos.cloud/production/{private,shared}`) ab und liefert:
  * `"root"` → `PrimarySync` (persönlich)
  * `"shared"` → Legacy Shared Albums (Photo Streams)
  * `"shared:SharedSync-<UUID>"` → **Shared Photo Library**, als `PhotoLibrary` mit `scope == "shared-library"`. Beim Besitzer liegt die Zone in der privaten, bei Teilnehmern in der geteilten Datenbank. pyicloud behandelt beide Fälle.
* Die Erkennung erfolgt über das Namenspräfix `SharedSync-` (`SHARED_LIBRARY_ZONE_PREFIX`).
* Für Shared Libraries stellt pyicloud die Smart Albums **`Library`** und **`Favorites`** bereit (`SUPPORTED_SHARED_LIBRARY_SMART_ALBUMS`).
* `Favorites` ist eine Abfrage `CPLAssetAndMasterInSmartAlbumByAssetDate` mit dem Filter `smartAlbum = FAVORITE` **in der Zone `SharedSync-<UUID>`**. Das ist genau das gesuchte Smart Album.
* `PhotoAsset.resources` / `download_url()` liefern die signierten Download-URLs der Derivate (`original`, `medium` = JPEG, `thumb` = JPEG).

**Fazit:** Eigene CloudKit-Requests oder Monkey Patches sind **nicht nötig**. Diese Integration nutzt ausschließlich öffentliche pyicloud-Objekte (`photos.libraries`, `library.albums`, `album.photos`, `album.get()`, `asset.resources`).

---

## 2. Architekturentscheidung

| Frage | Entscheidung |
|---|---|
| Authentifizierung | **Wiederverwendung** von `entry.runtime_data.api` der geladenen `icloud`-Config-Entries. Keine Zugangsdaten, kein zweites 2FA. |
| Eigene iCloud-Logik | Keine. Zonenerkennung, Queries und Paging übernimmt pyicloud. Die Integration wählt nur die `SharedSync-*`-Zonen aus, cached und führt Favoriten zusammen. |
| pyicloud-Version | Keine eigene Anforderung (`"requirements": []`). Es wird die vom Core installierte Version verwendet, also keine Versionskonflikte. |
| Config Flow | Minimal (ein Klick, `single_config_entry`) plus Optionen. Er ist nötig, damit HA die Media-Source-Plattform ohne YAML lädt. |
| Media Source | `media_source.py` → `IcloudSharedPhotosMediaSource` |
| Bildauslieferung | `GET /api/icloud_shared_photos/serve/{full|thumb}/{token}` (Auth erforderlich, von HA signierbar). Die Daten werden gestreamt, `Range`-Header werden durchgereicht. |
| Caching | Nur im RAM: Albumlisten (TTL einstellbar, Standard 5 min) und Asset-Metadaten inkl. Download-URLs (max. 30 min, LRU 2000). **Keine Bilddateien auf Disk.** |
| Abgelaufene URLs | Antwortet das CDN mit 401/403/404/410, wird das Asset einmal neu von iCloud geladen und der Download wiederholt. |
| HEIC | Option „Automatisch“: JPEG/PNG-Originale werden direkt ausgeliefert, bei HEIC/RAW kommt Apples JPEG-Derivat (`resJPEGMed`). So sind die Bilder in Browsern und auf Fotorahmen darstellbar. |

**Alternativen, falls der Zugriff auf `runtime_data` des Core künftig nicht mehr funktioniert:**
(a) ein Upstream-PR an HA-Core, der `photos.libraries` im Core-Media-Browser anbietet (die sauberste Langzeitlösung);
(b) ein eigener Config Flow mit eigener `PyiCloudService`-Anmeldung. Dann fällt aber ein zweiter 2FA-Login an und es gibt eine zweite Session. Genau das sollte hier vermieden werden.

---

## 3. Repository-Struktur

```
.
├── custom_components/icloud_shared_photos/
│   ├── __init__.py          Setup, Runtime-Daten (RAM-Caches)
│   ├── manifest.json
│   ├── config_flow.py       1-Klick-Setup + Optionen
│   ├── const.py
│   ├── identifier.py        Media-Source-IDs (ohne HA-Abhängigkeit)
│   ├── library.py           pyicloud-Zugriffsschicht (ohne HA-Abhängigkeit)
│   ├── media_source.py      MediaSource + Streaming-View
│   ├── strings.json
│   └── translations/{en,de}.json
├── tests/                   Unit-Tests gegen echtes pyicloud mit Fake-CloudKit
├── tests_ha/                HA-Tests (Browse → Resolve → Stream, Config-/Options-Flow)
├── .github/workflows/validate.yml   hassfest, HACS-Validierung, Tests
├── hacs.json
└── README.md
```

---

## 4. Installation über HACS

Voraussetzungen:

* Home Assistant **2026.9** oder neuer
* Die Core-Integration **Apple iCloud** ist eingerichtet und angemeldet (inkl. 2FA). Ob das der Fall ist, sieht man z. B. daran, dass unter *Medien → iCloud* die Alben erscheinen.
* In Apple Fotos ist die **geteilte Mediathek** aktiv (Besitzer oder Teilnehmer).

Schritte:

1. HACS → ⋮ → **Benutzerdefinierte Repositories** → URL `https://github.com/neuhausf/icloud_shared_photos`, Typ **Integration** → Hinzufügen.
2. In HACS nach **iCloud Shared Photos** suchen → **Herunterladen**.
3. Home Assistant **neu starten**.
4. *Einstellungen → Geräte & Dienste → Integration hinzufügen →* **iCloud Shared Photos** → Absenden. Es werden keine Zugangsdaten abgefragt.
5. *Medien* öffnen → **iCloud Shared Photos → Shared Library → Favorites**.

Manuell ohne HACS: `custom_components/icloud_shared_photos` nach `<config>/custom_components/` kopieren, neu starten und dann Schritt 4.

## 5. Konfiguration (Optionen)

*Einstellungen → Geräte & Dienste → iCloud Shared Photos → Konfigurieren*

| Option | Standard | Bedeutung |
|---|---|---|
| Maximale Anzahl Fotos pro Album | 500 | Obergrenze pro Liste. Große Mediatheken werden seitenweise geladen, bis das Limit erreicht ist. |
| Videos einbeziehen | aus | Standardmäßig werden nur Bilder gelistet. |
| Ausgelieferte Bildversion | Automatisch | `auto` / `original` / `medium` (siehe oben) |
| Cache-Dauer für Albumlisten | 300 s | Wie lange eine Albumliste im RAM gilt. Neue ❤️ erscheinen spätestens danach. `0` = immer neu laden. |

---

## 6. Debugging und Tests

### Debug-Logging

Entweder in der UI unter *Einstellungen → Geräte & Dienste → iCloud Shared Photos → ⋮ → Debug-Protokollierung aktivieren* oder in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.icloud_shared_photos: debug
    # optional, sehr ausführlich:
    # pyicloud: debug
```

Geloggt werden unter anderem:

| Ereignis | Level | Beispiel |
|---|---|---|
| Erkannte Libraries | INFO | `iCloud photo libraries for 'me@icloud.com': PrimarySync (personal), SharedSync-1A2B… (shared)` |
| SharedSync-Zone | INFO | `Detected iCloud Shared Photo Library zone SharedSync-1A2B… for '…'` |
| Keine Shared Library | INFO | `No iCloud Shared Photo Library (SharedSync-*) found for '…'` |
| Anzahl Favoriten | INFO | `Loaded 42 item(s) from shared library SharedSync-…/Favorites for '…' in 1.3s` |
| Ignorierte Zonen | DEBUG | `Ignoring iCloud photo library 'shared' (zone=None, scope=None)` (= Photo Streams) |
| Auth-Probleme | WARNING/ERROR | `… requires two-factor re-authentication; re-authenticate it in the core iCloud integration` |
| Ungültige Assets | WARNING | `Asset … not found in SharedSync-…/Favorites (deleted or no longer a favorite?)` |
| Download-URL-Probleme | WARNING | `No download URL for asset …`, `iCloud returned HTTP 403 for asset …` |

Tokens, Cookies, Passwörter und die signierten Download-URLs werden **nie** geloggt.

### Automatisierte Tests (lokal)

Python 3.14:

```bash
pip install -r requirements_test.txt
pytest -q
```

`tests/` betreibt den **echten** pyicloud-`PhotosService` gegen nachgebildete CloudKit-Antworten und prüft dabei unter anderem, dass die Favoriten-Abfragen ausschließlich an die `SharedSync-*`-Zone gehen. `tests_ha/` startet eine HA-Testinstanz und prüft Browse → Resolve → Streaming, das Neuladen abgelaufener URLs, die Fehlerfälle sowie Config- und Options-Flow.

### Manuelle Tests in HA

1. *Medien → iCloud Shared Photos → Shared Library → Favorites* öffnen. Die Vorschaubilder müssen laden.
2. Ein Foto anklicken: Es wird in voller Größe angezeigt.
3. *iCloud Favorites → All Favorites* öffnen: Persönliche und geteilte Favoriten erscheinen gemeinsam, neueste zuerst.

---

## 7. Wie verifiziere ich, dass wirklich die SharedSync-Library benutzt wird?

1. **Log beim ersten Öffnen:** `Detected iCloud Shared Photo Library zone SharedSync-<UUID>` sowie `Loaded N item(s) from shared library SharedSync-<UUID>/Favorites`.
2. **Media-Content-ID:** Jedes Foto hat eine ID der Form
   `media-source://icloud_shared_photos/<icloud_entry_id>/lib/SharedSync-<UUID>/Favorites/<asset_id>`.
   Die Zone steht also in der ID. Mit Debug-Logging erscheint sie zusätzlich bei jedem Abruf (`Resolved SharedSync-…/Favorites/<asset_id> (IMG_1234.HEIC) to version 'medium'`).
3. **Gegenprobe mit Apple Fotos:** In Fotos (Mac/iPhone) die Mediathek-Ansicht auf **„Geteilte Mediathek“** stellen und *Favoriten* öffnen. Anzahl und Bilder müssen mit *Shared Library → Favorites* übereinstimmen. Mit der Ansicht **„Persönliche Mediathek“** vergleicht man entsprechend *Personal Library → Favorites*.
4. **Umschalttest:** Ein Foto der geteilten Mediathek mit ❤️ markieren und nach Ablauf der Cache-Dauer (Standard 5 min) die Liste neu öffnen. Das Foto erscheint unter *Shared Favorites*, **nicht** unter *Personal Favorites*.
5. **Tiefer:** Die Tests in `tests/test_library.py` prüfen gegen den echten pyicloud-Code, dass die Favoriten-Abfrage an `zoneID.zoneName = SharedSync-…` geht.

---

## 8. Fotos an einen BLOOMIN8-Rahmen schicken

Die Integration steuert selbst **keinen** Fotorahmen an. Sie liefert aber signierte, zeitlich begrenzte URLs, die jede andere Integration ohne Login abrufen kann. Mit der Standardoption „Automatisch“ kommen HEIC-Fotos dabei als JPEG an (die BLOOMIN8-Integration kann kein HEIC lesen).

### Geteilte Alben: zwei Formate

Apple führt geteilte Alben in zwei Formaten:

* **Klassisch (Photo Streams):** Album-ID ist eine GUID (`5FD857E3-…`). Gelesen über pyicloud's `shared_streams`.
* **CloudKit (neu):** Der icloud.com-Link hat die Form `…/sharedalbums/sc,…`. Das Album ist eine eigene CloudKit-Zone `SharedCollection-<UUID>` mit `CPLMaster`/`CPLAsset`-Datensätzen wie in einer Mediathek, dazu Beiträge, Kommentare und Reaktionen. iCloud lehnt Index-Abfragen in diesen Zonen ab (`Index has invalid data`), deshalb liest die Integration Fotos und Titel aus dem Änderungs-Feed der Zone (`changes/zone`). Der Albumname ist `cloudkit.title` des `cloudkit.share`-Datensatzes; fehlt er, heißt das Album „Shared Album <UUID-Anfang>“. Es ist auch über die Zonen-ID ansprechbar.

Beide erscheinen unter *Shared Albums* und funktionieren mit `get_album_photos`. Neu geteilte Alben werden ohne Neustart erkannt (Cache-Dauer der Albumlisten).

### Diagnose: `icloud_shared_photos.inspect_zones`

Listet alle Foto-Zonen eines Kontos und beschreibt die Datensätze der `SharedCollection-*`-Zonen: Datensatztypen, Feldnamen, kurze Textwerte von Nicht-Foto-Datensätzen (z. B. Albumtitel) sowie Anzahl und Dateinamen der gefundenen Fotos. Download-URLs und Bilddaten werden nie ausgegeben.

### Dienst `icloud_shared_photos.get_album_photos`

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `album` | ja | Name (Groß-/Kleinschreibung egal) oder ID eines **geteilten Albums** |
| `account` | nein | Apple-ID bzw. Titel des iCloud-Eintrags. Standard: alle Konten, das erste mit passendem Album gewinnt |
| `expires` | nein | Gültigkeit der URLs in Sekunden (Standard 3600) |

Antwort (nur als `response_variable` nutzbar):

```yaml
account: me@icloud.com
album: Bilderrahmen
album_id: 5FD857E3-…
count: 2
photos:
  - id: …
    filename: IMG_0356.HEIC
    device_filename: IMG_0356_1a2b3c4d.jpg   # stabil, ASCII, eindeutig
    date: "2024-07-03T09:20:00+00:00"
    media_content_id: media-source://icloud_shared_photos/…
    url: http://192.168.1.10:8123/api/icloud_shared_photos/serve/full/…?authSig=…
```

Die URLs basieren auf der **internen URL** von Home Assistant (*Einstellungen → System → Netzwerk*). Sie verlieren ihre Gültigkeit nach `expires` Sekunden oder beim Neustart von HA.

### Beispiel: geteiltes Album → BLOOMIN8-Playlist

Die Automation lädt nur neue Fotos hoch, löscht aus dem Album entfernte Fotos vom Rahmen und schreibt die Playlist neu, wenn sich etwas geändert hat.

```yaml
alias: Bilderrahmen – Playlist „Ferien“ aus geteiltem iCloud-Album
mode: single
triggers:
  - trigger: time
    at: "06:10:00"
  - trigger: state
    entity_id: sensor.bilderrahmen_device_info
    to: Online
actions:
  - variables:
      album: Bilderrahmen
      gallery: ferien
      playlist: Ferien
      duration: 14400   # Sekunden pro Bild
  - action: icloud_shared_photos.get_album_photos
    data:
      album: "{{ album }}"
    response_variable: icloud
  - action: bloomin8_eink_canvas.get_playlist
    target:
      entity_id: media_player.bilderrahmen_media_player
    data:
      name: "{{ playlist }}"
    response_variable: current
  - variables:
      existing: >-
        {% set pl = current.playlist if current.playlist is mapping else {} %}
        {{ pl.get('list', []) | map(attribute='name') | map('regex_replace', '^.*/', '') | list }}
      wanted: "{{ icloud.photos | map(attribute='device_filename') | list }}"
      uploads: >-
        {% set ns = namespace(items=[]) %}
        {% for p in icloud.photos if p.device_filename not in existing %}
          {% set ns.items = ns.items + [{'url': p.url, 'filename': p.device_filename}] %}
        {% endfor %}
        {{ ns.items }}
      removed: "{{ existing | reject('in', wanted) | list }}"
  - repeat:
      for_each: "{{ uploads | batch(5) | list }}"
      sequence:
        - action: bloomin8_eink_canvas.upload_images_multi
          target:
            entity_id: media_player.bilderrahmen_media_player
          data:
            gallery: "{{ gallery }}"
            images: "{{ repeat.item }}"
            override: true
  - repeat:
      for_each: "{{ removed }}"
      sequence:
        - action: bloomin8_eink_canvas.delete_image
          target:
            entity_id: media_player.bilderrahmen_media_player
          data:
            gallery: "{{ gallery }}"
            filename: "{{ repeat.item }}"
  - if:
      - condition: template
        value_template: "{{ uploads | count > 0 or removed | count > 0 }}"
    then:
      - action: bloomin8_eink_canvas.put_playlist
        target:
          entity_id: media_player.bilderrahmen_media_player
        data:
          name: "{{ playlist }}"
          type: duration
          items: >-
            {% set ns = namespace(items=[]) %}
            {% for f in wanted %}
              {% set ns.items = ns.items + [{'name': '/gallerys/' ~ gallery ~ '/' ~ f, 'duration': duration | int}] %}
            {% endfor %}
            {{ ns.items }}
```

Alternativ lässt sich jedes Foto auch über `media_content_id` mit `media_source.async_resolve_media()` oder `media_player.play_media` auflösen.

---

## 9. Bekannte Einschränkungen und Risiken

* **Kein offizieller Core-Vertrag:** `entry.runtime_data.api` ist ein internes Detail der Core-Integration `icloud`. Wird es in einer künftigen HA-Version umbenannt, zeigt der Browser einen verständlichen Fehler statt Fotos, und die Integration muss angepasst werden. Die Stelle ist in `media_source.py::_icloud_api` gekapselt.
* **pyicloud-Umfang:** pyicloud unterstützt in Shared Libraries derzeit nur `Library` und `Favorites`. Benutzeralben innerhalb der geteilten Mediathek und gemischte Ansichten fehlen.
* **CloudKit-Geteilte-Alben** (`SharedCollection-*`) werden über pyicloud's allgemeine Mediathek-Abfragen gelesen, nicht über eine dafür vorgesehene API. Ändert Apple das Format dieser Zonen, kann das Auslesen ausfallen; `inspect_zones` hilft bei der Analyse.
* **Geteilte Alben** (Photo Streams) laufen über pyicloud's ältere Shared-Streams-API. Einzelne Fotos werden dort durch Blättern im Album gesucht; bei sehr großen geteilten Alben ist der erste Abruf nach Ablauf des Caches daher langsamer.
* **Große Mediatheken:** `Library` kann zehntausende Einträge haben. Daher gibt es das Limit „Maximale Anzahl Fotos pro Album“. `Library` wird von neu nach alt gelistet. Persönliche Favoriten liefert iCloud von alt nach neu: Hat man mehr Favoriten als das Limit, fehlen die neuesten. In dem Fall das Limit erhöhen.
* **Signierte Download-URLs laufen ab.** Deshalb gibt es die 30-Minuten-TTL und den automatischen Retry. Eine kurze Verzögerung beim ersten Abruf nach längerer Zeit ist normal.
* **Advanced Data Protection (ADP):** Mit aktivem ADP ist der Webzugriff auf iCloud-Fotos nur eingeschränkt oder gar nicht möglich. Das betrifft die Core-Integration genauso.
* **Session teilen:** Beide Integrationen nutzen dieselbe `requests`-Session. Läuft die Core-Session ab (z. B. nach ca. 2 Monaten mit erneutem 2FA), wird die Wiederanmeldung ausschließlich in der Core-Integration *Apple iCloud* erledigt. Diese Integration meldet dann „requires re-authentication“.
* **Media-IDs enthalten die Entry-ID** der Core-iCloud-Integration. Wird die iCloud-Integration gelöscht und neu angelegt, ändern sich die IDs, und gespeicherte IDs in Automationen müssen erneuert werden.
* **Duplikate in „All Favorites“** werden über Zone + Asset-ID und über den Inhalts-Fingerprint des Originals erkannt. Bearbeitete Kopien mit anderem Original bleiben getrennte Einträge.
* **Live Photos** werden als Standbild ausgeliefert. Videos erscheinen nur, wenn die Option aktiviert ist.
* Keine offizielle Apple-API: Apple kann das Verhalten von CloudKit jederzeit ändern.

## Lizenz

MIT
