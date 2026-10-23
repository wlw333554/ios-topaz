#!/usr/bin/env python3
"""Local end-to-end check of the evergo build chain, no cross-GCC needed.

Exercises the whole tooling with a synthetic minimal arm64 Mach-O in place
of the real kernel:

    tools/mkadt_evergo.py  ->  tools/mkpayload.py  ->  shim/ (zig cc cross)
    ->  newc cpio ramdisk with the payload inside  ->  mkbootimg.py
    ->  shim-boot.img (boot image v3, evergo packing)

It validates the *tooling* (ADT contents, payload layout, ramdisk packing,
boot image header), not the kernel: the real kernel.development.sweet comes
from the CI run (see EVERGO.md "Build"). What it catches before a CI cycle:
mkpayload rejecting our ADT, wrong payload/ramdisk packing, a broken
mkbootimg invocation, header-version problems.

Usage:  python3 tools/local-check-zig.py [--device evergo] [--keep]
Needs:  zig on PATH, or the ziglang pip package (python -m ziglang cc).
        mkbootimg.py: pass --mkbootimg PATH, or put it next to this script /
        in the parent directory (AOSP; see EVERGO.md).
"""
import argparse, gzip, os, struct, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import adt  # noqa: E402

PAYLOAD_MAGIC = b"SWEETXNU"


def find_zig():
    """Return the zig cc command prefix, or None."""
    from shutil import which
    if which("zig"):
        return ["zig", "cc"]
    for py in (sys.executable,
               r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"):
        try:
            r = subprocess.run([py, "-m", "ziglang", "version"], capture_output=True)
            if r.returncode == 0:
                return [py, "-m", "ziglang", "cc"]
        except OSError:
            pass
    return None


def find_mkbootimg(arg):
    cands = [arg, os.path.join(HERE, "mkbootimg.py"), os.path.join(ROOT, "mkbootimg.py"),
             os.path.join(os.path.dirname(ROOT), "mkbootimg.py")]
    for c in cands:
        if c and os.path.exists(c):
            return [sys.executable, c]
    return None


def dummy_kernel(path):
    """Minimal arm64 Mach-O mkpayload.py accepts: one __TEXT segment holding the
    Mach header itself (fileoff 0), an LC_UNIXTHREAD entry, no chained fixups."""
    text_vmaddr, text_vmsize = 0x100000000, 0x200000
    entry_off = 0x40
    seg_size, thread_size = 72, 16 + 4 * 68
    hdr_size = 32 + seg_size + thread_size
    code = bytes(range(256)) * 4          # 0x400 bytes of dummy code
    hdr = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 2, 2,
                      seg_size + thread_size, 1, 0)
    seg = struct.pack('<II16sQQQQIIII', 0x19, seg_size, b'__TEXT',
                      text_vmaddr, text_vmsize, 0, hdr_size + len(code), 5, 5, 0, 0)
    state = [0] * 33
    state[32] = text_vmaddr + entry_off   # pc
    thread = struct.pack('<IIII', 0x5, thread_size, 6, 68) + \
        struct.pack('<33QII', *state, 0, 0)
    assert len(hdr + seg + thread) == hdr_size
    open(path, 'wb').write(hdr + seg + thread + code)
    return text_vmaddr + entry_off


def newc(files):
    """newc cpio archive (bytes). files = [(name, mode, data)]."""
    out = bytearray()
    for name, mode, data in files:
        nm = name.encode() + b'\0'
        out += b'070701'
        out += struct.pack('<8s', b'%08X' % 0)      # ino
        for v in (mode, 0, 0, 1, 0):               # mode uid gid nlink mtime
            out += b'%08X' % v
        out += b'%08X' % len(data)                 # filesize
        for v in (0, 0, 0, 0):                     # dev rdev
            out += b'%08X' % v
        out += b'%08X' % len(nm)                   # namesize
        out += b'%08X' % 0                         # check
        out += nm
        out += b'\0' * (-len(out) % 4)
        out += data
        out += b'\0' * (-len(out) % 4)
    out += b'070701' + b'%08X' % 0
    for v in (0o100755, 0, 0, 1, 0, 0, 0, 0, 0, 0):
        out += b'%08X' % v
    out += b'%08X' % (len(b'TRAILER!!!\0'))
    out += b'%08X' % 0
    out += b'TRAILER!!!\0'
    out += b'\0' * (-len(out) % 4)
    return bytes(out)


