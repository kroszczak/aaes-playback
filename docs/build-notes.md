# Build notes

Everything here was worked out on a **Mac (Apple Silicon)** host, so every container
runs with `--platform linux/amd64` under emulation. On a native x86_64 Linux host you
can drop that flag and most of the slowness with it.

Nothing in this repository needs to be built to *run* the trap — the Python code runs
directly on a stock Luckfox image. You need this document only if you want to rebuild
the firmware (for the device-tree change) or the NPU demo.

---

## 1. Vendor SDK container

```bash
docker run -it --platform linux/amd64 \
  -v luckfox_data:/workspace -v ~/LuckfoxImages:/output \
  -w /workspace ubuntu:22.04 /bin/bash
```

The named volume keeps the SDK tree between runs, but **not** installed apt packages or
`PATH` — reinstall tools in every new container:

```bash
apt-get update && apt-get install -y git ssh make gcc libssl-dev liblz4-tool expect \
  g++ patchelf chrpath gawk texinfo diffstat bzip2 libtool bison flex fakeroot cmake \
  gcc-multilib g++-multilib unzip rsync wget python3 python3-pip python3-pexpect \
  libncurses5-dev bc file cpio python-is-python3 pkg-config device-tree-compiler gperf

export PATH=/workspace/luckfox-pico/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH
```

Toolchain: **`arm-rockchip830-linux-uclibcgnueabihf`** — 32-bit ARM against **uclibc**,
not glibc. The same toolchain builds both the firmware and the RKNN demo.

### Build order

```bash
./build.sh rootfs     # buildroot / busybox / packages
./build.sh kernel     # kernel + device tree
./build.sh media      # Rockchip closed libraries (ISP, rockiva) — REQUIRED before app
./build.sh app        # rkipc and the camera binaries
./build.sh firmware   # packs everything into update.img
```

Only rebuild the stages your change touches (a `.dts` edit needs `kernel` + `firmware`).

---

## 2. Gotchas worth knowing

### The device-tree edit that silently does nothing

Enabling `i2c3` in `rv1106-luckfox-pico-pro-max-ipc.dtsi` **has no effect**, because the
board-level file `rv1106g-luckfox-pico-pro-max.dts` contains its own, *later*
`&i2c3 { status = "disabled"; }`. In `dtc`, the later definition wins.

Edit the board `.dts`, not just the `.dtsi`. Confirmation that it worked:
`i2cdetect -y 3` lists `0x68` (DS3231) and `0x50` (AT24C32 — the address varies with
module strapping, `0x57` is also common).

**General lesson:** when a device-tree change appears not to apply, look for a later
file redefining the same node path.

### `write jobserver: Bad file descriptor`

A GNU Make bug in deeply nested parallel sub-makes, made much worse by QEMU emulation.
Before a build that rebuilds busybox:

```bash
export MAKEFLAGS="-j1"    # restore to "" afterwards, other stages don't need it
```

An occasional `Segmentation fault` in `make` itself after a long compile is QEMU
instability — just retry.

### `Build rootfs is not yet complete`

Caused by building rootfs with a bare `make -C sysdrv rootfs`. Always use
`./build.sh rootfs`.

### `rockiva_ba_api.h: No such file or directory`

Build order: `app` needs libraries produced by `media`. Always `./build.sh media`
first.

### Camera binaries have different names than most examples claim

The real names in `/oem/usr/bin/` are **`simple_vi_bind_venc_jpeg`** and
**`simple_vi_bind_venc`**. Check with `ls /oem/usr/bin/ | grep -i venc` rather than
trusting names found online.

Note that `simple_vi_bind_venc` writes nothing at all without `-o` — it reports
`Output Path: (null)` and exits successfully, which is an easy hour to lose.

### Disabling `rkipc` without breaking the camera

`rkipc` competes for ISP/DMA and interferes with audio. The tempting fix —
`chmod -x /etc/init.d/S21appinit` — also disables `insmod_ko.sh`, so the camera kernel
modules never load and the camera stops working entirely. Disable only the binary:

```bash
chmod +x /etc/init.d/S21appinit      # leave the init script alone
chmod -x /oem/usr/bin/rkipc          # disable just rkipc
```

### NPU and RGA modules

`/oem/usr/ko/rknpu.ko` and `rga3.ko` are loaded at boot by `insmod_ko.sh`, but not when
you run a demo outside the normal boot flow. `ensure_npu_ready()` in `birdbox_main.py`
checks `lsmod` and loads them if missing — idempotent and safe on every start.

### UART3 pinmux

The serial port used by the audio module needs `UART3_M1_STATUS=1` in
`/etc/luckfox.cfg`. **This file resets to a default template on every rootfs
rebuild**, so re-apply it after reflashing.

---

## 3. RKNN demo

The model conversion toolkit (`rknn-toolkit2`) ships **Linux x86_64 wheels only** — no
macOS build exists — so model conversion needs its own container. Ubuntu 22.04 gives
Python 3.10, so use the `cp310` wheels; installing `python3.8` fails on jammy.

Two dependency traps:

- `ImportError: libGL.so.1` → `apt-get install -y libgl1-mesa-glx libglib2.0-0`
- `AttributeError: module 'onnx' has no attribute 'mapping'` → a too-new `onnx`.
  Install the toolkit's pinned requirements file in one go so pip resolves versions
  together, rather than installing packages one at a time.

Building the detection demo itself uses the SDK toolchain:

```bash
./build-linux.sh -t rv1106 -a armhf -d yolov5
```

Apply [`rknn-demo-no-image-write.patch`](rknn-demo-no-image-write.patch) to
`examples/yolov5/cpp/main.cc` first — it cuts inference from 2.41 s to 0.29 s by not
encoding an annotated PNG the trap never reads. Set `RKNN_OUT_IMAGE=<path>` when you do
want the annotated image, for threshold calibration.

**Replacing the demo does not require reflashing.** It is an ordinary userspace binary;
copy it over with `scp`.

---

## 4. Flashing

```bash
# in the container
cp /workspace/luckfox-pico/output/image/update.img /output/
```

On the host, with the board in MaskROM mode (hold BOOT while connecting USB-C):

```bash
softwareupdate --install-rosetta --agree-to-license   # once, Apple Silicon
xattr -cr ./upgrade_tool && chmod +x ./upgrade_tool   # once, if it segfaults
sudo arch -x86_64 ./upgrade_tool LD                    # expect Mode=Maskrom
sudo arch -x86_64 ./upgrade_tool uf ../update.img
```

**Disconnect the cells before flashing** — do not feed the board from two sources at
once. Reconnect them only after `Upgrade firmware ok.` and after unplugging USB-C.

### After any reflash, re-check

- `UART3_M1_STATUS=1` in `/etc/luckfox.cfg`
- `chmod -x /oem/usr/bin/rkipc`
- the init scripts for the trap and the recovery daemon

---

## 5. Getting a console

`eth0` takes DHCP. With no router, share the host's connection over Ethernet and find
the address in the host's DHCP leases. RNDIS over `usb0` does **not** work on macOS
without third-party kexts, so Ethernet is the practical route.

Note that the USB-A and USB-C ports cannot be used at the same time; switch modes with
`luckfox-config` → Advanced Options → USB.

---

Funded by Study@research (ID UB), Adam Mickiewicz University, Poznań. Project number: **201/34/UAM/0068**.
