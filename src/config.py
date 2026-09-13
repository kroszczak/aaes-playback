"""Konfiguracja fotopułapki - jedno miejsce na wszystkie parametry.

Architektura "aktywnego wabika": RTC budzi płytkę na start okna czasowego,
przez całe okno system gra playback i obserwuje kadr kamerą + NPU.
"""

import logging
import os
import re

# ==========================================
# OKNA CZASOWE
# ==========================================
# UWAGA: DS3231 ma tylko DWA alarmy sprzętowe, więc obsługujemy maksymalnie
# dwa okna dziennie. Nadmiarowe okna z pliku konfiguracyjnego są odrzucane.
MAX_WINDOWS = 2

WINDOWS_DEFAULT = [("06:00", "08:00"), ("18:00", "20:00")]

# Plik na karcie SD nadpisujący okna. Format: jedna linia na okno, "HH:MM-HH:MM".
# Puste linie i linie zaczynające się od '#' są ignorowane.
WINDOWS_FILE = "/mnt/sdcard/windows.txt"

# ==========================================
# STREFA CZASOWA
# ==========================================
# Łańcuch POSIX (płytka nie ma bazy zoneinfo, ale busybox i Python rozumieją
# ten format). DS3231 trzyma czas LOKALNY, więc godziny alarmów i godziny okien
# są w tej samej skali - bez tego okna odpalałyby się z przesunięciem o offset UTC.
TIMEZONE = "CET-1CEST,M3.5.0,M10.5.0/3"   # Polska, z automatyczną zmianą czasu

# ==========================================
# ŚCIEŻKI
# ==========================================
MEDIA_ROOT = "/mnt/sdcard/fotopulapka"   # natywny slot TF (vfat, jedna partycja)
MEDIA_FALLBACK = "/root/fotopulapka"     # gdy SD niezamontowana - NAND, tylko awaryjnie

# Log na karcie SD, pisany na bieżąco. NAND tylko wtedy, gdy karty brak.
LOG_FILE = "/mnt/sdcard/fotopulapka/system.log"
LOG_FILE_FALLBACK = "/root/system.log"

# ==========================================
# ODPORNOŚĆ NA AWARIE (praca w terenie bez nadzoru)
# ==========================================
# Strażnik: osobny proces, który po końcu okna + margines BEZWARUNKOWO odcina
# zasilanie. Jedyna ochrona ogniwa, gdy główny program zawiesi się w stanie "D",
# z którego żaden kod w tym procesie już się nie wybudzi.
WATCHDOG_MARGIN_S = 300
WATCHDOG_DONE_FLAG = "/tmp/window_done"

# Kronika przebiegów - pozwala po powrocie z terenu zobaczyć, ile cykli było
# i który zakończył się nieczysto.
STATE_FILE = "/mnt/sdcard/fotopulapka/state.json"

# Kamienie milowe: kilka linii na okno z fsync - najkrótsza odpowiedź na pytanie
# "dokąd pułapka doszła", także gdy coś przerwało cykl.
MILESTONE_FILE = "/mnt/sdcard/fotopulapka/milestones.log"
MILESTONE_EVERY = 500           # kamień milowy co tyle analiz NPU
SLEEP_SCRIPT = "/root/sleep.sh"
LOCK_FILE = "/root/blokada"
# Druga lokalizacja blokady - plik STOP na karcie SD. Pozwala zatrzymać pułapkę
# w terenie BEZ logowania się przez SSH: wystarczy wyjąć kartę, utworzyć na niej
# pusty plik i włożyć z powrotem. Blokada z NAND-a wymaga dostępu do konsoli,
# którego w lesie zwykle nie ma.
LOCK_FILE_CARD = "/mnt/sdcard/fotopulapka/STOP"

# ==========================================
# KAMERA - JEDEN CIĄGŁY STRUMIEŃ NA CAŁE OKNO
# ==========================================
# Zmierzone na płytce (bisekcja, 11.09.2026):
#   - jeden ciągły strumień przez 250 s, bez nośnika            -> czysto
#   - ciągły strumień + 203 MB zapisu na kartę w 4 min           -> czysto
#   - 21x uruchom/zatrzymaj potok, bez nośnika i bez NPU          -> zawieszenie I/O
# Czyli WIELOKROTNE uruchamianie potoku (VI/ISP/VENC) po ~20 cyklach blokuje I/O
# całego systemu. Dlatego kamera startuje RAZ na okno, a klatki czytamy na bieżąco
# ze strumienia MJPEG przez potok nazwany (FIFO): 30 kl./s, ~63 kB/klatkę.
CAMERA_FIFO = "/tmp/cam.mjpeg"
STREAM_START_TIMEOUT_S = 20     # ile czekać na pierwszą klatkę po starcie
STREAM_STALL_S = 10             # brak nowych klatek dłużej = strumień martwy
# Każdy restart strumienia to rozruch potoku - czyli dokładnie to, co szkodzi.
# Kilka na okno jest bezpiecznych (awaria pojawiała się po ~20).
CAMERA_RESTART_LIMIT = 3

