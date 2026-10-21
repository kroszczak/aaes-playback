# Fotopułapka ornitologiczna — Active Playback

Autonomiczna pułapka na Luckfox Pico Pro Max (RV1106). RTC budzi płytkę na start
okna czasowego; przez całe okno system gra w pętli śpiew terytorialny z modułu
DFPlayer Mini i równolegle analizuje obraz z kamery przez NPU. Wykrycie ptaka
uruchamia nagranie — **bez przerywania playbacku**, bo na tym polega eksperyment:
„intruz" śpiewa dalej, gdy właściciel terytorium przylatuje go sprawdzić.

## Pliki

| Plik | Rola |
|---|---|
| `birdbox_main.py` | Cykl: okna czasowe, playback, strumień kamery, NPU, nagrywanie, domknięcie |
| `config.py` | Wszystkie parametry w jednym miejscu + parser okien |
| `dfplayer.py` | Sterowanie DFPlayer Mini (UART3, liczone ramki, status) |
| `rtc.py` | DS3231 po I2C: czas, alarmy, flagi |
| `recoveryd.py` + `S98recoveryd` | Demon ratunkowy: restart płytki, gdy główny program zawiśnie |
| `pir_legacy.py` | Stara ścieżka PIR — nieużywana, zachowana jako fallback |
| `windows.txt.example` | Wzór konfiguracji okien na kartę SD |
| `migrate_runtime.sh` | Niepotrzebny w obecnej architekturze (zostawiony dla historii) |

Autostart: `/etc/init.d/S98recoveryd` (demon), potem `/etc/init.d/S99birdbox`
(`cd /root && python3 birdbox_main.py`).

## Architektura cyklu

1. **Start** — czas z DS3231, jeśli zegar systemowy jest sprzed NTP; wczytanie okien
   (`/mnt/sdcard/windows.txt` albo domyślne 06:00–08:00 i 18:00–20:00).
2. **Alarmy RTC programowane od razu**, zanim ruszy kamera — nawet jeśli okno padnie,
   następne wybudzenie jest już ustawione.
3. **Strażnik zasilania** — program zostawia demonowi termin (koniec okna + 5 min);
   jeśli cykl się do tego czasu nie domknie, demon sam odcina zasilanie przez GPIO 64.
4. **Okno**: DFPlayer zapętla utwór po stronie modułu; kamera startuje **raz** i daje
   ciągły strumień MJPEG (30 kl./s) do potoku nazwanego; wątek czytnika trzyma w
   pamięci ostatnią klatkę; NPU analizuje najświeższą klatkę ~1×/s.
5. **Detekcja** → zdjęcie + klip 45 s (15 kl./s) z tego samego strumienia. Analiza
   trwa podczas nagrywania; jeśli ptak był widziany w ostatnich 10 s klipu, kolejny
   klip startuje w tej samej klatce — łańcuch bez przerwy.
6. **Domknięcie** — stop playbacku i zamknięcie klipu, potem RTC, kronika i `sync`
   **przy wciąż działającym strumieniu kamery**, na końcu odcięcie zasilania przez
   Pololu (GPIO 64). Potoku kamery w terenie nie zatrzymujemy wcale — zatrzymanie to
   ta sama ryzykowna operacja, która w oknie 60-minutowym zablokowała system
   właśnie w tym miejscu. Na stole (`--test`) kamera gaśnie dopiero po zapisaniu wszystkiego.

Każdy podsystem jest odizolowany: brak DFPlayera → okno bez playbacku; awaria
strumienia → do 3 restartów; wyjątek w oknie nie omija domknięcia cyklu.

### Demon ratunkowy (`recoveryd.py`)

Gdy I/O się zablokuje, nie da się uruchomić żadnego programu — nawet `reboot`.
Demon działa od startu systemu, ma strony przypięte w RAM (`mlockall`) i restartuje
wywołaniem systemowym `reboot()` przez ctypes, bez `exec`.

- `touch /tmp/DO_REBOOT` — restart na żądanie
- puls `/tmp/heartbeat` starszy niż 10 min — restart automatyczny (limit 3, zerowany
  po każdym czystym cyklu)
- termin `/tmp/power_deadline` minął, a cykl się nie domknął — odcięcie zasilania
  (na zasilaniu z sieci, gdy pin nic nie robi, po 30 s restart)
- przed restartem i odcięciem próbuje `sync()` przez maks. 5 s w osobnym wątku: na
  zdrowym systemie zapisze wszystko, na zablokowanym nie zawiśnie
- każdą decyzję zapisuje w `milestones.log` jako `RECOVERYD: ...`

