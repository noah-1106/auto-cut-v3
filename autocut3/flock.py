# -*- coding: utf-8 -*-
"""跨平台文件锁（2026-09-14 跨平台分发要求）：POSIX=flock，Windows=msvcrt 区域锁。
接口对齐 fcntl 子集（flock/LOCK_EX/LOCK_UN），调用方用 `import flock as fcntl` 零改动换锁实现。
锁语义=同一路径文件的互斥占位：所有写 pack.json 者共用 materials/packs/<pid>/.lock（约束 4 不变）。"""
import os, sys

if sys.platform == "win32":
    import msvcrt

    LOCK_EX, LOCK_UN, LOCK_NB = 1, 2, 4

    def flock(f, op):
        f.seek(0)
        if op & LOCK_EX:
            # LOCK_NB=非阻塞（占用即抛）；阻塞型 LK_LOCK 约 10s 后抛错，与 flock 超时常态一致
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if op & LOCK_NB else msvcrt.LK_LOCK, 1)
        elif op == LOCK_UN:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise ValueError("unsupported lock op")
else:
    import fcntl as _f

    LOCK_EX, LOCK_UN, LOCK_NB = _f.LOCK_EX, _f.LOCK_UN, _f.LOCK_NB
    flock = _f.flock


def write_json(path, obj, indent=1):
    """原子写 JSON：tmp 带 pid（并行写者互不踩 tmp）→ os.replace 原子换名，读者永不读到半截文件。
    实证 2026-09-17：draft 双跑（工具超时幸存进程 + Agent 重跑并存）非原子 dump 会交错写坏故事线。"""
    import json
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
