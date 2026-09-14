# -*- coding: utf-8 -*-
"""跨平台文件锁（2026-09-14 跨平台分发要求）：POSIX=flock，Windows=msvcrt 区域锁。
接口对齐 fcntl 子集（flock/LOCK_EX/LOCK_UN），调用方用 `import flock as fcntl` 零改动换锁实现。
锁语义=同一路径文件的互斥占位：所有写 pack.json 者共用 materials/packs/<pid>/.lock（约束 4 不变）。"""
import sys

if sys.platform == "win32":
    import msvcrt

    LOCK_EX, LOCK_UN = 1, 2

    def flock(f, op):
        f.seek(0)
        if op == LOCK_EX:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # 阻塞型；msvcrt 约 10s 后抛错，与 flock 超时常态一致
        elif op == LOCK_UN:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise ValueError("unsupported lock op")
else:
    import fcntl as _f

    LOCK_EX, LOCK_UN = _f.LOCK_EX, _f.LOCK_UN
    flock = _f.flock