Puls i termin są zapisywane w **zegarze monotonicznym** (`CLOCK_MONOTONIC`), nie
ściennym. Pierwsza wersja porównywała czasy z zegarem ściennym, a ten po zimnym
starcie przeskakuje z 2021 na aktualną datę (NTP albo odczyt DS3231) — puls wyglądał
na „sprzed 5,7 roku" i demon restartował płytkę przy każdym wybudzeniu (zatrzymał
go dopiero limit trzech restartów). Program zdejmuje puls przy każdym wyjściu,
także awaryjnym; zostaje on tylko wtedy, gdy program naprawdę zawiśnie.

## Główna przyczyna awarii: wielokrotne uruchamianie potoku kamery

Poprzednia wersja uruchamiała enkoder przy **każdym zdjęciu**. Po ~20 rozruchach
potoku VI/ISP/VENC system tracił I/O: najpierw tor karty SD (`mmc_wait_for_req_done`),
potem rootfs na NAND (UBIFS read-only, procesy ginące z `SIGBUS`).

Testy rozstrzygające (11.09.2026, każdy na świeżo zrestartowanej płytce):

| Test | Nośnik | NPU | Wynik |
|---|---|---|---|
| Pętla zdjęć przy **odmontowanej** karcie SD | brak | tak | **pada** po ~100 s |
| A: jeden ciągły strumień, 250 s | brak | nie | czysto |
| B: 21× uruchom/zatrzymaj potok | brak | nie | **zawieszenie** na #22 |
| C: ciągły strumień + 203 MB zapisu na kartę | karta | nie | czysto |
| Okno 15 min, nowa architektura | karta | tak | czysto |
| Okno 30 min + 4 klipy w łańcuchu | karta | tak | czysto |

Wniosek: winne są **rozruchy potoku**, nie karta, nie zapis, nie NPU. Stąd jeden
strumień na całe okno.

### Fałszywe tropy (dla porządku)

Po drodze obwiniane i wykluczone: karta SD (dwie różne karty, vfat i ext4, 25 i 50 MHz
— bez zmian), zasilanie (Pololu U3V40F5 ma kilkukrotny zapas), przegrzewanie (awarie
przy 58 °C), pamięć CMA (wolna), zużyty NAND (18 min bezczynności bez błędu). Karta
i NAND padały jako **ofiary** zablokowanego I/O, nie sprawcy.

## Sprzęt — stan zweryfikowany na płytce

| Element | Ustalenie |
|---|---|
| UART do DFPlayera | `/dev/ttyS3`, 9600 8N1, piny **19** (TX→RX) i **20** (RX→TX) |
| Pinmux UART3_M1 | **Wymaga włączenia**: `UART3_M1_STATUS=1` w `/etc/luckfox.cfg` (resetuje się przy przebudowie rootfs — patrz handoff Dockera) |
| DS3231 | i2c-3, `0x68` (+ EEPROM `0x50`); brak sterownika jądra, obsługa wprost po I2C |
| Karta na media | 32 GB, **jedna partycja vfat**, `/mnt/sdcard` |
| Zasilanie OFF | GPIO **64** (pin OFF przetwornicy Pololu) |
| Kamera | sensor `sc3336`, 2304×1296, MJPEG 30 kl./s, ~63 kB/klatkę |

## Błędy znalezione w handoffie

1. **Ramka „stop" ma złą sumę kontrolną** (`FE EA` zamiast `FE E5`). Dlatego
   `dfplayer.build_frame()` sumę liczy.
2. **Pin 27 to GPIO2_A7 (sysfs 71)**, nie GPIO2_A3 (67) — ten leży na pinie 34.
3. **Pin BUSY jest nieużyteczny** (stałe `LOW`) — kod wykrywa to sam i przechodzi na
   status z UART (`0x42`).
4. **`/dev/rtc1` nie istnieje** — synchronizacja przez `hwclock` nigdy nie działała.

## Błędy naprawione w starym kodzie

- `record_video()` nie nagrywał niczego (brak `-o` → `Output Path: (null)`).
- Kadry zerowej długości (wybór pliku przed ubiciem enkodera).
- Rozjazd stref czasowych o 2 h (DS3231 lokalnie, system UTC) — `config.TIMEZONE`.
- Demo RKNN zapisywało `out.png` (~460 kB) na NAND przy każdej inferencji —
  ~640 MB zapisów na okno; katalog roboczy przeniesiony do RAM (`config.NPU_WORKDIR`).
- Odświeżanie playbacku wołało DFPlayera bez sprawdzenia, czy istnieje — przy
  niedziałającym module okno wywracało się po 5 minutach.

## Pomiary na sprzęcie

