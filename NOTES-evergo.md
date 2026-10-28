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
  11 → INTID 27, ptimer PPI 14 → INTID 30 (topaz/QC used 19/18 — QC PPI wiring; MTK uses standard ARM PPI 11/14).
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
under SOC_BASE `0x08000000`), `virtual-timer-intid 0x1b` (27), timebase
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

## 2026-10-24 — TWRP dmesg: runtime ground truth for DISP + UFS

Captured `E:\iOS\twrp_dmesg.txt` from a TWRP recovery booted on evergo
(`fastboot flash boot twrp.img`). This is the **runtime** answer (not
source/dts), so it locks down the display and UFS bring-up that the port
still needs.

**Display HW map (confirmed live — matches `device_evergo.h`):**

- `dispsys_config` `0x14000000`; `disp_ovl0` `0x14005000` (comp_id 18);
  `disp_ovl0_2l` `0x14006000`; `disp_rdma0` `0x14007000`; `disp_rsz0`
  `0x14008000`; `disp_color0` `0x14009000`; `disp_ccorr0` `0x1400b000`;
  `disp_aal0` `0x1400c000`; `disp_gamma0` `0x1400d000`;
  `disp_postmask0` `0x1400e000`; `disp_dither0` `0x1400f000`; `dsi`
  `0x14013000` (comp_id 12); `disp_wdma0` `0x14014000`; `disp_pwm0`
  `0x1100e000`; `disp_smi_2x1_sub_common_u0/u1` `0x1401b000`/`0x1401c000`;
  `m4u (iommu)` `0x14016000`; `emicen` `0x10219000`.
- **`DISP_OVL0_L0_ADDR = 0x14005f40` is confirmed**: it is `disp_ovl0`
  base `0x14005000` + `DISP_REG_OVL_ADDR_MT6833 = 0x0f40` (layer 0). So the
  shim's "read OVL0 layer0 src addr, write console there" hijack reads the
  buffer the panel is *actually* scanning.
- Panel probe string: `wt,tianma,nt36672c,cphy,vdo,90hz` → **Tianma
  `k16a_36_02_0a_vdo`** (current LCM), **CPHY** (not D-PHY), **VDO video
  mode** (continuous refresh — our console writes will be visible if LK
  leaves DISP on), **90 Hz**. Touch = Novatek NT36672C. A second CSOT panel
  driver exists but is skipped (`csot_probe+ skip probe due to not current
  lcm`). Resolution 1080×2400 (`mtk_dsi_connector_get_modes` htotal 1364 →
  1080 active; vertical 2400 from dts).
- Backlight = **LM36273** (TI LED driver) via `leds_mtk_disp`
  (`lcd-backlight`, max 2047); `lm36273_brightness_set backlight=1200,
  temp_level=847`. So backlight is an I2C/DSI-controlled LED driver, **not**
  a bare PWM/gpio — if LK powers it off on hand-off we get a black (but
  still-scanning) screen and would need to re-enable LM36273.

**UFS (confirmed live):**

- `ufshci@11270000` (0x2300), MPHY `0x11fa0000`. Device **SAMSUNG
  KM8F9001JM-B813** (UFS 2.2). Negotiated power mode: **HS-G3, 2 lanes,
  rate B (rate=2), FAST MODE** (from `gear=[3,3], lane[2,2]`). Initial
  link is SLOWAUTO gear=[1,1] lane[1,1] then ramps up.
- Regulators `vdd-hba`/`vccq`/`vccq2` are "assuming enabled" (no regulator
  control in the dts node) — good simplification for a shim UFS driver.
- Device reset via `ufs_mtk_pltfrm_ufs_device_reset`, host sw reset
  `ufs_mtk_host_sw_rst`. Vendor crypto clks skipped.
- `HAS_UFS_PROBE` is still opt-in/off; this is the data the read-only probe
  would dump, useful when writing the real driver.

**Implication for the boot screen.** The OVL0-layer0 hijack is architecturally
correct and the dmesg validates every address. VDO mode means the panel is
continuously refreshed from that buffer. So `shim/out` build
`cb59c855…` (which reads `0x14005f40`, falls back to `atag,videolfb-fb_base_l`
`0x7e605000`) should show: red SHIM banner → MMFR0/GICR/MEM/JUMP → XNU text
on the **same** buffer (`loader.c` sets `ba->Video.v_baseAddr = con_fb_addr()`).
Remaining risk if it is still black: MTK LK powering off DISP/backlight on
hand-off → need a minimal DISP takeover (OVL0 + DSI/CPHY + LM36273 backlight).

**Next action.** Flash `shim-boot-root.img` (cb59c855…), report what the
screen shows (red banner / MTK logo / black). That single observation
decides whether to escalate to a DISP takeover.

## 2026-10-24 (later) — first phone boot attempt: result + diagnosis

Flashed `cb59c855…` `shim-boot-root.img` to `boot` (A/B: `boot_a`/`boot_b`).
**Result: MTK logo shows ~a few seconds (LK splash), then BLACK, then
reboot.** The red SHIM banner is NEVER seen.

**Diagnosis (high confidence).** The logo is LK (bootloader) showing its
splash; the black screen is LK **blanking / powering off the display right
before it jumps to the kernel** (our shim), which does not re-light it — so
`con_clear(RED)` writes to a buffer that is no longer scanned. This
empirically **confirms the "LK powers off DISP on hand-off" hypothesis**
from the section above. The reboot is ambiguous: either XNU reaches a panic
(no UFS/root driver yet) and resets, or a residual WDT fires — it implies
the **shim→XNU chain may actually be functional** and only the display is
missing (good news for the port, bad for visibility).

**Implication.** Seeing XNU logs now requires the shim to **re-light the
display** before/at hand-off:
- Re-enable OVL0 layer0 → our console framebuffer (base `0x14005000`, src
  addr `0x14005f40`, layer0 con/size/pitch regs needed).
- Un-gate DISP clocks / power domains (DISPSYS `0x14000000` CG regs) if LK
  gated them.
- Re-enable backlight (LM36273, I2C-controlled) and re-init the panel
  (Tianma `k16a_36_02_0a_vdo`, CPHY/VDO) only if it was put to sleep.
- All init sequences live in `Xiaomi-MT6833/kernel_xiaomi_evergo`
  (`mtk_disp_ovl.c`, panel tianma LCM driver, `lm36273` backlight,
  `mtk_dsi.c`). This is the "minimal display path" milestone — a real
  project, not a one-liner.

**Faster alternative: UART serial.** MTK UART0 (`console=ttyS0,921600n1` in
the TWRP cmdline; AP UART0 base ~`0x11002000`) — add a ~20-line `putc` to
the shim and let XNU's early console (patch 0003) use it; capture with a
USB-TTL on the board's UART test points. Gets the panic visible in minutes,
no display driver needed, but requires soldering/hooking pads.

**Decision pending from user:** pursue the screen (DISP takeover), UART
serial, or both.

Two tool gotchas worth remembering (both handled inside the script, plus
documented in EVERGO.md *Toolchain notes*):

- clang's integrated assembler rejects an EL1 `smc #0`; the repo builds with
  GNU `as` (which accepts it) so the source keeps `smc #0` and the script
  only rewrites a scratch copy.
- AOSP `mkbootimg.py` imports `gki.generate_gki_certificate` at module load,
  but only calls it for header **v4** + `--gki_signing_*`; a 2-line stub on
  `PYTHONPATH` is enough for our v3 images.

## 2026-10-24 (final) — DISP takeover implemented (user chose screen over UART)

User picked **screen DISP takeover** over UART serial. Implemented
`shim/disp.c` + `shim/disp.h`, gated by `HAS_DISP_TAKEOVER` (evergo only) and
called from `shim_main()` right after `con_clear(RED)`. Rebuilt via
`build_evergo_zig.py --root` → `shim-boot-root.img` (6.1 MB, ANDROID! v2,
VERIFY ok, contains SWEETXNU), sha256 **f5083876…** (replaces cb59c855…).
Staged into the flash package dir.

**What the takeover does (runs with MMU OFF, physical addresses):**

1. **Re-enable OVL0.** Soft-reset the engine (`OVL_RST` 1→0), set `ROI_SIZE`
   = 1080×2400, point **layer 0** at our console framebuffer
   (`DISP_REG_OVL_ADDR(0)` = base `0x14005000` + `0x0f40`), `SRC_SIZE` =
   1080×2400, `OFFSET` = 0, `PITCH` = `FB_STRIDE` (**bytes**, not pixels —
   confirmed from `mtk_disp_ovl.c`), `CON` = `OVL_FMT_ARGB8888 (3<<12)` |
   `AEN` | alpha 0xff (matches our console's B,G,R,A memory order; word
   `0xAARRGGBB`). Enable the layer's RDMA (`RDMA_CTRL`=1, `GMC`=0x40402020),
   set `SRC_CON` layer0-enable bit, then `OVL_EN` = `1 | BYPASS_SHADOW(bit22)`
   and kick `OVL_TRIG`. (Source of every offset: `mtk_disp_ovl.c`, MT6833 addr
   offset `0x0f40`; shadow/bypass confirmed from the R-branch driver.)