# Demo RKNN przy każdej inferencji zapisuje out.png (~460 kB) do katalogu
# roboczego. Na NAND-zie dawało to ~640 MB zapisów na dwugodzinne okno -
# dlatego katalog roboczy leży w RAM-ie.
NPU_WORKDIR = "/tmp/npu"

# Co ile analizowanych klatek zachowywać kadr w trybie --test. Przy ~1 analizie/s
# zapisywanie każdej dałoby ~7000 plików i 900 MB na dwugodzinne okno.
TEST_PHOTO_EVERY = 10

# ==========================================
# RYTM OBSERWACJI I NAGRANIA
# ==========================================
# Dolna granica odstępu między analizami. Po usunięciu zapisu out.png z demo RKNN
# inferencja trwa 0,29 s (było 2,41 s - 88% czasu zjadało kodowanie PNG obrazka,
# którego nie używamy). Przy 0 pętla kręciłaby się ~3x/s, obciążając procesor bez
# potrzeby; 1 s daje ~1 analizę na sekundę, czyli i tak 2,5x częściej niż wcześniej,
# a procesor ma czas bezczynny - co na ogniwie przekłada się na dłuższą pracę.
PHOTO_INTERVAL_S = 1.0
# Test z prawdziwym zdjęciem ptaka (kowalik na ekranie, ~8% kadru) dał pewność
# 0,63-0,70 stabilnie przez 8 kolejnych analiz. Przy mniejszym albo częściowo
# zasłoniętym ptaku pewność spadnie, a koszty są bardzo niesymetryczne:
# fałszywa detekcja to jeden klip na karcie (jest miejsce na kilkaset),
# a przegapiona to utrata całego zdarzenia badawczego. Dlatego czułość
# nad precyzję. Wartość do skalibrowania po pierwszej sesji w terenie.
BIRD_CONFIDENCE_THRESHOLD = 0.35

VIDEO_SECONDS = 45              # długość jednego klipu po detekcji
# Sensor daje 30 kl./s; do klipu trafia co N-ta klatka. 15 kl./s to płynny obraz
# zachowania ptaka przy ~1 MB/s zapisu i ~42 MB na 45-sekundowy klip.
SENSOR_FPS = 30
VIDEO_FPS = 15
# Łańcuch nagrań: jeśli ptak był widziany w ostatnich N sekundach klipu, kolejny
# klip startuje w tej samej klatce, w której skończył się poprzedni - bez przerwy.
# Analiza NPU trwa także podczas nagrywania, więc wiemy, czy ptak wciąż jest.
CHAIN_WINDOW_S = 10

# ==========================================
# DFPLAYER MINI (audio przez UART, poza USB)
# ==========================================
DFPLAYER_PORT = "/dev/ttyS3"
DFPLAYER_BAUD = 9600
# Pin 27 złącza to GPIO2_A7 -> sysfs 71. Handoff podawał "pin 27 = GPIO2_A3 = 67",
# co jest sprzeczne z mapą pinów samej płytki (/tmp/.pin_diagram.txt): GPIO2_A3
# leży na pinie 34. Zweryfikowane fizycznie - BUSY jest wpięty w pin 27.
DFPLAYER_BUSY_GPIO = 71         # GPIO2_A7, pin 27 złącza; LOW = gra
# Zakres 0-30. Na 8 dźwięk jest wyraźnie słyszalny, ale cichy - dobre do pracy
# przy biurku. Do terenu trzeba będzie podnieść, bo playback ma nieść w otwartej
# przestrzeni; wartość dobrać na miejscu.
DFPLAYER_VOLUME = 8
TRACKS = [1]                    # numery utworów na karcie DFPlayera (0001.mp3 itd.)

# Awaryjny restart utworu, gdy BUSY ani drgnie - chroni przed zgubioną ramką UART
# albo zawieszonym DFPlayerem. Powinien być dłuższy niż najdłuższy utwór.
PLAY_TIMEOUT_S = 300

# ==========================================
# RTC / ZASILANIE
# ==========================================
I2C_BUS = 3
RTC_ADDR = "0x68"
POWER_OFF_GPIO = 64             # pin OFF przetwornicy Pololu (patrz sleep.sh)

# ==========================================
# KAMERA / NPU
# ==========================================
CAMERA_WIDTH = 2304
CAMERA_HEIGHT = 1296
AE_SETTLE_S = 3                 # ile dać serwerowi auto-ekspozycji na rozjaśnienie kadru