| Operacja | Wynik |
|---|---|
| Strumień kamery | 30 kl./s stabilnie, 1,9 MB/s, jeden rozruch na okno |
| Inferencja NPU (YOLOv5, int8) | **0,29 s** po usunięciu zapisu `out.png` z demo (było 2,41 s) |
| Rytm analizy | 1 analiza/s, procesor bezczynny w 72% (wcześniej 0,38/s przy 100% obciążenia) |
| Klip 45 s, 15 kl./s | 675–682 klatki, odtwarzalny (`ffprobe`); 42 MB w scenie ubogiej, **80 MB w jasnej i bogatej w szczegóły** |
| Detekcja prawdziwego ptaka (zdjęcie, ~8% kadru) | pewność 0,63–0,70 stabilnie przez 8 analiz z rzędu |
| RAM pod pełnym obciążeniem | ~120 MB wolnego ze 180 MB |

## Optymalizacja demo RKNN (zmierzona)

Demo `rknn_yolov5_demo` przy każdej inferencji kodowało `out.png` (2304×1296), którego
fotopułapka nigdy nie używa — parsuje wyłącznie linie `printf` z wynikami. Pomiar
na tym samym zdjęciu, 8 przebiegów na wersję:

| Wersja | Mediana | Detekcja |
|---|---|---|
| Oryginalna | 2,41 s | `bird @ (633 428 871 683) 0.668` |
| Bez zapisu obrazu | **0,29 s** | identyczna |
| Bez zapisu, ale z `RKNN_OUT_IMAGE=<ścieżka>` | 2,40 s | identyczna |

Czyli **88% czasu inferencji** szło na obrazek do wyrzucenia. Zmiana to jedna linia
w `examples/yolov5/cpp/main.cc`: rysowanie ramek i zapis wykonują się tylko, gdy
ustawiona jest zmienna `RKNN_OUT_IMAGE` — narzędzie do kalibracji progu zostaje,
koszt w produkcji znika. Oryginał leży na płytce jako `rknn_yolov5_demo.orig`.

**Nie wymaga to przeflashowania systemu** — demo jest zwykłym programem w przestrzeni
użytkownika, podmienia się jedną binarką przez `scp`. Budowanie: kontener
`ubuntu:22.04 --platform linux/amd64` z zamontowanym toolchainem z `~/Grant/tools`
i drzewem `rknn_model_zoo`, `./build-linux.sh -t rv1106 -a armhf -d yolov5`.

## Zatrzymanie pułapki w terenie

Dwie lokalizacje blokady, sprawdzane przy starcie i w każdej iteracji okna:

| Plik | Zastosowanie |
|---|---|
| `/root/blokada` | praca przez SSH |
| `STOP` na karcie (`/mnt/sdcard/fotopulapka/STOP`) | **bez konsoli** — wyjmij kartę, utwórz pusty plik, włóż z powrotem |

## Stan testów

| Test | Wynik |
|---|---|
| DFPlayer izolowany | ✅ gra, status po UART |
| Zapis na kartę | ✅ |
| Okno 15 min | ✅ 345 analiz, czyste domknięcie |
| Okno 30 min + symulowana detekcja | ✅ 685 analiz, 4 klipy w łańcuchu |
| Okno 60 min + detekcja w 50. minucie | ✅ 1362 analizy, 3 klipy; zawieszenie przy zatrzymywaniu kamery po oknie → kolejność domknięcia zmieniona |
| Demon: odcięcie zasilania po terminie | ✅ na sprzęcie (na sieci → restart po 30 s) |
| Demon: skok zegara o 4 lata | ✅ brak fałszywego restartu |
| Demon: prawdziwie przeterminowany puls | ✅ restart w 1 s, ślad na karcie |
| Okno 120 min | ✅ 2592 analizy, 3 klipy w łańcuchu, `clean_exit: true` |
| **Prawdziwa detekcja ptaka → zdjęcie → klip → łańcuch → koniec łańcucha** | ✅ 3 klipy, 675 klatek każdy, klatka z wnętrza klipu zawiera wykryty obiekt |
| Magistrala RTC po przelutowaniu ścieżki pinu 10 | ✅ 591/592 OK w 120 s, EEPROM też wrócił |
| **Pełna pętla RTC → okno → uśpienie → RTC** | ✅ budzik na 12:27, samodzielne wybudzenie o 12:27:13, okno, domknięcie, ponowne uśpienie |
| Demo RKNN bez zapisu obrazu | ✅ 0,29 s zamiast 2,41 s, detekcja identyczna |

## Elektronika toru wybudzania — stan zmierzony

Stopień budzika wisi na szynie 5 V **przed** wyłącznikiem, więc pobiera prąd także
wtedy, gdy płytka jest wyłączona. Pomiary napięć na rezystorach (12.09.2026):

