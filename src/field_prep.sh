#!/bin/sh
# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
# Przygotowanie pulapki do wyjazdu w teren. Nic nie kasuje bezpowrotnie -
# material testowy trafia do archiwum na karcie.
set -u
F=/mnt/sdcard/fotopulapka
STAMP=$(date +%Y%m%d_%H%M)

echo "=== 1. harmonogram okien ==="
# Test petli RTC zostawil tymczasowe okno (np. 12:27-12:31). Bez przywrocenia
# pulapka budzilaby sie codziennie o tej godzinie.
cat > /mnt/sdcard/windows.txt <<'WIN'
# Okna czasowe fotopulapki. Jedna linia na okno, HH:MM-HH:MM, czas lokalny.
# DS3231 ma dwa alarmy sprzetowe, wiec dzialaja najwyzej dwa okna dziennie.
06:00-08:00
18:00-20:00
WIN
cat /mnt/sdcard/windows.txt

echo "=== 2. archiwizacja materialu testowego ==="
A=$F/archiwum_testow/przed_terenem_$STAMP
mkdir -p "$A"
for item in photos videos system.log milestones.log state.json run_stdout.log wake_test.log; do
    [ -e "$F/$item" ] && mv "$F/$item" "$A/" 2>/dev/null
done
mkdir -p $F/photos $F/videos
rm -f $F/podglad.jpg $F/klatka_z_klipu.jpg $F/sample.jpg $F/burst.mjpeg $F/i2c*.txt 2>/dev/null
rm -rf $F/mjtest $F/writetest 2>/dev/null

echo "=== 3. licznik ratunkowy i blokada ==="
echo 0 > /root/recovery_count
rm -f /root/blokada        # autostart MUSI dzialac w terenie
rm -f /tmp/window_done /tmp/power_deadline /tmp/heartbeat

echo "=== 4. weryfikacja ==="
echo "demon ratunkowy: $(ps w | grep -c '[r]ecoveryd.py') proces(y)"
printf "RTC 0x68: "; i2cget -f -y 3 0x68 0x0e >/dev/null 2>&1 && echo odpowiada || echo "NIE ODPOWIADA"
printf "DFPlayer: "; [ -c /dev/ttyS3 ] && echo "/dev/ttyS3 obecny" || echo "BRAK /dev/ttyS3"
echo "blokada: $([ -e /root/blokada ] && echo ISTNIEJE_ZLE || echo brak_ok)"
sync
df -h /mnt/sdcard | tail -1
echo "=== GOTOWE. Material testowy w $A ==="
