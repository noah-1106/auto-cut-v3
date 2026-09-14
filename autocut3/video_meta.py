#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频几何元数据：返回【显示尺寸】（ffmpeg 自动转正后的内容方向），而非编码像素。

教训（2026-09-11 苏炜首单实锤）：手机素材普遍"编码 1920x1080 + rotation=-90"，
播放/滤镜看到的是 1080x1920 竖版画面。只读编码宽高 → 项目画幅定错 → 全片压扁。
判定：源显示 H > W（竖版内容）→ 竖版项目；源显示 W > H → 横版项目。"""
import json, os, shutil, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ffprobe_path():
    for c in (os.environ.get("FFPROBE"), os.path.join(ROOT, "bin", "ffprobe"),
              os.path.join(ROOT, "bin", "ffprobe.exe")):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffprobe")


def ffmpeg_path():
    for c in (os.environ.get("FFMPEG"), os.path.join(ROOT, "bin", "ffmpeg"),
              os.path.join(ROOT, "bin", "ffmpeg.exe")):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg")


def read_rotation(s):
    """从流元数据读 rotation（角度），跨版本字段路径兜底。读不到返回 None。"""
    for sd in (s.get("side_data_list") or []):  # 新版：side_data_list[].rotation
        if isinstance(sd, dict) and sd.get("rotation") is not None:
            return float(sd["rotation"])
    rot = (s.get("tags") or {}).get("rotate")  # 旧版：tags.rotate="90"
    if rot is not None:
        try:
            return float(rot)
        except (TypeError, ValueError):
            return None
    return None


def display_geometry(path):
    """{encode_w, encode_h, w, h, rotated, rotation, geometry_source, duration}

    双路径：① 快路径直接读 rotation 元数据（字段路径跨版本兜底）换算显示尺寸；
           ② 一帧实测交叉校验（ffmpeg 自动转正后的真实帧）——元数据说谎时以实测为准。
    rotation 角度始终落盘：入库可审计，排查几何问题时能直接看到病因。"""
    fp = ffprobe_path()
    r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,sample_aspect_ratio:stream_side_data=rotation:stream_tags=rotate:format=duration",
                        "-of", "json", path], capture_output=True, text=True)
    meta = json.loads(r.stdout or "{}")
    s = (meta.get("streams") or [{}])[0]
    ew, eh = int(s.get("width") or 0), int(s.get("height") or 0)
    rotation = read_rotation(s)

    # 快路径：±90/270 旋转 → 编码宽高互换
    swapped = rotation is not None and abs(abs(rotation) - 90) % 180 < 0.001
    rw, rh = (eh, ew) if swapped else (ew, eh)

    # 一帧实测交叉校验（autorotate 后的真实帧——渲染链看到的就是它）
    import tempfile
    dw, dh = rw, rh
    source = "rotation-read"
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        r2 = subprocess.run([ffmpeg_path() or "ffmpeg", "-y", "-loglevel", "error", "-i", path,
                             "-frames:v", "1", tmp], capture_output=True, text=True)
        if r2.returncode == 0 and os.path.getsize(tmp) > 0:
            with open(tmp, "rb") as fh:
                head = fh.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                import struct
                w, h = struct.unpack(">II", head[16:24])
                if (w, h) != (rw, rh):
                    print(f"  ⚠ 几何元数据与实测不符：rotation 推算 {rw}x{rh}，实测 {w}x{h}——以实测为准")
                    source = "frame-probe"
                dw, dh = w, h
        os.remove(tmp)
    except Exception:
        pass  # 实测失败时用 rotation 推算值
    return {"encode_w": ew, "encode_h": eh, "w": dw, "h": dh,
            "rotated": (dw, dh) != (ew, eh),
            "rotation": rotation,
            "geometry_source": source,
            "duration": round(float((meta.get("format") or {}).get("duration") or 0), 1)}


if __name__ == "__main__":
    import sys
    print(json.dumps(display_geometry(sys.argv[1]), ensure_ascii=False))