2. **Re-pulse the backlight.** `lcm-bl-enable-gpios = <&pio 87 0>`: drive
   GPIO 87 high via the **modern mediatek "paris" PIO layout** (base
   `0x1000b000`: `MODE` at `+0x300+(pin/8)*0x10` 4b/pin, `DIR` at `+0x000`,
   `DOUT` at `+0x100`, 1b/pin, 32 pins/reg). RMW MODE→0 (GPIO), DIR→out,
   DOUT→high.

**Deliberately left to LK (not re-initialised):** the DISP **MUTEX**, the
**DSI/CPHY** link and the **panel** hardware. LK configured all of these for
the splash; at hand-off it only *stops OVL scanout + turns the backlight off*.
So the minimal v1 lever is OVL + backlight. **ASSUMPTION:** LK did NOT disable
the MUTEX or power down the panel — if it did, v1 stays black.

**If v1 is STILL black → next levers (in order):**
- Re-enable the DISP MUTEX (RMW its EN bit0, preserving LK's existing
  MOD/SOF — base is inside `dispsys_config@0x14000000`, most likely
  `0x14001000`; the mutex is the frame synchroniser and without it no frames
  flow even with OVL on).
- If LK fully powered the panel down: re-pulse panel `pm-enable-gpios`
  `<&pio 136 0>`, `bias-gpios` `<&pio 137 0>,<&pio 138 0>`, toggle
  `reset-gpios` `<&pio 86 0>` — then re-init DSI + Tianma `k16a_36_02_0a_vdo`
  (big: needs the panel DCS init sequence + CPHY/VDO DSI config).
- Backlight may actually be I2C LM36273, not pure GPIO: if GPIO 87 alone
  doesn't light it, re-send the LM36273 brightness via I2C (needs an I2C
  driver in the shim).

**Why this is the decisive test.** If the red SHIM banner now appears, the
**shim→XNU chain is proven functional** and the subsequent XNU early console
(`loader.c` shares the same framebuffer) will show the panic that is currently
causing the reboot — which names the next blocker (UFS/root driver). If it
stays black, escalate to the levers above.

## 2026-10-24 (22:08) — DISP v2: also re-arm DISP MUTEX0

v1 (OVL0 + backlight GPIO 87) produced **nothing visible**. Strong signal that
LK at hand-off stops the **DISP MUTEX** (frame synchroniser): OVL0 → RDMA0 →
DSI0 frames are gated by it, so re-enabling OVL alone can't push pixels to the
panel. This is consistent with the LK Redmi logo having been visible earlier
(its OVL path ran under an enabled MUTEX).