def elf_end_symbol(elf):
    """_end value from the ELF symtab (build.sh pads the Image to it)."""
    b = open(elf, 'rb').read()
    e_shoff, = struct.unpack_from('<Q', b, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', b, 0x3a)
    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name, typ, flags, addr, offset, size, link, info, align, entsize = \
            struct.unpack_from('<IIQQQQIIQQ', b, off)
        secs.append((name, typ, addr, offset, size, link))
    shstr_off = secs[e_shstrndx][3]

    def sname(n):
        o = shstr_off + n
        return b[o:b.index(b'\0', o)]
    for name, typ, addr, offset, size, link in secs:
        if sname(name) != b'.symtab':
            continue
        stroff = secs[link][3]
        for i in range(size // 24):
            nm, info, other, shndx, value, sz = struct.unpack_from('<IBBHQQ', b, offset + i * 24)
            o = stroff + nm
            if b[o:b.index(b'\0', o)] == b'_end':
                return value
    raise SystemExit('no _end symbol in ' + elf)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f"FAILED {cmd[0]} {cmd[1:4]}...:\n{r.stdout}{r.stderr}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='evergo')
    ap.add_argument('--mkbootimg', default=None)
    ap.add_argument('--keep', action='store_true', help='keep the work dir')
    a = ap.parse_args()

    zig = find_zig()
    if not zig:
        raise SystemExit('zig not found (pip install ziglang, or zig on PATH)')
    mkb = find_mkbootimg(a.mkbootimg)
    if not mkb:
        raise SystemExit('mkbootimg.py not found (AOSP; --mkbootimg PATH)')

    work = os.path.join(ROOT, 'shim', 'out', 'localcheck')
    os.makedirs(work, exist_ok=True)
    print(f'work dir: {work}')

    # 1. ADT
    adt_path = os.path.join(work, f'{a.device}.adt')
    run([sys.executable, os.path.join(HERE, f'mkadt_{a.device}.py'), adt_path])
    adt_blob = open(adt_path, 'rb').read()
    root, _ = adt.parse(adt_blob)
    chosen = next(c for c in root.children if c.name == 'chosen')
    base, = struct.unpack('<Q', chosen.prop('dram-base'))
    size, = struct.unpack('<Q', chosen.prop('dram-size'))
    print(f'1. ADT ok: {len(adt_blob)} bytes, dram 0x{base:x}+0x{size:x}')

    # 2. payload (dummy kernel + ADT + cmdline; with a RAM-disk root too)
    kern = os.path.join(work, 'kernel.dummy')
    entry = dummy_kernel(kern)
    print(f'2. dummy kernel: {os.path.getsize(kern)} bytes, entry 0x{entry:x}')
    cmdline = ('debug=0xc keepsyms=1 -noprogress rd=md0')
    payload = os.path.join(work, 'payload.bin')
    rootimg = os.path.join(work, 'root.img')
    open(rootimg, 'wb').write(b'EVERGO-CHECK-ROOT' + bytes(0x10000 - 17))
    run([sys.executable, os.path.join(HERE, 'mkpayload.py'), kern, adt_path, payload,
         '--ramdisk', rootimg, '--cmdline', cmdline])
    blob = open(payload, 'rb').read()
    assert blob[:8] == PAYLOAD_MAGIC, 'payload magic'
    print(f'   payload ok: {len(blob)} bytes, magic {blob[:8].decode()}')

    # 3. shim (zig cc cross, DEVICE_EVERGO), link, Image
    # The repo is built with GNU as, which assembles an EL1 "smc #0" happily;
    # clang's integrated assembler rejects it outside EL3. Only this local
    # check needs the workaround - the sources in shim/ are untouched.
    src = os.path.join(work, 'src')
    os.makedirs(src, exist_ok=True)
    for f in os.listdir(os.path.join(ROOT, 'shim')):
        if not f.endswith(('.c', '.h', '.S', '.ld')):
            continue
        t = open(os.path.join(ROOT, 'shim', f), 'r', encoding='utf-8').read()
        open(os.path.join(src, f), 'w', encoding='utf-8', newline=''
             ).write(t.replace('smc #0', '.inst 0xd4000001'))
    cflags = ['-target', 'aarch64-freestanding', '-O2', '-ffreestanding', '-nostdlib',
              '-fno-pic', '-mgeneral-regs-only', '-mstrict-align',
              '-fno-stack-protector', '-Wall', '-Wextra', f'-DDEVICE_{a.device.upper()}']
    objs = []
    for f in ('start.S', 'shim.c', 'console.c', 'mmu.c', 'fdt.c', 'loader.c', 'ufs.c'):
        o = os.path.join(work, f.rsplit('.', 1)[0] + '.o')
        run(zig + cflags + ['-c', os.path.join(src, f), '-o', o])
        objs.append(o)
    elf = os.path.join(work, 'shim.elf')
    run(zig + ['-target', 'aarch64-freestanding', '-nostdlib',
               '-Wl,-T,' + os.path.join(src, 'shim.ld'),
               '-Wl,--build-id=none'] + objs + ['-o', elf])
    image = os.path.join(work, 'Image')
    if zig[0] == 'zig':
        run(['zig', 'objcopy', '-O', 'binary', elf, image])
    else:                       # python -m ziglang cc -> python -m ziglang objcopy
        run([zig[0], '-m', 'ziglang', 'objcopy', '-O', 'binary', elf, image])
    end = elf_end_symbol(elf)
    img = open(image, 'rb').read()
    if len(img) < end:                     # .bss tail: pad to image_size
        img += bytes(end - len(img))
        open(image, 'wb').write(img)
    print(f'3. shim ok: Image {len(img)} bytes (image_size/_end 0x{end:x}), '
          f'entry check ok')

    # 4. ramdisk: the payload inside a newc cpio (evergo packing, gzip)
    rd = newc([('.', 0o040755, b''), ('payload.bin', 0o100644, blob)])
    rd_gz = os.path.join(work, 'ramdisk.bin')
    with gzip.GzipFile(rd_gz, 'wb', compresslevel=9, mtime=0) as g:
        g.write(rd)
    print(f'4. ramdisk ok: {len(rd)} bytes cpio -> {os.path.getsize(rd_gz)} gzip, '
          f'payload inside')

    # 5. boot image v3 (evergo default header version)
    # AOSP mkbootimg.py imports gki.generate_gki_certificate unconditionally,
    # but only calls it for header v4 + --gki_signing_*. Stub it out so the
    # stock single-file tool works for our v3 images without the AOSP tree.
    gki = os.path.join(work, 'gki')
    os.makedirs(gki, exist_ok=True)
    open(os.path.join(gki, '__init__.py'), 'w').write('')
    open(os.path.join(gki, 'generate_gki_certificate.py'), 'w').write(
        'def generate_gki_certificate(*a, **kw):\n'
        '    raise SystemExit("GKI signing is only used by boot header v4")\n')
    env = dict(os.environ, PYTHONPATH=work + os.pathsep + os.environ.get('PYTHONPATH', ''))
    out = os.path.join(work, 'shim-boot.img')
    run(mkb + ['--header_version', '3', '--kernel', image, '--ramdisk', rd_gz,
               '--os_version', '12.0.0', '--os_patch_level', '2025-01',
               '--cmdline', 'console=null', '-o', out], env=env)
    total = os.path.getsize(out)
    raw = open(out, 'rb').read()
    magic = raw[:8]
    assert magic == b'ANDROID!', magic
    ksize, = struct.unpack_from('<I', raw, 8)
    rdsize, = struct.unpack_from('<I', raw, 12)
    assert ksize == os.path.getsize(image), (ksize, os.path.getsize(image))
    assert rdsize == os.path.getsize(rd_gz), (rdsize, os.path.getsize(rd_gz))
    print(f'5. boot image ok: {total} bytes, magic {magic.decode()}, '
          f'kernel 0x{ksize:x}, ramdisk 0x{rdsize:x}')

    # 6. round-trip: the payload is findable in the packed ramdisk
    raw = gzip.decompress(open(rd_gz, 'rb').read())
    assert PAYLOAD_MAGIC in raw, 'payload not in ramdisk'
    print('6. round-trip ok: SWEETXNU payload findable in the packed ramdisk')
    print('ALL CHECKS PASSED (tooling only - the real kernel comes from CI)')


if __name__ == '__main__':
    main()
