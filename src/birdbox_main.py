# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
"""Fotopułapka ornitologiczna - pętla "aktywnego wabika".

RTC (DS3231) budzi płytkę na START okna czasowego. Przez CAŁE okno system
pozostaje obudzony: gra bez przerwy śpiew terytorialny z DFPlayera i równolegle
robi zdjęcia analizowane przez NPU. Wykrycie ptaka uruchamia nagranie wideo -
BEZ przerywania playbacku, bo cały eksperyment polega na tym, że "intruz"
śpiewa dalej, kiedy właściciel terytorium przylatuje go sprawdzić.

Po zakończeniu okna alarmy RTC są przestawiane na start następnego okna,
a Pololu odcina zasilanie do zera.

Stara ścieżka PIR leży w pir_legacy.py - nieużywana, zachowana na wypadek
powrotu po testach terenowych.

Zmierzone na płytce (Luckfox Pico Pro Max, 2304x1296):
  zdjęcie JPEG      ~0.23 s   (serwer AE podniesiony raz na całe okno)
  inferencja NPU    ~2.0-3.2 s
  pełny cykl        ~2.7 s    -> PHOTO_INTERVAL_S to dolna granica, nie obietnica
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import config
import rtc
from dfplayer import DFPlayer

# Ile minut przed startem okna wybudzenie wciąż liczy się jako "to okno".
# Alarm RTC pada punktualnie, ale cold boot Linuksa trwa ~15 s, a zegar
# systemowy potrafi się różnić od RTC o kilkadziesiąt sekund.
WINDOW_GRACE_MIN = 3

# Po wysłaniu komendy odtwarzania moduł potrzebuje chwili, zanim zacznie
# meldować "gra". Bez tej karencji pętla widziałaby "stoi" i spamowała komendami.
PLAY_SETTLE_S = 1.5

# Ile razy z rzędu moduł musi zameldować "nie gra", zanim wznowimy playback.
# Pojedynczy odczyt potrafi trafić w moment przewijania na początek pętli.
SILENCE_STRIKES = 2


TEST_MODE = False

# Strumień kamery zostaje włączony po oknie, aż cykl zapisze wszystko, co ważne.
ACTIVE_STREAM = None


# ==========================================
# LOGOWANIE
# ==========================================
def resolve_log_file():
    """Log na karcie SD, pisany na bieżąco; NAND tylko gdy karty brak."""
    try:
        os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
        with open(config.LOG_FILE, "a"):
            pass
        return config.LOG_FILE
    except OSError:
        return config.LOG_FILE_FALLBACK


def setup_timezone():
    """Ustawia strefę czasową procesu, żeby datetime.now() dawało czas lokalny.

    DS3231 trzyma czas lokalny, a okna są zdefiniowane godzinami lokalnymi,
    więc bez tego porównywalibyśmy godziny okien z czasem UTC.
    """
    os.environ["TZ"] = config.TIMEZONE
    time.tzset()


def setup_logging():
    logging.basicConfig(
        filename=resolve_log_file(),
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                           datefmt='%H:%M:%S'))
    logging.getLogger().addHandler(console)


# ==========================================
# SYSTEM
# ==========================================
def get_media_env():
    """Wspólne środowisko z LD_LIBRARY_PATH dla binarek mediów (karta SD lub /oem)."""
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{config.MEDIA_LIB_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    return env


def ensure_clock_sane():
    """Pilnuje, żeby okna czasowe liczyły się od prawdziwej godziny.

    S99birdbox startuje przed synchronizacją NTP, więc zegar systemowy pokazuje
    wtedy 2021 rok - a wtedy KAŻDE wybudzenie wygląda na "poza oknem" i pułapka
    przesypia całą sesję. Dokładnie to widać było w logu z autostartu.

    Ratunkiem jest DS3231 z podtrzymaniem bateryjnym. Czytamy go wprost po I2C,
    bo nie ma sterownika jądra (istnieje tylko /dev/rtc0 - zegar SoC).
    """
    if datetime.now().year >= 2025:
        return True
    logging.warning(f"Zegar systemowy niezsynchronizowany ({datetime.now()}). Czytam DS3231.")
    when = rtc.read_time()
    if when is None or when.year < 2025:
        logging.error(f"DS3231 też nie ma sensownej daty ({when}) - okna mogą być błędne.")
        return False
    result = subprocess.run(["date", "-s", when.strftime("%Y-%m-%d %H:%M:%S")],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logging.error(f"Nie udało się ustawić czasu systemowego: {result.stderr.strip()}")
        return False
    logging.info(f"Czas ustawiony z DS3231: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return True


def resolve_media_dir():
    """Zwraca katalog na zdjęcia i wideo: karta SD, a awaryjnie NAND.

    Zapis na NAND jest ostatecznością - to 181 MB współdzielone z systemem,
    więc lecimy tam tylko po to, żeby nie zgubić materiału, gdy karta odpadnie.
    """
    for candidate, label in ((config.MEDIA_ROOT, "karta SD"),
                             (config.MEDIA_FALLBACK, "NAND (AWARYJNIE)")):
        try:
            os.makedirs(os.path.join(candidate, "photos"), exist_ok=True)
            os.makedirs(os.path.join(candidate, "videos"), exist_ok=True)
            probe = os.path.join(candidate, ".write_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            logging.error(f"Katalog {candidate} nie nadaje się do zapisu ({label}): {e}")
            continue
        if label.startswith("NAND"):
            logging.error(f"UWAGA: karta SD niedostępna, zapisuję na {candidate} - mało miejsca!")
        else:
            logging.info(f"Media trafiają na {candidate} ({label}).")
        return candidate
    logging.critical("Żadna lokalizacja na media nie jest zapisywalna.")
    return None


def lock_present():
    """Ścieżka aktywnej blokady albo None.

    Dwie lokalizacje: /root/blokada (praca przez SSH) oraz plik STOP na karcie
    (terenowe zatrzymanie bez konsoli - wystarczy wyjąć kartę i utworzyć plik).
    """
    for path in (config.LOCK_FILE, config.LOCK_FILE_CARD):
        if os.path.exists(path):
            return path
    return None


def check_safety_lock():
    """Plik blokady pozwala przerwać automatykę bez rozbierania obudowy."""
    lock = lock_present()
    if lock:
        logging.warning(f"Zabezpieczenie aktywne ({lock}). Kończę bez działania.")
        return False
    return True


# ==========================================
# STRAŻNIK ZASILANIA I KRONIKA PRZEBIEGÓW
# ==========================================
POWER_DEADLINE_FILE = "/tmp/power_deadline"


def start_power_watchdog(deadline_dt):
    """Zostawia demonowi ratunkowemu termin, po którym ma odciąć zasilanie.

    Wcześniej był to osobny proces `sh -c "sleep N; sleep.sh"` - ale sleep.sh
    zaczyna od `sync`, a na zablokowanym I/O `sync` wisi w nieskończoność, więc
    do odcięcia zasilania nigdy nie dochodziło. Demon (recoveryd.py) jest cały
    czas w pamięci, nie uruchamia żadnych programów i robi sync z limitem czasu.
    """
    # Termin w zegarze MONOTONICZNYM - odporny na przestawienie czasu ściennego
    # (NTP, DS3231), które inaczej potrafi przesunąć termin o lata.
    remaining = (deadline_dt - datetime.now()).total_seconds() + config.WATCHDOG_MARGIN_S
    deadline = time.monotonic() + remaining
    try:
        os.remove(config.WATCHDOG_DONE_FLAG)
    except OSError:
        pass
    try:
        with open(POWER_DEADLINE_FILE, "w") as f:
            f.write(f"{deadline:.0f}")
    except OSError as e:
        logging.error(f"Nie udało się uzbroić strażnika zasilania: {e}")
        return False
    logging.info(f"Strażnik zasilania uzbrojony: odcięcie za {remaining / 60:.0f} min, "
                 "jeśli cykl się nie domknie.")
    return True


def disarm_power_watchdog():
    """Rozbraja strażnika - wołane, gdy cykl domknął się sam."""
    try:
        with open(config.WATCHDOG_DONE_FLAG, "w") as f:
            f.write("ok")
        logging.info("Strażnik zasilania rozbrojony (cykl zakończony normalnie).")
    except OSError as e:
        logging.warning(f"Nie udało się rozbroić strażnika: {e} - odetnie zasilanie sam.")


HEARTBEAT_FILE = "/tmp/heartbeat"


def beat():
    """Puls dla demona ratunkowego (recoveryd.py).

    Demon restartuje płytkę, gdy puls zamilknie - to jedyny sposób wyjścia
    z zawieszenia, w którym NAND przestaje oddawać strony i nie da się już
    uruchomić ŻADNEGO programu, łącznie z `reboot`.

    Plik leży w tmpfs, więc bicie pulsu nie generuje ruchu na karcie.
    """
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(f"{time.monotonic():.1f}")
    except OSError:
        pass


def reset_recovery_counter():
    """Czysty cykl = system zdrowy, więc demon odzyskuje pełny limit auto-restartów.

    Bez tego po trzech restartach w całym sezonie demon przestałby ratować na zawsze.
    Wołane na końcu cyklu, gdy kamera jest wyłączona i I/O nie jest obciążone.
    """
    try:
        with open("/root/recovery_count", "w") as f:
            f.write("0")
    except OSError:
        pass


def stop_beating():
    """Cisza po czystym zakończeniu - żeby demon nie restartował uśpionej płytki."""
    try:
        os.remove(HEARTBEAT_FILE)
    except OSError:
        pass


def milestone(message):
    """Zapisuje jedną linię WPROST na kartę, z fsync.

    Wołane wyłącznie w chwilach, gdy enkoder nie pracuje (start okna, planowa
    odbudowa potoku, koniec okna) - więc nie ryzykuje kolizji, która wywraca tor mmc.
    To jedyny ślad, jaki zostanie, jeśli płytka zawiesi się w środku okna:
    szczegółowy log siedzi wtedy jeszcze w RAM-ie i przepadnie.
    """
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
    try:
        os.makedirs(os.path.dirname(config.MILESTONE_FILE), exist_ok=True)
        with open(config.MILESTONE_FILE, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logging.warning(f"Nie udało się zapisać kamienia milowego: {e}")


def read_state():
    try:
        with open(config.STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(**changes):
    """Kronika na karcie: ile było cykli i który skończył się nieczysto."""
    state = read_state()
    state.update(changes)
    try:
        os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
        tmp = config.STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.STATE_FILE)
    except OSError as e:
        logging.warning(f"Nie udało się zapisać kroniki: {e}")
    return state


def go_to_sleep():
    logging.info("Cykl zakończony. Odcinanie zasilania.")
    os.sync()
    subprocess.run([config.SLEEP_SCRIPT], check=False)


# ==========================================
# NPU
# ==========================================
def _module_loaded(module_name):
    try:
        result = subprocess.run(["lsmod"], capture_output=True, text=True, check=True)
        return any(line.split()[0] == module_name
                   for line in result.stdout.splitlines()[1:] if line.strip())
    except Exception:
        return False


def ensure_npu_ready():
    """Ładuje moduły jądra wymagane przez NPU (rknpu) i akcelerator obrazu (rga).

    Idempotentne - bezpieczne przy każdym starcie, nawet jeśli moduły już siedzą.
    """
    for name, path in (("rknpu", config.RKNPU_KO_PATH), ("rga3", config.RGA_KO_PATH)):
        if _module_loaded(name):
            logging.info(f"Moduł {name} już załadowany.")
            continue
        logging.info(f"Ładowanie modułu {name} ({path})...")
        result = subprocess.run(["insmod", path], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logging.error(f"insmod {path} nie powiodło się: {result.stderr.strip()}")
        else:
            logging.info(f"Moduł {name} załadowany poprawnie.")


# Dopasowuje linie w formacie: "bird @ (109 238 231 534) 0.840"
_DETECTION_LINE_RE = re.compile(r'^([a-zA-Z_ ]+)\s*@\s*\([\d\s]+\)\s*([\d.]+)\s*$')


def prepare_npu_workdir():
    """Katalog roboczy demo RKNN w RAM-ie.

    Demo czyta etykiety z `./model/coco_80_labels_list.txt` (względem katalogu
    roboczego) i przy KAŻDEJ inferencji zapisuje `out.png` (~460 kB) do tego samego
    katalogu. Uruchamiane z katalogu na NAND-zie dawało ~640 MB zapisów do flasha
    na dwugodzinne okno. Link `model` wskazuje na oryginał, a out.png ląduje w tmpfs.
    """
    os.makedirs(config.NPU_WORKDIR, exist_ok=True)
    link = os.path.join(config.NPU_WORKDIR, "model")
    target = os.path.join(os.path.dirname(config.RKNN_DEMO_BIN), "model")
    if not os.path.lexists(link):
        os.symlink(target, link)


def analyze_image_for_bird(image_path):
    """Odpala demo RKNN na NPU i szuka klasy 'bird' powyżej progu.

    Zwraca (is_bird, best_confidence). best_confidence to najwyższa pewność
    dla klasy 'bird' nawet poniżej progu (albo None, gdy 'bird' nie wystąpił) -
    dzięki temu w logach widać, jak blisko progu ociera się detekcja.
    """
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = config.RKNN_LIB_DIR
    try:
        result = subprocess.run(
            [config.RKNN_DEMO_BIN, config.RKNN_MODEL_PATH, image_path],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=config.NPU_WORKDIR,
        )
    except subprocess.TimeoutExpired:
        logging.error("Inferencja NPU przekroczyła timeout.")
        return False, None
    except FileNotFoundError:
        logging.error(f"Nie znaleziono binarki demo: {config.RKNN_DEMO_BIN}")
        return False, None

    if result.returncode != 0:
        logging.error(f"rknn_yolov5_demo błąd (kod {result.returncode}): {result.stderr.strip()[:200]}")
        return False, None

    best = None
    for line in result.stdout.splitlines():
        match = _DETECTION_LINE_RE.match(line.strip())
        if not match:
            continue
        if match.group(1).strip() == "bird":
            confidence = float(match.group(2))
            if best is None or confidence > best:
                best = confidence

    is_bird = best is not None and best >= config.BIRD_CONFIDENCE_THRESHOLD
    if best is not None and not is_bird:
        logging.info(f"'bird' wykryty, ale poniżej progu ({best:.3f} < {config.BIRD_CONFIDENCE_THRESHOLD}).")
    return is_bird, best


# ==========================================
# KAMERA - JEDEN CIĄGŁY STRUMIEŃ NA CAŁE OKNO
# ==========================================
def start_ae_server():
    """Podnosi serwer auto-ekspozycji. Działa przez całe okno, razem ze strumieniem."""
    logging.info("Start rkaiq_3A_server (auto-ekspozycja)...")
    subprocess.run([config.RKLUNCH_STOP], check=False, capture_output=True)
    subprocess.run(["killall", "rkipc", "rkaiq_3A_server"], check=False, capture_output=True)
    time.sleep(1)
    proc = subprocess.Popen([config.AE_SERVER_BIN], env=get_media_env(),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(config.AE_SETTLE_S)
    return proc


def stop_ae_server(proc):
    logging.info("Zatrzymywanie rkaiq_3A_server...")
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    subprocess.run(["killall", "rkaiq_3A_server"], check=False, capture_output=True)


_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


class CameraStream:
    """Potok kamery uruchamiany RAZ na całe okno; klatki czytane na bieżąco.

    Dlaczego tak: bisekcja na sprzęcie wykazała, że WIELOKROTNE uruchamianie
    potoku VI/ISP/VENC po ~20 cyklach blokuje I/O całego systemu (najpierw tor
    karty SD, potem NAND z rootfs), niezależnie od nośnika i od NPU. Jeden ciągły
    strumień był czysty przez 250 s, również przy równoczesnym zapisie 203 MB na
    kartę. Stara pętla uruchamiała enkoder przy KAŻDYM zdjęciu - czyli sama
    wywoływała awarię.

    Enkoder pisze strumień MJPEG do potoku nazwanego (FIFO), a wątek czytnika tnie
    go na klatki (granice SOI/EOI) i trzyma w pamięci tylko ostatnią. Zero plików
    pośrednich, zero rozruchów w trakcie okna. Zmierzone: 30 kl./s, ~63 kB/klatkę.

    Nagrywanie odbywa się w tym samym wątku, z tego samego strumienia: do klipu
    trafia co N-ta klatka. Dzięki temu analiza NPU trwa także podczas nagrywania,
    a kolejny klip w łańcuchu startuje w tej samej klatce, w której skończył się
    poprzedni.
    """

    def __init__(self, videos_dir):
        self.videos_dir = videos_dir
        self.ae_proc = None
        self.enc = None
        self.thread = None
        self.restarts = 0
        self.frames = 0
        self._lock = threading.Lock()
        self._frame = None
        self._frame_ts = 0.0
        self._stop = threading.Event()
        # nagrywanie - stan dzielony z wątkiem czytnika
        self._rec_file = None
        self._rec_path = None
        self._rec_deadline = 0.0
        self._rec_counter = 0
        self._rec_every = max(1, round(config.SENSOR_FPS / max(1, config.VIDEO_FPS)))
        self._last_bird_ts = 0.0
        self.clips = []

    # --- cykl życia ---------------------------------------------------------
    def start(self):
        """Uruchamia serwer AE i enkoder, czeka na pierwszą klatkę."""
        subprocess.run(["killall", os.path.basename(config.VENC_VIDEO_BIN),
                        os.path.basename(config.VENC_JPEG_BIN)], check=False, capture_output=True)
        self.ae_proc = start_ae_server()
        try:
            if os.path.lexists(config.CAMERA_FIFO):
                os.remove(config.CAMERA_FIFO)
            os.mkfifo(config.CAMERA_FIFO)
        except OSError as e:
            logging.error(f"Nie udało się utworzyć potoku {config.CAMERA_FIFO}: {e}")
            return False

        logging.info("Start ciągłego strumienia MJPEG (jeden rozruch potoku na całe okno)...")
        self.enc = subprocess.Popen(
            [config.VENC_VIDEO_BIN, "-w", str(config.CAMERA_WIDTH), "-h", str(config.CAMERA_HEIGHT),
             "-I", "0", "-e", "mjpeg", "-o", config.CAMERA_FIFO],
            env=get_media_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._stop.clear()
        self.thread = threading.Thread(target=self._reader, name="camera-reader", daemon=True)
        self.thread.start()

        deadline = time.monotonic() + config.STREAM_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.frames > 0:
                logging.info(f"Strumień działa (pierwsza klatka po "
                             f"{config.STREAM_START_TIMEOUT_S - (deadline - time.monotonic()):.1f}s).")
                return True
            if self.enc.poll() is not None:
                break
            time.sleep(0.2)
        logging.error("Strumień kamery nie dostarczył żadnej klatki.")
        return False

    def stop(self):
        self.stop_recording()
        self._stop.set()
        if self.enc is not None:
            self.enc.terminate()
            try:
                self.enc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.enc.kill()
            self.enc = None
        subprocess.run(["killall", os.path.basename(config.VENC_VIDEO_BIN)],
                       check=False, capture_output=True)
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        stop_ae_server(self.ae_proc)
        self.ae_proc = None
        try:
            os.remove(config.CAMERA_FIFO)
        except OSError:
            pass

    def restart(self):
        """Pełny restart strumienia. Liczony, bo każdy rozruch potoku to ryzyko."""
        self.restarts += 1
        logging.warning(f"Restart strumienia kamery ({self.restarts}/{config.CAMERA_RESTART_LIMIT}).")
        self.stop()
        time.sleep(2)
        return self.start()

    # --- stan -----------------------------------------------------------------
    def healthy(self):
        if self.enc is None or self.enc.poll() is not None:
            return False
        with self._lock:
            age = time.monotonic() - self._frame_ts
        return age < config.STREAM_STALL_S

    def latest(self, max_age_s=2.0):
        """Najświeższa kompletna klatka JPEG albo None, gdy strumień stoi."""
        with self._lock:
            if self._frame is None or time.monotonic() - self._frame_ts > max_age_s:
                return None
            return self._frame

    @property
    def recording(self):
        with self._lock:
            return self._rec_file is not None

    # --- nagrywanie -----------------------------------------------------------
    def note_bird(self):
        """Wołane przy każdej pozytywnej detekcji - decyduje o łańcuchu klipów."""
        with self._lock:
            self._last_bird_ts = time.monotonic()

    def start_recording(self):
        with self._lock:
            if self._rec_file is not None:
                return self._rec_path
            return self._open_clip_locked()

    def _open_clip_locked(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.videos_dir, f"bird_{stamp}.mjpeg")
        try:
            self._rec_file = open(path, "wb")
        except OSError as e:
            logging.error(f"Nie mogę otworzyć pliku nagrania {path}: {e}")
            self._rec_file = None
            return None
        self._rec_path = path
        self._rec_deadline = time.monotonic() + config.VIDEO_SECONDS
        self._rec_counter = 0
        logging.info(f"NAGRYWANIE START: {path} ({config.VIDEO_SECONDS}s, {config.VIDEO_FPS} kl./s)")
        return path

    def _close_clip_locked(self):
        if self._rec_file is None:
            return
        try:
            self._rec_file.flush()
            os.fsync(self._rec_file.fileno())
            self._rec_file.close()
            size = os.path.getsize(self._rec_path)
            self.clips.append(self._rec_path)
            logging.info(f"NAGRYWANIE KONIEC: {self._rec_path} ({size // 1024} kB)")
        except OSError as e:
            logging.error(f"Błąd przy zamykaniu nagrania {self._rec_path}: {e}")
        self._rec_file = None
        self._rec_path = None

    def stop_recording(self):
        with self._lock:
            self._close_clip_locked()

    # --- wątek czytnika -------------------------------------------------------
    def _handle_frame(self, frame):
        with self._lock:
            self._frame = frame
            self._frame_ts = time.monotonic()
            self.frames += 1
            if self._rec_file is None:
                return
            self._rec_counter += 1
            if self._rec_counter % self._rec_every == 0:
                try:
                    self._rec_file.write(frame)
                except OSError as e:
                    logging.error(f"Zapis klatki nagrania nie powiódł się: {e}")
                    self._close_clip_locked()
                    return
            if time.monotonic() >= self._rec_deadline:
                bird_recent = time.monotonic() - self._last_bird_ts <= config.CHAIN_WINDOW_S
                self._close_clip_locked()
                if bird_recent:
                    logging.info("Ptak wciąż w kadrze - kolejny klip w łańcuchu, bez przerwy.")
                    self._open_clip_locked()

    def _reader(self):
        buf = b""
        try:
            with open(config.CAMERA_FIFO, "rb", buffering=0) as fifo:
                while not self._stop.is_set():
                    chunk = fifo.read(262144)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(_JPEG_SOI)
                        if start < 0:
                            buf = b""
                            break
                        end = buf.find(_JPEG_EOI, start + 3)
                        if end < 0:
                            buf = buf[start:]
                            break
                        self._handle_frame(buf[start:end + 2])
                        buf = buf[end + 2:]
        except Exception as e:
            logging.error(f"Czytnik strumienia kamery zakończony błędem: {e}")
        logging.info("Czytnik strumienia kamery zakończył pracę.")


# ==========================================
# OKNA CZASOWE
# ==========================================
def find_active_window(windows, now=None):
    """Zwraca okno obejmujące teraz, uwzględniając karencję na czas bootu."""
    now = now or datetime.now()
    now_min = now.hour * 60 + now.minute
    for window in windows:
        start, end = window
        if start - WINDOW_GRACE_MIN <= now_min < end:
            return window
    return None


def window_end_datetime(window, now=None):
    now = now or datetime.now()
    end_h, end_m = divmod(window[1], 60)
    end = now.replace(hour=end_h % 24, minute=end_m, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


def next_window_start(windows, now=None):
    """Najbliższy start okna - do komunikatu w logu (budzik i tak jest sprzętowy)."""
    now = now or datetime.now()
    now_min = now.hour * 60 + now.minute
    for start, _ in windows:
        if start > now_min:
            return now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    start = windows[0][0]
    return (now + timedelta(days=1)).replace(hour=start // 60, minute=start % 60,
                                             second=0, microsecond=0)


# ==========================================
# PĘTLA OKNA
# ==========================================
def save_jpeg(frame, path):
    """Zapisuje klatkę JPEG na kartę. Błąd zapisu nie przerywa obserwacji."""
    try:
        with open(path, "wb") as f:
            f.write(frame)
        return True
    except OSError as e:
        logging.error(f"Nie udało się zapisać kadru {path}: {e}")
        return False


def keep_playback(player, state):
    """Pilnuje, żeby playback grał nieprzerwanie. Bez DFPlayera - nic nie robi."""
    if player is None:
        return
    since_play = time.monotonic() - state["last_play_ts"]
    if since_play > PLAY_SETTLE_S:
        if player.is_busy():
            state["silence_strikes"] = 0
        else:
            state["silence_strikes"] += 1
            if state["silence_strikes"] >= SILENCE_STRIKES:
                logging.warning(f"Moduł milczy {state['silence_strikes']} odczyty z rzędu "
                                "- wznawiam pętlę utworu.")
                player.loop_track(state["track"])
                state["last_play_ts"] = time.monotonic()
                state["silence_strikes"] = 0
    if since_play > config.PLAY_TIMEOUT_S:
        logging.info("Odświeżam komendę pętli (timeout zapasowy).")
        player.loop_track(state["track"])
        state["last_play_ts"] = time.monotonic()
    player.drain_responses()


def parse_simulation(spec):
    """'300-450' -> (300, 450) sekund od startu okna; None gdy brak lub błąd."""
    if not spec:
        return None
    try:
        start, end = (int(x) for x in spec.split("-", 1))
        return (start, end) if end > start >= 0 else None
    except ValueError:
        logging.error(f"Niepoprawny zakres --simulate-bird: {spec!r} - ignoruję.")
        return None


def run_window(end_dt, media_dir, simulate=None):
    """Playback + ciągła obserwacja kamerą i NPU aż do końca okna."""
    logging.info(f"=== OKNO AKTYWNE do {end_dt:%H:%M:%S} ===")
    milestone(f"OKNO START, koniec o {end_dt:%H:%M:%S}")

    photos_dir = os.path.join(media_dir, "photos")
    videos_dir = os.path.join(media_dir, "videos")
    prepare_npu_workdir()
    frame_path = os.path.join(config.NPU_WORKDIR, "frame.jpg")

    # Brak audio psuje eksperyment, ale NIE jest powodem do rezygnacji z okna:
    # kamera i NPU nadal zbierają materiał, a pułapka zostaje w terenie użyteczna.
    try:
        player = DFPlayer()
    except Exception as e:
        logging.error(f"DFPlayer niedostępny ({e}) - okno pojedzie BEZ playbacku.")
        player = None

    stream = CameraStream(videos_dir)
    analyses = detections = 0
    try:
        # Świadomie NIE wołamy player.reset(): komenda 0x0C przemontowuje kartę w module
        # i to właśnie na niej płytka raz się zresetowała (skok poboru prądu).
        if player is not None:
            player.set_volume(config.DFPLAYER_VOLUME)
            files = player.query_file_count()
            if files is None:
                logging.warning("DFPlayer nie odpowiada na zapytania - playback może nie ruszyć.")
            else:
                logging.info(f"DFPlayer widzi {files} plików na swojej karcie.")
                if max(config.TRACKS) > files:
                    logging.error(f"Skonfigurowano utwór {max(config.TRACKS)}, "
                                  f"a na karcie DFPlayera jest tylko {files}.")
            player.probe_busy_pin()

        camera_ok = stream.start()
        if not camera_ok:
            camera_ok = stream.restart()
        if not camera_ok:
            logging.critical("Kamera nie wstała - okno pojedzie z samym playbackiem.")
            milestone("KAMERA NIE WSTALA - sam playback")

        # Zapętlenie po stronie modułu: dźwięk nie ma dziury nawet wtedy, gdy
        # Linux akurat liczy inferencję NPU. Pętla poniżej tylko tego pilnuje.
        play = {"track": config.TRACKS[0], "last_play_ts": time.monotonic(), "silence_strikes": 0}
        if player is not None:
            player.loop_track(play["track"])

        window_start = time.monotonic()
        while datetime.now() < end_dt:
            cycle_start = time.monotonic()
            beat()

            lock = lock_present()
            if lock:
                logging.warning(f"Blokada aktywowana w trakcie okna ({lock}) - przerywam.")
                break

            try:
                keep_playback(player, play)
            except Exception as e:
                logging.error(f"Błąd sterowania playbackiem: {e}")

            # --- kamera: tylko pilnujemy, żeby strumień żył ---
            if camera_ok and not stream.healthy():
                logging.error("Strumień kamery przestał dostarczać klatki.")
                if stream.restarts < config.CAMERA_RESTART_LIMIT:
                    camera_ok = stream.restart()
                    milestone(f"restart strumienia kamery {stream.restarts}: "
                              f"{'OK' if camera_ok else 'NIEUDANY'}")
                else:
                    camera_ok = False
                    logging.critical("Limit restartów strumienia wyczerpany - dalej sam playback.")
                    milestone("LIMIT RESTARTOW KAMERY - sam playback")
            if not camera_ok:
                time.sleep(5)
                continue

            frame = stream.latest(max_age_s=2.0)
            if frame is None:
                time.sleep(0.2)
                continue

            # --- analiza NPU na najświeższej klatce ---
            try:
                with open(frame_path, "wb") as f:
                    f.write(frame)
                is_bird, confidence = analyze_image_for_bird(frame_path)
            except Exception as e:
                logging.error(f"Analiza klatki nie powiodła się: {e}")
                time.sleep(1)
                continue
            if simulate and simulate[0] <= time.monotonic() - window_start < simulate[1]:
                is_bird, confidence = True, 1.0
                logging.info("[SYMULACJA] detekcja wymuszona przełącznikiem --simulate-bird")
            analyses += 1
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            conf_txt = f"{confidence:.3f}" if confidence is not None else "brak"
            logging.info(f"[{analyses}] bird={is_bird} pewność={conf_txt} "
                         f"klatek_ze_strumienia={stream.frames}"
                         + (" [NAGRYWANIE]" if stream.recording else ""))

            if is_bird:
                detections += 1
                stream.note_bird()
                if not stream.recording:
                    # Kadr zapisujemy tylko przy STARCIE nagrania: to zdjęcie jest
                    # materiałem podglądowym, a całe zdarzenie i tak jest w klipie.
                    # Przy ~1 analizie/s zapisywanie każdej detekcji dałoby kilkadziesiąt
                    # niemal identycznych plików na jeden przylot.
                    logging.info("DETEKCJA POZYTYWNA - start nagrania, playback gra dalej.")
                    save_jpeg(frame, os.path.join(photos_dir, f"bird_{stamp}.jpg"))
                    stream.start_recording()
            elif TEST_MODE and analyses % config.TEST_PHOTO_EVERY == 0:
                save_jpeg(frame, os.path.join(photos_dir, f"kadr_{stamp}.jpg"))

            if analyses % config.MILESTONE_EVERY == 0:
                milestone(f"zyje: {analyses} analiz, {detections} detekcji, "
                          f"{len(stream.clips)} klipow, {stream.frames} klatek strumienia")

            elapsed = time.monotonic() - cycle_start
            if elapsed < config.PHOTO_INTERVAL_S:
                time.sleep(config.PHOTO_INTERVAL_S - elapsed)

        logging.info(f"=== KONIEC OKNA: {analyses} analiz, {detections} detekcji, "
                     f"{len(stream.clips)} klipów ===")
        milestone(f"OKNO KONIEC: {analyses} analiz, {detections} detekcji, {len(stream.clips)} klipow")
    finally:
        # Świadomie NIE zatrzymujemy tu potoku kamery. Zatrzymanie potoku to ta sama
        # operacja, która po wielu powtórzeniach blokuje I/O - i w oknie 60-minutowym
        # zablokowała system właśnie w tym miejscu, przed zapisaniem kroniki i RTC.
        # Kamera gaśnie dopiero na samym końcu cyklu (albo razem z zasilaniem).
        try:
            stream.stop_recording()
        except Exception as e:
            logging.error(f"Problem przy zamykaniu nagrania: {e}")
        try:
            if player is not None:
                player.stop()
                player.close()
        except Exception as e:
            logging.error(f"Problem przy zamykaniu DFPlayera: {e}")
        global ACTIVE_STREAM
        ACTIVE_STREAM = stream


# ==========================================
# MAIN
# ==========================================
def parse_args():
    parser = argparse.ArgumentParser(description="Fotopułapka - pętla aktywnego wabika")
    parser.add_argument("--test", action="store_true",
                        help="Nie odcina zasilania na końcu i zachowuje WSZYSTKIE klatki, "
                             "także te bez ptaka - do oceny naświetlenia i progu detekcji.")
    parser.add_argument("--window", type=int, default=None, metavar="SEK",
                        help="Skraca okno do podanej liczby sekund, licząc od teraz. "
                             "Do testów na stole zamiast czekania dwóch godzin.")
    parser.add_argument("--force-window", action="store_true",
                        help="Uruchamia okno nawet poza harmonogramem.")
    parser.add_argument("--simulate-bird", default=None, metavar="OD-DO",
                        help="Test nagrywania bez ptaka: w przedziale sekund od startu okna "
                             "(np. 300-450) każda analiza jest traktowana jak detekcja. "
                             "WYŁĄCZNIE do testów na stole.")
    parser.add_argument("--ignore-rtc", action="store_true",
                        help="Nie przerywa cyklu, gdy DS3231 nie odpowiada. WYŁĄCZNIE do testów "
                             "na stole - w terenie brak RTC oznacza brak następnego wybudzenia.")
    return parser.parse_args()


def main():
    global TEST_MODE
    args = parse_args()
    TEST_MODE = args.test

    setup_timezone()
    setup_logging()
    beat()
    logging.info("=== START SYSTEMU ===" + (" [TRYB TESTOWY]" if TEST_MODE else ""))

    if not check_safety_lock():
        return

    clock_ok = ensure_clock_sane()

    windows = config.load_windows()
    logging.info("Okna czasowe: " + ", ".join(config.format_window(w) for w in windows))

    # --- ALARMY OD RAZU -------------------------------------------------
    # Programujemy budzik ZANIM ruszy kamera. Nawet jeśli okno padnie w środku
    # albo płytka się zawiesi, następne wybudzenie jest już ustawione i globalna
    # pętla RTC -> atraktor -> uśpienie -> RTC nigdy się nie rwie.
    rtc.sync_to_rtc()
    if not rtc.program_windows(windows):
        logging.error("Nie udało się zaprogramować alarmów na starcie - spróbuję ponownie na końcu.")
    if not rtc.clear_alarm_flags():
        if args.ignore_rtc:
            logging.warning("RTC nie odpowiada, ale --ignore-rtc: jadę dalej. "
                            "TYLKO DO TESTÓW NA STOLE.")
        elif clock_ok:
            # Czas znamy (z DS3231 albo NTP), więc okno da się wyznaczyć i materiał
            # zebrać. Nieskasowana flaga nie jest powodem, żeby go tracić - spróbujemy
            # ją skasować ponownie przy domknięciu cyklu, zanim zapadnie decyzja o uśpieniu.
            logging.error("Flaga alarmu RTC nie zeszła, ale czas jest znany - "
                          "okno pojedzie, ponowna próba przy domknięciu.")
            milestone("RTC niepewne: flaga nie zeszla na starcie, okno mimo to")
        else:
            logging.critical("RTC nie odpowiada i czas jest nieznany - nie da się wyznaczyć "
                             "okna. Usypiam awaryjnie.")
            milestone("RTC NIE ODPOWIADA, czas nieznany - uspienie awaryjne")
            if not TEST_MODE:
                go_to_sleep()
            return

    # --- kronika: czy poprzedni cykl domknął się czysto ------------------
    previous = read_state()
    boots = int(previous.get("boots", 0)) + 1
    if previous.get("clean_exit") is False:
        logging.warning(f"Poprzedni cykl ({previous.get('last_window')}) NIE domknął się czysto "
                        "- prawdopodobnie zadziałał strażnik albo zabrakło zasilania.")
    write_state(boots=boots, clean_exit=False, last_boot=datetime.now().isoformat(timespec="seconds"))

    try:
        ensure_npu_ready()
    except Exception:
        logging.exception("Ładowanie modułów NPU nie powiodło się - detekcja może nie działać.")

    media_dir = resolve_media_dir()
    if media_dir is None:
        logging.critical("Brak miejsca na zapis - kończę bez obserwacji.")
        write_state(clean_exit=True, last_result="brak nosnika")
        if not TEST_MODE:
            go_to_sleep()
        return

    window = find_active_window(windows)
    end_dt = None
    if args.window is not None:
        end_dt = datetime.now() + timedelta(seconds=args.window)
        logging.info(f"Okno skrócone argumentem --window do {args.window}s (koniec {end_dt:%H:%M:%S}).")
    elif window is not None:
        end_dt = window_end_datetime(window)
    elif args.force_window:
        logging.warning("Poza harmonogramem, ale --force-window - okno domyślnej długości.")
        end_dt = datetime.now() + timedelta(minutes=30)
    else:
        logging.info(f"Wybudzenie poza oknem ({datetime.now():%H:%M}). Nic nie robię.")

    if end_dt is not None:
        if not TEST_MODE:
            start_power_watchdog(end_dt)
        write_state(last_window=f"{datetime.now():%Y-%m-%d %H:%M}-{end_dt:%H:%M}")
        try:
            run_window(end_dt, media_dir, simulate=parse_simulation(args.simulate_bird))
        except Exception:
            # Okno mogło paść, ale cykl MUSI się domknąć: alarmy odświeżone,
            # bufor zrzucony, zasilanie odcięte. Inaczej pułapka nie wstanie jutro.
            logging.exception("Okno zakończone wyjątkiem - domykam cykl mimo to.")

    # --- domknięcie cyklu ------------------------------------------------
    for attempt in range(3):
        if rtc.program_windows(windows) and rtc.clear_alarm_flags():
            break
        logging.warning(f"Domykanie RTC nie powiodło się (próba {attempt + 1}/3).")
        time.sleep(1)
    logging.info(f"Następne okno startuje {next_window_start(windows):%Y-%m-%d %H:%M}.")
    stats = rtc.retry_stats()
    if stats["retried"] or stats["failed"]:
        logging.warning(f"Magistrala RTC w tym cyklu: {stats['retried']} operacji wymagało "
                        f"ponowień, {stats['failed']} zawiodło mimo ponowień.")
        milestone(f"RTC: ponowienia={stats['retried']} porazki={stats['failed']}")

    write_state(clean_exit=True, last_end=datetime.now().isoformat(timespec="seconds"))
    milestone("CYKL DOMKNIETY, usypiam")
    reset_recovery_counter()
    os.sync()

    # Dopiero teraz, gdy wszystko jest zapisane, dotykamy potoku kamery. W terenie
    # nie trzeba go zatrzymywać wcale - zasilanie zniknie razem z nim.
    if TEST_MODE and ACTIVE_STREAM is not None:
        logging.info("[TEST] Zatrzymuję kamerę po zapisaniu cyklu.")
        ACTIVE_STREAM.stop()
    stop_beating()

    if TEST_MODE:
        logging.info("[TEST] Pomijam go_to_sleep() - zasilanie zostaje.")
        os.sync()
    else:
        # Strażnik zostaje UZBROJONY na czas usypiania: jeśli sleep.sh zawiśnie na
        # `sync`, demon odetnie zasilanie sam. Rozbrajamy go dopiero, gdy wróciliśmy
        # z go_to_sleep() - czyli zasilanie nie idzie przez Pololu (np. stół).
        go_to_sleep()
        disarm_power_watchdog()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Nieobsłużony błąd w main().")
        # Ostatnia deska ratunku: choćby wszystko inne zawiodło, alarm na następne
        # okno ma być ustawiony, a zasilanie odcięte.
        try:
            rtc.program_windows(config.load_windows())
            rtc.clear_alarm_flags()
        except Exception:
            logging.exception("Nie udało się nawet awaryjnie zaprogramować RTC.")
        if TEST_MODE:
            logging.info("[TEST] Pomijam go_to_sleep() mimo wyjątku.")
        else:
            go_to_sleep()
    finally:
        # Każde wyjście z programu - normalne, awaryjne (np. martwy RTC) czy przez
        # wyjątek - zdejmuje puls. Zostaje on tylko wtedy, gdy program ZAWISŁ,
        # i właśnie wtedy demon ma zareagować. Dziś awaryjna ścieżka RTC zostawiała
        # puls na płytce, co przy skoku zegara dało pętlę trzech restartów.
        stop_beating()
