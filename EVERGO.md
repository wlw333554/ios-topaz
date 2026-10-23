# ios-topaz on evergo (Redmi Note 11 5G)

Port of this project to the Xiaomi **Redmi Note 11 5G** (`evergo`,
21091116AC / 22101317G, MediaTek **MT6833 "Dimensity 810"**). The `topaz`
branch targets a Qualcomm SM6225 phone; this branch keeps the sweet XNU
base (which was already a 2×A76 + 6×A55 ARMv8.2 SoC) and moves it to
MediaTek silicon.

> **Status: scaffolding + first-boot bring-up, not a working phone.** The
> kernel configuration, Apple Device Tree, boot shim and build flow are in
> place; nothing has been tested on hardware yet. The blockers below (GPU,
> display, storage, touch) are whole-driver ports, each a project of its own.

## Why evergo is close to sweet (and far from topaz)

| | sweet (base) | topaz (this repo) | evergo (this branch) |
| --- | --- | --- | --- |
| SoC | Qualcomm SM7150 | Qualcomm SM6225 | **MediaTek MT6833** |
| CPU | 2×A76 + 6×A55, ARMv8.2 | 4×A53 + 4×A73, **ARMv8.0** | 2×A76 + 6×A55, **ARMv8.2** |
| 16K granule on all cores | yes | **no (Silver A53)** | **yes** |
| Boot core runs XNU | yes (CPU0) | no → Gold hand-off | **yes (CPU0, an A55)** |
| Bootloader | Qualcomm ABL | Qualcomm ABL | **MediaTek LK** |
| GPU | Adreno 618 | Adreno 610 | **Mali-G57 MC2** |
| Storage UFS | QC UFSHCI | QC UFSHCI | **MTK ufshci + MPHY** |
| Touch bus | QUP/GENI (QC) | QUP/GENI (QC) | **MTK SPI1, NT36672C** |
| WiFi/BT | QC WCN | QC WCN3988 | **MTK consys** |

The CPU story is the good news: evergo's MT6833 is the *same CPU
architecture* sweet was originally ported on (ARMv8.2, LSE atomics, PAN,
16K pages on every core). So:

- sweet's original patch series **0001–0079 applies unchanged**;
- topaz's **0081** (ARMv8.0: `-mcpu=cortex-a53`, no PAN, PFZ without CASA,
  userland LSE emulation) is **not needed and must not be applied**;
- topaz's **0082** (boot on Gold CPU `0x100`) is **not needed** — MTK LK
  boots CPU0 (`0x0`), which is exactly what sweet's original code expects;
- topaz's **0080** is replaced by **`0080e-evergo-cpu-topology-skip-sweet-hw`**;
- **all eight cores** can run XNU (`cpus=8`); there is no Gold/Silver split.

Everything else (display, storage, touch, WiFi, GPU, audio) is
MediaTek-vs-Qualcomm silicon and needs new drivers — see *Blockers*.

## Hardware facts (evergo)

