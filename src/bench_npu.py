#!/usr/bin/env python3
# Grant: Study@research (ID UB), project no. 201/34/UAM/0068, Adam Mickiewicz University, Poznań
"""Pomiar czasu inferencji NPU: stare demo (zapis out.png) vs nowe (bez zapisu).

Uruchom z dwiema binarkami obok siebie, żeby porównać na TYM SAMYM obrazie:
    python3 bench_npu.py /root/rknn_yolov5_demo/rknn_yolov5_demo.orig \\
                         /root/rknn_yolov5_demo/rknn_yolov5_demo

Mierzy 8 przebiegów każdej wersji i podaje medianę - pojedynczy pomiar na
jednordzeniowym procesorze potrafi się wahać o kilkadziesiąt procent.
"""
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, "/root")
import config  # noqa: E402

RUNS = 8


def measure(binary, image, env_extra=None):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = config.RKNN_LIB_DIR
    env.pop("RKNN_OUT_IMAGE", None)
    if env_extra:
        env.update(env_extra)
    times, detections = [], None
    for _ in range(RUNS):
        start = time.monotonic()
        result = subprocess.run([binary, config.RKNN_MODEL_PATH, image],
                                capture_output=True, text=True, timeout=60,
                                env=env, cwd=config.NPU_WORKDIR)
        times.append(time.monotonic() - start)
        if result.returncode != 0:
            return None, f"kod wyjscia {result.returncode}"
        found = [l for l in result.stdout.splitlines() if "@" in l and "(" in l]
        detections = found
    return times, detections


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    image = sys.argv[3] if len(sys.argv) > 3 else os.path.join(config.NPU_WORKDIR, "frame.jpg")
    if not os.path.exists(image):
        print(f"brak obrazu testowego {image}")
        return 1
    print(f"obraz: {image} ({os.path.getsize(image)} B), {RUNS} przebiegow na wersje\n")

    for label, binary, extra in (("STARE (zapis out.png)", sys.argv[1], None),
                                 ("NOWE  (bez zapisu)", sys.argv[2], None),
                                 ("NOWE + RKNN_OUT_IMAGE", sys.argv[2],
                                  {"RKNN_OUT_IMAGE": "/tmp/npu/annotated.png"})):
        if not os.path.exists(binary):
            print(f"{label}: brak pliku {binary}")
            continue
        times, detections = measure(binary, image, extra)
        if times is None:
            print(f"{label}: BLAD ({detections})")
            continue
        print(f"{label}")
        print(f"  mediana {statistics.median(times):.2f}s  min {min(times):.2f}s  max {max(times):.2f}s")
        print(f"  detekcje: {detections if detections else 'brak'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
