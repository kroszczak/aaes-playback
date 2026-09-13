"""DS3231 na I2C - budzik wyznaczający starty okien czasowych.

Logika BCD i układ rejestrów przeniesione z /root/rtc_setup.sh, który działał
poprawnie, ale miał godziny zaszyte na sztywno. Tutaj alarmy biorą się wprost
z konfiguracji okien, więc zmiana windows.txt na karcie realnie przestawia budzik.

Rejestry alarmów są podtrzymywane bateryjnie (VBAT) - przeżywają odcięcie
zasilania przez Pololu, restart i wgranie nowego firmware'u.
"""

import logging
import subprocess
import time
from datetime import datetime

import config

# Alarm 1: sekundy, minuty, godziny, dzień/data
REG_A1_SEC, REG_A1_MIN, REG_A1_HOUR, REG_A1_DAY = 0x07, 0x08, 0x09, 0x0A
# Alarm 2: minuty, godziny, dzień/data (DS3231 nie ma sekund dla Alarmu 2)
REG_A2_MIN, REG_A2_HOUR, REG_A2_DAY = 0x0B, 0x0C, 0x0D
REG_CONTROL, REG_STATUS = 0x0E, 0x0F

# Rejestry czasu bieżącego (BCD): sekundy, minuty, godziny, dzień tygodnia,
# data, miesiąc, rok (0-99, liczony od 2000).
REG_SEC, REG_MIN, REG_HOUR = 0x00, 0x01, 0x02
REG_WDAY, REG_DATE, REG_MONTH, REG_YEAR = 0x03, 0x04, 0x05, 0x06

# bit7 w rejestrze dnia/daty = "ignoruj dzień" -> alarm wyzwala się CODZIENNIE
DAILY = 0x80

# Rejestr kontrolny: BBSQW(bit6) | INTCN(bit2) | A2IE(bit1) | A1IE(bit0)
CTRL_BASE = 0x44          # BBSQW + INTCN
CTRL_A1 = 0x01
CTRL_A2 = 0x02


