# Aither Dupe Checker

Kézi qBittorrent → Aither dupe-ellenőrző és feltöltési lista-előkészítő.

## Működés

1. Lekéri a kész qBittorrent release-eket, és az azonos normalizált nevű összes
   hash forrás-trackerét ellenőrzi. Az Aitherről származó torrent eleve duplikátum,
   ehhez nem használ Aither API-t és privát tracker URL-t nem tárol.
2. A blacklistet még metadata- és Aither-kérés előtt alkalmazza. A manifestből
   bizonyítható játék/szoftver `NOT_MEDIA`, ezért szintén nem használ katalógus-
   vagy Aither API-t. ISO esetén csak egy olvasási `7z l -slt --` listát vizsgál.
3. A release-nevet a qB fájlmanifesttel egészíti ki. A BDMV/VIDEO_TS/ISO valódi
   `Full Disc`; a `WEB.H264/H265` pedig `WEB-DL`, nem általános encode.
4. Helyi NFO/XML/TXT IMDb-ID-t, BDMV META címet, végső fallbackként pedig teljesen
   és korlátozottan átvizsgált Matroska-tagokat keres biztonságos fájlolvasással.
   Sorozatnál az exact epizódcím is feloldhat több azonos című művet. Ezután az
   eredeti release-névből megőrzött cím-/aliasváltozatokat próbálja TMDb-n. Nem
   fogad el fuzzy névhasonlóságot: eltérő piaci cím csak
   azonos év és a TMDb hivatalos alternative-title adata alapján érvényes. Ha
   nincs TMDb-találat, szigorú, pontos cím+év+médiatípus IMDb suggestion
   következik, majd lehetőség szerint TMDb `find`.
5. Az Aither-lekérdezés szándékosan széles: csak azonosító, kategória és TV-nél
   évad szerepel benne (`perPage=100`). Nincs episode-, type- vagy resolution-
   szűrő, ezért egy rossz névparse nem rejtheti el a valódi egyezést.
6. A sanitizált teljes találati oldal tartós query-cache-be kerül. Egy futáson
   belül azonos identity+kategória+évad (például több epizód) nulla új
   Aither-hívással osztozik rajta. A döntés a lap teljes tartalmának SHA-256
   lenyomatához és az evidence-séma verziójához kötött, ezért egy későbbi cache-
   felülírás nem változtathat meg régi döntést. Megszakadt release kizárólag a
   pontosan hozzá kötött cache-lapból folytatható. Download link, magnet, token
   és passkey soha nem kerül a state-be.
7. `DUPLICATE` csak pozitív technikai egyezés lehet. Full Disc esetén többek közt
   a kiadás/distributor, terjesztési és fizikai régió, lemezkapacitás, felbontás,
   codec, HDR, hangsávprofil, fájlszám, BDInfo és a lemezenkénti BDMV/VIDEO_TS
   path+méret-manifest számít. Egy epizód csak akkor egyezik évadcsomaggal, ha a
   csomagban az adott epizód exact csoport-, technika-, kiterjesztés- és bájtméret-
   bizonyítékkal megtalálható. Az ISO mérete vagy neve önmagában soha nem bizonyít
   egyezést. Így például egy CEE és HKG/USA/AUS/GBR lemez nem mosódik össze.
8. Feltöltésre csak teljes, nem lapozott válaszból bizonyított `NOT_FOUND` kerül.
   A `MANUAL`, `UNRESOLVED`, `NOT_MEDIA` és `RECHECK_REQUIRED` soha nem READY.

Minden valódi Aither HTTP-próbálkozás beleszámít a limitbe. A hívások között
legalább 5 másodperc telik el, minden tizedik után pedig kötelező 30 másodperces
szünet következik. A számláló az állapotfájlban marad, tehát újraindítással sem
kerülhető meg.

## Döntési állapotok

- `DUPLICATE`: bizonyítottan azonos technikai kiadás, vagy a qB forrása Aither;
- `NOT_FOUND`: a teljes széles Aither-oldalon nincs egyező technikai kiadás;
- `MANUAL`: van releváns jelölt, de az edition/disc bizonyíték nem elég biztos;
- `UNRESOLVED`: nem állapítható meg egyetlen biztonságos katalógus-azonosító;
- `NOT_MEDIA`: a helyi manifest bizonyítottan nem film/sorozat payload;
- `RECHECK_REQUIRED`: átmeneti karantén egy bizonyítottan Aither-keresés előtt
  elakadt régi `UNRESOLVED` rekord első feloldásához.

