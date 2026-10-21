#!/usr/bin/env python3
# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
"""Test pełnej pętli: ustaw budzik za N minut, uśpij płytkę, sprawdź czy wstanie.

Po co: to jedyny element, którego nie da się sprawdzić przy zasilaniu z pominięciem
Pololu. Sprawdza całą drogę: zapis alarmu do DS3231 -> odcięcie zasilania przez
GPIO 64 -> sygnał SQW na pin ON Pololu -> cold boot -> autostart -> okno -> uśpienie.

Co robi:
  1. czyta czas z DS3231 (jedyne źródło czasu w terenie),
  2. zapisuje windows.txt z krótkim oknem startującym za N minut (plus okno wieczorne),
  3. programuje alarmy z tego pliku i WERYFIKUJE je odczytem zwrotnym,
  4. kasuje flagi alarmu (bez tego SQW zostaje nisko i Pololu nie da się wyłączyć),
  5. zapisuje kamień milowy na kartę i odcina zasilanie.

Po wybudzeniu autostart (S99birdbox) sam wykryje, że jest w oknie.
Dowodem przejścia testu jest wpis "OKNO START" w milestones.log z godziną wybudzenia.

Użycie: python3 wake_test.py [minuty]     (domyślnie 3)
"""
import os
import subprocess
import sys
import time
from datetime import timedelta

sys.path.insert(0, "/root")
os.environ.setdefault("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
time.tzset()

import config          # noqa: E402
import rtc             # noqa: E402

WINDOW_MINUTES = 4
EVENING_WINDOW = "18:00-20:00"


def note(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} WAKE_TEST: {message}\n"
    print(line.rstrip(), flush=True)
    try:
        os.makedirs(os.path.dirname(config.MILESTONE_FILE), exist_ok=True)
        with open(config.MILESTONE_FILE, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"nie udało się zapisać kamienia milowego: {e}", flush=True)


def main():
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    now = rtc.read_time()
    if now is None:
        note("PRZERWANE: DS3231 nie odpowiada, nie ma czym ustawić budzika")
        return 1
    note(f"czas z DS3231: {now:%Y-%m-%d %H:%M:%S}")

    start = now + timedelta(minutes=minutes)
    end = start + timedelta(minutes=WINDOW_MINUTES)
    spec = f"{start:%H:%M}-{end:%H:%M}"
    try:
        with open(config.WINDOWS_FILE, "w") as f:
            f.write("# tymczasowe okno testowe, wygenerowane przez wake_test.py\n")
            f.write(f"{spec}\n{EVENING_WINDOW}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        note(f"PRZERWANE: nie mogę zapisać {config.WINDOWS_FILE}: {e}")
        return 1
    note(f"okno testowe {spec} zapisane w {config.WINDOWS_FILE}")

    windows = config.load_windows()
    if not rtc.program_windows(windows):
        note("PRZERWANE: nie udało się zaprogramować alarmów")
        return 1
    if not rtc.clear_alarm_flags():
        note("PRZERWANE: flaga alarmu nie zeszła - Pololu nie dałoby się wyłączyć")
        return 1

    note(f"budzik ustawiony na {start:%H:%M}, usypiam. Dowodem przejścia testu "
         f"będzie wpis OKNO START po {start:%H:%M}.")
    os.sync()
    time.sleep(1)
    subprocess.run([config.SLEEP_SCRIPT], check=False)
    # Jeśli tu dotarliśmy, zasilanie nie idzie przez Pololu.
    note("UWAGA: zasilanie nie zostało odcięte - płytka nie jest zasilana przez Pololu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