From the live device tree (`mt6833.dts` + `evergo.dts`,
[Xiaomi-MT6833/kernel_xiaomi_evergo](https://github.com/Xiaomi-MT6833/kernel_xiaomi_evergo)
branch `R`):

| Item | Value |
| --- | --- |
| SoC | MT6833, 6 nm; 6× Cortex-A55 @ 2.0 GHz + 2× Cortex-A76 @ 2.4 GHz |
| CPU MPIDR | A55: `0x000`–`0x005`; A76: `0x100`–`0x101` (MTK puts little first) |
| GICv3 | GICD `0x0c000000` (0x40000), GICR `0x0c040000` (0x200000) |
| Arch timer | 13 MHz; vtimer PPI 13 → **INTID 29**, ptimer PPI 14 → INTID 30 |
| DDR | dtsi bank0 `0x40000000 + 0x3e605000` (LK patches real banks into `/memory`); fixed carve-outs live below `0x80000000` |
| Splash FB | `atag,videolfb-fb_base_l = 0x7e605000`, vram `0x1be0000`, 60 fps, 1080×2400 (MTK LK passes it in DTB `/chosen`) |
| Panel | `k16a_36_02_0a_vdo` / `k16a_42_02_0b_vdo`, DSI **video** mode; RST gpio86, backlight gpio87 |
| Display blocks | dsi `0x14013000`, ovl0 `0x14005000`, rdma0 `0x14007000`, mutex `0x14001000`, dispsys `0x14000000` |
| UFS | `ufshci@11270000` (0x2300), MPHY `0x11fa0000`, IRQ SPI 105 |
| Touch | Novatek **NT36672C** on **SPI1** `0x11010000`, RST gpio13, IRQ gpio14, 9.6 MHz, 16×36 channels |
| WiFi/BT | MTK consys: `wifi@18000000`, `consys@18002000` (CONN_MCU_CONFIG), TOP_RGU `0x10007000` |
| UART0 | `0x11002000` (26 MHz) — pads not exposed, fbcon is the console |
| Watchdog | TOP_RGU `0x10007000` |
| Clocks | topckgen `0x10000000`, pericfg `0x10003000`, sys_timer `0x10017000` (13 MHz); 26 MHz/13 MHz/32 kHz fixed clocks |
| PMIC | MT6360 (rtc, charger, codec) |

## What is in this branch

| Path | What |
| --- | --- |
| `shim/device.h`, `device_topaz.h`, `device_evergo.h` | Per-device constants; `DEVICE=evergo shim/build.sh` selects |
| `shim/shim.c` | Boot-core detection (no Gold hand-off on evergo), GIC prep for all 8 cores, 64K GICR page walk, MTK reset path (PSCI only) |
| `shim/fdt.c` | `/memory` bank parsing + `atag,videolfb-*` from DTB `/chosen` |
| `shim/loader.c` | Payload from the **initrd** (MTK LK passes it; Image-embedded fallback), runtime **DDR clamp** to the real `/memory` bank (patches payload header + ADT `/chosen` dram props), boot_args Video from the runtime FB |
| `shim/build.sh` | `DEVICE=topaz\|evergo`, `HEADER_VERSION=2\|3\|4`; evergo packs the payload into the (gzip) ramdisk |
| `tools/mkadt_evergo.py` | Apple Device Tree for MT6833: 8 cores, GIC/timer/FB/UFS/consys addresses, iPhone X identity, `--qemu`/`--highmem`/`--native` |
| `tools/build-evergo.sh` | One-shot: ADT → payload → shim boot image |
| `xnu/patches/0001–0079` | sweet base (unchanged, applies as-is) |
| `xnu/patches/0080e-*` | evergo CPU topology (MPIDR map) + sweet QC input drivers gated behind `sweethw=1` |
| `.github/workflows/xnu.yml` | Kernel CI, `evergo` branch; **no** `-mcpu=cortex-a53` (MT6833 is ARMv8.2) |
| `xnu/patches/0080+ (topaz)` | **Removed on this branch** — Qualcomm-specific. They stay on the `topaz` branch. |

### Runtime DDR sizing

The ADT is built for bank1 (`0x80000000` + 2 GiB by default, `--highmem`
for the top bank). MTK LK reports the detected banks in the DTB `/memory`
node; the shim finds the bank that contains the kernel's `phys_base` and
shrinks the kernel RAM to fit (never below 128 MB), patching the payload
header and the ADT `/chosen` `dram-base`/`dram-size` to match. One image
therefore boots the 4/6/8 GB variants — as long as the chosen build-time
bank exists on the device. The 4 GB model has bank1 = 2 GiB; if your unit
reports a different layout, watch the shim's `MEM bank/size` lines on the
console and rebuild with `EXTRA_ADT=--highmem` (or edit `PHYS_BASE`) if
needed.

## Build

```sh
# 0. (optional) dry-run the whole tooling chain locally, no kernel needed.
#    Exercises mkadt -> mkpayload -> shim cross build -> ramdisk -> mkbootimg
#    with a synthetic Mach-O, so a CI cycle is never wasted on a tool bug.
python3 tools/local-check-zig.py --mkbootimg /path/to/AOSP-mkbootimg.py

# 1. kernel (GitHub macOS runner, ~8 min) - same as topaz, SWEET config
gh workflow run xnu.yml -R YOU/ios-topaz --ref evergo
gh run download RUN_ID -R YOU/ios-topaz -n xnu-kernel -D xnu/out

# 2. ADT + payload + shim boot image (Linux)
tools/build-evergo.sh xnu/out/kernel.development.sweet        # no root yet
# or with a RAM-disk root (mockfs, rd=md0):
tools/build-evergo.sh xnu/out/kernel.development.sweet path/to/root.img

# 3. boot (RAM only; nothing is written)
fastboot boot shim/out/shim-boot.img
```

### First-boot checklist

1. Unlock the bootloader and keep a copy of the stock `boot.img`
   (`fastboot getvar all` works, and `fastboot boot` never writes anything,
   so a bad image only costs a reboot - hold Power to get back to fastboot
   or to LK's normal boot).
2. `fastboot boot shim/out/shim-boot.img`. LK loads it at the kernel address,
   the shim runs first.
3. **Milestone 1** is only this much: the shim screen (red) comes up with
   `MIDR`/`MMFR0` (make sure `tg16` is non-zero), `GICD`/`GICR` lines,
   `MEM bank/size` lines, and `XNU jumping`; then XNU paints its own console
   into the splash framebuffer (`sweet-fbcon`). A `no root` / `vfs` panic
   afterwards is expected and fine - it means the kernel booted.
4. Copy the `MEM bank/size` lines: they are the real MT6833 DDR layout the
   shim derived from the DTB. If the kernel's RAM window lands outside a
   real bank, raise `PHYS_BASE` in `tools/mkadt_evergo.py` to match.
5. If nothing appears on screen, the splash framebuffer address is the first
   suspect - the shim reads `atag,videolfb-fb_base_l` from the DTB and falls
   back to `0x7e605000`. On a device with a different splash reserve the
   screen stays black while the kernel actually boots; check with
   `fastboot oem` logging or a UART if you have one.

### Toolchain notes

- `tools/local-check-zig.py` needs `zig` (or `pip install ziglang`) and AOSP's
  `mkbootimg.py`. It builds with clang, which unlike the repo's GNU `as`
  rejects an EL1 `smc #0`; the script rewrites that one instruction in a
  scratch copy, `shim/` is left untouched.
- AOSP `mkbootimg.py` imports `gki.generate_gki_certificate` unconditionally
  but only calls it for header **v4** + `--gki_signing_*`. Using the tool
  standalone (outside the AOSP tree) therefore fails at import time;
  `shim/build.sh` detects that and puts a two-line stub on `PYTHONPATH`
  (`shim/out/gki-stub`) unless the real module is importable. Set `MKBOOTIMG`
  to a full AOSP checkout if you want the real thing; `build.sh` otherwise
  looks for `mkbootimg.py` next to itself, then in `tools/`.

Notes:

- The shim screen (red) prints `MIDR`/`MMFR0` (`tg16` must be non-zero),
  `GICD`/`GICR` state, `MEM bank/size` lines, then `XNU jumping`. XNU then
  draws its own console into the splash framebuffer (`sweet-fbcon`).
- Use `HEADER_VERSION=3` (default). If MTK LK refuses the image, try
  `HEADER_VERSION=4`; both are accepted by evergo's bootloader.
- The kernel build must **not** include topaz's 0081 patch; the CI on this
  branch already builds with SWEET's `-march=armv8.2-a+crypto`.
- Boot-args worth knowing: `cpus=8` (default on this SoC), `debug=0xc`
  (kprintf to the framebuffer console), `sweethw=1` would start sweet's
  Qualcomm input/USB drivers — **do not** use it on evergo.

## Blockers (whole-driver ports)

Each of these is MediaTek silicon with no XNU driver anywhere in this
project; the topaz equivalents are all Qualcomm-specific.

1. **GPU — Mali-G57 MC2.** The biggest blocker for anything graphical:
   topaz draws the UI through Mesa **freedreno** (Adreno-only) plus
   kernel patch series 0900+ and the `userland/` stack. There is no Mali
   equivalent; Panfrost is Linux-only. Until a Mali kernel driver exists,
   CoreAnimation cannot render — the CPU framebuffer console is the only
   output.
2. **Display — MTK DSI/OVL/RDMA.** topaz's direct MDP scanout
   (patches 0092–0120) pokes the Qualcomm display controller. evergo needs
   an MTK `disp_ovl0`/`disp_rdma0`/`dsi` path (or a simple RDMA blit into
   the splash framebuffer as an interim) before any iOS surface can be
   presented.
3. **Storage — MTK ufshci.** `bsd/dev/sweet_ufs.c` is a Qualcomm UFSHCI
   2.1 driver. MTK's controller at `0x11270000` needs its MPHY
   (`0x11fa0000`) initialised and a different register interface. Until
   then there is no HFS+ iOS root: boot with `rd=md0` (RAM-disk root via
   mockfs) or expect the no-root panic.
4. **Touch — Novatek NT36672C on SPI1.** sweet/topaz touch drivers speak
   QUP/GENI. The NT36672C is an SPI device with its own firmware/protocol
   (see the Novatek driver in the evergo kernel tree).
5. **WiFi/BT — MTK consys.** The `TopazModem`/WCN series (0113–0130) is
   Qualcomm QRTR/GLINK/WLAN. evergo's combo chip needs MTK consys firmware
   loading, its own host interface, and a new `wifid` glue path.
6. **Audio — MTK ADSP / AudioReach.** `RedwoodAudio` and the ADSP patches
   (0144–0146) are Qualcomm. MT6360 codec + MTK ADSP is a different graph.
7. **Sensors / RTC / vibrator / NFC / fingerprint.** No XNU drivers;
   the PMIC RTC (MT6360) would fix the "31 December" clock.

## Suggested order of work

1. **Kernel boots, console on splash FB** (this branch's milestone 1):
   GICv3 + 13 MHz timer + all-core PSCI + fbcon. Expected to reach the
   no-root panic with a readable log.
2. **RAM-disk root** (`rd=md0`, `tools/mkinit.py` sweet-init) — first
   userspace PID 1 on MTK.
3. **MTK UFS driver** → HFS+ iOS root in `userdata` (same layout as
   topaz: `tools/mkiosroot.sh` + `sweet-root-tweaks.py` are device-neutral).
4. **Minimal display path** (RDMA blit or DSI command mode into the splash
   FB) → `AppleCLCD`/IOMobileFramebuffer surfaces.
5. **Touch** (NT36672C over SPI1) → HID events.
6. Only then: GPU (Mali) — the long pole — and the rest.

## References

- evergo kernel/device tree: `Xiaomi-MT6833/kernel_xiaomi_evergo` (branch `R`),
  `arch/arm64/boot/dts/mediatek/mt6833.dts`, `evergo.dts`,
  `cust_mt6833_touch_nt36672c_1080x2400.dtsi`
- Base port: `dipa-studio/ios-sweet` (XNU on Redmi Note 10 Pro / SM7150)
- This project: `MakrSas/ios-topaz` (`topaz` branch), `MakrSas/topaz-woa`
- HackiOS (`oosexgang/hackios-xnu-kernel` + `-userland`): same XNU
  generation with the iOS 16.5 userland on a POCO X5 Pro 5G (Snapdragon
  778G). Qualcomm again, so its drivers do not cover the MTK blockers, and
  its UEFI boot chain is Qualcomm-only (MTK LK has no UEFI). What is worth
  copying: the DTB-derived device profile (`common/new-device.sh`), the
  loader platform description (same fields as `shim/device_evergo.h`), and
  the idea of running Linux drivers (there ath11k) on a LinuxKPI-style
  compatibility layer — the realistic long-term path for evergo's MTK DSI
  display and consys Wi-Fi drivers, which exist in the Linux kernel.
  Their QEMU-virt workflow matches `mkadt_evergo.py --qemu` here.
- XNU: xnu-8796 (iOS 16 generation), iPhone X (iPhone10,3 / D22AP) userland