A kész listában a sima `READY` útvonal minden futáskor friss qB-tagság-,
útvonal-, létezés- és symlinkellenőrzést kap. Az eltűnt tétel `STALE`, ezért nem
marad a TXT-ben; ha újra érvényessé válik, automatikusan visszakerülhet.

Az `identity_overrides.json` média-override csak exact qB infohash, teljes
kiterjesztés+bájtméret multihalmaz, fájlszám és összméret mellett aktiválódhat.
Kötelező `payload_review` marker esetén a katalógus-ID feloldódik, de a release
Aither-kérés és READY nélkül közvetlenül `MANUAL` lesz.

A release az első Aither HTTP-próbálkozás után hálózatról soha nem kereshető újra.
Az állapot az összes ismert qB infohash-t append-only módon is megőrzi, ezért az
átnevezés vagy másik normalizált név sem kerülheti meg ezt a tilalmat: az ilyen
tétel új API-hívás és READY nélkül `MANUAL` lesz.
Ez a régi `MANUAL`, `NOT_FOUND` és `DUPLICATE` rekordokra akkor is érvényes, ha a
korábbi program nem őrizte meg a találati oldalt: ezek READY/dupe hatása megszűnik,
és biztonságos `MANUAL` állapotba kerülnek. Régi `UNRESOLVED` csak akkor oldható
fel automatikusan, ha a tartós előzmény bizonyítja, hogy még egyetlen Aither-kérése
sem volt. Csak a tartalmilag és sémaverzióval is pontosan kötött találati oldal
értékelhető újra API-hívás nélkül. A régi, digest nélküli cache nem válhat új
automatikus döntéssé; kizárólag dokumentált offline operátori audit sorolhatja be.
A teljes előzmény `previous_decision` alatt auditálható marad.

## Indítás

```bash
aither
```

Nincs cron vagy systemd időzítő; a program kizárólag kézzel indul.

## RAR és ISO feldolgozás

A normál médiafájlok/mappák automatikusan bekerülnek a kész listába. RAR-os és
ISO-s release-eknél a program előbb egyenként bekéri a döntést:

- `i` — helyben kicsomagolja; siker és ellenőrzés után az eredeti teljes release
  helyére csak a payload kerül, majd a pontos qBittorrent-bejegyzések
  `deleteFiles=false` értékkel eltűnnek a listából;
- `n` — változtatás nélkül kihagyja;
- `k` — külön release-ként a
  `/home/accofil/torrents/qbittorrent/up` alá készíti el ugyanazzal a relatív
  struktúrával; az eredeti és a qBittorrent-bejegyzés változatlan;
- `q` — kilép, a további release-ek függőben maradnak.

A választás csak tervet készít. Bármilyen fájl- vagy qBittorrent-módosítás előtt
a teljes tervet még egyszer jóvá kell hagyni. EOF, `Ctrl-C`, hibás válasz vagy
nem interaktív futás soha nem jelent automatikus igent.

Az archívumokat a program előbb teszteli és üres staging területre bontja.
Abszolút/`..` útvonal, symlink, speciális fájl, hiányos RAR-készlet, elégtelen
hely, célütközés vagy bizonytalan qBittorrent-tulajdonlás esetén az eredeti
érintetlen marad. RAR-ból kibontott ISO-t ugyanabban a tranzakcióban bont tovább.

## Állományok

- konfiguráció: `~/.aithercheck.env` (`0600`)
- keresési, sanitizált query-cache és feldolgozási állapot:
  `~/aither_checked_seen.json` (`0600`, state v2)
- kész feltöltési lista: `~/aither_upload_ready.txt` (`0600`)
- üzleti napló: `~/aither_missing.log`
- ellenőrzött, pontos lemez-ujjlenyomathoz kötött katalógus-hintek:
  `identity_overrides.json` (név önmagában soha nem aktivál override-ot)

A korábbi listás `aither_checked_seen.json` formátum automatikusan,
adatvesztés nélkül betöltődik.

## Telepítés

```bash
./install.sh
```

Szükséges rendszereszközök: Python 3.11+, `unrar`, `7z`. A Python-függőségek a
`requirements.txt` fájlban vannak.

## Tesztek

```bash
~/aithercheck-venv/bin/python -m unittest discover -s tests -v
```
