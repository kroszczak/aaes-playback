"""Sterowanie modułem DFPlayer Mini przez UART + GPIO.

Audio jest tu celowo odseparowane od USB: w RV1106 jeden fizyczny kontroler
USB OTG obsługiwał jednocześnie audio izochroniczne i transfer danych, co dawało
słyszalne trzaski dokładnie w momencie robienia zdjęcia. DFPlayer ma własną kartę
SD i gada z płytką po UART3, więc kamera i NPU nie mają jak mu przeszkodzić.

Stan odtwarzania czytamy z ZAPYTANIA UART 0x42, nie z pinu BUSY. Pierwotny
plan zakładał odwrotnie, ale pomiar na sprzęcie to wywrócił: BUSY czyta stałe
LOW niezależnie od tego, czy moduł gra, czy stoi (sprawdzone na obu kandydatach
na ten pin - sysfs 71 i 67). UART odpowiada za to bezbłędnie i powtarzalnie,
więc to on jest źródłem prawdy. Pin BUSY został jako opcjonalne przyspieszenie:
włącza się sam, jeśli kiedykolwiek pokaże stan wysoki.
"""

import logging
import os
import time

import serial

import config

# Protokół DFPlayer: ramka 10-bajtowa
# 7E FF 06 <cmd> <feedback> <param_hi> <param_lo> <ck_hi> <ck_lo> EF
_START = 0x7E
_VERSION = 0xFF
_LEN = 0x06
_END = 0xEF

CMD_NEXT = 0x01
CMD_PREV = 0x02
CMD_PLAY_TRACK = 0x03
CMD_VOLUME_UP = 0x04
CMD_VOLUME_DOWN = 0x05
CMD_SET_VOLUME = 0x06
CMD_RESET = 0x0C
CMD_PLAY = 0x0D
CMD_PAUSE = 0x0E
CMD_LOOP_TRACK = 0x08     # zapętlenie POJEDYNCZEGO utworu po stronie modułu
CMD_STOP = 0x16
CMD_QUERY_STATUS = 0x42
CMD_QUERY_VERSION = 0x46
CMD_QUERY_FILES = 0x48

# Odpowiedź na 0x42 niesie stan w ostatnim bajcie parametru.
STATUS_STOPPED, STATUS_PLAYING, STATUS_PAUSED = 0, 1, 2
_STATUS_NAMES = {STATUS_STOPPED: "stop", STATUS_PLAYING: "gra", STATUS_PAUSED: "pauza"}

# Odstęp między kolejnymi ramkami - DFPlayer gubi komendy wysyłane plecami.
_INTER_COMMAND_S = 0.1


def build_frame(cmd, param=0, feedback=0):
    """Buduje 10-bajtową ramkę z LICZONĄ sumą kontrolną.

    Suma to dopełnienie do dwóch z bajtów 1..6 (bez bajtu startu i bez samej sumy):
        checksum = -(VER + LEN + CMD + FEEDBACK + PARAM_HI + PARAM_LO)

    Liczymy ją, zamiast wklejać gotowe ramki z dokumentacji - w krążących po
    internecie tabelkach są błędy. Przykład: często cytowana ramka "stop"
    `7E FF 06 16 00 00 00 FE EA EF` ma złą sumę; poprawna końcówka to FE E5.
    """
    hi, lo = (param >> 8) & 0xFF, param & 0xFF
    checksum = -(_VERSION + _LEN + cmd + feedback + hi + lo) & 0xFFFF
    return bytes([
        _START, _VERSION, _LEN, cmd, feedback, hi, lo,
        (checksum >> 8) & 0xFF, checksum & 0xFF, _END,
    ])


class BusyPin:
    """Pin BUSY DFPlayera przez sysfs (LOW = gra, HIGH = cisza).

    Ta sama konwencja eksportu co dotychczasowy odczyt PIR-a: obsługujemy
    przypadek "już wyeksportowany przez kogoś innego", bo po miękkim restarcie
    skryptu katalog gpio zostaje w systemie.
    """

    def __init__(self, pin=config.DFPLAYER_BUSY_GPIO):
        self.pin = pin
        self.path = f"/sys/class/gpio/gpio{pin}"
        self._ready = False

    def _ensure_exported(self):
        if self._ready:
            return True
        if not os.path.exists(self.path):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(self.pin))
            except OSError as e:
                if not os.path.exists(self.path):
                    logging.error(f"Nie udało się wyeksportować GPIO{self.pin} (BUSY): {e}")
                    return False
        try:
            with open(f"{self.path}/direction", "w") as f:
                f.write("in")
        except OSError as e:
            logging.error(f"Nie udało się ustawić kierunku GPIO{self.pin}: {e}")
            return False
        self._ready = True
        return True

    def is_playing(self):
        """True gdy DFPlayer gra. None gdy pinu nie da się odczytać."""
        if not self._ensure_exported():
            return None
        try:
            with open(f"{self.path}/value", "r") as f:
                return f.read().strip() == "0"
        except OSError as e:
            logging.error(f"Błąd odczytu GPIO{self.pin} (BUSY): {e}")
            return None


