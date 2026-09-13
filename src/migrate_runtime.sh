#!/bin/sh
# Przenosi na kartę SD wszystko, co jest wielokrotnie uruchamiane w trakcie okna.
# Cel: zdjąć z NAND-a odczyty na ścieżce krytycznej. Binarki i biblioteki są
# mapowane z systemu plików, więc każde uruchomienie enkodera to seria page-inów
# z NAND-a - a to on pada pod obciążeniem (SIGBUS + rootfs read-only).
# System jako taki ZOSTAJE na NAND; przenosimy tylko runtime mediów i NPU.
set -u
DST=/mnt/sdcard/fotopulapka/runtime
touch /root/blokada 2>/dev/null

echo "=== przygotowanie ==="
mkdir -p "$DST/bin" "$DST/lib" "$DST/ko" || exit 1

echo "=== biblioteki /oem/usr/lib -> SD (najwazniejsze, najwiecej page-inow) ==="
cp -a /oem/usr/lib/. "$DST/lib/" && echo "lib OK" || echo "lib BLAD"

echo "=== binarki mediow ==="
for b in simple_vi_bind_venc_jpeg simple_vi_bind_venc rkaiq_3A_server RkLunch-stop.sh; do
    cp -a "/oem/usr/bin/$b" "$DST/bin/" 2>/dev/null && echo "  $b OK" || echo "  $b BLAD"
done

echo "=== moduly jadra NPU/RGA ==="
cp -a /oem/usr/ko/rknpu.ko /oem/usr/ko/rga3.ko "$DST/ko/" 2>/dev/null && echo "ko OK" || echo "ko BLAD"

echo "=== pliki kalibracji ISP (potrzebne serwerowi AE) ==="
if [ -d /oem/usr/share ]; then
    mkdir -p "$DST/share"
    cp -a /oem/usr/share/. "$DST/share/" 2>/dev/null && echo "share OK" || echo "share BLAD"
fi

echo "=== demo RKNN (binarka + model + lib) ==="
cp -a /root/rknn_yolov5_demo "$DST/" 2>/dev/null && echo "rknn OK" || echo "rknn BLAD"

sync
echo "=== WERYFIKACJA ==="
echo "rozmiar na SD:"; du -sh "$DST" 2>/dev/null || echo "(du niedostepne)"
echo "kluczowe pliki:"
for f in "$DST/bin/simple_vi_bind_venc_jpeg" "$DST/bin/simple_vi_bind_venc" \
         "$DST/bin/rkaiq_3A_server" "$DST/ko/rknpu.ko" \
         "$DST/rknn_yolov5_demo/rknn_yolov5_demo" "$DST/rknn_yolov5_demo/model/yolov5.rknn"; do
    if [ -f "$f" ]; then echo "  OK   $(ls -l "$f" | awk '{print $5}')  $f"; else echo "  BRAK $f"; fi
done
echo "liczba bibliotek: $(ls "$DST/lib" 2>/dev/null | wc -l)"
echo "=== porownanie sum kontrolnych (NAND vs SD) ==="
for b in simple_vi_bind_venc_jpeg simple_vi_bind_venc rkaiq_3A_server; do
    A=$(md5sum "/oem/usr/bin/$b" 2>/dev/null | cut -d" " -f1)
    B=$(md5sum "$DST/bin/$b" 2>/dev/null | cut -d" " -f1)
    [ "$A" = "$B" ] && [ -n "$A" ] && echo "  $b: ZGODNE" || echo "  $b: ROZNE ($A vs $B)"
done
echo "=== GOTOWE ==="
