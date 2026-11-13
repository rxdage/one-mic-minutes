"""一台收音设备的闭门会录音 -> 带时间戳的中文转写 + 说话人分类 + 自动对名。全部在本机运行。

python scribe/pipeline.py 录音.m4a 输出目录 [--attendees 座位.json] [--voiceprints 声纹库.npz] [--hotwords 热词.txt] [--k K]
python scribe/pipeline.py - 输出目录 --reuse [--attendees ...] [--voiceprints ...] [--k K]   # 只重做分人/对名

场景：线下闭门会、只有一台收音设备、会前能给出参会名单（可选座位）。名单人数 = 默认类数，
名单里的名字自动当热词，声纹只在名单内比对；输出 review_k*.md（对名核对表）和 named_k*.txt。

步骤：ffmpeg 转 16 kHz 单声道 -> SeACo-Paraformer + VAD + 标点（句级时间戳）
     -> CAM++ 1.5 s/0.75 s 滑窗声纹 -> 谱聚类（K = 名单人数；没名单时 eigengap 给两个候选及 +1 都出，
        靠读稿挑：K 偏大时同一人会被切成两类交替出现，这是最明显的过分迹象）
     -> 前后 ±3 窗多数投票平滑、<2 s 的切换不算 -> 声纹库比对 -> 分人稿 + 核对表
产物：sentences.json emb.npz labels_k*.npy spk_k*.txt named_k*.txt review_k*.md speakers_k*.md summary.json
      （中间生成的音频在结束时删掉）
"""
import argparse, json, os, re, shutil, subprocess, sys, time
import numpy as np

WIN, HOP, SR = 1.5, 0.75, 16000
SM, MINRUN = 3, 2000
DEVICE = "cpu"
UNIT = re.compile(r"[一-鿿]|[A-Za-z0-9']+")


def ts(ms):
    s = int(ms // 1000); return f"{s//3600:d}:{s%3600//60:02d}:{s%60:02d}"


# ---------- 1. ASR ----------
def asr(wav, hotwords):
    from funasr import AutoModel
    m = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc",
                  device=DEVICE, disable_update=True)
    r = m.generate(input=wav, batch_size_s=300, hotword=hotwords, sentence_timestamp=True)[0]
    sents = r.get("sentence_info") or []
    return [{"text": s["text"], "start": s["start"], "end": s["end"],
             "timestamp": s.get("timestamp", [[s["start"], s["end"]]])} for s in sents]


# ---------- 2. 滑窗声纹 ----------
def windows(sents, gap=0.3):
    segs = sorted((s["start"] / 1000, s["end"] / 1000) for s in sents)
    regs = [list(segs[0])]
    for a, b in segs[1:]:
        if a - regs[-1][1] < gap: regs[-1][1] = max(regs[-1][1], b)
        else: regs.append([a, b])
    w = []
    for a, b in regs:
        if b - a <= WIN: w.append((a, b)); continue
        t = a
        while t + WIN < b: w.append((t, t + WIN)); t += HOP
        w.append((b - WIN, b))
    return [x for x in w if x[1] - x[0] >= 0.4]


def embed(x, sents, xs=None):
    """x: 单声道；xs: 原始立体声 (n,2) 或 None。返回 嵌入、窗、每窗音量 dB、每窗左右差 dB。"""
    import torch
    from funasr import AutoModel
    from funasr.models.campplus.utils import extract_feature
    W = windows(sents)
    net = AutoModel(model="cam++", device="cpu", disable_update=True).model.eval()   # 声纹在 CPU 上就够快
    E = []
    with torch.no_grad():
        for i in range(0, len(W), 256):
            chunk = [torch.from_numpy(np.ascontiguousarray(x[int(a * SR):int(b * SR)])) for a, b in W[i:i + 256]]
            feats, _, _ = extract_feature(chunk)
            E.append(net.forward(feats.to(torch.float32)).numpy())
    db = lambda v: 20 * np.log10(np.sqrt(np.mean(v.astype(np.float64) ** 2)) + 1e-9)
    rms = np.array([db(x[int(a * SR):int(b * SR)]) for a, b in W])
    ild = (np.array([db(xs[int(a * SR):int(b * SR), 0]) - db(xs[int(a * SR):int(b * SR), 1]) for a, b in W])
           if xs is not None else np.full(len(W), np.nan))
    return np.concatenate(E), np.array(W), rms, ild