class DFPlayer:
    def __init__(self, port=config.DFPLAYER_PORT, baud=config.DFPLAYER_BAUD,
                 busy_gpio=config.DFPLAYER_BUSY_GPIO):
        self.serial = serial.Serial(port, baud, timeout=0, write_timeout=1)
        self.busy = BusyPin(busy_gpio)
        self._last_command_ts = 0.0
        self._busy_pin_usable = False
        logging.info(f"DFPlayer: otwarty {port} @ {baud} 8N1, BUSY na GPIO{busy_gpio}.")

    # --- komunikacja ---------------------------------------------------
    def _send(self, cmd, param=0):
        frame = build_frame(cmd, param)
        gap = _INTER_COMMAND_S - (time.monotonic() - self._last_command_ts)
        if gap > 0:
            time.sleep(gap)
        try:
            self.serial.write(frame)
            self.serial.flush()
        except serial.SerialException as e:
            logging.error(f"DFPlayer: zapis na UART nie powiódł się: {e}")
            return False
        self._last_command_ts = time.monotonic()
        logging.debug(f"DFPlayer -> {frame.hex(' ')}")
        return True

    def drain_responses(self):
        """Czyta i loguje ramki zwrotne (TX DFPlayera jest podłączony).

        Czysto diagnostyczne - nie blokuje i nie wpływa na logikę sterowania.
        """
        try:
            pending = self.serial.in_waiting
            if pending:
                data = self.serial.read(pending)
                logging.debug(f"DFPlayer <- {data.hex(' ')}")
                return data
        except (OSError, serial.SerialException) as e:
            logging.debug(f"DFPlayer: odczyt UART pominięty: {e}")
        return b""

    # --- API wysokiego poziomu -----------------------------------------
    def reset(self, settle_s=2.0):
        """Miękki reset modułu. DFPlayer potrzebuje chwili na przemontowanie karty."""
        self._send(CMD_RESET)
        time.sleep(settle_s)
        self.drain_responses()

    def set_volume(self, volume):
        volume = max(0, min(30, int(volume)))
        return self._send(CMD_SET_VOLUME, volume)

    def play(self, track):
        logging.info(f"DFPlayer: play track {track}")
        return self._send(CMD_PLAY_TRACK, int(track))

    def loop_track(self, track):
        """Zapętla utwór po stronie modułu.

        Dużo pewniejsze niż dosyłanie "play" po każdym zakończeniu: playback
        nie ma dziury nawet gdy Linux akurat liczy inferencję NPU, a przy
        utworze trwającym kilka sekund oszczędza setki ramek UART na okno.
        """
        logging.info(f"DFPlayer: zapętlam utwór {track}")
        return self._send(CMD_LOOP_TRACK, int(track))

    def query_status(self, timeout=1.0):
        """Odpytuje moduł o stan. Zwraca STATUS_* albo None gdy brak odpowiedzi."""
        try:
            self.serial.reset_input_buffer()
        except (OSError, serial.SerialException):
            pass
        if not self._send(CMD_QUERY_STATUS):
            return None
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            buffer += self.drain_responses()
            index = buffer.find(bytes([_START, _VERSION, _LEN, CMD_QUERY_STATUS]))
            if index >= 0 and len(buffer) >= index + 10:
                return buffer[index + 6]
            time.sleep(0.05)
        logging.warning("DFPlayer: brak odpowiedzi na zapytanie o status.")
        return None

    def query_file_count(self, timeout=1.0):
        """Liczba plików na karcie DFPlayera - sanity check przy starcie okna."""
        try:
            self.serial.reset_input_buffer()
        except (OSError, serial.SerialException):
            pass
        if not self._send(CMD_QUERY_FILES):
            return None
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            buffer += self.drain_responses()
            index = buffer.find(bytes([_START, _VERSION, _LEN, CMD_QUERY_FILES]))
            if index >= 0 and len(buffer) >= index + 10:
                return (buffer[index + 5] << 8) | buffer[index + 6]
            time.sleep(0.05)
        return None

    def stop(self):
        logging.info("DFPlayer: stop")
        return self._send(CMD_STOP)

    def is_busy(self):
        """True gdy trwa odtwarzanie.

        Pin BUSY używany tylko wtedy, gdy przy starcie okazał się wiarygodny;
        w przeciwnym razie pytamy moduł po UART. Gdy i to zawiedzie, zwracamy
        True - "zakładaj, że gra" jest bezpieczniejsze niż zasypanie modułu
        komendami play w kółko.
        """
        if self._busy_pin_usable:
            playing = self.busy.is_playing()
            if playing is not None:
                return playing
        status = self.query_status()
        if status is None:
            return True
        return status == STATUS_PLAYING

    def probe_busy_pin(self):
        """Sprawdza, czy pin BUSY w ogóle odzwierciedla stan modułu.

        Moduł stoi (świeżo po otwarciu portu), więc BUSY powinien być HIGH.
        Jeśli mimo to czyta LOW, pin jest niepodłączony albo wisi w powietrzu
        i nie wolno na nim polegać.
        """
        value = self.busy.is_playing()
        if value is None:
            logging.warning(f"DFPlayer: pin BUSY (GPIO{self.busy.pin}) nieczytelny.")
            self._busy_pin_usable = False
        elif value:
            logging.warning(
                f"DFPlayer: pin BUSY (GPIO{self.busy.pin}) pokazuje 'gra' przy zatrzymanym "
                "module - traktuję go jako niepodłączony i jadę na statusie z UART.")
            self._busy_pin_usable = False
        else:
            logging.info(f"DFPlayer: pin BUSY (GPIO{self.busy.pin}) wygląda wiarygodnie.")
            self._busy_pin_usable = True
        return self._busy_pin_usable

    def close(self):
        try:
            self.serial.close()
        except Exception:
            pass
