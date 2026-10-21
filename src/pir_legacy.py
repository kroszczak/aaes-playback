# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
"""FALLBACK: stara logika wybudzania czujnikiem PIR. NIEUŻYWANA w pętli głównej.

Zachowane celowo, na wypadek gdyby test terenowy kazał wrócić do wykrywania
ruchu. Sam czujnik HC-SR501 jest fizycznie zdemontowany, więc żeby to ożywić
trzeba go najpierw wpiąć z powrotem na pin 25 (GPIO2_A1_d, przez rezystor 10k).

Dlaczego to porzuciliśmy:
  1. fałszywe alarmy od wiatru i nagrzanych liści w lesie,
  2. cold boot Linuksa (~15 s) jest zawsze dłuższy niż czas obecności ptaka
     na gałęzi (~3 s), więc szansa złapania kadru była bliska zeru,
  3. brak hipotezy badawczej - pasywne czekanie nie jest eksperymentem.

Kod przeniesiony żywcem z birdbox_main.py sprzed migracji, bez zmian logiki.
"""

import logging
import os
import subprocess

import config

PIR_GPIO_PIN = 65      # GPIO2_A1_d (pin 25) - zabezpieczone rezystorem 10k
PIR_DEBOUNCE_S = 1.0   # ignoruj kolejne wykrycia przez ten czas po poprzednim

_pir_exported = False


def _pir_ensure_exported():
    global _pir_exported
    if _pir_exported:
        return
    gpio_path = f"/sys/class/gpio/gpio{PIR_GPIO_PIN}"
    if not os.path.exists(gpio_path):
        try:
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(PIR_GPIO_PIN))
        except OSError as e:
            # "busy" zwykle znaczy że już wyeksportowany przez kogoś innego - nieszkodliwe
            if not os.path.exists(gpio_path):
                logging.error(f"Nie udało się wyeksportować GPIO{PIR_GPIO_PIN}: {e}")
                return
    try:
        with open(f"{gpio_path}/direction", "w") as f:
            f.write("in")
    except OSError as e:
        logging.error(f"Nie udało się ustawić kierunku GPIO{PIR_GPIO_PIN}: {e}")
    _pir_exported = True


def pir_active() -> bool:
    _pir_ensure_exported()
    try:
        with open(f"/sys/class/gpio/gpio{PIR_GPIO_PIN}/value", "r") as f:
            return f.read().strip() == "1"
    except OSError as e:
        logging.error(f"Błąd odczytu GPIO{PIR_GPIO_PIN} (PIR): {e}")
        return False


def get_wake_source():
    """Rozróżniało wybudzenie z RTC od wybudzenia z PIR po fladze alarmu.

    W architekturze okien czasowych nieużywane: RTC jest jedynym triggerem,
    więc nie ma czego rozróżniać.
    """
    try:
        result = subprocess.run(
            ["i2cget", "-f", "-y", str(config.I2C_BUS), config.RTC_ADDR, "0x0f"],
            capture_output=True, text=True, check=True
        )
        val = int(result.stdout.strip(), 16)
        logging.info(f"Rejestr statusu RTC (0x0F) = {hex(val)}")
        # maska 0x03 łapie OBA alarmy (A1F=bit0, A2F=bit1)
        return "RTC" if (val & 0x03) else "PIR"
    except Exception as e:
        logging.error(f"Błąd I2C przy sprawdzaniu źródła: {e}")
        return "UNKNOWN"