# ---------- 3. 聚类 ----------
def affinity(E):
    from sklearn.preprocessing import normalize
    E = normalize(E); A = np.clip(E @ E.T, 0, 1)
    keep = max(int(len(A) * 0.02), 6)
    idx = np.argsort(A, axis=1)[:, :-keep]
    np.put_along_axis(A, idx, 0, axis=1)
    return 0.5 * (A + A.T)


def eigengap_candidates(A, kmax=15):
    kmax = max(2, min(kmax, len(A) - 2))          # 很短的录音窗口少
    if len(A) < 6:
        return [1, 1]
    L = np.diag(A.sum(1)) - A
    ev = np.linalg.eigvalsh(L)[:kmax + 1]
    gaps = np.diff(ev)                      # gaps[i] 大 => i+1 类
    order = sorted(range(1, kmax), key=lambda i: -gaps[i])
    return [i + 1 for i in order[:2]]


def labels_for(A, cen, K):
    from sklearn.cluster import SpectralClustering
    lab = SpectralClustering(n_clusters=K, affinity="precomputed", random_state=0,
                             assign_labels="cluster_qr").fit_predict(A)
    order = {}
    for l in lab[np.argsort(cen)]: order.setdefault(l, len(order))
    lab = np.array([order[l] for l in lab])
    o = np.argsort(cen); out = lab.copy()
    for j in range(len(o)):
        nb = [o[m] for m in range(max(0, j - SM), min(len(o), j + SM + 1))
              if abs(cen[o[m]] - cen[o[j]]) <= SM * HOP + 0.01]
        out[o[j]] = np.bincount(lab[nb]).argmax()
    return out


def split_sentence(s, lab_of):
    tst, text = s["timestamp"], s["text"]
    units = list(UNIT.finditer(text))
    labs = [lab_of((a + b) / 2000) for a, b in tst]
    if len(units) != len(tst):
        return [(max(set(labs), key=labs.count), s["start"], text)]
    runs = []
    for i, l in enumerate(labs):
        if runs and runs[-1][0] == l: runs[-1][2] = i
        else: runs.append([l, i, i])
    while len(runs) > 1:
        short = [j for j, (l, i0, i1) in enumerate(runs) if tst[i1][1] - tst[i0][0] < MINRUN]
        if not short: break
        j = short[0]; k = j - 1 if j > 0 else j + 1
        runs[k][1] = min(runs[k][1], runs[j][1]); runs[k][2] = max(runs[k][2], runs[j][2])
        runs.pop(j)
        m = [runs[0]]
        for r in runs[1:]:
            if r[0] == m[-1][0]: m[-1][2] = r[2]
            else: m.append(r)
        runs = m
    out = []
    for l, i0, i1 in runs:
        c0 = units[i0].start() if i0 > 0 else 0
        c1 = units[i1 + 1].start() if i1 + 1 < len(units) else len(text)
        out.append((l, tst[i0][0], text[c0:c1]))
    return out


def turns(sents, lab, cen):
    pieces = []
    lab_of = lambda t: int(lab[np.argmin(np.abs(cen - t))])
    for s in sents: pieces += split_sentence(s, lab_of)
    T, cur = [], None
    for l, t0, tx in pieces:
        if l != cur: T.append([l, t0, tx]); cur = l
        else: T[-1][2] += tx
    return T


VP_ACCEPT, VP_MARGIN = 0.80, 0.05   # 一场 12 人会议的半场交叉验证：本人 0.89–0.98，他人最高 0.82


