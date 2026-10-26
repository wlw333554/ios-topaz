#pragma once
void test_ufs(void);

/* 1 if the blob is QEMU's virt FDT. The probe touches a hard-coded MMIO
 * base that does not exist there, so it must not run under QEMU; every
 * other machine (the phone) gets it. */
int ufs_is_qemu(uintptr_t fdt);