**v2 change (`shim/disp.c`):** added `disp_mutex_resume()` — reads MUTEX0 MOD/SOF/CTL
(keeping LK's existing display-path config) and sets `MUTEX_EN(0)` bit0. Called
first in `disp_takeover()`, before the OVL re-enable + backlight. MUTEX base
`0x14001000` (inside `dispsys_config@0x14000000`); `EN=+0x000`, `CTL=+0x004`,
`MOD=+0x008`, `SOF=+0x00c`, stride 0x20/instance (from `mtk_disp_mutex.c`).
Rebuilt `build_evergo_zig.py --root` → `shim-boot-root.img` (6,117,376 B,
VERIFY ok), sha256 **ca9be120…** (replaces f5083876…), re-staged into the flash
package dir (overwrote `shim-boot-root.img` + refreshed its `.sha256`).

**If v2 is STILL black** → remaining levers (each a bigger step, blind):
- Re-pulse panel GPIOs and re-init DSI + Tianma `k16a_36_02_0a_vdo`
  (`pm-enable <&pio 136>`, `bias <&pio 137>,<&pio 138>`, `reset <&pio 86>`).
- Backlight may actually be I2C **LM36273**, not the GPIO-87 enable line — would
  need a shim I2C driver (bus base + slave addr + reg sequence are guesses).
- **Reliable alternative: UART.** With no output channel at all, every iteration
  is a blind guess. Tapping the MTK UART test points with a USB-TTL adapter (and
  a shim UART driver) is the only way to actually see shim/XNU output and stop
  guessing register by register. QEMU shows the *generic* XNU boot (already
  reaches idle) but cannot model MTK display/GIC, so it won't reveal the panel
  fix.

## 2026-10-24 (16:31) — DISP v3: CORRECTED MUTEX register layout (the real fix)

**Root cause of v1/v2 black screen found.** v2's MUTEX offsets were **wrong**.
I had guessed `EN=+0x000, CTL=+0x004, MOD=+0x008, SOF=+0x00c` (stride 0x20),
but the **real** layout (extracted from the actual evergo kernel
`mt6833/dispsys/ddp_reg_mutex.h`) is **per-mutex stride 0x20** with:
- `EN  @ +0x020`, `RST @ +0x028`, `SOF @ +0x02C`, `MOD0 @ +0x030`.
- There is **no per-mutex "CTL" register** at all (the only CFG is global at
  `base+0x008`, left untouched).

So v2's `disp_mutex_resume()` literally wrote bit0 of `MUTEX_INTEN` (base+0x000)
instead of `MUTEX0_EN` (base+0x020) and never re-armed the synchroniser → the
OVL→DSI path stayed gated → black. The offsets, not just "LK left MOD=0", were
the bug. MUTEX physical base **0x14001000** (from `evergo.dts disp_mutex@14001000`)
and OVL0 **0x14005000** were already correct.

**v3 change (`shim/disp.c`):** rewrote `disp_mutex_resume()` to write, in order:
1. `MUTEX_MOD0(0) = 0` (clear), then `= 0x45B5`
2. `MUTEX_SOF(0) = 0x41` (SOF+EOF both sourced from DSI0, video mode)
3. `MUTEX_EN(0) = 1`

`0x45B5` is the authoritative primary-path module mask from the real kernel
`ddp_mutex.c module_mutex_map[]`: OVL0(bit0)+RDMA0(bit2)+COLOR0(bit4)+CCORR0(bit5)
+AAL0(bit7)+GAMMA0(bit8)+DITHER0(bit10)+DSI0(bit14). `0x41` = `SOF_FLD(bits[2:0])=1
| EOF_FLD(bits[8:6])=1`, matching `ddp_mutex_set_l()` for the DSI0 video path.
Added a 200k-iteration `spin_delay()` after the backlight GPIO pulse so the
panel/backlight settle. `disp_mutex_resume()` reads MOD0/EN back and prints them
via `con_hex2`, so the on-screen "MUX mod0=… en=…" line now *proves* the write
landed (if it reads back 0x45B5/1, the mutex is genuinely armed).

**Rebuilt** `build_evergo_zig.py --root` → `shim-boot-root.img` (6,117,376 B,
VERIFY ok, SWEETXNU present). Re-staged into the flash package dir, sha256
**a4c3bb19…** (replaces ca9be120…; 4e3586f7… was an intermediate v3 build before a
backlight-settle delay bump, also flashed-safe).

**Why this is the decisive test.** With the correct MUTEX registers, the
display frame-sync is genuinely re-armed for the first time. If the red SHIM
banner + "MUX/DISP" debug lines now appear, the shim→XNU chain is proven and
the next blocker is whatever makes XNU panic → reboot (UFS/root). If it stays
black, the display pipeline was *fully* powered down by LK (not just
mutex-gated) and we must escalate to panel-GPIO re-pulse + DSI re-init + LM36273
I2C backlight — see prior sections. No UART required for this test; the screen
itself is the channel.

## 2026-10-24 (22:56) — DISP v4: GPIO base was WRONG (真·根因)

v3（写对 MUTEX）**仍黑**。深挖发现根因不在显示链，而在 **GPIO 控制器基址**：

- mt6833 pinctrl（`pio: pinctrl`）用 `reg_bases = <&gpio>,<&iocfg_rb>,...`，
  `base[0] = &gpio = 0x10005000`。MODE/DIR/DO/DI 字段表（pinctrl-mt6833.c）
  **全部 `i_base=0`** → 都相对 **0x10005000**。
- disp.c 里 `PIO_BASE` 一直写成 **0x1000b000 = &eint（中断控制器 apirq）**，
  于是 v1/v2/v3 所有 GPIO poke（背光、pinmux、方向、输出）**全打到中断控制器
  上，背光 GPIO 87 从未真正翻转** → 这才是「始终黑屏」的根本原因。
- 偏移本身是对的：MODE `0x300+(p/8)*0x10`(4bit)、DIR `0x000+(p/32)*0x10`、
  **DO(输出) `0x100+(p/32)*0x10`**、DI(输入) `0x200+(p/32)*0x10`。只有 base 错。

**v4 修复（`shim/disp.c`）：**
1. `PIO_BASE` 0x1000b000 → **0x10005000**。
2. 新增 `panel_power_up()`：按 evergo.dts 面板 GPIO 链上电——`pm-enable`(GPIO136)、
   `bias VSP+`(GPIO137)、`bias VSN-`(GPIO138)、`backlight`(GPIO87)，
   bias 之间加毫秒级 `spin_delay`。**不脉冲 reset(86)、不重发 DCS init**——
  复用 LK 已初始化的 DSI 状态（DSI0 持续刷帧）；DVDD(PMIC VCN13) 假定仍开。
3. 保留 v3 的 MUTEX 重开 + OVL 重指。
4. **加了一个「盲确认信号」**：背光灭→亮闪一次。用户无 UART/黑屏时，若看到
   背光闪 ⇒ 面板供电链已通、问题在视频管线；无反应 ⇒ 供电链仍死。上屏还打印
   `PANEL pm/bias/bl` 与 `DISP do=`（读回 DO 寄存器验证 GPIO 写成功）。

已重编 `build_evergo_zig.py --root` → `shim-boot-root.img`（6,117,376B，VERIFY ok
含 SWEETXNU），已覆盖刷机包，sha256 **d836b5093f3a4719dad4c33d59b008f11be45675563a4550e49aaaab75be4045**
（替换 a4c3bb19…）。

**判定**：这次背光/偏压是真·驱动 GPIO。若红 SHIM 横幅+MUX/PANEL/DISP 调试行出现
⇒ 彻底通了；若只见背光闪一下仍无字 ⇒ 视频/DSI 或 PWM0 亮度是下一个 blocker
（PWM0 重开+占空比、reset 脉冲、DCS 重初始化）。

## 2026-10-24 (23:30) — v6: 照搬 ruby，停止面板接管（方向性修正）

用户提示参考 **Redmi Note 12 Pro 5G = ruby（MT6877/天玑1080）** 的 iOS bringup：
`github.com/lky005/ruby-ios-bringup`（+ `ruby-ios-probes`）。拉取其显示/boot-chain
文档与 shim 后，结论与我们的做法**相反**：

- ruby 的 shim **完全不碰显示硬件**：不重开 MUTEX、不重指 OVL、不驱面板 GPIO、
  不点背光。**LK 把整条显示链留着运行**。
- 屏幕显示的是 LK「可见扫描层」的内容。ruby 可见层 = `0x7cdfc000`（pitch 4352），
  **不是** kernel/atag fb 基址 `0x7ba10000`。文档原话：「早期 shim 写 0x7ba10000，
  屏幕不可见；v2 改用 0x7cdfc000」。
- ruby 的「红屏探针」成功判据就是「整屏变红」——只往可见层填红、零硬件接管即成功。
- 显示最终由 **XNU 的 IOMFB** 把 iOS surface memcpy 进该可见层完成；shim 调试靠
  持久日志环（sweet-log），不是屏幕。

**对 evergo 的判断**：evergo 与 ruby 同属 MTK，LK 极可能也把显示留着。我们 v1–v5
一直黑，是因为 `disp_takeover()` 在**搞坏本来正常的显示**——重配 MUTEX(MOD0=0x45B5
可能与 LK 实际配置不符)、把 OVL 重指、驱面板 GPIO、点背光/PWM，全是多余甚至有害的。
（v4 的 GPIO 基址修正 0x10005000 本身没错，但「该不该驱这些 GPIO」这个前提错了。）

**v6（`shim/disp.c`）**：`disp_takeover()` 改为**只读诊断**——不写任何显示硬件，
只读回 OVL0 layer0 地址(l0)/pitch 并 `con_hex2` 上屏，打印 `DISP fb=/l0=/pitch=/hw=0`。
控制台仍在 shim_main 的 fb 探针设好的「OVL0 layer0 = 可见层」上画（con_clear 红）。
旧硬件序列保留在 `#if DISP_HW_TAKEOVER`（默认 0），一改即可切回。

已重编 `build_evergo_zig.py --root` → `shim-boot-root.img`（6,117,376B，VERIFY ok，
无警告），已覆盖刷机包，sha256 **a211272f4f06b8eb22e098ec8903236c6f036a9714eb59b35a6484cb8232a3f6**
（替换 d836b509…）。ruby 参考文件存于 `E://iOS//ruby_ref//`。

**判定（照搬 ruby 红屏判据）**：
- **整屏变红**（哪怕文字因 stride 略歪）⇒ 链路通！证明 evergo LK 确实留着显示、
  我们之前的接管正是在搞坏它。下一步把该可见层地址+几何正确地传给 XNU IOMFB，
  并核对 layer pitch（ruby 是 4352 非 4320）让文字清晰。
- **仍全黑** ⇒ evergo LK 与 ruby 不同、确实在交接时灭屏 ⇒ 才需要 `DISP_HW_TAKEOVER=1`
  重新上电，但要先解决可见层地址/pitch。

## 2026-10-24 (23:50) — 真凶：kernel 没 gzip + 镜像必须整 128 MiB

用户发现反复重启，并指出「boot_a/b 是 128MB，你给的才 ~5MB」。结合 ruby boot-chain
文档（「LK 只认完整 128 MiB + stock kernel 流大小」）解剖原厂 `boot_a_backup.img` 后实锤：

- MTK LK 是 **「gzip kernel -> 0x40080000」**，kernel/ramdisk 都必须是 **gzip 流**。
- 原厂布局（标准 Android boot）：header @0x0、**kernel gzip @0x800**（kernel_size=14,638,612）、
  **ramdisk gzip @0xdf6800**（=0x800+roundup(kernel_size,2048)，ramdisk_size=36,170,076）、
  **DTB @0x3075800**（=ramdisk+roundup(ramdisk_size,2048)，即 stock_dtb.bin 169925B）、
  AVB0 @0x309f000、AVB footer @0x7ffffc0。总 134,217,728 = 128 MiB。
- **我们的旧构建把 221KB 的裸 shim Image 直接当 kernel 写（first4=`00020014`，非 gzip）**，
  且只产出 ~6MB 小镜像 → LK gunzip 失败/布局不符 → **shim 从来没被加载过**。
  v1–v6 的「黑屏」很可能是 LK 加载失败，不是我们显示没搞定（display 调试的前提被推翻）。

**修复（ruby 配方，`build_full_boot.py`）**：拿原厂 128MB 备份，**只把 kernel 区
[0x800,0xdf6800) 和 ramdisk 区 [0xdf6800,0x3075800) 换成我们 gzip 后的数据**，
header/DTB/AVB0/footer 全原样。用「串联空 gzip 成员 + 一个 FEXTRA 成员」把每区填满到
精确区大小，保证整段都是合法 gzip（任何 gunzip 行为都得到我们的数据）。round-trip 验证通过。

产出 `E://iOS//build_out//shim-boot-full.img` = **134,217,728 B (128 MiB)**，已覆盖到刷机包
`E://iOS//ios16-redmi-note-11-5g//shim-boot-full.img`，sha256
**8a198a8607eca7ff46cf1bce4c096e861a270c36ed2339a8bc2625c3f028ef7f**。

**刷法（关键：刷整个 128MB 镜像，不是旧的 6MB）**：
```
fastboot flash boot E://iOS//ios16-redmi-note-11-5g//shim-boot-full.img
fastboot reboot
```
**判定**：这次 shim 才第一次真正被 LK 加载执行。若链路正常，应看到 con_clear 的红屏 /
SHIM 横幅；XNU 早期 console 会暴露真正的下一个 blocker（很可能是 UFS 根驱动）。

## 2026-10-25 (00:29) — v8：可见层锁定 OVL0 layer1（绿=layer1）

v7 颜色 HUNT 结果：**整屏变绿 = 可见层是 OVL0 第 1 层（layer1, 0x14005f60）**，不是
我们一直假设的 layer0，也不是 /chosen 的 videolfb 基址。（与 ruby 一致：可见层不在默认层。）

修复：
- `device_evergo.h`：`DISP_OVL0_L1_ADDR=0x14005f60`、`DISP_OVL0_L1_PITCH=0x14005064`
  （替换原 L0；L1_ADDR/L1_PITCH 偏移据真实内核 ddp_reg_ovl.h 确认）。
- `shim.c` fb 探针：改读 OVL0 **L1** 地址设 con_fb，并读 L1 的字节 pitch 设 console
  stride（新增运行时 stride，避免文字歪）。
- `console.c/.h`：新增 `con_set_stride()/con_rowbytes()`，con_clear/put_char 改用运行时
  stride（默认仍 FB_STRIDE=4320）。
- `loader.c`：`Video.v_rowBytes = con_rowbytes()`（XNU IOMFB 用真实 pitch，不歪）。
- `disp.c`：恢复 v6 只读版（不碰显示硬件、不停住，让 shim 继续启动 XNU）；硬件序列留在
  `#if DISP_HW_TAKEOVER`。

重编 + 打补丁 → `shim-boot-full.img`（128 MiB，VERIFY ok，无警告），已覆盖刷机包，
sha256 **d8db69cbefa00457815e4ca747a36b249e863c0563b459fdc12efebadd849dc4**。

**判定**：这次 con_fb=可见层 L1，应看到 con_clear 红底 + SHIM 横幅/DISP 调试字（文字不歪）。
之后 XNU IOMFB 会把 iOS surface blit 到同一 L1 层 → 苹果 logo → 锁屏。若见到这些，显示链路
全通，下一步就是 XNU 卡住点（多半 UFS 根）。

## 2026-10-25 (00:52) — v9：SMC 编码 bug + ramdisk 回退（蓝屏诊断实锤）

用户拍蓝屏：PANIC(SYNC) STAGE=psci ESR=0x56000000 ELR=0x40081A3B FAR=0 X30=0x400819E0。
反查 shim.elf（链接基址0，运行基址0x40080000）：ELR=**reset()+0x7B**、X30 也在 reset()。
ESR EC=0x15=**SVC**（非 SMC）。⇒ 两个叠加 bug：

1. **SMC 编码错误**：`build_evergo_zig.py` 把 `smc #0` 替换成 `.inst 0xd4000001`，
   但 0xd4000001=**SVC #0**，SMC #0 应为 **0xd4000003**。于是 shim 所有 PSCI 调用
   （PSCI_VERSION、reset 的 SYSTEM_RESET…）实际发 SVC → 同步异常（EC 0x15）。
   诊断路径里 smc(PSCI_VERSION) 首先踩雷 → exc_handler 蓝屏 → reset(40) 的 smc 又踩 → 每 40s 刷一次蓝屏（ELR 落在 reset）。**已修为 0xd4000003**（已在编译产物确认）。
2. **loader_boot 返回了**（STAGE=psci 在诊断路径，只有 loader_boot 返回才会到）→ XNU 没启动。
   最可能原因：MTK LK 的 DTB /chosen 没有 `linux,initrd-*`，loader 找不到 ramdisk →
   `no SWEETXNU payload`（该行被蓝屏盖掉）。

v9 修复：
- `build_evergo_zig.py`：`.inst 0xd4000001` → `0xd4000003`（真 SMC）。
- `device_evergo.h` + `loader.c`：加 ramdisk 回退——/chosen 找不到时，直接搜已知
  ramdisk 装载地址 `0x51100000`（LK 会把 boot ramdisk 解压到这）里的 SWEETXNU。
- `shim.c`：诊断路径末尾由 reset(40) 改为 `for(;;) wfi` 停住，方便无 UART 读屏。

重编+补丁 → `shim-boot-full.img`（128MiB，VERIFY ok，无警告），sha256
**8ec1493e470aef4bbbc49448fd4bb3373be35927d7362401b0a470fcba5cc69f**，已覆盖刷机包。

**判定**：若 ramdisk 回退命中 → loader 直接跳 XNU（可能启动）；若仍未启动 → 屏幕停住不重置，
显示 `LOADER <why>` + `INITRD lo/hi` + `RDFBK <addr>`，据 why 精准修 payload 定位。

## 2026-10-25 (01:30) — v11：payload 内嵌 kernel + ramdisk 改未压缩（LK 不解压 ramdisk）

用户红屏定格实锤：`INITRD lo=51100000 hi=53377E95C`、`LOADER no SWEETXNU payload`。
推断：hi-lo≈36MB=header ramdisk_size（原始），且扫不到 SWEETXNU ⇒ **MTK LK 不解压
ramdisk，把 gzip 压缩的 ramdisk 原样载入 0x51100000**，SWEETXNU 被压缩所以按字节扫不到
（本地验证：gzip ramdisk 里无 SWEETXNU 明文，未压缩 cpio 里有）。这也正是 topaz/ruby 用
「payload 内嵌进 kernel Image」的原因——kernel LK 是会解压的。

v11（`build_full_boot.py`）双保险：
1. **内嵌**：kernel Image 后接 payload.bin（page 对齐到 _end），gzip 进 kernel 区
   （155648+16613632=16.77MB → gzip 5.73MB， fits 14.6MB）。LK 解压 kernel 后 payload
   以明文落在 0x40080000+page(_end)，loader 的 `emb` 回退命中。
2. **ramdisk 改未压缩 cpio**：ramdisk 区直接放未压缩 cpio（零填充到区大小）。LK 原样载入，
   loader 的 initrd 扫描按字节命中 SWEETXNU。
   round-trip 验证：kernel 解出 shim+payload（含 SWEETXNU）、ramdisk 原始 cpio 可扫到 SWEETXNU。

重打 → `shim-boot-full.img`（128MiB，VERIFY ok，无警告），sha256
**beeeeaec0f3e3402abed53eefae9c3184805ace9d559972523cba3eb8d82573c4**，已覆盖刷机包。

**判定**：这次 loader 应能命中 payload（initrd 明文优先，emb 兜底）→ 打印 `ENTRY/JUMP/XNU
jumping` 并跳 XNU。若见 XNU 启动但仍卡（苹果 logo / 无画面），就是下一个 blocker：
iOS 根文件系统未刷（rd=ufsroot0）或 XNU 早期驱动。


## 2026-10-25 (23:20) — v12：launchd 被 CS_KILLED（namespace 9 code 0xe）根因与修复

**现象**：root 版镜像刷机后 panic：
`SWEET EARLY PANIC: unexpected SIGKILL of launchd (CS_KILLED) with reason - namespace 9 code 0xe`
（kern_sig.c:2069 psignal_internal：initproc 收到带 reason 的 SIGKILL 就 panic）。

**根因（不是签名损坏，是平台检查）**：
- namespace 9 = OS_REASON_EXEC，code 0xe = 14 = `EXEC_EXIT_REASON_WRONG_PLATFORM`（reason.h:257）。
- 唯一抛出点 kern_exec.c:7451/7466，在 `#if XNU_TARGET_OS_OSX` 块里（7431-7474）——
  **这个块在 sweet 内核里是编进去的**（内核里有 "Allowing spawn of iOS binary" 字符串可证）。
- 触发链：launchd 的 Mach-O 带 LC_BUILD_VERSION platform=PLATFORM_IOS →
  kern_exec.c:1368 `proc_setplatformdata(p, PLATFORM_IOS, ...)` → process_signature 里
  `proc_platform(p) == PLATFORM_IOS` 为真；load_init_program 用裸 execve（无 posix_spawnattr）
  ⇒ `ip_px_sa == NULL` ⇒ 无 no-sandbox entitlement ⇒ EACCES + SIGKILL(launchd)。
- 为什么以前没卡这：0089e 的早退（kern_exec.c:7384， enforcement 双关全关时直接 return 0）
  本来就在这个平台检查**前面**，但前提是 `cs_enforcement_disable=1` 被兑现——
  而旧镜像 boot-args 里没有这个参数 ⇒ 强制开着 ⇒ 先撞 WRONG_PLATFORM。

**修复**：boot-args 加 `cs_enforcement_disable=1`（build_evergo_zig.py 的 EXTRA_BOOTARGS
就是为它准备的，脚本注释里的示例正是这个值）。
兑现前提逐条已核实：
1. `PE_i_can_has_debugger()` 在 arm 上只认 **DT /chosen/debug-enabled**（pexpert/arm/pe_init.c:453-470），
   不是 debug= boot-arg。mkadt_evergo.py:125 已写 `debug-enabled = u32(1)`，
   且 shim 把 payload 里的 ADT 原装设为 boot_args.deviceTreeP（loader.c:467），
   ADT 范围 [adt_off, adt_off+adt_size) 内确有 debug-enabled（payload+16601656）。✔
2. `!SECURE_KERNEL`（kern_cs.c:165）：内核里有 `cs_enforcement_disable` 字符串（k+801606）⇒ 编入了。✔
3. cs_init 是 __startup_func，晚于 PE_init_platform 读 DT。✔
生效后：cs_system/cs_process enforcement 双 0 ⇒ 0089e 早退跳过整套签名 kill 网
（WRONG_PLATFORM / SECURITY_POLICY / TASKGATED_OTHER / MACHINE_THREAD / launch-constraints /
proc_process_signature），0088e+0089e 撕掉 CS_HARD|CS_KILL|CS_ENFORCEMENT，mach_loader
忽略 LC_ENCRYPTION_INFO。**无需改内核、无需重编，只改 boot-args。**

**重建**（一条命令，EXTRA_BOOTARGS 会透传给 build_evergo_zig.py）：
    cd E:////iOS && EXTRA_BOOTARGS="cs_enforcement_disable=1" python rebuild_images.py
产物（已装进刷机包目录 E:////iOS////ios16-redmi-note-11-5g////）：
- shim-boot-full.img        boot-args `debug=0xc keepsyms=1 -noprogress serial=1 cs_enforcement_disable=1`
  sha256 041757385ec998ea96eebc92c6d7da1387209cadc05ed64c964e9734e67bc009
- shim-boot-full-root.img   boot-args 同上 + `rd=ufsroot0`
  sha256 6d377eff725d68eff2a31375e9ffdd97858e89e0ead0e4f7a09008e4b32dedcf
校验：payload 头部 version=1/nseg=10、cmdline 含新参数、0081e(0x7d006000)/0089e 字符串都在、
SWEETXNU 在 ramdisk 区可扫、.sha256 与文件一致。

**下次开机判据**：日志出现
`SWEET exec: launchd: enforcement disabled, signature checks skipped (mac_return .., cs_error ..)`
= 修复兑现。之后每个 exec 都会打这行。再往后可能的 blocker（按顺序猜）：
dyld 共享缓存映射、HFS+ journal 重放（0086e/0087e）、launchd 的沙箱/XPC、
kextd 想加载驱动（本内核无 kext 加载路径）。用 read_sweetlog.py 读 pstore 日志定位。

**教训**：arm 上 PE_i_can_has_debugger 只认 DT 属性；"debug=0xc 就够" 是错的认知，
这次能修好是因为 mkadt 早就白送了 debug-enabled=1。


## v12 收尾：镜像被"无参重建"静默刷掉一次（已修复 + 已加固，10-26 早）

- **事件**：07:34 发布目录两个镜像被一次**未带 EXTRA_BOOTARGS** 的 `rebuild_images.py` 重建替换，
  boot-args 丢掉 `cs_enforcement_disable=1`（sha256 `04175738…`/`6d377eff…` → `d19a396a…`/`6f6941ea…`）。
  触发者未能定位：无相关自动化（唯一一个是暂停的资讯总结）、无残留进程、bash/PowerShell 历史无线索；
  疑为外部手动触发或上会话遗留后台任务。**教训：发布镜像可能被任何来源的无参重建打回原型。**
- **修复**：带参重建恢复；随后**加固** `build_evergo_zig.py`——把 `cs_enforcement_disable=1`
  烧进默认 `CMDLINE_KERNEL`（该内核含 0089e，没有此参数根本起不到 launchd，
  所以它是必需项不是可选项）。裸跑 `rebuild_images.py` 实测已验证自带该参数。
- **新增自检工具** `E:\iOS\verify_image_kernel.py`（已复制进发布目录）：一条命令端到端校验
  boot-args / 内核逐段身份 / 0081e+0089e 在位性 / ADT debug-enabled / DTB / AVB / .sha256，
  输出 `总体: ALL PASS` 才允许刷机。**刷机前必跑。**
- **最终镜像**（加固后产物，ALL PASS）：`shim-boot-full.img` sha256 `88d4614582446a3991a9ee3a0fd64974e0d0f259169e5a4e746865f1359d41f7`；
  `shim-boot-full-root.img` `a83c760790ab5952f74a7365819e80e9e1e92e73c2d7bb8637c64f6f262ffee8`
  （root 的 boot-args 顺序变为 `… serial=1 cs_enforcement_disable=1 rd=ufsroot0`，无害）。
- **内核身份验证法（本次定案，可复用）**：镜像内嵌内核 == `kern_tmp/kernel.development.sweet`
  （sha `3ab62dd6…`），**按 payload 段表逐段比 10/10 全中**。整文件 sha **必然不同**——
  mkpayload 丢 `vmsize=0` 的段（`__CTF` 606208 字节）+ 段间 16 字节对齐填充。
  ADT `firmware-version=evergo-shim-k3ab62dd6` 可交叉互证。以后别用整文件 sha 判断内核身份。


## v13：mount[3] exit status 1 定位 + 大字日志 + 上游 boot-args 补齐（10-26）

- **现象**：CS_KILL 修好后 launchd 跑起来了，panic 变成
  `SWEET EARLY PANIC: mount[3] exited -- no exit reason available -- (signal 0, exit status 1)`。
- **退出码 1 的来源（反汇编定案）**：`/sbin/mount` 的 errx 路径全部走
  `sysexit_code(errno,-1)`（0x100004bd4），退出码**恒 ≥ 64**；`-e` 才切原始 errno
  模式（全局 0x10000c050），launchd 只传 `-P 1|2` 所以是 sysexits 模式。
  唯一产出 1 的路径是 `mountfs()`（0x1000058c4）fork/exec `mount_<fstype>` 后
  `ubfx w8,w8,#8,#8` 取 WEXITSTATUS **原样传播**（打印 `"%s failed with %d"`），
  main 的 `bl mountfs; bl _exit` 直接放大成进程退出码。
  ⇒ exit 1 = `mount_hfs` 退 1。
- **根因**：根卷只读。上游 0036hr（只读启动）同款链条（NOTES.md:141）：
  `mount_hfs update of /` → `mount() EROFS` → `mount: / failed with 1`。
  **evergo 缺 `ufsrw=1 rootrw=1`**——这正是上游从 0031w 起的"通过配置"。
  两个补丁都在内核里（字符串 `ufsrw` @0x865fa、`rootrw` @0x899a9）。
- **修复**：`build_evergo_zig.py` 的 `CMDLINE_KERNEL` 烧入上游已知可用集合
  （去掉 SM7150 专有项和 sweetfb_noconsole）：
  `… cs_enforcement_disable=1 unentitled_ios_sim_launch=1 ufsrw=1 rootrw=1 launchd_unsecure_cache=1 wdt_test=no_user_checkin wdt=-1 [rd=ufsroot0]`。
  后三个是**预防性**的：`launchd_cache_loader` 的 `Unable to find AMFI`
  （NOTES.md:132）、watchdogd 的 `_os_crash`（128/149）各要一次刷机才能撞上，
  先一步加掉。
- **大字日志（用户拍不了照的对策）**：shim `plog.c` 以 `PLOG_SCALE=2`（16x32 单元）
  把 pstore 里**上一轮**的完整内核日志尾部画到屏幕下半部（约 49 行 x 67 列），
  标题 `== prev boot PANICKED - held for reading ==` 或 `== previous boot log (tail, 2x) ==`；
  `loader.c` 在 `plog_had_panic()` 为真时用 `PLOG_HOLD_SECONDS=120` **停留 120 秒**
  再继续启动（掉电重启会重新计时）。判据：panic 行和它上面的报错就在这一屏尾部。
- **产物（v14，ALL PASS）**：`shim-boot-full.img` sha256 `71f2f965…`；
  `shim-boot-full-root.img` sha256 `5d933914…`。发布目录 README.txt / flash_root.bat
  已同步（GBK+CRLF，含大字日志判读清单）。
- **副作用（要记住）**：根卷从这一版开始**可写**，panic/掉电会改坏 userdata 上的
  HFS+ 卷；恢复源 `E:\iOS\ios16-root.img`（sha `15ef1a87…`），
  `flash_root.bat userdata` 重刷（约十几分钟）。
- **方法论**：上游 `NOTES.md` 是本项目最值钱的情报源——它按 boot 编号记录了
  "哪个参数解决哪个 panic"。本机遇到新的用户态 panic 时，**先去 NOTES.md 搜
  panic 串/进程名**，多半能直接找到对应的 boot-arg 或补丁号，比自己反汇编快得多。


## v14 首次开机结果（10-26 晚）：过了 mount，launchd 进 main 后自杀

刷 v14（`ufsrw=1 rootrw=1` + 上游其余参数）后的现象：

1. **mount[3] panic 消失**（ufsrw/rootrw 对策成立，和上游 0036hr→0031w 完全一致）。
2. 新 panic：`initproc exited -- exit reason namespace 2 subcode 0x5 description none`
   - `kern_exit.c:1806-1812`：p==initproc 时，`initproc_spawned` 为真才用
     `"initproc exited"`，否则是 `"initproc failed to start"`。
     **`initproc_spawned` 由 launchd 自己通过 sysctl `kern.initproc_spawned` 置 1
     （kern_exit.c:1306-1320）⇒ launchd 确实进了 main()。这是实打实的进展。**
   - `bsd/sys/reason.h:89` OS_REASON_SIGNAL = 2 ⇒ namespace 2 = 信号，
     subcode 0x5 = **SIGTRAP** = launchd 主动 `_os_crash()`（和上游 watchdogd
     "namespace 2 subcode 5 (SIGTRAP = its _os_crash)" 同款，NOTES.md:130）。
3. 另一条：`coalition.c:1031 assertion failed: tas->coaition[COALITION_TYPE_JETSAM]==coal`
   - 源码 `osfmk/kern/coalition.c:1031` = `i_coal_jetsam_set_taskrole()` 里的
     `assert(task->coalition[COALITION_TYPE_JETSAM] == coal)`（上游源码里成员名
     就是拼错的 `coaition`）。给任务设 JETSAM coalition 角色时发现记录不匹配。
4. 用户反馈"每重启一次多一次 bug"。

**已排除的嫌疑：UFS 写路径写出垃圾。** 逐条核对 `bsd/dev/sweet_ufs.c`：
   - `ufs_write_blocks()` 发 WRITE(10) 前重查分区边界（越界直接 EPERM，sweet_ufs.c:281-286）；
   - `ufs_scsi()` 方向正确：`ucd->cmd[1] = write ? 0x20 : 0x40`（data out/in），
     `utrd->dw0` 的 DD 位 `write ? 1 : 2`（内存→设备 / 设备→内存）；
   - DMA 缓存：`dma_sync()` = `flush_dcache64`（arm64 上是 clean+invalidate，
     写前会把脏行写回），`dma_clean()` = `clean_dcache64`；`su.cache=0` 分支对
     写也是 flush，不会丢脏数据。
   - MTK 路径（`sweet_ufs_mtk_prepare`）复用 LK 已拉起的控制器，读写走同一条 `ufs_scsi`。

**下一步（待用户执行）**：重刷干净 root（`flash_root.bat userdata`）再开机一次，
区分两种可能：① 根卷被反复非正常掉电写坏（累积性损坏，需每次重刷）；
② 其实在往前走，每次撞到下一个 blocker（journaled HFS+ 有日志重放，未必会坏）。

## v15 (2026-10-26 晚): root 镜像漏了 sweet-root-tweaks.py —— 两条 panic 同根

用户报的两条 panic:

```
SWEET EARLY PANIC: initproc exited -- exit reason namespace 2 subcode 0x5 description none
coalition.c:1031 assertion failed: tas->coaition[COALITION_TYPE_JETSAM]==coal
```

**namespace 2 / subcode 0x5 = SIGTRAP**（bsd/sys/reason.h: OS_REASON_JETSAM=1、
SIGNAL=2、EXEC=9）。panic 串是 "initproc exited" 而不是 "initproc failed to start"
（kern_exit.c:1806-1813），说明 `initproc_spawned` 为真，而该标志由 launchd 自己
通过 sysctl 置位 ⇒ **launchd 进了 main()**，不是 exec 阶段被杀。

### 找到的真凶

直接从 16 GiB HFS+ 镜像里把 `/System/Library/xpc/launchd.plist` 读出来
（1,456,980 字节，extent [1966687, 356]，BS=4096；用 hfs_walk2.py 的 catalog dump
定位，自己写了个 bplist 解析器，因为 plistlib 报 Invalid file —— 顺带记住
Apple 的 bplist 标记是 **0xA0=Array / 0xD0=Dict**，不是 0x8/0xA）：

| 检查项 | 跑过 tweaks 应为 | 实际 |
|---|---|---|
| `_PanicOnCrasX` | 7 | **0** |
| `_PanicOnCrash` | 0 | **7** |
| `HAL.disabled/` | 存在 | 不存在 |
| `VirtualAudio.plugin` 原位 | 已移走 | 仍在原位 |
| `/private/var/containers/Bundle` | 存在 | 不存在 |
| `/private/var/keybags/systembag.kb` | 存在 | 存在 |

⇒ 镜像只跑了 `tools/mkiosroot.sh`，**`tools/sweet-root-tweaks.py` 这一步漏了**。

7 个带活 `_PanicOnCrash` 的 job（每个的 `PanicOnConsecutiveCrash=False`，
即**崩一次就 panic**）：watchdogd、containermanagerd、usermanagerd、trustd、
lockdownd、remoted、remotepairingdeviced。

**这解释了一直困惑的"每重启一次多一次 bug"**：不是累积损坏，也不是每次都
前进——每轮开机 launchd 往前推一点，撞上下一个必然崩溃的 daemon（这台机器
没有基带 / GPU 驱动 / SEP），崩一个就整机 panic，所以每次都是新的进程名、
看起来像新 bug。同一个根因的不同位置。

### 修复 A（已写进镜像）

`_PanicOnCrash` -> `_PanicOnCrasX` 等长，直接在数据块上改 7 个字节：
offset 8055549952 (=1966687*4096)，命中 636639/708912/797318/839625/
863885/891772/905115，每处只把末尾 'h'(0x68) 改成 'X'(0x58)。
文件大小、catalog、extents 全不动。回读校验 `_PanicOnCrash=0`、
`_PanicOnCrasX=7`、仍能解析出 532 个 LaunchDaemons。

脚本：`E:\iOS\_patch_root_launchd.py`（幂等）。
镜像 sha256 `15ef1a87...` -> `c62b33f4e57aa0bc...`。

### 修复 B（补丁 0090e，要 CI 重编）

`posix_spawn`(coalition 属性) -> kern_exec.c:3727 解析 cid -> fork1 ->
task_create_internal -> `coalitions_adopt_task` 失败（reaped/terminated，
或任务已有同类 coalition）-> 回到 kern_exec.c:3782 `coalitions_set_roles`
-> `i_coal_jetsam_set_taskrole` 的断言炸。kern_exec.c:3756 注释原文：
"also the call to coalitions_set_roles will panic"。

0090e 把 `coalitions_set_roles()` 改成跳过任务未加入的类型、
`i_coal_jetsam_set_taskrole()` 改成打印后返回 KERN_SUCCESS、
`assert(kr == KERN_SUCCESS)` 改成打印。已在 `E:\iOS\_bt10\xnu` 上
`git apply --check` / `apply` / `apply -R` 全部验证。已 commit c1d8e49，
**需 push 触发 CI 才会进 boot 镜像**。

### 方法论（值得记住）

1. 遇用户态 panic，先查**上游 NOTES.md**（这次没有 coalition 记录 ⇒ 是
   evergo 特有，得自己挖），再查 **root 镜像的配置**——upstream 的
   `tools/sweet-root-tweaks.py` 是镜像的必备工序，漏了就会一直撞"随机 panic"。
2. 16 GiB HFS+ 镜像可以直接用 Python 读（`_files.json` + extents），
   **等长改内容 = 零风险原地补丁**，不用挂载、不用 Linux。
3. 改镜像内容前先确认刷机脚本用的是哪个文件：flash_root.bat 第 48 行
   优先 `E:/iOS/ios16-root.img`，`.7z` 只是后备。

### 还没做的 tweaks（都要动 catalog，等长覆写做不到）

- 删 5 个 CommCenter job（无基带，crash-loop 卡 Setup）
- 建 `/private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}`
- 写 purplebuddy/migration/springboard 偏好（跳过 Setup；不跳过
  DataMigration 会跑一小时）
- VirtualAudio.plugin -> HAL.disabled（mediaserverd 崩）
- MediaToolbox `StartRecorderServer=false`


## v15 补记：设备树决定哪些 launchd job 加载（回答"无基带会卡住吗"时查出）

532 个 LaunchDaemons 里只有 5 个带设备树约束（`_LimitLoadTo/FromDeviceTree`）：

    To:   hilo        -> com.apple.SepUpdateTimer
          pearl-sep   -> com.apple.biometrickitd.pearl
          baseband    -> com.apple.CommCenter          (cellular)
          [wlan,marconi-wifi] -> com.apple.wifid
          [hilo,twodbl]       -> com.apple.iomfb_bics_daemon
    From: baseband    -> com.apple.CommCenter          (-noncellular)

我们的 ADT 里这些节点一个都没有（mkadt.py:118 "no SEP, no baseband here"），所以：

- iOS 侧永远无基带（XNU 没有 MTK modem 驱动），与安卓侧蜂窝无关。
- cellular CommCenter 压根不加载；会跑的只有 -noncellular 版，而它不在
  7 个 `_PanicOnCrash` 之列 ⇒ 崩了只自己重启，不会 panic。
  **所以在 launchd 存活这个阶段，无基带不会卡住。**
- 但 Setup 会卡在 CoreTelephony XPC（sweet-root-tweaks.py:13 注释原文），
  所以仍必须同时跳过 Setup。
- **`com.apple.wifid` 不加载 ⇒ 当前没有 WiFi**；将来要 WiFi 需先在 ADT 补 `wlan`。
- SepUpdateTimer / biometrickitd.pearl 不加载（无 SEP、无面容），符合预期。


## v16：0090e 内核进 boot 镜像（2026-10-27，只刷 boot 不用重刷 userdata）

### CI #15 先失败了一次：0090e 第二处 hunk 落进 file scope

日志（xnu #15，4m47s，1 error）尾部被截断，只剩 `hfs_catalog.c` 的一堆
`implicit conversion loses integer precision` —— 那是补丁 0026（sweet-builtin-hfs
第三方代码）自带的**老噪声，不是新错误**，定位时只看 `error:`。

真因在生成脚本 `_gen_0090e.py`：三处编辑都往同一个 list 里**往前插**并手算偏移
补偿，第二处漏算了第一处新增的 16 行 ⇒ 成员检查那段被插到上一个函数结束之后、
`coalitions_set_roles` 之前 = **file scope**，语句落在函数外，编译必错。

修法：`new[i_kr_assert:...]` → `new[i_lock:...]` → `new[i_assert:...]` **倒序插入**，
前面的索引就永远不用补偿。顺手把 `%lld` 改 `%llu`（`struct coalition.id` 是 uint64_t）。

**暴露的验证盲区**：之前跑过 `git apply --check` / `apply` / `apply -R` 全过就判定
"最强验证"，但这套只能证明**能贴上、能还原**，证明不了**代码落在对的位置**。
已把"必须 apply 后读一遍目标函数 + 查括号平衡"补进 MEMORY.md。

### CI #16 成功（commit 7af5bda）

- 新内核 17206016 字节 `f80e83e1acf8...`（旧 17206112 `3ab62dd61497...`）。
- **验证方式：在二进制里找那三条 printf 字符串**（0x4a231 / 0x49df7 / 0x49e44）
  全部 FOUND；0081e 的帧缓冲 0x7d006000 仍在、旧值 0x9c000000 为 0 次。
- `verify_image_kernel.py` 加了 0090e 三条字符串检查，两个镜像 `总体: ALL PASS`（12 项）。
- 成品：`shim-boot-full.img` `4a7c477ce87ef5ac...` /
  `shim-boot-full-root.img` `f959da299667b81b...`。
- **root 镜像这轮没动 ⇒ 只刷 boot 即可。**
- 坑：README.txt / flash_root.bat 必须保持 **GBK + CRLF**；脚本要先把 CRLF 统一
  成 LF 再改、写完再统一下去，否则多行 replace 会因换行符不匹配**静默失败**。


## v17：QEMU RAM-disk root 门禁（2026-10-27~28）

目标是把"**先放 QEMU 测、真没问题了再给手机镜像**"这条规矩真正落地。
在此之前 QEMU 只能验内核（到 `Waiting on ... Apple_HFS` 就停，因为没有 root）。

### 为什么必须做 RAM-disk root

- 真机 UFS 路径在 QEMU 里不存在；ios-topaz 的 xnu 树**完全没有 virtio**
  （grep "virtio" 全树无命中）⇒ 磁盘后端 root 要移植驱动，工作量大。
- 所以 root 只能整体放进客 RAM（补丁 0091e 的 memdisk / `rd=ufsroot0`）。

### 内存是硬约束（这一步推翻了之前的误判）

- 宿主 15.7 GiB 总、**仅 ~3 GiB 空闲**（WorkBuddy 自身 ~5 GiB）。用户关掉
  Chrome/QQ/ABDownload/Steam++ 后约 6.6 GiB。
- **QEMU 11.1.0 只有 `memory-backend-ram`、没有 `memory-backend-file`**
  （`-object help` 亲自查过）⇒ HakiOS 那个"sparse-file 把整盘 map 进客 RAM"
  的 trick 用不了，**客 RAM = 宿主提交内存**。
- dyld 共享缓存 **3.11 GiB** 是硬下限（单体不可裁，必须整块常驻客 RAM）。

### 裁剪规则：5.55 → 3.73 GiB

`tools/mk_trimmed_root_manifest.py`（HakiOS select-system.py 的移植）+ 新建的
`tools/build_trimmed_root.py`。关键思路：**dyld 缓存已经提供了所有 framework
二进制**，于是把磁盘上重复的那份删掉：

- `System/Library/{Frameworks,PrivateFrameworks}` 只留 `Info.plist` 和 ≤8 KiB
  元数据（保留 plist 是为了让 dyld 仍能校验缓存条目）；
- 整个 `Applications/`、音频栈、多余字体（只留 2 个 `TRIMMED_FONTS`）全丢。

结果：**dyld 3.11 + 其余 0.62 = 3.73 GiB**（52838 个文件），镜像约 4074 MiB。

### 修掉的三个 bug（都会让 QEMU 测试必败）

1. **`qemu_check.py` 的 root 镜像会盖住内核**：`--root-addr` 默认 `0x90000000`
   **低于**内核 `phys_base`，还无条件传给 qemu-sweet.py ⇒ ~4G 镜像直接覆盖内核。
   改成默认 `None`，由 qemu-sweet 放到 `phys_base + mem_size` 之上。
2. **顶层符号链接全丢**：`etc`/`tmp`/`var` 是 depth-1 符号链接（→ `private/…`），
   原筛选只认 `WHOLE`/`DYLD_PREFIX` 前缀，三者都不匹配 ⇒ 被丢掉，裁剪根与手机
   不一致、userland 起不来。改成 depth-1 符号链接无条件保留。
3. **镜像大小在 umount 之后才统计**：`total` 恒为 0 ⇒ 镜像被锁死在 `--min-mib`
   平顶。改成在遍历时边走边累加。

### 客机内存修正（差点整个方案不成立）

之前用"内核 phys_base=0xb0000000"估算的 6G **是错的**。实际常量
`PHYS_BASE = 0x80000000`、`MEM_SIZE = 0x80000000`（2 GiB），而 RAM-disk root
必须落在内核管理的 DRAM **之上** ⇒ 镜像起点 4 GiB，加上 ~3.98 GiB 镜像
⇒ **客机 `-m 7G`（7168 MiB）> 宿主可用 6758 MiB ⇒ 必 OOM**。

修法：新增 `QEMU_MEMSIZE`（仅 QEMU，手机默认不变），`qemu_check.py --root-img`
时设 1 GiB ⇒ 镜像起点降到 `0xC0000000` ⇒ **客机 `-m 6G`（6144 MiB），留 ~600 MiB
余量**。已用脚本复算两种取值确认。

### 补丁 0091e / 0092e

- **0091e（ufs memdisk root）**：sweet_ufs 认设备树 `memdisk` 属性或 boot-arg
  `ufsmemdisk`，把节点的 reg 当映像直接映射、注册 `ufsroot0`、读写改 `memcpy`
  不发 UFS 命令、跳过缓存同步。
- **0092e（spinlock 诊断）**：`lck_spin_verify()` 在 panic 前打印 caller
  （`__builtin_return_address(0/1)`），可离线用内核符号定位是哪把锁；
  `ignore_bad_spin=1` 降级为只报告，一次开机能看全所有坏锁。

### CI #17 又失败一次：0092e 两个 printf 都不可编译

run `40529589145`，build 5m6s，1 error，exit 2。

- `ignore_bad_spin` 那条写成 `printf("SWEET SPIN: "ignored" spinlock %p ...", "ignored", ...)`
  ⇒ 字符串 + **裸标识符** + 字符串，中间无运算符 ⇒ **语法错误**（就是那个 1 error），
  且该词还被当成多余参数传了一次。
- panic 路径那条把 `how` **写死**在格式串里（该写 `%s`）⇒ 7 个占位符对 8 个参数，
  `how`/`lck` 及之后全部错位（就算编过，打出来的锁地址也是错的）。

两处都改成 `%s` + 配一个参数（行数不变，hunk 计数不受影响）。**三重验证**：
zig 编复刻片段（`-Wall -Wformat=2`）rc=0；两个补丁对 `_bt10/xnu`
`git apply --check/apply/-R` 全 rc=0；**apply 后读落位**确认都在函数体内。

⇒ 这是"git apply 全绿 ≠ 代码合法"的**第二次实例**（第一次是 0090e 落进 file scope）。
已把"补丁里新写的 C 要单独 zig 编译验证"补进 MEMORY.md。

**注**：GitHub MCP **读不了 workflow run**（只有 issue/PR/branch 那几个工具，
`ui_get` 也不支持 runs）；WebFetch 能拿到状态/时长/artifacts 但拿不到错误正文。

### CI 侧：iosroot.yml 可以直接吃预置的 ios16-root.img

新增 `root_img_url` / `root_img_tag` 输入。给了预置镜像就**跳过 DMG 下载 +
mkiosroot.sh**（省 ~10 分钟）。更关键的是**避开 v15 那个坑**：`mkiosroot.sh`
**不跑** `sweet-root-tweaks.py`，从 DMG 重建出来的根带着 7 个活的 `_PanicOnCrash`。
（裁剪那步自己会跑 tweaks，所以两条路出来的裁剪根都是对的。）

### v18：spinlock panic 深挖（locks_arm.c:740）

原始 panic：`invalid spinlock 0xfffffe1b3b00c4d0 @ locks_arm.c:740`。

- **0092e 是活的**：`osfmk/arm/locks.h:34 #define LCK_SPIN_IS_TICKET_LOCK 0`
  ⇒ 编译的是 `!LCK_SPIN_IS_TICKET_LOCK` 分支（locks_arm.c 725–824），即经典
  `lck_spin_lock → lck_spin_verify → __lck_spin_invalid_panic`。0092e 往这条路径
  插的 `SWEET SPIN` 诊断**会**真正执行（不是死代码）。曾担心 ticket-lock 分支会
  让 0092e 失效，已排除。
- **格式串不一致**：当前 `_bt12` 源码 line 740 是
  `panic("%s spinlock %p: <0x%016lx 0x%16lx>", ...)`（how∈{Invalid,Destroyed}），
  与用户贴的 `invalid spinlock %p @ %s:%d` 不同 ⇒ 手机那版是**更旧的 locks_arm.c
  修订**，别被旧格式串误导；要的是 0092e 新加的 `SWEET SPIN: ... from %p %p` 行。
- **锁在 kmap 动态区**：`0xfffffe1b3b00c4d0` 落在 `0xfffffe1b...`，远高于静态内核
  文本（`_lck_spin_lock` 在 `0xfffffe0007486dac`）。静态符号表解析不到它 ⇒ 是**动态
  kalloc 出来的结构里的锁**。
- **how=Invalid（非 Destroyed）** ⇒ `lck->type != LCK_SPIN_TYPE` 且 data 不是
  `LCK_SPIN_TAG_DESTROYED` ⇒ 锁的 type 字段被清零 ⇒ 结构是**未初始化或已释放后被
  复用（use-after-free）**。定位靠 `SWEET SPIN` 的 caller PC（在静态文本里，可符号化）。
- 离线符号化工具已就绪：`E:/iOS/sym_kernel.py`（解析 development 内核 Mach-O 符号表，
  把 PC 还原成 `symbol+offset`）。已用 `_lck_spin_lock` 自解析验证可用。

### 当前状态（2026-10-26）

**待 push 的 2 个 commit**（前 4 个 `d7005f1`/`1c2abca`/`142cf4c`/`fba7388` 用户已在本机
push，只剩这两个）：`794fcf7`（docs v16/v17）、`3360e9e`（iosroot.yml 跑 tweaks）。
工作树 clean，分支 `evergo` 领先 `origin/evergo` 2 个。

**QEMU 门禁当前被两道墙挡住**：
1. 本地 `kern_tmp/kernel.development.sweet` 是**旧内核**，二进制里搜不到 `SWEET SPIN` /
   `ignore_bad_spin` ⇒ 没有 0092e。要拿带诊断的内核**必须先 push + CI 重编**。
2. 宿主空闲内存仅 **2.79 GiB**（Chrome/QQ 等没关），连无 root 的 4G 虚机都 OOM，
   更别说 6G。要跑门禁得把空闲堆到 **≥6GiB**。

**之后流程**：push 2 commit → 给 CI 16G 源（或预置 `ios16-root.img`，用
`root_img_tag` 入口触发 `iosroot.yml`）→ 取 `trimmed-root-img-parts`（约 4G）`cat`
重组出 `trimmed-root.img` → 宿主空闲 ≥6GiB → `qemu_check.py --root-img
trimmed-root.img --secs 75`（如需全看坏锁加 `ignore_bad_spin=1` 进 boot-args）→
从 `SWEET SPIN` 行取 caller PC → `sym_kernel.py` 离线符号化定位是哪把锁。

## v19：spinlock 诊断模块深挖（2026-10-29 凌晨）

v18 只确定了「0092e 是活代码、锁在 kmap 动态区」。这一轮把 `osfmk/arm/locks_arm.c`
这个模块本身拆开看，补上四件之前不知道的事。

### 1. panic 只可能发生在「加锁」路径上

`lck_spin_verify()` 全文件共 10 个调用点，**没有一个在 unlock 路径**：

`lck_spin_lock`、`lck_spin_lock_grp`、`lck_spin_lock_nopreempt`、
`lck_spin_lock_nopreempt_grp`、`lck_spin_try_lock`、`lck_spin_try_lock_grp`、
`lck_spin_try_lock_nopreempt`、`lck_spin_try_lock_nopreempt_grp`（820–875），
外加 882 / 889 / 896 三处。

⇒ panic 的语义是「**有人试图获取一把 type 字段不对的锁**」，而不是「解锁时才发现坏了」。
排查方向因此是「谁拿到了这个（未初始化 / 已释放的）结构的指针并去加锁」，
而不是「谁把它改坏了」。

### 2. 结构体布局与常量（用来解读打印出来的两个 word）

```c
/* osfmk/arm/locks.h:70，!LCK_SPIN_IS_TICKET_LOCK 分支 */
typedef struct {
        struct hslock   hwlock;   /* offset 0: { uintptr_t lock_data; }  8B */
        unsigned long   type;     /* offset 8 */
} lck_spin_t;                     /* 共 16 字节 */
#define lck_spin_data hwlock.lock_data
```

| 常量 | 值 | 含义 |
|---|---|---|
| `LCK_SPIN_TYPE` | `0x00000011` | 合法初始化的自旋锁 |
| `LCK_SPIN_TYPE_DESTROYED` | `0x000000ee` | 已销毁 |
| `LCK_SPIN_TAG_DESTROYED` | `0xdead` | data 字上的销毁标记 |

- `lck_spin_data`（第一个 word）= **持有者的 thread 指针**，未锁时为 0
  （locks_arm.c:916 正是用 `data != 0` 判断是否已上锁）。
- 坏锁 `0xfffffe1b3b00c4d0` **16 字节对齐**，与 `lck_spin_alloc_init()` 走的
  `zalloc(KT_LCK_SPIN)` zone 元素尺寸吻合 ⇒ 确认是**动态分配**出来的锁，
  不是静态变量、也不是嵌在全局结构里的那种。
- `how=Invalid` ⇒ type 既不是 `0x11` 也不是 `0xee`、data 也不是 `0xdead`；
  最常见就是 **type 为 0**：结构从未初始化，或释放后内存被复用 / 清零（UAF）。

### 3. ★ 两个 printf 的 `return_address(0/1)` 含义不一样（最容易读错）

0092e 插了**两处** `SWEET SPIN`，它们的栈帧并不相同：

| 打印位置 | how 字段 | RA(0) | RA(1) |
|---|---|---|---|
| `__lck_spin_invalid_panic()`（真函数，`__abortlike`） | `Invalid` / `Destroyed` | 锁操作函数（`lck_spin_lock` 等） | **真凶** |
| `lck_spin_verify()` 的 ignore 分支（`static inline`） | `ignored` | **真凶** | 真凶的调用者 |

判定办法：两个地址**都**喂给 `sym_kernel.py`，
**落在上面那 10 个 lock-op 名字里的那个是「锁操作函数」，另一个才是真凶**。
若 RA(0) 符号化出来是 `lck_spin_verify` 本身（说明该调用点没被内联），
真凶就还在更外一层，只能看 panic 自带的 backtrace。

### 4. ★ 0092e 没盖住的口子：`lck_spin_assert()`

`locks_arm.c:798` 还有一处：

```c
if (lock->type != LCK_SPIN_TYPE) {
        panic("Invalid spinlock %p", lock);   /* ← 0092e 没动这里 */
}
```

0092e 的三个 hunk 只覆盖 `sweet_ignore_bad_spin()`、`__lck_spin_invalid_panic()`
和 `lck_spin_verify()`，**没有覆盖 `lck_spin_assert()`**。
万一真凶从 assert 路径进来，就**永远等不到 SWEET SPIN 行**。
⇒ 若带 0092e 的内核一次都没打出 SWEET SPIN 却照样 panic，先怀疑这里，
需要再补一个 0093e 给它加上同样的打印。

### 5. 那一行怎么读

```
SWEET SPIN: <how> spinlock <addr> <<data> <type>> from <pc0> <pc1>, thread <t>/<tid>
```

- `how`：`Invalid` / `Destroyed` / `ignored`（`ignore_bad_spin=1` 时）
- `data`：非 0 即持有者 thread 指针（可再看是谁正拿着它）
- `type`：**必须是 `0x11`**；为 0 ⇒ 未初始化或 UAF
- `pc0/pc1`：按第 3 节那张表取真凶，交给 `sym_kernel.py`
- 加 `ignore_bad_spin=1` 只报告不 panic，一次开机能把**所有**坏锁一次性看全

### 当前状态（2026-10-29 00:10）

- 内核：`kern_tmp/kernel.development.sweet`（CI run 40600859540，含修好的 0092e，
  sha256 `2d29a773…`），二进制里 `SWEET SPIN` / `sweet_ignore_bad_spin` 已确认存在。
- 无 root 的 QEMU 启动已跑过：**全程干净，无 SWEET SPIN、无 panic**
  ⇒ 坏锁在 root 挂载 / 早期用户态 / launchd 之后，必须挂 root 才复现。
- 卡点仍是 root：CI `ios16-root image` 一连修了 5 个 bug（#6–#9，见
  `.workbuddy/memory/2026-10-28.md`），#10（run 40722516918）在跑；
  出 `trimmed-root.img` 后才能上 QEMU 门禁，宿主仍需 **≥6 GiB** 空闲。

**捷径（若用户有手机端完整 panic 日志）**：xnu `panic()` 会打 backtrace（一串 PC）。
若有当时的寄存器/回溯 dump，可**直接**用现有 `kernel.development.sweet` 的符号表
（`sym_kernel.py`）符号化，省掉整个 QEMU+CI 等待。
