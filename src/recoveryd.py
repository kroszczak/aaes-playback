#!/usr/bin/env python3
"""Rezydentny demon ratunkowy - jedyna droga do restartu, gdy NAND przestaje czytać.

Problem: gdy pamięć NAND zaczyna gubić odczyty, KAŻDE uruchomienie programu zawodzi.
`reboot`, `sync`, nawet `wc` przestają działać, bo jądro nie może wczytać ich stron.
Sesja SSH też przestaje wstawać, bo sshd nie potrafi się rozmnożyć. Płytka odpowiada
na ping i nic więcej - jedynym wyjściem zostaje fizyczne przepięcie zasilania.

Rozwiązanie: proces, który JUŻ DZIAŁA i ma wszystkie strony przypięte w RAM
(`mlockall`), nie potrzebuje niczego doczytywać. Restart wywołujemy wprost
wywołaniem systemowym `reboot()` przez ctypes - bez `exec`, bez dotykania dysku.

Uruchamiany z /etc/init.d/S98recoveryd, czyli PRZED birdbox.

Trzy wyzwalacze:
  1. plik-znacznik  /tmp/DO_REBOOT        - restart na żądanie
  2. puls           /tmp/heartbeat        - restart, gdy główny program zamilkł
  3. termin         /tmp/power_deadline   - odcięcie zasilania po końcu okna, jeśli
                                            główny program nie domknął cyklu sam

Strażnik zasilania mieszka tutaj, a nie w osobnym skrypcie powłoki, bo skrypt
wołał `sleep.sh`, a ten zaczyna od `sync` - na zablokowanym I/O `sync` wisi
w nieskończoność i do odcięcia zasilania nigdy nie dochodziło.
"""

import ctypes
import os
import sys
import threading
import time

MCL_CURRENT = 1
MCL_FUTURE = 2
LINUX_REBOOT_CMD_RESTART = 0x01234567

REBOOT_FLAG = "/tmp/DO_REBOOT"
HEARTBEAT = "/tmp/heartbeat"
COUNTER_FILE = "/root/recovery_count"
# Oba pliki niosą czas MONOTONICZNY (CLOCK_MONOTONIC, sekundy od startu systemu,
# wspólny dla wszystkich procesów). Pierwsza wersja porównywała czas modyfikacji
# pliku z zegarem ściennym - a zegar po zimnym starcie przeskakuje z 2021 na
# aktualną datę (NTP albo DS3231). Puls wyglądał wtedy na "sprzed 5,7 roku"
# i demon restartował płytkę przy KAŻDYM wybudzeniu. Zegar monotoniczny nie skacze.
POWER_DEADLINE = "/tmp/power_deadline"   # monotoniczny termin odcięcia zasilania
WINDOW_DONE_FLAG = "/tmp/window_done"    # główny program domknął cykl sam
EVENT_LOG = "/mnt/sdcard/fotopulapka/milestones.log"

# Pin OFF przetwornicy Pololu. Przed restartem zwalniamy go do stanu wysokiej
# impedancji, żeby nie zostawić go wysterowanego na wysoki poziom.
#
# UWAGA: to NIE jest pełne zabezpieczenie. W trakcie samego resetu SoC piny i tak
# przechodzą w stan domyślny i linia OFF może zostać na chwilę podciągnięta -
# a wtedy Pololu zatrzaskuje wyłączenie i płytka nie wstaje. Jedyne pewne
# rozwiązanie to rezystor 10k z tego pinu do masy.
POWER_OFF_GPIO = 64

# Puls starszy niż to = główny program wisi. Hojnie, bo pojedyncze nagranie
# trwa 45 s, a odbudowa potoku kamery kilka sekund.
HEARTBEAT_TIMEOUT_S = 600

# Zabezpieczenie przed pętlą restartów: tyle automatycznych restartów i koniec.
MAX_AUTO_REBOOTS = 3

POLL_S = 2

# Ile czekać na sync() przed restartem. Na zdrowym systemie trwa ułamek sekundy.
SYNC_TIMEOUT_S = 5

libc = ctypes.CDLL(None, use_errno=True)

# Ta sama strefa co reszta systemu - bez tego wpisy demona w milestones.log
# miały czas UTC, przesunięty względem logów głównego programu.
try:
    import config
    os.environ["TZ"] = config.TIMEZONE
    time.tzset()
except Exception:
    pass


def pin_memory():
    """Przypina wszystkie strony procesu w RAM - bez tego demon sam padłby
    na tej samej przyczynie, którą ma ratować."""
    if libc.mlockall(MCL_CURRENT | MCL_FUTURE) != 0:
        err = ctypes.get_errno()
        print(f"recoveryd: mlockall nieudane (errno {err}) - działam bez przypięcia",
              file=sys.stderr, flush=True)
        return False
    return True


