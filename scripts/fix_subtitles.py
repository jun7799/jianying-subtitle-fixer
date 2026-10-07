# -*- coding: utf-8 -*-
"""
剪映字幕 AI 纠错工具
====================
流程：剪映识别字幕 → 完全退出剪映 → 本脚本(解密→AI纠错→回加密) → 重开剪映手动剪辑

用法：
    python fix_subtitles.py <草稿名> [--dry-run] [--no-llm] [--batch N]
                             [--reuse] [--model MODEL]

    --dry-run  只生成对照表和SRT，不写回草稿（剪映开着也能跑）
    --no-llm   只跑规则层替换，不调智谱API（快速+零成本）
    --batch N  LLM每批条数，默认30
    --reuse    复用最近一次计算的纠错结果直接写回，跳过LLM重跑（秒级）。
               前提：自那次 dry-run 后没有在剪映里改过字幕
    --model    智谱模型名，默认环境变量 ZHIPU_MODEL 或 glm-4-flash；
               晚高峰限流慢时可换 glm-5.3-flash

环境要求（详见 references/setup.md）：
    - jy-draftc.exe 放在本 skill 的 tools/ 下（自行从 GitHub 下载）
    - 剪映安装目录中有 videoeditor.dll（自动探测，或设 JY_INSTALL_DIR 环境变量）
    - 智谱 API key：环境变量 ZHIPU_API_KEY，或自动读取 ~/.claude/settings.json

产物（workspace/<草稿名>/ 下）：
    original.srt   纠错前的字幕存档
    fixed.srt      纠错后的字幕
    changes.md     修改对照表（人工抽查用）
    backups/       原草稿文件备份（回滚用）
"""
import sys
import io
import os
import json
import re
import glob
import time
import argparse
import subprocess
import shutil
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMS_FILE = os.path.join(SKILL_DIR, r"config\terms.json")
WORKSPACE = os.path.join(SKILL_DIR, r"workspace")

# ============ 智谱 API ============
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")  # 可用 --model 覆盖


def log(tag, msg):
    colors = {"OK": "32", "INFO": "36", "WARN": "33", "ERROR": "31", "STEP": "35"}
    print(f"\033[{colors.get(tag,'0')}m[{tag}]\033[0m {msg}")


# ============ 路径自动探测 ============

def find_draft_root():
    """剪映草稿根目录：环境变量 > 标准位置"""
    env = os.environ.get("JY_DRAFT_ROOT")
    if env and os.path.isdir(env):
        return env
    local = os.environ.get("LOCALAPPDATA", "")
    p = os.path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
    if os.path.isdir(p):
        return p
    log("ERROR", f"未找到剪映草稿目录（尝试过 {p}）。可用环境变量 JY_DRAFT_ROOT 指定。")
    sys.exit(1)