def vp_cell(r):
    if not r: return "—"
    (n1, s1), rest = r[0], r[1:]
    s2 = rest[0][1] if rest else 0
    ok = s1 >= VP_ACCEPT and s1 - s2 >= VP_MARGIN
    tail = f"（次 {rest[0][0]} {s2:.2f}）" if rest else ""
    return f"{'✓' if ok else '?'} {n1} {s1:.2f}{tail}"


def speakers_md(T, K, lab, rms=None, ild=None, vp=None):
    """音量：该类窗口音量中位数，相对最响的类（0 dB = 离收音器最近的人，越负越远）。
    左右差：L−R 中位数，仅原始录音是立体声时有，正 = 偏左声道。
    声纹：与声纹库比对的最佳候选；✓ = 分数 ≥ 0.80 且领先第二名 ≥ 0.05，? = 待定。"""
    lines = [f"# 说话人概览（K={K}）\n",
             "| 类 | 发言时长 | 段数 | 首次出现 | 音量 | 左右差 | 声纹匹配 |", "|---|---|---|---|---|---|---|"]
    mins = np.bincount(lab, minlength=K) * HOP / 60
    med = lambda v, k: float(np.median(v[lab == k])) if v is not None and np.any(lab == k) else np.nan
    loud = [med(rms, k) for k in range(K)]
    top = np.nanmax(loud) if np.any(np.isfinite(loud)) else 0
    for k in range(K):
        mine = [t for t in T if t[0] == k]
        if not mine: continue
        lv = f"{loud[k]-top:+.1f} dB" if np.isfinite(loud[k]) else "—"
        il = med(ild, k); il = f"{il:+.1f} dB" if np.isfinite(il) else "—"
        lines.append(f"| S{k} | {mins[k]:.1f} min | {len(mine)} | {ts(mine[0][1])} | {lv} | {il} | {vp_cell((vp or {}).get(k))} |")
    for k in range(K):
        mine = sorted((t for t in T if t[0] == k), key=lambda t: -len(t[2]))[:3]
        if not mine: continue
        lines.append(f"\n## S{k}\n")
        for _, t0, tx in sorted(mine, key=lambda t: t[1]):
            lines.append(f"- [{ts(t0)}] {tx[:220]}{'…' if len(tx) > 220 else ''}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="录音文件；用 --reuse 时写 - ")
    ap.add_argument("outdir")
    ap.add_argument("--hotwords"); ap.add_argument("--k", type=int, help="说话人类数；默认 = 名单人数")
    ap.add_argument("--attendees", help="参会名单：座位布置页导出的 JSON，或每行一个名字的 txt")
    ap.add_argument("--voiceprints", help="声纹库 npz（scribe/voiceprint.py 生成）")
    ap.add_argument("--reuse", action="store_true", help="不重跑识别：读 outdir 里已有的 sentences.json / emb.npz，只重做分人和对名")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--device", default="cpu", help="语音识别用的设备，如 cuda:0；默认 cpu")
    a = ap.parse_args()
    global DEVICE; DEVICE = a.device
    os.makedirs(a.outdir, exist_ok=True)
    t0 = time.time(); log = lambda m: print(f"[{time.time()-t0:6.0f}s] {m}", flush=True)
    people = load_attendees(a.attendees)
    if people: log(f"attendees: {len(people)}")

    if a.reuse:
        sents = json.load(open(os.path.join(a.outdir, "sentences.json"), encoding="utf-8"))
        d = np.load(os.path.join(a.outdir, "emb.npz"))
        E, W = d["emb"], d["win"]
        rms = d["rms"] if "rms" in d.files else None
        ild = d["ild"] if "ild" in d.files else None
        dur = float(W[:, 1].max())
        log(f"reuse: {len(sents)} sentences, {len(W)} windows")
        return postprocess(a, sents, E, W, rms, ild, dur, people, log)

    os.environ.setdefault("OMP_NUM_THREADS", str(a.threads))
    import torch; torch.set_num_threads(a.threads)
    import soundfile as sf
    wav = os.path.join(a.outdir, "audio16k.wav"); raw = os.path.join(a.outdir, "audio_raw.wav")
    ff = (shutil.which("ffmpeg") or next((p for p in (os.path.join(sys.prefix, "bin", "ffmpeg"),
          os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe")) if os.path.exists(p)), None))
    if not ff: sys.exit("找不到 ffmpeg：conda install -c conda-forge ffmpeg，或装好后放进 PATH")
    try:
        subprocess.run([ff, "-v", "error", "-y", "-i", a.audio, "-ar", str(SR), raw], check=True)
        xr, _ = sf.read(raw, dtype="float32")
        xs = xr[:, :2] if xr.ndim == 2 and xr.shape[1] >= 2 else None
        if xs is not None and np.allclose(xs[:, 0], xs[:, 1], atol=1e-4):
            xs = None                                   # 假立体声（两声道一样）
        x = xr.mean(1) if xr.ndim == 2 else xr
        sf.write(wav, x, SR)
        dur = len(x) / SR
        log(f"audio {dur/60:.1f} min, {'真立体声' if xs is not None else '单声道'}")
        hw = ""
        if a.hotwords and os.path.exists(a.hotwords):
            hw = " ".join(w.strip() for w in open(a.hotwords, encoding="utf-8") if w.strip() and not w.startswith("#"))
        hw = " ".join(dict.fromkeys(hw.split() + [p["name"] for p in people]))   # 名单里的名字自动当热词
        sents = asr(wav, hw)
        json.dump(sents, open(os.path.join(a.outdir, "sentences.json"), "w", encoding="utf-8"), ensure_ascii=False)
        log(f"ASR done: {len(sents)} sentences")
        E, W, rms, ild = embed(x, sents, xs)
        np.savez(os.path.join(a.outdir, "emb.npz"), emb=E, win=W, rms=rms, ild=ild)
        log(f"embeddings: {len(W)} windows")
    finally:
        for f in (wav, raw):
            if os.path.exists(f): os.remove(f)
    postprocess(a, sents, E, W, rms, ild, dur, people, log)


# ---------- 参会名单 / 座位 ----------
def load_attendees(path):
    """接受：座位布置页导出的 JSON；{"people": ["名字", ...]}；或每行一个名字的 txt。
    返回 [{"name", "dist"(到收音器的米数或 None)}]。"""
    if not path or not os.path.exists(path):
        return []
    if path.endswith(".json"):
        d = json.load(open(path, encoding="utf-8"))
        out = []
        for p in d.get("people", []):
            if isinstance(p, str): out.append({"name": p, "dist": None})
            elif p.get("name"): out.append({"name": p["name"], "dist": p.get("dist_to_recorder_m")})
        return out
    return [{"name": l.strip(), "dist": None} for l in open(path, encoding="utf-8")
            if l.strip() and not l.startswith("#")]


def name_clusters(vp, people):
    """声纹匹配 → 每类的名字。只在名单内比对（vp 已按名单过滤）；✓ 才认名，同名的类视为同一人。"""
    names = {}
    for k, r in (vp or {}).items():
        if r and vp_cell(r).startswith("✓"):
            names[k] = r[0][0]
    return names


def review_md(T, K, lab, vp, names, people, rms):
    """给人核对用的对名表。"""
    mins = np.bincount(lab, minlength=K) * HOP / 60
    loud = {k: float(np.median(rms[lab == k])) for k in range(K) if rms is not None and np.any(lab == k)}
    top = max(loud.values()) if loud else 0
    dist = {p["name"]: p["dist"] for p in people}
    L = [f"# 对名核对表（K={K}）\n",
         "> ✓ = 声纹分数 ≥ 0.80 且领先第二名 ≥ 0.05，已自动填名；? = 待定，请按代表发言判断。",
         "> 「音量」相对最响的一类；离收音器近的人通常更响，但房间混响和个人嗓门会让它不准，只作旁证。\n",
         "| 类 | 自动对名 | 声纹候选 | 座位距收音器 | 音量 | 时长 | 代表发言 |", "|---|---|---|---|---|---|---|"]
    for k in range(K):
        mine = [t for t in T if t[0] == k]
        if not mine: continue
        nm = names.get(k, "")
        d = dist.get(nm)
        lv = f"{loud[k]-top:+.1f} dB" if k in loud and np.isfinite(loud[k]) else "—"
        ex = max(mine, key=lambda t: len(t[2]))
        L.append(f"| S{k} | {nm or '?'} | {vp_cell((vp or {}).get(k))} | {f'{d:.1f} m' if d else '—'} | {lv} "
                 f"| {mins[k]:.1f} min | [{ts(ex[1])}] {ex[2][:60]}… |")
    found = set(names.values())
    miss = [p["name"] for p in people if p["name"] not in found]
    if people:
        L.append(f"\n**名单里还没对上声音的人**：{'、'.join(miss) if miss else '无'}")
    # 音量与距离是否一致（只看已对名的类）
    pairs = [(dist[n], loud[k]) for k, n in names.items() if dist.get(n) and k in loud]
    if len(pairs) >= 4:
        a_, b_ = np.array(pairs).T
        rho = np.corrcoef(np.argsort(np.argsort(a_)), np.argsort(np.argsort(b_)))[0, 1]
        L.append(f"\n音量与座位距离的秩相关 ρ = {rho:+.2f}（越接近 −1 越说明对名和座位吻合；接近 0 说明音量帮不上忙）")
    return "\n".join(L) + "\n"


def postprocess(a, sents, E, W, rms, ild, dur, people, log):
    cen = W.mean(1); A = affinity(E)
    cands = eigengap_candidates(A)
    K0 = a.k or (len(people) if people else None)
    ks = [K0] if K0 else sorted(set(cands + [max(cands) + 1]))
    primary = K0 or max(cands)
    summary = {"minutes": round(dur / 60, 1), "sentences": len(sents), "eigengap": cands,
               "ks": ks, "primary": primary, "attendees": len(people)}
    lib = None
    if a.voiceprints and os.path.exists(a.voiceprints):
        import voiceprint as vl
        lib = vl.load(a.voiceprints)
        if people:                                   # 只在到场的人里比对
            keep = np.isin(lib["names"], [p["name"] for p in people])
            lib = {k: v[keep] for k, v in lib.items()}
        log(f"voiceprints: {len(set(lib['names']))} people in scope")
    for K in ks:
        lab = labels_for(A, cen, K)
        np.save(os.path.join(a.outdir, f"labels_k{K}.npy"), lab)
        T = turns(sents, lab, cen)
        vp = {}
        if lib is not None and len(lib["names"]):
            vp = vl.match(vl.cluster_centroids(E, lab), lib)
        json.dump({f"S{k}": v for k, v in (vp or {}).items()},
                  open(os.path.join(a.outdir, f"vp_match_k{K}.json"), "w", encoding="utf-8"), ensure_ascii=False)
        names = name_clusters(vp, people)
        open(os.path.join(a.outdir, f"spk_k{K}.txt"), "w", encoding="utf-8").write(
            "\n".join(f"[{ts(t0)}] S{l}: {tx}" for l, t0, tx in T))
        # 带名字的稿：已认名的用名字，同名相邻段合并
        out, prev = [], None
        for l, t0, tx in T:
            who = names.get(l, f"S{l}")
            if out and who == prev: out[-1] += tx
            else: out.append(f"[{ts(t0)}] {who}: {tx}"); prev = who
        open(os.path.join(a.outdir, f"named_k{K}.txt"), "w", encoding="utf-8").write("\n".join(out))
        open(os.path.join(a.outdir, f"speakers_k{K}.md"), "w", encoding="utf-8").write(speakers_md(T, K, lab, rms, ild, vp))
        open(os.path.join(a.outdir, f"review_k{K}.md"), "w", encoding="utf-8").write(review_md(T, K, lab, vp, names, people, rms))
        log(f"K={K}: {len(T)} turns, auto-named {len(names)}/{K} clusters")
    json.dump(summary, open(os.path.join(a.outdir, "summary.json"), "w"), indent=1)
    log(f"done {summary}")


if __name__ == "__main__":
    main()