def read_counter():
    try:
        with open(COUNTER_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_counter_best_effort(value):
    try:
        with open(COUNTER_FILE, "w") as f:
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def release_power_pin():
    """Ustawia pin OFF przetwornicy w stan niski i wejściowy przed restartem.

    Zapis do sysfs GPIO to nie I/O blokowe, więc nie grozi zawieszeniem na
    zablokowanym nośniku. Cel: nigdy nie zostawić linii OFF wysterowanej wysoko.
    """
    path = f"/sys/class/gpio/gpio{POWER_OFF_GPIO}"
    try:
        if os.path.isdir(path):
            with open(f"{path}/value", "w") as f:
                f.write("0")
            with open(f"{path}/direction", "w") as f:
                f.write("in")
    except OSError as e:
        print(f"recoveryd: nie udało się zwolnić GPIO{POWER_OFF_GPIO}: {e}",
              file=sys.stderr, flush=True)


def _log_event_best_effort(message):
    """Ślad decyzji demona na karcie - bez tego po restarcie nie wiadomo, kto go zrobił."""
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} RECOVERYD: {message}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _read_monotonic(path):
    """Czas monotoniczny zapisany w pliku przez główny program, albo None."""
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _bounded(fn, timeout):
    """Uruchamia fn w wątku i czeka najwyżej `timeout` s. Zawieszony wątek zostaje w tle."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return not t.is_alive()


def power_off(reason):
    """Odcina zasilanie przez pin OFF przetwornicy Pololu - bez uruchamiania programów.

    Na zasilaniu z sieci (stół) pin nic nie robi i płytka zostaje włączona -
    wtedy po chwili restartujemy, żeby nie wisiała w zablokowanym stanie.
    """
    print(f"recoveryd: ODCIĘCIE ZASILANIA ({reason})", flush=True)

    def flush():
        _log_event_best_effort(f"odciecie zasilania: {reason}")
        libc.sync()

    if not _bounded(flush, SYNC_TIMEOUT_S):
        print(f"recoveryd: sync() nie zdążył w {SYNC_TIMEOUT_S}s - tnę mimo to", flush=True)
    path = f"/sys/class/gpio/gpio{POWER_OFF_GPIO}"
    try:
        if not os.path.isdir(path):
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(POWER_OFF_GPIO))
        with open(f"{path}/direction", "w") as f:
            f.write("out")
        with open(f"{path}/value", "w") as f:
            f.write("1")
    except OSError as e:
        print(f"recoveryd: nie udało się wysterować GPIO{POWER_OFF_GPIO}: {e}", flush=True)
    # Jeśli nadal żyjemy po 30 s, zasilanie nie idzie przez Pololu (np. stół) -
    # restart zamiast wiszenia w zablokowanym stanie.
    time.sleep(30)
    do_reboot("zasilanie nie zostało odcięte (zasilanie z pominięciem Pololu?)")


def do_reboot(reason, new_count=None):
    """Restart BEZ jakiegokolwiek czekania na I/O.

    Pierwsza wersja wołała sync() i zapisywała licznik przed restartem - i na
    zablokowanym torze karty utknęła w `wb_wait_for_completion` na zawsze, bo
    sync() czeka na nośnik, który właśnie przestał odpowiadać. Ścieżka ratunkowa
    nie może zależeć od tego, przed czym ratuje.

    Sprawdzone na sprzęcie: gołe reboot() przechodzi nawet przy zablokowanym mmc.
    Zapis licznika i sync() idą w osobnym wątku z limitem czasu.
    """
    print(f"recoveryd: RESTART ({reason})", flush=True)
    release_power_pin()

    def flush_what_we_can():
        if new_count is not None:
            _bump_counter_best_effort(new_count)
        _log_event_best_effort(f"restart: {reason}")
        libc.sync()

    # sync() w osobnym wątku z limitem czasu: na zdrowym systemie zdąży zapisać
    # wszystko (bez tego świeżo zapisane pliki potrafią wyjść po restarcie
    # posklejane ze starych i nowych stron - sprawdzone boleśnie), a na
    # zablokowanym nośniku wątek utknie w stanie D, a my i tak restartujemy.
    if not _bounded(flush_what_we_can, SYNC_TIMEOUT_S):
        print(f"recoveryd: sync() nie zdążył w {SYNC_TIMEOUT_S}s - restart mimo to", flush=True)
    libc.reboot(LINUX_REBOOT_CMD_RESTART)
    print("recoveryd: wywołanie reboot() NIE zadziałało!", file=sys.stderr, flush=True)


def main():
    # Licznik czytamy RAZ, na starcie, gdy I/O jest jeszcze zdrowe. Później
    # decyzja zapada wyłącznie na wartości w pamięci - bez dotykania nośnika.
    auto_reboots = read_counter()
    pin_memory()
    print(f"recoveryd: czuwam (puls: {HEARTBEAT}, znacznik: {REBOOT_FLAG}, "
          f"dotychczasowe auto-restarty: {auto_reboots}/{MAX_AUTO_REBOOTS})", flush=True)
    seen_heartbeat = False

    while True:
        # 1. restart na żądanie - zawsze, bez limitu
        if os.path.exists(REBOOT_FLAG):
            try:
                os.remove(REBOOT_FLAG)
            except OSError:
                pass
            do_reboot("znacznik DO_REBOOT")

        # 2. termin odcięcia zasilania po oknie
        deadline = _read_monotonic(POWER_DEADLINE)
        if deadline is not None and time.monotonic() > deadline and not os.path.exists(WINDOW_DONE_FLAG):
            power_off("minął termin okna, a cykl się nie domknął")

        # 3. puls głównego programu
        beat_ts = _read_monotonic(HEARTBEAT)
        if beat_ts is not None:
            seen_heartbeat = True
            age = time.monotonic() - beat_ts
        else:
            age = None

        if seen_heartbeat and age is not None and age > HEARTBEAT_TIMEOUT_S:
            if auto_reboots < MAX_AUTO_REBOOTS:
                do_reboot(f"puls milczy od {int(age)}s", new_count=auto_reboots + 1)
            else:
                print(f"recoveryd: limit {MAX_AUTO_REBOOTS} restartów wyczerpany "
                      "- nie restartuję, żeby nie wpaść w pętlę.", flush=True)
                seen_heartbeat = False

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