def find_jy_install():
    """剪映安装目录（含 videoeditor.dll）：环境变量 > 常见位置扫描"""
    env = os.environ.get("JY_INSTALL_DIR")
    if env and os.path.exists(os.path.join(env, "videoeditor.dll")):
        return env
    candidates = []
    for drive in ["C:", "D:", "E:"]:
        candidates += [
            os.path.join(drive, os.sep, "Program Files", "JianyingPro"),
            os.path.join(drive, os.sep, "软件", "JianyingPro"),
            os.path.join(drive, os.sep, "Apps", "JianyingPro"),
        ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        # 版本号子目录里找 videoeditor.dll，取最新
        hits = glob.glob(os.path.join(base, "*", "videoeditor.dll"))
        if hits:
            return max(hits, key=lambda p: p).rsplit(os.sep, 1)[0]
    log("ERROR", "未找到剪映安装目录（含 videoeditor.dll）。"
                "请设置环境变量 JY_INSTALL_DIR 指向剪映版本目录。")
    sys.exit(1)


def find_jy_exe():
    """jy-draftc.exe：本 skill tools/ 下（自行下载，见 references/setup.md）"""
    hits = glob.glob(os.path.join(SKILL_DIR, "tools", "**", "jy-draftc*.exe"), recursive=True)
    if hits:
        return hits[0]
    log("ERROR", "未找到 jy-draftc.exe。请按 references/setup.md 下载并放到本 skill 的 tools/ 目录。")
    sys.exit(1)


JY_EXE = None  # 延迟初始化


def check_jianying_running():
    """剪映开着时改草稿文件会冲突，必须先关"""
    try:
        out = subprocess.run(
            [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "tasklist.exe"),
             "/FI", "IMAGENAME eq JianyingPro.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        if "JianyingPro.exe" in out:
            log("ERROR", "检测到剪映正在运行！请先完全退出剪映（包括托盘），再跑本脚本。")
            log("INFO", "原因：剪映会缓存草稿并监听文件，外部改完会被覆盖或导致冲突。")
            sys.exit(1)
    except Exception as e:
        log("WARN", f"进程检测失败(忽略继续): {e}")


def load_terms():
    with open(TERMS_FILE, encoding="utf-8") as f:
        return json.load(f)


def run_jyd(mode, src, dst):
    """调用 jy-draftc 解密(-d)/回加密(-e)"""
    env = dict(os.environ)
    env["JY_INSTALL_DIR"] = find_jy_install()
    r = subprocess.run(
        [JY_EXE, mode, src, dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.dirname(JY_EXE), env=env)
    return r


def decrypt(draft_content_path, out_path):
    r = run_jyd("-d", draft_content_path, out_path)
    if not os.path.exists(out_path):
        log("ERROR", f"解密失败: {r.stdout} {r.stderr}")
        sys.exit(1)


def encrypt(plain_path, out_path):
    r = run_jyd("-e", plain_path, out_path)
    if not os.path.exists(out_path):
        log("ERROR", f"回加密失败: {r.stdout} {r.stderr}")
        sys.exit(1)
    # 校验输出不是明文
    with open(out_path, "rb") as f:
        head = f.read(16)
    if head.strip().startswith(b"{"):
        log("ERROR", f"回加密似乎失败(输出仍为明文): {r.stdout} {r.stderr}")
        sys.exit(1)


def extract_subtitles(doc):
    """
    从明文草稿提取字幕列表。
    返回 [(start_us, dur_us, text, material_id)]，按时间排序。
    """
    text_mats = {m["id"]: m for m in doc.get("materials", {}).get("texts", [])}
    subs = []
    for track in doc.get("tracks", []):
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []):
            mat = text_mats.get(seg.get("material_id"))
            if not mat:
                continue
            try:
                rich = json.loads(mat.get("content", "{}"))
            except json.JSONDecodeError:
                continue
            text = rich.get("text", "")
            tr = seg.get("target_timerange", {})
            subs.append((tr.get("start", 0), tr.get("duration", 0), text, mat["id"]))
    subs.sort(key=lambda x: x[0])
    return subs


def fmt_ts(us):
    """微秒 → SRT 时间戳 00:00:01,500"""
    ms = int(us / 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ts_short(us):
    ms = int(us / 1000)
    m, ms = divmod(ms, 60000)
    s, _ = divmod(ms, 1000)
    return f"{m:02d}:{s:02d}"


def write_srt(path, subs):
    lines = []
    for i, (start, dur, text, _) in enumerate(subs, 1):
        lines.append(f"{i}\n{fmt_ts(start)} --> {fmt_ts(start + dur)}\n{text}\n")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


# ============ 纠错层 ============

# 正则级规则：处理"发音变体×上下文形式"矩阵（枚举映射追不完的一类错拼）。
# 注意：Python 的 \b 在中英文交界处失效（中文也是\w），必须用 (?<![a-zA-Z])/(?![a-zA-Z]) 做边界。
# 顺序重要：先具体（带code/md后缀），后泛化（单独的cloud/clow）。
REGEX_FIXES = [
    # Claude Code 的音变+形式变体：claw扣 / claw code / cloud code / cloak code / clou code...
    (re.compile(r"(?<![a-zA-Z])(cl[ao]{1,2}w?k?|cloud|clou)\s*(扣|code|科德)(?![a-zA-Z])", re.IGNORECASE), "Claude Code"),
    # CLAUDE.md 文件：cloud MD / cloud MB / cloak md / cloud点MD / clow.md ...
    (re.compile(r"(?<![a-zA-Z])(cl[ao]{1,2}w?k?|cloud|clou)\s*[.·点]?\s*(md|mb)(?![a-zA-Z])", re.IGNORECASE), "CLAUDE.md"),
    # 单独的 claude 误识：cloud / clow（教程语境几乎不会说英文"云"，要说云都是"云端"）
    (re.compile(r"(?<![a-zA-Z])(cloud|clow)(?![a-zA-Z])", re.IGNORECASE), "Claude"),
]


def rule_fix(text, terms_cfg):
    """规则层：确定映射替换（不区分大小写、全词感知）"""
    changed = []
    for wrong, right in terms_cfg.get("fix", {}).items():
        pattern = re.compile(re.escape(wrong), re.IGNORECASE)
        if pattern.search(text):
            new = pattern.sub(right, text)
            if new != text:  # 已是正确写法时不记录（忽略大小写的重复匹配）
                text = new
                changed.append(f'"{wrong}"→"{right}"')
    for pattern, target in REGEX_FIXES:
        new = pattern.sub(target, text)
        if new != text:
            text = new
            changed.append(f"正则→{target}")
    return text, changed


def enforce_terms(text, terms_cfg):
    """写回前的最后防线：术语表里的词必须按权威写法（英文词边界防子串误伤）。"""
    for term in terms_cfg.get("terms", []):
        if re.search(r"[a-zA-Z]", term):
            pattern = re.compile(rf"(?<![a-zA-Z]){re.escape(term)}(?![a-zA-Z])", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
        text = pattern.sub(term, text)
    return text


def get_api_key():
    """优先 ZHIPU_API_KEY 环境变量；无效则读 ~/.claude/settings.json（Claude Code 智谱用户免配置）"""
    key = os.environ.get("ZHIPU_API_KEY", "")
    try:
        body = json.dumps({"model": ZHIPU_MODEL,
                           "messages": [{"role": "user", "content": "hi"}],
                           "max_tokens": 1}, ensure_ascii=False).encode()
        req = urllib.request.Request(ZHIPU_URL, data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return key
    except Exception:
        pass
    try:
        import pathlib
        sf = pathlib.Path.home() / ".claude" / "settings.json"
        env_cfg = json.loads(sf.read_text(encoding="utf-8")).get("env", {})
        token = env_cfg.get("ANTHROPIC_AUTH_TOKEN", "")
        base = env_cfg.get("ANTHROPIC_BASE_URL", "")
        if "bigmodel" in base and token:
            return token
    except Exception:
        pass
    return key


def parse_llm_json(content):
    """容错解析模型输出：整体JSON数组 → 逐对象提取"""
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    items = []
    for obj in re.findall(r"\{[^{}]*\}", content, re.DOTALL):
        try:
            items.append(json.loads(obj))
        except json.JSONDecodeError:
            continue
    return items


def llm_fix_batch(batch, terms_cfg, api_key):
    """
    调智谱 glm-4-flash 纠错一批字幕（30条/批，并发调用）。
    batch: [(idx, text)]，返回 {idx: (new_text, reason)}
    """
    if not api_key:
        return {}
    terms_str = " / ".join(terms_cfg.get("terms", []))
    items = [{"id": i, "text": t} for i, t in batch]
    prompt = f"""你是字幕纠错助手。下面是从视频语音识别出的中文字幕（JSON数组）。识别引擎常把英文专有名词和术语听错、写错。

【权威术语表】（涉及这些词必须严格按此写法，含大小写和空格）：
{terms_str}

【纠错原则】
1. 只修复语音识别的错别字和术语拼写错误
2. 不改变口语风格：语气词（呃、嗯、呢、啊、就是、对吧）绝对不要删除或替换，不书面化
3. 不增删内容、不调整语序、不合并拆分句子，不要试图"补全"语义不完整的句子
4. 中文原本正确的字不要动；标点保持原样（没有就不加）
5. 确定是同音错字才改，拿不准就不改（changed=false）。宁可漏改，不可错改

【待纠错字幕】
{json.dumps(items, ensure_ascii=False)}

【输出】严格的JSON数组，每项：
{{"id": 序号, "text": "纠错后文本", "changed": true或false, "reason": "改动原因，中文，简短；没改就留空"}}"""

    body = json.dumps({
        "model": ZHIPU_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 8000,
    }, ensure_ascii=False).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                ZHIPU_URL, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            arr = parse_llm_json(content)
            result = {}
            for item in arr:
                if item.get("changed") and item.get("text"):
                    result[item["id"]] = (item["text"], item.get("reason", ""))
            return result
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                log("WARN", f"本批LLM纠错失败(跳过该批): {e}")
                return {}
    return {}


def main():
    global JY_EXE, TERMS_FILE, ZHIPU_MODEL
    parser = argparse.ArgumentParser(description="剪映字幕AI纠错")
    parser.add_argument("draft", help="剪映草稿名（即草稿列表里显示的名字）")
    parser.add_argument("--dry-run", action="store_true", help="只出对照表，不写回")
    parser.add_argument("--no-llm", action="store_true", help="只跑规则层，不调API")
    parser.add_argument("--batch", type=int, default=30, help="LLM每批条数")
    parser.add_argument("--reuse", action="store_true",
                        help="复用最近一次计算的纠错结果直接写回（秒级，跳过LLM）")
    parser.add_argument("--model", default=ZHIPU_MODEL,
                        help=f"智谱模型名（默认 {ZHIPU_MODEL}）")
    args = parser.parse_args()

    ZHIPU_MODEL = args.model

    t0 = time.time()
    JY_EXE = find_jy_exe()
    draft_root = find_draft_root()
    if not args.dry_run:
        check_jianying_running()
    else:
        log("INFO", "dry-run 只读模式（不写回草稿，剪映开着也没关系）")

    # ---- 定位草稿 ----
    draft_dir = os.path.join(draft_root, args.draft)
    content_file = os.path.join(draft_dir, "draft_content.json")
    if not os.path.exists(content_file):
        cands = [d for d in os.listdir(draft_root)
                 if args.draft in d and os.path.isdir(os.path.join(draft_root, d))]
        if len(cands) == 1:
            draft_dir = os.path.join(draft_root, cands[0])
            content_file = os.path.join(draft_dir, "draft_content.json")
            log("INFO", f"模糊匹配到草稿: {cands[0]}")
        else:
            log("ERROR", f"找不到草稿 '{args.draft}'，可选草稿：")
            for d in sorted(os.listdir(draft_root)):
                if os.path.isdir(os.path.join(draft_root, d)):
                    print("    -", d)
            sys.exit(1)

    out_dir = os.path.join(WORKSPACE, args.draft)
    os.makedirs(out_dir, exist_ok=True)
    backup_dir = os.path.join(out_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    # ---- 备份 ----
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(backup_dir, f"draft_content.{stamp}.json")
    shutil.copy2(content_file, bak)
    log("OK", f"已备份原草稿: {bak}")

    # ---- 解密 ----
    log("STEP", "[1/5] 解密草稿 ...")
    plain_file = os.path.join(out_dir, "draft_plain.json")
    decrypt(content_file, plain_file)
    with open(plain_file, encoding="utf-8") as f:
        doc = json.load(f)
    log("OK", f"解密成功")

    # ---- 提取字幕 ----
    log("STEP", "[2/5] 提取字幕 ...")
    subs = extract_subtitles(doc)
    if not subs:
        log("ERROR", "草稿里没有找到文本/字幕轨道。请确认已在剪映里执行过「智能字幕」识别。")
        sys.exit(1)
    log("OK", f"共 {len(subs)} 条字幕，总时长 {fmt_ts_short(subs[-1][0] + subs[-1][1])}")
    write_srt(os.path.join(out_dir, "original.srt"), subs)

    terms_cfg = load_terms()
    changes = []  # (idx, start_us, old, new, reason)

    # ---- 复用模式：直接加载上次计算结果，跳过规则层与LLM层 ----
    if args.reuse:
        plan_file = os.path.join(out_dir, "plan.json")
        if not os.path.exists(plan_file):
            log("ERROR", f"无可复用结果（{plan_file} 不存在）。请先完整跑一次 dry-run 生成。")
            sys.exit(1)
        plan = json.load(open(plan_file, encoding="utf-8"))
        log("INFO", f"复用 {plan.get('created','?')} 的纠错结果（模型 {plan.get('model','?')}），跳过重算")
        log("WARN", "前提：自那次运行后没有在剪映里改过字幕，否则改动会被覆盖。")
        fixed_map_plan = {item["mid"]: item["text"] for item in plan.get("fixes", [])}
        fixed_texts = [fixed_map_plan.get(s[3], s[2]) for s in subs]
        changes = [(i, subs[i][0], subs[i][2], fixed_texts[i], "复用")
                   for i in range(len(subs))
                   if subs[i][3] in fixed_map_plan and subs[i][2] != fixed_texts[i]]
        log("OK", f"复用模式：待写回改动 {len(changes)} 条")

    # ---- 规则层 ----（复用模式跳过）
    if not args.reuse:
        log("STEP", "[3/5] 规则层替换（术语表映射）...")
        fixed_texts = []
        for i, (start, dur, text, mid) in enumerate(subs):
            new_text, reasons = rule_fix(text, terms_cfg)
            if reasons:
                changes.append((i, start, text, new_text, "规则: " + "、".join(reasons)))
            fixed_texts.append(new_text)
        log("OK", f"规则层修正 {sum(1 for c in changes)} 条")

    # ---- LLM层 ----（复用模式跳过）
    if not args.reuse and not args.no_llm:
        log("STEP", f"[4/5] LLM 纠错（{ZHIPU_MODEL}，每批{args.batch}条，4路并发）...")
        api_key = get_api_key()
        if api_key:
            log("OK", f"API key 就绪: {api_key[:8]}...")
        else:
            log("WARN", "未找到可用智谱 API key，LLM 层将跳过")
        batches, cur = [], []
        for i, t in enumerate(fixed_texts):
            cur.append((i, t))
            if len(cur) >= args.batch:
                batches.append(cur)
                cur = []
        if cur:
            batches.append(cur)
        total_llm_changes = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done_count = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(llm_fix_batch, b, terms_cfg, api_key): b for b in batches}
            for fut in as_completed(futures):
                done_count += 1
                result = fut.result()
                bc = 0
                for idx, (new_text, reason) in result.items():
                    old_text = fixed_texts[idx]
                    new_text = enforce_terms(new_text, terms_cfg)
                    if len(new_text) < len(old_text) * 0.4 or len(new_text) > len(old_text) * 2.5:
                        continue  # 长度突变视为幻觉，丢弃
                    if new_text != old_text:
                        start = subs[idx][0]
                        changes.append((idx, start, old_text, new_text, f"LLM: {reason}"))
                        fixed_texts[idx] = new_text
                        bc += 1
                total_llm_changes += bc
                print(f"    进度 {done_count}/{len(batches)} 批完成，本批改 {bc} 条", flush=True)
        log("OK", f"LLM 层修正 {total_llm_changes} 条")
    elif not args.reuse:
        log("INFO", "跳过 LLM 层（--no-llm）")

    # ---- 保存纠错plan（供 --reuse 秒级复用；复用模式不覆盖） ----
    if not args.reuse:
        plan = {"draft": args.draft,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": ZHIPU_MODEL,
                "fixes": [{"mid": subs[i][3], "text": fixed_texts[i]}
                          for i in range(len(subs)) if fixed_texts[i] != subs[i][2]]}
        with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)

    # ---- 对照表 ----
    log("STEP", "[5/5] 生成对照表与产物 ...")
    changes.sort(key=lambda c: c[0])
    md = [f"# 字幕纠错对照表 — {args.draft}",
          f"\n- 时间：{time.strftime('%Y-%m-%d %H:%M')}",
          f"- 字幕总数：{len(subs)}",
          f"- 修改条数：{len(changes)}",
          f"- 模式：{'dry-run（未写回）' if args.dry_run else '已写回草稿'}",
          "\n| # | 时间 | 原文 | 改后 | 原因 |", "|---|---|---|---|---|"]
    for idx, start, old, new, reason in changes:
        md.append(f"| {idx+1} | {fmt_ts_short(start)} | {old} | **{new}** | {reason} |")
    md_path = os.path.join(out_dir, "changes.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    final_subs = [(subs[i][0], subs[i][1], fixed_texts[i], subs[i][3]) for i in range(len(subs))]
    write_srt(os.path.join(out_dir, "fixed.srt"), final_subs)
    log("OK", f"产物: {md_path}")
    log("OK", f"      {os.path.join(out_dir, 'original.srt')} / fixed.srt")

    if args.dry_run:
        log("INFO", "dry-run 模式，草稿未修改。检查 changes.md 无误后去掉 --dry-run 重跑。")
        log("INFO", f"耗时 {time.time()-t0:.0f}s")
        return

    if not changes:
        log("INFO", "没有需要修改的字幕，草稿保持原样。")
        return

    # ---- 写回 ----
    log("STEP", "写回草稿 ...")
    fixed_map = {subs[i][3]: fixed_texts[i] for i in range(len(subs))}
    for mat in doc["materials"]["texts"]:
        if mat["id"] in fixed_map:
            rich = json.loads(mat["content"])
            new_text = fixed_map[mat["id"]]
            if rich.get("text") != new_text:
                rich["text"] = new_text
                mat["content"] = json.dumps(rich, ensure_ascii=False, separators=(",", ":"))

    with open(plain_file, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    encrypted_tmp = os.path.join(out_dir, "draft_encrypted.json")
    encrypt(plain_file, encrypted_tmp)
    shutil.copy2(encrypted_tmp, content_file)
    log("OK", f"已写回草稿并回加密（原文件在 {backup_dir}）")
    print()
    log("OK", "全部完成！现在可以打开剪映：字幕已纠错，可直接开始剪辑。")
    log("INFO", "如发现个别改错了：对照表里有原句，可在剪映里直接手动改那一两处；")
    log("INFO", f"如要整体回滚：把 {backup_dir} 里最新备份复制回草稿目录覆盖 draft_content.json 即可。")
    log("INFO", f"总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