# --- Runtime mediów i NPU: karta SD zamiast NAND-a ---------------------
# Binarki i biblioteki są mapowane z systemu plików, więc każde uruchomienie
# enkodera to seria odczytów z nośnika. NAND pod obciążeniem przestaje je
# oddawać (błędy ECC -> SIGBUS w procesie i rootfs w trybie read-only), a karta
# SD przez całe testy nie zawiodła ani razu. Dlatego to, co uruchamiamy
# wielokrotnie w trakcie okna, trzymamy na karcie. SYSTEM zostaje na NAND.
#
# Kopię zakłada scripts/migrate_runtime.sh. Gdy jej nie ma, wracamy do /oem,
# żeby brak karty nie unieruchomił pułapki.
RUNTIME_ROOT = "/mnt/sdcard/fotopulapka/runtime"


def _runtime_path(relative, fallback):
    """Ścieżka na karcie, jeśli kopia istnieje; w przeciwnym razie oryginał w NAND."""
    candidate = os.path.join(RUNTIME_ROOT, relative)
    return candidate if os.path.exists(candidate) else fallback


MEDIA_LIB_DIR = _runtime_path("lib", "/oem/usr/lib")

RKNPU_KO_PATH = _runtime_path("ko/rknpu.ko", "/oem/usr/ko/rknpu.ko")
RGA_KO_PATH = _runtime_path("ko/rga3.ko", "/oem/usr/ko/rga3.ko")

RKNN_DEMO_BIN = _runtime_path("rknn_yolov5_demo/rknn_yolov5_demo",
                              "/root/rknn_yolov5_demo/rknn_yolov5_demo")
RKNN_MODEL_PATH = _runtime_path("rknn_yolov5_demo/model/yolov5.rknn",
                                "/root/rknn_yolov5_demo/model/yolov5.rknn")
RKNN_LIB_DIR = _runtime_path("rknn_yolov5_demo/lib", "/root/rknn_yolov5_demo/lib")

VENC_JPEG_BIN = _runtime_path("bin/simple_vi_bind_venc_jpeg",
                              "/oem/usr/bin/simple_vi_bind_venc_jpeg")
VENC_VIDEO_BIN = _runtime_path("bin/simple_vi_bind_venc", "/oem/usr/bin/simple_vi_bind_venc")
AE_SERVER_BIN = _runtime_path("bin/rkaiq_3A_server", "/oem/usr/bin/rkaiq_3A_server")
RKLUNCH_STOP = _runtime_path("bin/RkLunch-stop.sh", "/oem/usr/bin/RkLunch-stop.sh")


# ==========================================
# WCZYTYWANIE OKIEN Z KARTY SD
# ==========================================
_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def parse_window(text):
    """'06:00-08:00' -> (360, 480) w minutach od północy. None gdy niepoprawne.

    Okna przechodzące przez północ nie są wspierane - ptaki i tak nie śpiewają
    terytorialnie w nocy, a obsługa zawijania komplikowałaby logikę alarmów.
    """
    match = _WINDOW_RE.match(text.strip())
    if not match:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (0 <= h1 < 24 and 0 <= m1 < 60 and 0 <= h2 < 24 and 0 <= m2 < 60):
        return None
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if end <= start:
        return None
    return (start, end)


def load_windows(path=WINDOWS_FILE):
    """Zwraca listę okien jako (start_min, end_min), posortowaną po godzinie startu.

    Kolejność źródeł: plik na SD -> wbudowane wartości domyślne. Każdy problem
    z plikiem (brak, śmieci, same błędne linie) cofa nas do wartości domyślnych,
    żeby literówka w pliku na karcie nigdy nie unieruchomiła pułapki w terenie.
    """
    windows = []
    try:
        with open(path, "r") as handle:
            for lineno, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                window = parse_window(line)
                if window is None:
                    logging.warning(f"{path}:{lineno}: nie rozumiem okna '{line}' - pomijam.")
                    continue
                windows.append(window)
    except FileNotFoundError:
        logging.info(f"Brak {path} - używam okien domyślnych.")
    except OSError as e:
        logging.error(f"Nie udało się odczytać {path}: {e} - używam okien domyślnych.")

    if not windows:
        windows = [parse_window(f"{s}-{e}") for s, e in WINDOWS_DEFAULT]
    else:
        logging.info(f"Wczytano {len(windows)} okien z {path}.")

    windows.sort()
    if len(windows) > MAX_WINDOWS:
        odrzucone = windows[MAX_WINDOWS:]
        logging.warning(
            f"DS3231 ma tylko {MAX_WINDOWS} alarmy sprzętowe - ignoruję nadmiarowe okna: {odrzucone}"
        )
        windows = windows[:MAX_WINDOWS]
    return windows


def format_window(window):
    """(360, 480) -> '06:00-08:00' - do logów i komunikatów."""
    start, end = window
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
