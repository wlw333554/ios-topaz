#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the EVERGO xnu needs to boot.

evergo = Xiaomi Redmi Note 11 5G (21091116AC), MediaTek MT6833 (Dimensity 810).
Only what the early boot path actually reads; every MMIO address comes from
the evergo live device tree (mt6833.dts / evergo.dts, Xiaomi-MT6833/
kernel_xiaomi_evergo branch R). The DDR size is a build-time default: the
shim clamps it to the real /memory bank of the DTB the bootloader passes
(loader.c clamp_memory), so one image boots the 4/6/8 GB variants.

Usage:  mkadt_evergo.py OUT.adt [--dump] [--qemu] [--highmem] [--native]

MT6833 hardware facts used here:
  cpu     6x Cortex-A55 @ MPIDR 0x000-0x005 + 2x Cortex-A76 @ 0x100-0x101,
          all ARMv8.2 (LSE, PAN, 16K granule) -> one homogeneous SMP cluster,
          XNU may use all eight cores (no sweet Gold/Silver split needed).
  gic     GICD 0x0c000000 (0x40000), GICR 0x0c040000 (0x200000).
  timer   arm,armv8-timer clock-frequency 13 MHz; vtimer PPI 11 (INTID 27).
  memory  dtsi bank0 = 0x40000000 + 0x3e605000; MTK LK patches the real banks
          into /memory. Default: bank1 0x80000000 + 2 GiB (above every MTK
          fixed carve-out, which live below 0x80000000).
  splash  the panel scans DISP OVL0 layer 1, buffer at 0x7d006000 (measured on
          the live device 2026-10-25), vram 0x1be0000, 60 fps, 1080x2400.
          The DTB's atag,videolfb-fb_base_l is a different, non-visible address.
  ufs     mediatek,ufshci @ 0x11270000 (0x2300) - driver: bsd/dev/sweet_ufs.c (MTK path)
          (see EVERGO.md); the node exists for the future driver.
  wlan    mediatek,mt6833-consys (WiFi/BT combo; driver not ported yet).
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: DDR bank1, above every MTK fixed carve-out (the dtsi
# reserved-memory alloc-ranges are 0x40000000..0x80000000). MTK LK reports
# the real banks in the DTB /memory node and the shim clamps to them.
PHYS_BASE = 0x80000000
MEM_SIZE = 0x80000000		# 2 GiB
# --highmem (phone only): the top DDR bank. The 4 GB variant has 1 GiB at
# 0x1_40000000; the 6/8 GB variants have more (see EVERGO.md). The shim
# clamps to the real bank at boot either way.
HIGH_PHYS_BASE = 0x140000000
HIGH_MEM_SIZE = int(__import__("os").environ.get("HIGHSIZE", "0x40000000"), 16)
# /arm-io children are relative to this; it must not be 0 (serial_init bails).
# 0x08000000 (chipid) covers every MTK block: GIC 0x0c000000, infra 0x10000000,
# UFS 0x11270000, dispsys 0x14000000, mdpsys 0x1f000000, consys 0x18002000.
SOC_BASE = 0x08000000

# interrupt-controller: gic@0c000000 (GICD 0x40000) + redistributors 0x0c040000
GICD, GICD_SIZE = 0x0c000000, 0x40000
GICR, GICR_SIZE = 0x0c040000, 0x200000
# The framebuffer the panel actually scans: DISP OVL0 **layer 1**, measured on
# the live device 2026-10-25 (the shim reads OVL0_L1_ADDR at boot and gets
# 0x7d006000). The old dts-derived 0x7e605000 was the DTB's atag,videolfb
# base, which is NOT the visible layer - the shim overrides it at runtime
# (adt_patch_fbcon) and the XNU early console (0081e) hardcodes the same value.
FB, FB_SIZE = 0x7d006000, 0x1be0000
SWEET_ECID = 0x4556474f00000001  # 'EVGO' + 1, synthetic
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (mediatek,ufshci@11270000); QEMU virt has none
UFS, UFS_SIZE = 0x11270000, 0x2300
TIMEBASE = 13000000
VTIMER_INTID = 27		# PPI 11 (virtual timer)
PTIMER_INTID = 30		# PPI 14
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

BOOT_MANIFEST_HASH = hashlib.sha384(b'evergo boot manifest').digest()
# Persistent-RAM console zone (Linux ramoops layout, sig "DBGC").
# 256 KiB in bank0, 4 MiB below the framebuffer block: outside the kernel's RAM
# window, so the VM can never touch it. The kernel mirrors the console here
# (patch 0083e) and the shim prints the previous boot's log from it.
PSTORE_BASE = 0x7b000000
PSTORE_SIZE = 0x00040000
# Build marker (kernel sha256 prefix), injected by build_evergo_zig.py.
BUILD_ID = os.environ.get('EVERGO_BUILD_ID', 'dev')

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def build(seed=None, qemu=False, highmem=False, native=False):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py)."""
    phys_base, mem_size = ((HIGH_PHYS_BASE, HIGH_MEM_SIZE) if highmem else (PHYS_BASE, MEM_SIZE))
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        # The kernel prints this as "iBoot version: %.128s", so it doubles
        # as a build marker: the phone screen then says which image ran.
        ('firmware-version', cstr('evergo-shim-' + BUILD_ID)),
        ('system-firmware-version', cstr('evergo-shim-' + BUILD_ID)),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # Persistent-RAM console zone (Linux ramoops layout): the kernel's fbcon
        # mirrors every console character here (patch 0083e) and the shim prints
        # the previous boot's log from it. It lives in bank0 BELOW the kernel's
        # RAM window, so the VM can never touch it. Both the kernel and the shim
        # read the address from here, so there is a single source of truth.
        ('sweet-pstore', u64(PSTORE_BASE, PSTORE_SIZE)),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # EVERGO SMP: single-core bring-up test. v15 (6x A55) still hung before the
    # framebuffer console, so try the boot core alone (cpu0). If this reaches
    # fbcon we know XNU's SMP/secondary bring-up is the blocker; if it still
    # hangs, the problem is earlier (MMU/PE) and unrelated to core count.
    # QEMU retains its original single-CPU tree: its MPIDRs differ.
    cpu_nodes = []
    for core in range(1 if qemu else 1):
        big = (not qemu) and core >= 6
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            # 'qcom,kryo470' must stay: patches 0018 (boot CPU driver) and 0038
            # (AppleKeyStore) match their IONameMatch on it, so without it the
            # CPU nub never appears and the IOKit matching never completes -
            # the boot thread then waits forever (kernel idle, no more output).
            ('compatible', (cstr('mediatek,mt6833-cpu') if big else cstr('mediatek,mt6833-cpu'))
                          + cstr('qcom,kryo470') + cstr('ARM,v8')),
            ('reg', u32(core if qemu else (0x100 | (core - 6) if big else core))),  # MPIDR Aff1:Aff0
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(0)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(2400000000 if big else 2000000000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),	# descriptive; LPDDR4X
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(QEMU_VTIMER_INTID if qemu else VTIMER_INTID)),
        ('reg', u64(gicd - SOC_BASE, GICD_SIZE, gicr - SOC_BASE, GICR_SIZE)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        # launchd starts wifid only when the device tree has a "wlan" node
        # (_LimitLoadToDeviceTree = wlan, marconi-wifi)
        node('wlan', [
            ph(),
            ('device_type', cstr('wlan')),
            ('compatible', cstr('mediatek,mt6833-consys')),
        ]),
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            # --native: present the real 1080x2400 panel
            ('canvas-width', u32(1080 if native else 1125)),
            ('canvas-height', u32(2400 if native else 2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(FB, FB_SIZE)),
        ('width', u32(FB_W)),
        ('height', u32(FB_H)),
        ('stride', u32(FB_STRIDE)),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host the bootloader leaves running (absolute
    # physical reg). MTK ufshci needs its own driver (EVERGO.md); the node is
    # here for it.
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('mediatek,ufshci')),
        ('reg', u64(UFS, UFS_SIZE)),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    # Our framebuffer is sRGB 8-bit. With the iPhone X's 'P3' MobileGestalt says
    # HasExtendedColorDisplay and WebKit renders page tiles into 10-bit surfaces
    # ('w30r', 'b3a8') that this kernel does not provide: Safari pages stay blank.
    if product_props:
        product_props = [(k, cstr('sRGB') if k == 'artwork-display-gamut' else v) for k, v in product_props]
    product = [node('product', [ph()] + product_props)] if product_props else []

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). 'xiaomi,sweet' stays because the SweetPlatformExpert and
    # CPU IONameMatch still use it; the MTK strings are ours.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('mediatek,mt6833') + cstr('xiaomi,evergo') + cstr('xiaomi,sweet') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons] + product + ([] if qemu else [ufs]))


if __name__ == '__main__':
    root = build(qemu='--qemu' in sys.argv, highmem='--highmem' in sys.argv,
                 native='--native' in sys.argv)
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
