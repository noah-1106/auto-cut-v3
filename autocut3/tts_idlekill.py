#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audio8 TTS 服务闲置看门狗（2026-09-19 Noah 点单：空闲 60 分钟自动退出）。

tts_audio8.ensure_server 拉起服务时随附派生本进程（脱离会话）：
每 5 分钟醒一次查两件事——①服务 pid 已死 → 看门狗自退；②last-use 标记
超过闲置窗（默认 3600s）→ SIGTERM 结束服务并自退。使用方（synth/register）
成功即 touch last-use，服务自然续命；下次调用 ensure_server 按需自启，零感知。

手动外部拉起的服务（run_server.sh）不在本看门狗覆盖内——pid 只有自启路径知道。

用法（自动派生，一般不手跑）: python3 tts_idlekill.py <服务pid> [闲置秒=3600]
"""
import os
import signal
import sys
import time

pid = int(sys.argv[1])
idle_s = int(sys.argv[2]) if len(sys.argv) > 2 else 3600
check_s = max(5, min(300, idle_s // 12))  # 检查粒度：≤5min（小闲置窗探针/测试可到 5s）
marker = os.path.join(os.path.expanduser(
    os.environ.get("AUTOCUT_AUDIO8_HOME", "~/.local/share/autocut3/audio8")), "last-use")


def _alive(p):
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False


while True:
    time.sleep(check_s)
    if not _alive(pid):
        break
    try:
        idle = time.time() - os.path.getmtime(marker)
    except OSError:
        idle = float("inf")  # 无标记=从未真正使用（voices 查询不算）→ 视为满闲置
    if idle >= idle_s:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        break
