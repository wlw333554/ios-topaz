# NOTES-evergo — engineering log (evergo / MT6833)

Append-only handoff log for the evergo (Redmi Note 11 5G, MT6833) port of
ios-topaz. Distinguish observations (from the device tree / source) from
guesses. Read `EVERGO.md` for the full picture; this file records what was
done, decided and observed.

## 2026-10-23 — initial evergo adaptation (no hardware testing yet)

**Goal.** Move the project from topaz (SM6225, Qualcomm) to evergo (MT6833,
MediaTek). No phone boot has been attempted; everything below is from
source and the evergo live device tree.

**Key finding — the CPU base matches sweet, not topaz.** sweet's SM7150 is
2×A76 + 6×A55 ARMv8.2; MT6833 is the same CPU architecture (6×A55
`0x0`–`0x5` + 2×A76 `0x100`–`0x101`). Therefore sweet's patches 0001–0079
apply unchanged, and topaz's 0080 (ARMv8.0 workarounds) and 0081 (Gold
hand-off) are *wrong* for evergo. MTK LK boots CPU0 (`0x0`, an A55) which
has the 16K granule, so XNU runs on the boot core directly and can use all
8 cores (`cpus=8`).

**Hardware facts (observed, from `Xiaomi-MT6833/kernel_xiaomi_evergo`
branch R, `mt6833.dts` + `evergo.dts`):**

- GICv3: GICD `0x0c000000` (0x40000), GICR `0x0c040000` (0x200000).
- Arch timer 13 MHz (`timer { clock-frequency = <13000000> }`); vtimer PPI
  13 → INTID 29, ptimer PPI 14 → INTID 30 (topaz used 19/18 — QC wiring).
