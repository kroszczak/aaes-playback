#!/usr/bin/env python3
# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
"""Monitor magistrali I2C modułu DS3231 - do lokalizacji uszkodzonego przewodu.

Pyta 5x/s oba układy modułu ZS-042: zegar DS3231 (0x68) i EEPROM AT24C32 (0x50).
Wypisuje linię przy KAŻDEJ zmianie stanu, z rodzajem błędu:
  OK       - układ odpowiada
  NACK     - magistrala działa, ale układ nie potwierdza adresu
  TIMEOUT  - magistrala zablokowana (linia SDA/SCL trzymana w stanie niskim)

Jak czytać wynik podczas poruszania kablami:
  oba NACK            -> przerwa na SDA albo SCL
  oba TIMEOUT         -> przerwa na VCC modułu (układy ściągają linie w dół)
  chaos NACK/TIMEOUT  -> niepewna masa (GND)
  tylko 0x68 NACK     -> lut/zasilanie samego DS3231 na płytce modułu

Pokazuje też POZIOMY linii odczytane przez GPIO (bez zmiany funkcji pinów):
  SDA = GPIO1_D2 (pin 9 złącza), SCL = GPIO1_D3 (pin 10 złącza).
  W spoczynku obie linie powinny być w stanie 1. Stałe SCL=0 przy SDA=1
  oznacza przerwany przewód SCL albo zwarcie SCL do masy.

Użycie: python3 i2cmon.py [sekundy]   (domyślnie działa do Ctrl+C)
Czyste ioctl na /dev/i2c-3 - bez uruchamiania i2cget, więc nie obciąża systemu.
"""
import errno
import fcntl
import os
import sys
import time

I2C_SLAVE_FORCE = 0x0706
BUS = "/dev/i2c-3"
DEVICES = {0x68: "DS3231", 0x50: "EEPROM"}
LINES = {"SDA": 58, "SCL": 59}   # GPIO1_D2 / GPIO1_D3 (sysfs)


def export_lines():
    for gpio in LINES.values():
        if not os.path.isdir(f"/sys/class/gpio/gpio{gpio}"):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(gpio))
            except OSError:
                pass


def unexport_lines():
    for gpio in LINES.values():
        try:
            with open("/sys/class/gpio/unexport", "w") as f:
                f.write(str(gpio))
        except OSError:
            pass


def line_levels():
    out = {}
    for name, gpio in LINES.items():
        try:
            with open(f"/sys/class/gpio/gpio{gpio}/value") as f:
                out[name] = f.read().strip()
        except OSError:
            out[name] = "?"
    return out


def probe(fd, addr):
    try:
        fcntl.ioctl(fd, I2C_SLAVE_FORCE, addr)
        if addr == 0x50:
            os.write(fd, b"\x00\x00")      # AT24C32: 2-bajtowy adres komórki
        else:
            os.write(fd, b"\x00")          # DS3231: rejestr sekund
        os.read(fd, 1)
        return "OK"
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.EREMOTEIO):
            return "NACK"
        if e.errno == errno.ETIMEDOUT:
            return "TIMEOUT"
        return f"BLAD({e.errno})"


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    export_lines()
    fd = os.open(BUS, os.O_RDWR)
    start = time.monotonic()
    last = None
    counts = {a: {} for a in DEVICES}
    last_summary = start
    print("czas      SDA SCL   DS3231(0x68)  EEPROM(0x50)", flush=True)
    while duration is None or time.monotonic() - start < duration:
        state = {a: probe(fd, a) for a in DEVICES}
        for a, s in state.items():
            counts[a][s] = counts[a].get(s, 0) + 1
        lv = line_levels()
        snapshot = (state[0x68], state[0x50], lv["SDA"], lv["SCL"])
        if snapshot != last:
            print(f"{time.strftime('%H:%M:%S')}   {lv['SDA']}   {lv['SCL']}    "
                  f"{state[0x68]:<12}  {state[0x50]}", flush=True)
            last = snapshot
        if time.monotonic() - last_summary >= 10:
            summary = "  ".join(f"{DEVICES[a]}:{dict(sorted(c.items()))}" for a, c in counts.items())
            print(f"          ... {summary}", flush=True)
            last_summary = time.monotonic()
        time.sleep(0.2)
    os.close(fd)
    unexport_lines()
    print("PODSUMOWANIE:", {DEVICES[a]: c for a, c in counts.items()}, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        unexport_lines()