| Gałąź | Prąd | Uwagi |
|---|---|---|
| Dioda LED modułu RTC (1 kΩ) | **3,0 mA** | świeci bez przerwy, 79% poboru tego stopnia |
| Podciągnięcie SQW (4,7 kΩ) + rezystor bazy (10 kΩ) | 0,3 mA | |
| Podciągnięcie kolektora (10 kΩ) | 0,5 mA | tranzystor przewodzi w spoczynku |
| **Razem z szyny 5 V** | **3,8 mA** | ≈ 5,7 mA z ogniwa ≈ 137 mAh/dobę |

**I2C jest w specyfikacji.** Piny 9 (SDA) i 10 (SCL) trzymają 3,3 V — moduł DS3231
**nie ma** własnych rezystorów podciągających na tych liniach (dlatego bez dodanych
podciągnięć do 3V3 Luckfoxa komunikacja nie działała wcale). Nic tu nie wymaga zmiany.

Możliwe oszczędności, **w kolejności rosnącego ryzyka**:

1. **usunięcie diody LED modułu** — −3,0 mA, nie dotyka żadnej funkcji, dodatkowo
   przestaje świecić wewnątrz obudowy;
2. rezystory 10 kΩ (baza i kolektor) → 100 kΩ — −0,7 mA, ale **dotyka toru
   wybudzania**: za mały prąd bazy = urządzenie, które nigdy się nie obudzi, bez
   żadnego objawu w logu. Po takiej zmianie **obowiązkowy pełny test wybudzenia z
   alarmu**, nie samo sprawdzenie diody.

Przy zasilaniu z panelu solarnego punkt 2 może być niepotrzebny — 137 mAh/dobę jest
znacznie poniżej tego, co oddaje nawet mały panel w pogodny dzień.

## Przed wyjazdem w teren

1. ~~Okablowanie DS3231~~ — **naprawione**: przelutowanie całej ścieżki pinu 10 (SCL)
   usunęło problem. Dowód, że to była właśnie ta linia: wrócił też EEPROM modułu,
   który przez uszkodzoną SCL nie odpowiadał wcale.
2. **Test pełnej pętli** bez `--test`: okno ustawione na „za kilka minut" w
   `windows.txt`, uśpienie przez Pololu, wybudzenie alarmem, drugi cykl.
3. **Głośność** — obecnie 8/30 (biurko); w terenie dobrać na miejscu.
4. **Nagranie na DFPlayerze** — obecny utwór trwa ~4 s; do eksperymentu warto wgrać
   właściwą frazę śpiewu terytorialnego z naturalnymi pauzami.

## Uruchamianie

```bash
python3 birdbox_main.py                          # produkcyjnie: okno wg harmonogramu, potem uśpienie
python3 birdbox_main.py --test                   # bez odcinania zasilania, zachowuje wszystkie kadry
python3 birdbox_main.py --test --window 900      # okno 15 min od teraz
python3 birdbox_main.py --test --window 1800 --simulate-bird 300-450   # test nagrywania
python3 birdbox_main.py --force-window           # okno poza harmonogramem
python3 birdbox_main.py --ignore-rtc             # tylko na stole: nie przerywa przy martwym RTC
```

**Uwaga:** przy usuniętym bypassie linii OFF każdy przebieg bez `--test` kończy się
odcięciem zasilania. `touch /root/blokada` zatrzymuje automatykę przy starcie.

## Weryfikacja po powrocie z terenu

Wszystko na karcie, w `/mnt/sdcard/fotopulapka/`:

| Plik | Zawartość |
|---|---|
| `milestones.log` | start okna, „żyje: N analiz" co 100 analiz, koniec, domknięcie |
| `state.json` | licznik startów, `clean_exit` true/false dla ostatniego cyklu |
| `system.log` | pełny log, każda analiza i detekcja |
| `photos/bird_*.jpg` | kadry z detekcją |
| `videos/bird_*.mjpeg` | klipy 45 s |

Klipy to surowy MJPEG — VLC otwiera je bezpośrednio. Konwersja do MP4:

```bash
ffmpeg -framerate 15 -i bird_20260911_141208.mjpeg -c:v libx264 -pix_fmt yuv420p klip.mp4
```

## Finansowanie

Projekt sfinansowany z programu **Study@research**, Inicjatywa Doskonałości – Uczelnia Badawcza (ID UB), Uniwersytet im. Adama Mickiewicza w Poznaniu. Numer projektu: **201/34/UAM/0068**.
Numer grantu należy podawać we wszystkich rozliczeniach, sprawozdaniach i publikacjach dotyczących projektu.
