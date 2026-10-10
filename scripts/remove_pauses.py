# -*- coding: utf-8 -*-
"""
剪映草稿停顿自动剪除
====================
检测视频静音停顿，生成"去停顿"新草稿（视频轨重切+字幕时间轴同步重算）。
原草稿完全不动，不满意可调参数重跑。

用法：
    python remove_pauses.py <草稿名> [--min-dur 1.0] [--keep 0.25]
                            [--suffix 快剪] [--noise -35dB] [--detect 0.6]

    --min-dur  停顿多长(秒)才剪，默认 1.0（保守1.5 / 激进0.8）
    --keep     每处剪除点保留缓冲(秒)，默认 0.25（紧凑0.1 / 宽松0.4）
    --suffix   新草稿名后缀，默认"快剪"（原名-快剪）
    --noise    静音判定阈值，默认 -35dB
    --detect   静音检测最短时长(秒)，默认 0.6（比 min-dur 小才有意义）

限制（V1）：
- 仅支持视频轨为单段完整素材的草稿（未手动切分过）。多段时报错退出。
"""
import sys
import io
import os
import json
import re
import uuid
import copy
import shutil
import argparse
import subprocess

# 注意：本脚本 import fix_subtitles（其顶层已做 UTF-8 stdout 包装），
# 这里不再重复包装，否则两个 wrapper 争同一 buffer 导致 closed file。

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
US = 1_000_000


def log(tag, msg):
    colors = {"OK": "32", "INFO": "36", "WARN": "33", "ERROR": "31", "STEP": "35"}
    print(f"\033[{colors.get(tag,'0')}m[{tag}]\033[0m {msg}")


# 复用主脚本的路径探测与 jy-draftc 封装
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fix_subtitles as fs


