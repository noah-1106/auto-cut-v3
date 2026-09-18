#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 图片生成（MiniMax image-01，同步接口）——封面生成等静态图用途。

接口实证（2026-09-15 真 key 探测）：POST {base_url}/image_generation
  {"model":"image-01","prompt":...,"aspect_ratio":"9:16"}
→ data.image_urls[0] 为下载地址（同步返回，无轮询）。

图生图实证（2026-09-18 真 key 矩阵探测）：
  subject_reference=[{"type":"character","image_file":"data:image/jpeg;base64,…"}]
  · data URI 底图通道通（合成中性图/空镜帧均 status=0 出图）
  · ⚠ 含真人脸的底图一律 1026「input new_sensitive」审核拦截（换纯场景词照拦，
    与提示词无关）——含人封面禁走 AI 路线，用素材代表帧+drawtext 标题（pipeline.build_cover）
  · /files/upload 的 purpose 在 CN 站全拒（2013），本地文件→data URI 是唯一可用底图通道

用法：
  python3 autocut3/image_gen.py "prompt" out.jpg [--ar 9:16] [--base 底图.jpg]
  import image_gen; image_gen.gen_image(prompt, out_path, base_image="底图.jpg")
"""
import argparse, base64, json, os, sys, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asr import load_services, resolve_key  # noqa: E402 平台解析链同栈（key 全走 env）


def _backend():
    sv = load_services()
    img = sv.get("image") or {}
    be = (img.get("backends") or {}).get(img.get("provider") or "minimax")
    if not be:
        raise RuntimeError("services.json 缺 image 段——参考 video 段补 minimax backend")
    return be


def gen_image(prompt, out_path, aspect_ratio="9:16", model=None, timeout_s=180, base_image=None):
    """base_image=本地底图路径 → data URI 进 subject_reference（保主体图生图）。
    底图含真人脸会被平台审核拦（1026，见模块头）——调用方负责选空镜底图并给出可读失败。"""
    be = _backend()
    body = {"model": model or be.get("model", "image-01"),
            "prompt": prompt, "aspect_ratio": aspect_ratio}
    if base_image:
        with open(base_image, "rb") as fh:
            uri = "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
        body["subject_reference"] = [{"type": "character", "image_file": uri}]
    req = urllib.request.Request(
        be["base_url"].rstrip("/") + "/image_generation",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + resolve_key(be)})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout_s).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("图片生成失败 [%s]: %s" % (e.code, e.read()[:200]))
    st = (r.get("base_resp") or {})
    if st.get("status_code", 0) not in (0, None):
        raise RuntimeError("图片生成失败: [%s] %s" % (st.get("status_code"), st.get("status_msg")))
    urls = ((r.get("data") or {}).get("image_urls")) or []
    if not urls and r.get("images"):
        b64 = r["images"][0].get("base64")
        if b64:
            with open(out_path, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return out_path
    if not urls:
        raise RuntimeError("无图返回: %s" % json.dumps(r, ensure_ascii=False)[:200])
    with urllib.request.urlopen(urls[0], timeout=60) as resp, open(out_path, "wb") as fo:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fo.write(chunk)
    return out_path


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台/重定向兜底：emoji 输出 UnicodeEncodeError 不炸（2026-09-15 审计 P2-5）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="AI 图片生成（MiniMax image-01）")
    ap.add_argument("prompt")
    ap.add_argument("out")
    ap.add_argument("--ar", default="9:16")
    ap.add_argument("--base", help="底图路径（subject_reference 保主体；含真人脸会被平台审核拦）")
    a = ap.parse_args()
    print(gen_image(a.prompt, a.out, aspect_ratio=a.ar, base_image=a.base), flush=True)
