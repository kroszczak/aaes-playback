# Hardware

Everything is an off-the-shelf module. Nothing is a custom board, which is the point:
another group should be able to rebuild this without fabricating anything.

## Parts

| Part | Role | ≈ EUR |
|---|---|---|
| Luckfox Pico Pro Max (Rockchip RV1106) | compute + NPU | 28 |
| sc3336 camera module | 2304×1296 sensor | 12 |
| Pololu U3V40F5 step-up regulator | cell → 5 V | 23 |
| Pololu latching pushbutton power switch | program-controlled power cut | 12 |
| DS3231 module (ZS-042, with AT24C32) | scheduled wake | 3 |
| DFPlayer Mini | playback from its own microSD | 3 |
| Speaker | 2 |
| microSD, small | stimulus audio | 3 |
| microSD 32 GB | video and logs | 7 |
| 4 × Samsung INR18650-35E | 13.6 Ah pack | 28 |
| Cell holders | 4 |
| Charger board with protection | charging + pack protection | 4 |
| Solar panel | 19 |
| IP65 enclosure | 14 |
| Cable gland, transistor, diode, resistors, wire | 6 |
| **Total** | | **≈ 165** |

Prices are approximate and regional. The two Pololu parts are ~20 % of the cost and are
the obvious place to save: nothing here uses the 4 A headroom of that regulator — the
unit draws 400 mA.

The decoy is part of the experimental stimulus, not the instrument, and is not counted
here.

## Connections

| Signal | Where | Notes |
|---|---|---|
| Audio module TX/RX | header pins **19** / **20** | `/dev/ttyS3`, 9600 8N1. Needs `UART3_M1_STATUS=1` in `/etc/luckfox.cfg` |
| I2C SDA / SCL | header pins **9** / **10** | bus `i2c-3`; RTC at `0x68`, its EEPROM at `0x50` |
| RTC alarm output (SQW) | → transistor stage → diode → **ON** pin of the switch | this is what turns the whole unit on |
| Power-off | **GPIO 64** → **OFF** pin of the switch | how the program ends its own cycle |
| ADC (optional) | `SARADC_M0` = pin **30**, `SARADC_M1` = pin **29** | 10-bit, 1.8 V reference, 1.7578 mV/LSB |

### Power path

```
cells ──► charger + protection ──► step-up 5 V ──┬──► latching switch ──► board
                                                 │
                                                 └──► RTC + wake stage
                                                      (always powered)
```

The RTC lives **before** the switch, which is what lets it turn everything else on. This
also means it draws current continuously: **2.5 mA**, about 60 mAh/day.

## Things that cost us time

**This DS3231 module has no pull-up resistors on SDA/SCL.** Some do, some do not. If the
bus does not work at all, add pull-ups to the board's **3V3** — not to 5 V, which would
put the lines out of spec for the RV1106's inputs.

**The step-up has no short-circuit protection.** A stray offcut of solder wire under the
board drew 2 A through it, cooked it, and destroyed the RTC module — while the SoC,
sitting behind an open switch, was untouched. Put a **47–100 Ω series resistor or a
polyfuse in the peripheral supply** on the always-powered rail. At 1.5 mA the RTC does
not notice it.

**Equalise cells before paralleling them.** A charged cell connected next to a flat one
equalises through an unlimited path. Within ~0.05 V is fine.

**The pack protection latches.** After a short or a deep discharge the output stays
disconnected until charger voltage appears. In the field a solar panel releases that
latch at sunrise, which makes the panel a recovery mechanism and not only a power
source.

**The module's BUSY pin is unusable** on this hardware — it reads constant LOW. The code
detects that and falls back to querying playback state over the serial link instead.

## Mounting

- Aim the camera **at the decoy**: that is where the resident will come.
- The enclosure needs a window for the camera and a vent for the speaker. Both are
  common failure points: condensation on the inside of the window gives two hours of
  milky video, and a flooded speaker ends the trial. Use a desiccant sachet and a
  waterproof membrane.
- Keep the cell holders reachable without dismantling everything.