- Memory dtsi bank0 `0x40000000 + 0x3e605000`; MTK LK patches the detected
  banks into the DTB `/memory` node (guess: bank1 `0x80000000`+2G, bank2
  `0x1_40000000`+1G on the 4 GB model — to be confirmed on first boot from
  the shim's `MEM bank/size` console lines).
- Splash FB `0x7e605000`, vram `0x1be0000`, 60 fps — passed by MTK LK in
  DTB `/chosen` as `atag,videolfb-fb_base_l` (observation), so the shim
  reads it at runtime instead of hardcoding.
- UFS `ufshci@11270000` (0x2300) + MPHY `0x11fa0000`; display dsi/ovl/rdma
  at `0x1401{3000,5000,7000}`; touch Novatek NT36672C on SPI1
  `0x11010000` (RST gpio13, IRQ gpio14, 9.6 MHz); consys wifi
  `0x18000000` / `consys@18002000`; TOP_RGU (watchdog) `0x10007000`.

**Decisions.**

- Device split: `shim/device.h` + `device_topaz.h` + `device_evergo.h`;
  `DEVICE=evergo shim/build.sh`. topaz behaviour is unchanged.
- Payload placement: evergo packs the payload into the boot ramdisk (MTK
  LK passes it in `/chosen`, unlike topaz's ABL which drops the v4 boot
  ramdisk); `loader.c` scans the initrd first, Image-embedded second.
- DDR sizing at runtime: shim parses DTB `/memory`, clamps the kernel RAM
  to the bank containing `phys_base` (>=128 MB), patches the payload header
  and the ADT `/chosen` dram-base/dram-size in place. Default build target
  is bank1 `0x80000000` + 2 GiB (above all fixed carve-outs, whose
  dts alloc-ranges are `0x40000000..0x80000000`).
- GICR walk changed to 64K page granularity with TYPER-affinity matching
  (MTK reserves 0x200000 total; the fixed 0x20000 stride of the topaz shim
  would miss frames). Works for QC layouts too.
- Patch set on this branch: sweet 0001–0079 + `0080e` (topology + sweethw
  gate). topaz 0080–0152/0900–0950 removed (Qualcomm-specific).
- CI `xnu.yml`: no `-mcpu=cortex-a53` (that was for topaz's ARMv8.0
  cores); SWEET keeps `-march=armv8.2-a+crypto` from patch 0004.
- Boot image header version 3 by default (MTK LK accepts 3 and 4; 4 to be
  tried if 3 misbehaves).

**Verified locally.** `tools/mkadt_evergo.py` generates a 10156-byte ADT
that dumps correctly: 8 CPU nodes (MPIDR 0–5, 0x100, 0x101), GIC
`<0x4000000 0x40000 0x4040000 0x200000>` (i.e. `0x0c000000`/`0x0c040000`
under SOC_BASE `0x08000000`), `virtual-timer-intid 0x1d` (29), timebase
`0xc65d40` (13 MHz), fbcon `0x7e605000`, ufs `0x11270000`.
`--qemu` (1 CPU, vtimer 27), `--highmem` (`0x1_40000000` + 1 GiB) and
`--native` (1080×2400 canvas) modes verified by re-parsing the output.

**Compile-verified the shim** (no aarch64 toolchain on this machine; used
zig 0.16.0's clang as the cross compiler, `smc #0` → `.inst 0xd4000001`
because clang's integrated assembler rejects smc outside EL3 while the real
build's GNU as accepts it — repo code keeps `smc #0`):

- Both `DEVICE=EVERGO` and `DEVICE=TOPAZ` compile clean (no warnings) and
  link with `shim.ld`; `_head` at 0, entry branches to `_start`, `_end`
  = 0x2f000 (build.sh truncates the Image there and appends the payload on
  topaz).
- Bugs this caught and fixed:
  1. `shim.c` called `fdt_chosen_u64()` without including `fdt.h`.
  2. **`secondary_gicr_prepare()` was never called on evergo** — with the
     Gold hand-off compiled out, nothing prepared the 7 secondary
     redistributors (SGIs/PPIs Group 1, priorities, timer enables) before
     XNU starts them with PSCI CPU_ON. Now `shim_main()` runs
     `test_gic(); secondary_gicr_prepare();` before `loader_boot()`
     (topaz keeps doing it inside `gold_main()`).
  3. `loader.c`: `hit` was declared after its first use in the non-evergo
     branch (and a shadowing redeclaration would have broken the topaz
     payload search) — declaration moved to the top of `loader_boot()`.
  4. `ufs.c` relies on gcc expanding `strcmp(x, "literal")` inline; clang
     emits a libcall and the shim links `-nostdlib`. Added a small
     non-static `strcmp()` to `ufs.c` (dead-stripped when gcc inlines it),
     which also makes the shim buildable with clang.
- `speed_ticks()` marked `__attribute__((unused))` on evergo (only
  `gold_main()` uses it).

**Next actions.**

1. Build the kernel on CI (branch `evergo`), then
   `tools/build-evergo.sh xnu/out/kernel.development.sweet`.
2. First phone boot (`fastboot boot shim/out/shim-boot.img`), unlocked
   bootloader, everything on `userdata` is lost as on topaz. Milestone 1:
   shim screen shows `tg16 != 0`, `GICR+` lines for all 7 secondaries,
   `MEM bank/size` lines, then XNU's console on the splash FB and the
   no-root panic (no MTK UFS driver yet).
3. Confirm the real `/memory` banks from the console; adjust `PHYS_BASE`
   or use `--highmem` if the default bank1 assumption is wrong.
4. Then: RAM-disk root (`rd=md0`, sweet-init), MTK UFS driver, minimal
   display path, NT36672C touch — see EVERGO.md *Suggested order of work*.

## 2026-10-23 (later) — reviewed HackiOS (oosexgang/hackios-xnu-kernel + -userland)

- HackiOS boots the same XNU generation (xnu-8796) with an unmodified iOS
  16.5 userland on the **POCO X5 Pro 5G (redwood, Snapdragon 778G)** —
  Qualcomm again, so its drivers (Adreno 642L, WCN6750/ath11k, Goodix
  GT9916, ADSP audio) do **not** cover evergo's MediaTek blockers.
- Its boot chain (Mu-Silicium **UEFI** started by `fastboot boot`) is
  Qualcomm-specific; their PORTING.md says a Qualcomm phone is "the
  realistic next target" — MTK LK has no UEFI, which confirms the
  bare-metal shim approach used here.
- What transfers and confirms this branch's design: device profiles
  generated from the phone's DTB (`common/new-device.sh <codename>
  device.dtb`); the loader's `struct platform` description (io_base/gic/
  cores/core_shift/small_cores/psci_hvc/fb_base) — the same fields as
  `shim/device_evergo.h`; `io_base` "must not start at 0 (XNU then skips
  clock setup)" (SOC_BASE 0x08000000 here); PSCI via SMC on phones, HVC on
  QEMU; GICv3/timer/PSCI-SMP/framebuffer listed as the generic layer.
- Long-term idea worth remembering: their Wi-Fi is Linux's **ath11k** built
  on a FreeBSD **LinuxKPI** compatibility layer. MTK's DSI/RDMA display and
  consys Wi-Fi drivers exist in the Linux kernel — a similar shim layer is
  the realistic path to evergo's display/Wi-Fi blockers (big project, not
  for this milestone).
- They also run the same system on QEMU virt (no phone) — the `--qemu`
  mode of `mkadt_evergo.py` serves the same purpose here.

## 2026-10-23 (later) — local end-to-end tooling check passes

Added `tools/local-check-zig.py`: runs the *whole* evergo build chain with a
synthetic minimal arm64 Mach-O in place of the real kernel, so a CI cycle is
never spent on a tooling bug. Steps: `mkadt_evergo.py` → `mkpayload.py`
(with a `--ramdisk`) → shim cross-compiled with zig clang → link with
`shim.ld` → `objcopy -O binary` → newc cpio + gzip ramdisk carrying the
payload (evergo packing) → `mkbootimg.py --header_version 3` → round-trip.

Result (all green):

```
1. ADT ok: 10156 bytes, dram 0x80000000+0x80000000
2. dummy kernel: 1416 bytes, entry 0x100000040
   payload ok: 77904 bytes, magic SWEETXNU
3. shim ok: Image 258048 bytes (image_size/_end 0x3f000)
4. ramdisk ok: 78264 bytes cpio -> 2234 gzip, payload inside
5. boot image ok: 266240 bytes, magic ANDROID!, kernel 0x3f000, ramdisk 0x8ba
6. round-trip ok: SWEETXNU payload findable in the packed ramdisk
```

What that proves: `mkpayload.py` accepts our MT6833 ADT (incl. appending the
RAM-disk segment and patching `/chosen/memory-map` `RAMDisk`), the shim still
builds for `DEVICE_EVERGO` after the shared-code refactor, the evergo
ramdisk packing works, and the v3 boot header is well formed.

Also regression-checked `DEVICE_TOPAZ` with the same refactored sources:
compiles with zero warnings and links (`shim.elf` 154864 bytes), so the
device split did not break the topaz path.

Two tool gotchas worth remembering (both handled inside the script, plus
documented in EVERGO.md *Toolchain notes*):

- clang's integrated assembler rejects an EL1 `smc #0`; the repo builds with
  GNU `as` (which accepts it) so the source keeps `smc #0` and the script
  only rewrites a scratch copy.
- AOSP `mkbootimg.py` imports `gki.generate_gki_certificate` at module load,
  but only calls it for header **v4** + `--gki_signing_*`; a 2-line stub on
  `PYTHONPATH` is enough for our v3 images.