def find_ffmpeg():
    """PATH > 常见安装位置"""
    import shutil as _sh
    p = _sh.which("ffmpeg")
    if p:
        return p
    for c in (r"D:\软件\ffmpeg\bin\ffmpeg.exe",
              r"C:\ffmpeg\bin\ffmpeg.exe",
              os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe")):
        if os.path.exists(c):
            return c
    log("ERROR", "未找到 ffmpeg（用于静音检测）。请安装或加入 PATH。")
    sys.exit(1)


def detect_silences(video_path, dur_us, noise, detect_s):
    """ffmpeg silencedetect，逐行状态机配对（绝不错位），返回微秒静音段列表"""
    out = subprocess.run(
        [find_ffmpeg(), "-i", video_path,
         "-af", f"silencedetect=noise={noise}:d={detect_s}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    pairs, pending = [], None
    for m in re.finditer(r'silence_(start|end): ([\d.]+)', out):
        kind, val = m.group(1), int(round(float(m.group(2)) * US))
        if kind == 'start':
            pending = val
        else:
            pairs.append((pending, val))
            pending = None
    if pending is not None:
        pairs.append((pending, dur_us))  # 片尾静音无end
    return pairs


def main():
    parser = argparse.ArgumentParser(description="剪映草稿停顿剪除（生成新草稿）")
    parser.add_argument("draft", help="源草稿名")
    parser.add_argument("--min-dur", type=float, default=1.0, help="剪除门槛(秒)")
    parser.add_argument("--keep", type=float, default=0.25, help="每处保留缓冲(秒)")
    parser.add_argument("--suffix", default="快剪", help="新草稿名后缀")
    parser.add_argument("--noise", default="-35dB", help="静音阈值")
    parser.add_argument("--detect", type=float, default=0.6, help="静音检测最短时长(秒)")
    args = parser.parse_args()

    fs.JY_EXE = fs.find_jy_exe()
    draft_root = fs.find_draft_root()
    src = os.path.join(draft_root, args.draft)
    src_content = os.path.join(src, "draft_content.json")
    if not os.path.exists(src_content):
        log("ERROR", f"找不到草稿 '{args.draft}'")
        sys.exit(1)
    dst_name = f"{args.draft}-{args.suffix}"
    dst = os.path.join(draft_root, dst_name)

    # ---- 1. 解密源草稿（只读） ----
    log("STEP", "[1/5] 解密源草稿 ...")
    plain = os.path.join(os.environ.get("TEMP", "/tmp"), "rp_plain.json")
    fs.decrypt(src_content, plain)
    doc = json.load(open(plain, encoding="utf-8"))
    dur_us = int(doc["duration"])

    # ---- 2. 校验视频轨：必须单段完整素材 ----
    vtrack = next((t for t in doc["tracks"] if t["type"] == "video"), None)
    if not vtrack or len(vtrack["segments"]) != 1:
        log("ERROR", f"视频轨有 {len(vtrack['segments']) if vtrack else 0} 段（V1 仅支持单段完整素材的草稿）。"
                     "请先在未切分的原片草稿上跑，或手动处理。")
        sys.exit(1)
    oseg = vtrack["segments"][0]
    vid_mat_id = oseg["material_id"]
    vid_mat = next(m for m in doc["materials"]["videos"] if m["id"] == vid_mat_id)
    vpath = vid_mat.get("path", "")
    if vpath.startswith("##"):
        rel = vpath.split("##/")[-1]
        vpath = os.path.join(src, rel)  # 草稿相对路径占位符
    if not os.path.exists(vpath):
        log("ERROR", f"视频素材不存在: {vpath}")
        sys.exit(1)

    # ---- 3. 静音检测 → 剪除清单（全整数微秒） ----
    log("STEP", f"[2/5] 静音检测（{args.noise} / {args.detect}s+）...")
    silences = detect_silences(vpath, dur_us, args.noise, args.detect)
    keep_us, min_us = int(args.keep * US), int(args.min_dur * US)
    cuts = [(s, e - keep_us) for s, e in silences if e - s >= min_us]
    if not cuts:
        log("INFO", "没有达到剪除门槛的停顿，无需处理。")
        sys.exit(0)
    assert all(cs < ce for cs, ce in cuts) and all(
        cuts[i][1] <= cuts[i + 1][0] for i in range(len(cuts) - 1)), "静音配对异常"
    total = sum(ce - cs for cs, ce in cuts)
    log("OK", f"静音段 {len(silences)} 个 -> 剪除 {len(cuts)} 处 / {total/1e6:.1f}s "
             f"（门槛{args.min_dur}s 缓冲{args.keep}s）")

    # ---- 4. 时间轴手术（整数微秒 + off累计remap，两个已验证的坑规避） ----
    log("STEP", "[3/5] 时间轴手术 ...")

    def remap(t):  # 标准偏移累计：落剪除区间内→映射到该cut起点
        off = 0
        for cs, ce in cuts:
            if t >= ce:
                off += ce - cs
            elif t > cs:
                return cs - off
        return t - off

    base = oseg.get("source_timerange", {}).get("start", 0)
    bounds = [0] + [x for c in cuts for x in c] + [dur_us]
    keeps = [(bounds[i], bounds[i + 1]) for i in range(0, len(bounds) - 1, 2)]
    new_segs, cursor = [], 0
    for ks, ke in keeps:
        if ke - ks < 50_000:
            continue
        seg = copy.deepcopy(oseg)
        seg["id"] = str(uuid.uuid4()).upper()
        seg["source_timerange"] = {"start": ks + base, "duration": ke - ks}
        seg["target_timerange"] = {"start": cursor, "duration": ke - ks}
        new_segs.append(seg)
        cursor += ke - ks
    vtrack["segments"] = new_segs

    shrunk = 0
    for tr in doc["tracks"]:
        if tr["type"] == "text":
            for seg in tr["segments"]:
                t = seg["target_timerange"]
                na = remap(t["start"])
                nb = min(remap(t["start"] + t["duration"]), cursor)
                nd = max(nb - na, 200_000)
                if nd < t["duration"] - 1000:
                    shrunk += 1
                t["start"], t["duration"] = na, nd
    doc["duration"] = cursor

    # ---- 5. 生成新草稿 ----
    log("STEP", f"[4/5] 生成新草稿「{dst_name}」...")
    if os.path.exists(dst):
        shutil.rmtree(dst)
        log("WARN", f"已存在同名草稿，删除重建")
    shutil.copytree(src, dst)
    json.dump(doc, open(plain, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    enc = plain + ".enc"
    fs.encrypt(plain, enc)
    shutil.copy2(enc, os.path.join(dst, "draft_content.json"))

    # meta 改名
    plain_m = plain + ".m"
    env = dict(os.environ)
    env["JY_INSTALL_DIR"] = fs.find_jy_install()
    subprocess.run([fs.JY_EXE, "-d", os.path.join(dst, "draft_meta_info.json"), plain_m],
                   capture_output=True, env=env, cwd=os.path.dirname(fs.JY_EXE))
    meta = json.load(open(plain_m, encoding="utf-8"))
    meta["draft_name"] = dst_name
    json.dump(meta, open(plain_m, "w", encoding="utf-8"), ensure_ascii=False)
    subprocess.run([fs.JY_EXE, "-e", plain_m, os.path.join(dst, "draft_meta_info.json")],
                   capture_output=True, env=env, cwd=os.path.dirname(fs.JY_EXE))
    for f in (plain, enc, plain_m):
        try: os.remove(f)
        except OSError: pass

    # ---- 6. 回读终验 ----
    log("STEP", "[5/5] 回读终验 ...")
    vf = os.path.join(os.environ.get("TEMP", "/tmp"), "rp_verify.json")
    fs.decrypt(os.path.join(dst, "draft_content.json"), vf)
    d2 = json.load(open(vf, encoding="utf-8"))
    segs = next(t for t in d2["tracks"] if t["type"] == "video")["segments"]
    gaps = sum(1 for i in range(1, len(segs))
               if segs[i]["target_timerange"]["start"] !=
               segs[i-1]["target_timerange"]["start"] + segs[i-1]["target_timerange"]["duration"])
    tt = next(t for t in d2["tracks"] if t["type"] == "text")["segments"]
    max_end = max(s["target_timerange"]["start"] + s["target_timerange"]["duration"] for s in tt)
    os.remove(vf)
    ok = gaps == 0 and max_end <= d2["duration"]
    if not ok:
        log("ERROR", f"终验失败：断裂{gaps}处 / 字幕max_end {max_end/1e6:.1f}s vs 片长 {d2['duration']/1e6:.1f}s")
        sys.exit(1)

    print()
    log("OK", f"新草稿「{dst_name}」已生成（原「{args.draft}」未动）")
    log("OK", f"{dur_us/1e6/60:.1f}分钟 -> {cursor/1e6/60:.1f}分钟（剪除{len(cuts)}处/{total/1e6/60:.1f}分钟）")
    log("INFO", f"视频轨 {len(new_segs)} 段零断裂 | 字幕 {len(tt)} 条对齐（{shrunk}条因横跨剪除点显示时长略缩）")
    log("INFO", "打开剪映验收；太紧/太松可调 --min-dur / --keep 重跑")


if __name__ == "__main__":
    main()