def _i2c(args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


# Niepewna magistrala: 10-minutowy pomiar po lutowaniu dał ~14% nieudanych transakcji
# z DS3231 (NACK i TIMEOUT na zmianę), w seriach. Pojedyncza transakcja bez ponowienia
# to w tych warunkach loteria - dlatego każda operacja ma kilka prób z narastającą
# przerwą. Nie zastępuje to naprawy styku, ale zmienia "alarm się nie zapisał" w
# "alarm zapisany i sprawdzony".
I2C_ATTEMPTS = 6
I2C_BACKOFF_S = 0.05
_retry_stats = {"retried": 0, "failed": 0}


def _with_retries(describe, attempt_fn):
    """Wykonuje attempt_fn() do skutku (None = porażka). Loguje dopiero ostateczną porażkę."""
    last_error = None
    for attempt in range(I2C_ATTEMPTS):
        value, last_error = attempt_fn()
        if value is not None:
            if attempt:
                _retry_stats["retried"] += 1
                logging.info(f"{describe}: udało się za {attempt + 1}. próbą.")
            return value
        time.sleep(I2C_BACKOFF_S * (attempt + 1))
    _retry_stats["failed"] += 1
    logging.error(f"{describe}: nieudane po {I2C_ATTEMPTS} próbach ({last_error}).")
    return None


def read_register(reg):
    """Zwraca wartość rejestru jako int, albo None gdy I2C nie odpowiada mimo ponowień."""
    def attempt():
        result = _i2c(["i2cget", "-f", "-y", str(config.I2C_BUS), config.RTC_ADDR, f"0x{reg:02x}"])
        if result.returncode != 0:
            return None, result.stderr.strip()
        try:
            return int(result.stdout.strip(), 16), None
        except ValueError:
            return None, f"dziwna odpowiedź {result.stdout.strip()!r}"
    return _with_retries(f"i2cget 0x{reg:02x}", attempt)


def write_register(reg, value):
    def attempt():
        result = _i2c(["i2cset", "-f", "-y", str(config.I2C_BUS), config.RTC_ADDR,
                       f"0x{reg:02x}", f"0x{value:02x}"])
        return (True, None) if result.returncode == 0 else (None, result.stderr.strip())
    return _with_retries(f"i2cset 0x{reg:02x}=0x{value:02x}", attempt) is not None


def retry_stats():
    """Ile operacji wymagało ponowień i ile zawiodło - do logu na koniec cyklu."""
    return dict(_retry_stats)


def to_bcd(value):
    """23 -> 0x23. DS3231 trzyma czas w BCD, nie binarnie."""
    return ((value // 10) << 4) | (value % 10)


def from_bcd(value):
    """0x23 -> 23."""
    return ((value >> 4) & 0x0F) * 10 + (value & 0x0F)


def read_time():
    """Odczytuje czas z DS3231 wprost po I2C. Zwraca datetime albo None.

    Świadomie NIE używamy hwclock: DS3231 nie ma na tej płytce sterownika
    jądra (brak /sys/bus/i2c/devices/3-0068), istnieje tylko /dev/rtc0, czyli
    wewnętrzny zegar SoC. Stary kod wołał hwclock -f /dev/rtc1, którego nigdy
    nie było, więc synchronizacja czasu po cichu nie działała.

    Uwaga: układ trzyma czas LOKALNY, w tej samej strefie, którą ustawia
    config.TIMEZONE - dzięki temu godziny alarmów i godziny okien to jedno i to samo.
    """
    values = {}
    for reg in (REG_SEC, REG_MIN, REG_HOUR, REG_DATE, REG_MONTH, REG_YEAR):
        raw = read_register(reg)
        if raw is None:
            return None
        values[reg] = raw
    try:
        return datetime(
            2000 + from_bcd(values[REG_YEAR]),
            from_bcd(values[REG_MONTH] & 0x1F),
            from_bcd(values[REG_DATE]),
            from_bcd(values[REG_HOUR] & 0x3F),   # bit6=tryb 12h, bit5=PM - maskujemy
            from_bcd(values[REG_MIN]),
            from_bcd(values[REG_SEC] & 0x7F),
        )
    except ValueError as e:
        logging.error(f"DS3231 zwrócił niepoprawną datę: {e}")
        return None


def write_time(when=None):
    """Zapisuje czas (domyślnie lokalny czas systemowy) do DS3231."""
    when = when or datetime.now()
    ok = True
    ok &= write_register(REG_SEC, to_bcd(when.second))
    ok &= write_register(REG_MIN, to_bcd(when.minute))
    ok &= write_register(REG_HOUR, to_bcd(when.hour))       # bit6=0 -> tryb 24h
    ok &= write_register(REG_WDAY, to_bcd(when.isoweekday()))
    ok &= write_register(REG_DATE, to_bcd(when.day))
    ok &= write_register(REG_MONTH, to_bcd(when.month))
    ok &= write_register(REG_YEAR, to_bcd(when.year % 100))
    if ok:
        logging.info(f"DS3231 ustawiony na {when:%Y-%m-%d %H:%M:%S}.")
    else:
        logging.error("Zapis czasu do DS3231 nie powiódł się.")
    return ok


def read_status():
    """Rejestr 0x0F: bit0 = A1F (alarm 1), bit1 = A2F (alarm 2)."""
    return read_register(REG_STATUS)


def clear_alarm_flags(attempts=5):
    """Kasuje flagi alarmu i WERYFIKUJE odczytem, że faktycznie zeszły.

    Bez tego SQW zostaje w stanie niskim i Pololu wybudza płytkę w kółko,
    aż do rozładowania ogniwa. Zwraca True/False - decyzję, co zrobić przy
    porażce, zostawiamy wołającemu (main usypia system awaryjnie).
    """
    for i in range(attempts):
        write_register(REG_STATUS, 0x00)
        time.sleep(0.5)
        value = read_status()
        if value == 0x00:
            logging.info("Flaga alarmu RTC wyczyszczona pomyślnie.")
            return True
        logging.warning(f"Flaga RTC wciąż ustawiona (próba {i + 1}/{attempts}): {value!r}")
        time.sleep(1)
    logging.error("KRYTYCZNE: flaga RTC zablokowana, I2C nie odpowiada.")
    return False


def program_windows(windows, rounds=3):
    """Ustawia alarmy sprzętowe na starty okien i SPRAWDZA je odczytem zwrotnym.

    Alarm1 -> start pierwszego okna, Alarm2 -> start drugiego (jeśli istnieje).
    Przy jednym oknie A2IE zostaje wyłączone, żeby nieużywany alarm nie budził
    płytki resztkową wartością z poprzedniej konfiguracji.

    Rejestry alarmów są statyczne, więc po zapisie czytamy je z powrotem i dopisujemy
    tylko te, które się nie zgadzają. Od tych alarmów zależy, czy pułapka wstanie na
    następne okno - "zapis zgłosił sukces" to za mało, liczy się to, co jest w układzie.
    """
    if not windows:
        logging.error("program_windows: pusta lista okien - nie ruszam alarmów.")
        return False

    start_h, start_m = divmod(windows[0][0], 60)
    wanted = {
        REG_A1_SEC: 0x00,
        REG_A1_MIN: to_bcd(start_m),
        REG_A1_HOUR: to_bcd(start_h),
        REG_A1_DAY: DAILY,
    }
    control = CTRL_BASE | CTRL_A1
    summary = f"Alarm1={start_h:02d}:{start_m:02d}"
    if len(windows) > 1:
        start_h, start_m = divmod(windows[1][0], 60)
        wanted.update({
            REG_A2_MIN: to_bcd(start_m),
            REG_A2_HOUR: to_bcd(start_h),
            REG_A2_DAY: DAILY,
        })
        control |= CTRL_A2
        summary += f", Alarm2={start_h:02d}:{start_m:02d}"
    else:
        summary += ", Alarm2 wyłączony"
    wanted[REG_CONTROL] = control

    pending = dict(wanted)
    for round_no in range(rounds):
        for reg, value in pending.items():
            write_register(reg, value)
        mismatched = {}
        for reg, value in wanted.items():
            actual = read_register(reg)
            if actual != value:
                mismatched[reg] = value
        if not mismatched:
            logging.info(f"RTC: {summary} (codziennie) - zapisane i zweryfikowane.")
            return True
        logging.warning(f"RTC: {len(mismatched)} rejestrów alarmu się nie zgadza "
                        f"(runda {round_no + 1}/{rounds}) - dopisuję ponownie.")
        pending = mismatched
    logging.error(f"RTC: nie udało się zapisać alarmów ({summary}) po {rounds} rundach.")
    return False


def sync_to_rtc():
    """Przepisuje czas systemowy do DS3231, gdy system ma wiarygodną datę.

    Alarmy 6:00/18:00 mają sens tylko wtedy, gdy układ zna prawdziwą godzinę.
    """
    now = datetime.now()
    if now.year < 2025:
        logging.warning("Czas systemowy niewiarygodny - NIE nadpisuję DS3231.")
        return False
    return write_time(now)


def dump():
    """Zrzut rejestrów 0x07-0x0F - do diagnostyki z ręki."""
    return {reg: read_register(reg) for reg in range(0x07, 0x10)}
