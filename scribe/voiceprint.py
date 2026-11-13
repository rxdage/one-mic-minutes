"""声纹库：只依赖 numpy。

库文件 voiceprints.npz：
  names   (P,)      每个原型属于谁
  protos  (P,192)   原型向量（已归一化）
  nwin    (P,)      该原型由多少个 0.75 s 窗平均而来
  source  (P,)      来自哪场会、哪一类（如 "2030-01-01周会:S0"）
  confirmed (P,)    名字是否经用户确认
一人可以有多个原型（同一人在不同会议、或长段发言/短句接话被拆成两类）。

python scribe/voiceprint.py build  emb.npz labels.npy mapping.json SOURCE out.npz [--lib old.npz]
python scribe/voiceprint.py show   lib.npz
mapping.json: {"S0": {"name": "老周", "confirmed": true}, "S2": null, ...}
"""
import json, sys
import numpy as np

MAX_PROTOS_PER_PERSON = 6


def _norm(v):
    v = np.asarray(v, dtype=np.float64)
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-12)


def cluster_centroids(E, lab, keep=0.8):
    """每类的稳健中心：先求均值，丢掉离中心最远的 (1-keep) 窗口，再求一次。"""
    E = _norm(E); out = {}
    for k in np.unique(lab):
        X = E[lab == k]
        c = _norm(X.mean(0))
        if len(X) >= 10:
            s = X @ c
            X = X[s >= np.quantile(s, 1 - keep)]
            c = _norm(X.mean(0))
        out[int(k)] = (c, len(X))
    return out


def load(path):
    d = np.load(path, allow_pickle=False)
    return {k: d[k] for k in ("names", "protos", "nwin", "source", "confirmed")}


def empty():
    return {"names": np.array([], dtype="U64"), "protos": np.zeros((0, 192)), "nwin": np.zeros(0, int),
            "source": np.array([], dtype="U64"), "confirmed": np.zeros(0, bool)}


def save(lib, path):
    np.savez(path, **lib)


def add(lib, name, proto, nwin, source, confirmed):
    """加一个原型；同一人原型超过上限时，把最相近的两个按窗数加权合并。"""
    lib = {k: v.copy() for k, v in lib.items()}
    lib["names"] = np.append(lib["names"].astype("U64"), name)
    lib["protos"] = np.vstack([lib["protos"], _norm(proto)[None]])
    lib["nwin"] = np.append(lib["nwin"], nwin)
    lib["source"] = np.append(lib["source"].astype("U64"), source)
    lib["confirmed"] = np.append(lib["confirmed"], confirmed)
    idx = np.where(lib["names"] == name)[0]
    while len(idx) > MAX_PROTOS_PER_PERSON:
        P = lib["protos"][idx]; S = P @ P.T; np.fill_diagonal(S, -1)
        i, j = np.unravel_index(np.argmax(S), S.shape); a, b = idx[i], idx[j]
        w = lib["nwin"][a] + lib["nwin"][b]
        lib["protos"][a] = _norm(lib["protos"][a] * lib["nwin"][a] + lib["protos"][b] * lib["nwin"][b])
        lib["nwin"][a] = w; lib["source"][a] = f"{lib['source'][a]}+{lib['source'][b]}"[:64]
        lib["confirmed"][a] = lib["confirmed"][a] and lib["confirmed"][b]
        keep = np.ones(len(lib["names"]), bool); keep[b] = False
        lib = {k: v[keep] for k, v in lib.items()}
        idx = np.where(lib["names"] == name)[0]
    return lib


def match(centroids, lib, accept=None, margin=None):
    """centroids: {类号: (向量, 窗数)}。返回 {类号: [(名字, 分数), ...前3]}，分数=该人原型里的最大余弦。"""
    if not len(lib["names"]):
        return {k: [] for k in centroids}
    people = sorted(set(lib["names"].tolist()))
    res = {}
    for k, (c, _) in centroids.items():
        s = lib["protos"] @ _norm(c)
        best = sorted(((p, float(s[lib["names"] == p].max())) for p in people), key=lambda t: -t[1])
        res[k] = best[:3]
    return res


def build(emb, labels, mapping, source, out, lib_path=None):
    d = np.load(emb); E = d["emb"]; lab = np.load(labels)
    m = json.load(open(mapping, encoding="utf-8"))
    lib = load(lib_path) if lib_path else empty()
    cents = cluster_centroids(E, lab)
    for key, info in m.items():
        if not info or not (key.startswith("S") and key[1:].isdigit()): continue   # 跳过 null 和说明字段
        k = int(key[1:])
        if k not in cents: continue
        c, n = cents[k]
        lib = add(lib, info["name"], c, n, f"{source}:{key}", bool(info.get("confirmed", True)))
    save(lib, out); show(out)


def show(path):
    lib = load(path)
    print(f"{len(lib['names'])} 个原型, {len(set(lib['names']))} 人")
    for i, n in enumerate(lib["names"]):
        print(f"  {n:6s} {lib['nwin'][i]*0.75/60:5.1f} min  {'已确认' if lib['confirmed'][i] else '未确认'}  {lib['source'][i]}")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        a = sys.argv[2:]
        lib_path = a[a.index("--lib") + 1] if "--lib" in a else None
        build(a[0], a[1], a[2], a[3], a[4], lib_path)
    elif sys.argv[1] == "show":
        show(sys.argv[2])
