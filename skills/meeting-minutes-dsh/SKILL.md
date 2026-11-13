---
name: meeting-minutes
description: 把一台收音设备录下的线下闭门会，整理成按人归属的逐字稿和会议纪要（基于 one-mic-minutes）。用户给出会议录音、说“整理会议纪要”“区分每个人说了什么”时使用。先拿参会名单，跑完先给用户核对说话人，确认后才写纪要，最后把确认结果回写声纹库。
license: MIT
compatibility: Linux（DSH 服务器；bash + Python 3.11 + ffmpeg）；macOS 可用；Windows 建议 WSL / Git Bash。agent 读转写/写纪要时，模型必须在本机或用户自己的服务器；云端模型只能用 UI 核对、由人写纪要。
---

# 闭门会纪要（DSH 版）

## 规矩

- 录音、转写稿、声纹库**只在本机或用户自己的服务器上处理**，不发给任何外部服务，也不粘贴到外部网页。
- **模型边界：**只有 DSH 模型在本机或用户自己的服务器时，才让 agent 读 `named_k*.txt` / `review_k*.md` 或写纪要；模型在云端时，转写稿会进模型上下文，这时只做 UI 核对，由人写纪要。
- 声纹属于生物识别信息：声纹库只存本机或用户自己的服务器，不提交 git；建库前先征得参会人同意。
- 输出目录（`review.json`、`说话人对名.json`）可能含会议内容和名字，也不要提交 git。
- **先核对，后写纪要。** 说话人名字没经用户确认之前，不要输出正式纪要。

## 0. 先确认模型边界

问用户，或看 DSH 的模型配置：**模型在本机/用户自己的服务器，还是在云端？**

- 在本机/自有服务器：继续。
- 在云端，或不确定：**不要执行第 3 步读稿和第 5 步写纪要**。只把核对表交给用户，让用户在 UI 里核对、由人写纪要；不要自己读 `named_k*.txt` / `review_k*.md`。

## 0.1 先检查环境

```bash
export SCRIBE_HOME="${SCRIBE_HOME:-$HOME/one-mic-minutes}"
export SCRIBE_PY="${SCRIBE_PY:-$SCRIBE_HOME/.venv/bin/python}"
export SKILL_DIR="${SKILL_DIR:-$HOME/.dsh/skills/meeting-minutes}"
"$SCRIBE_PY" "$SKILL_DIR/check_env.py"
```

`check_env.py` 只检查、不下载、不安装。缺 `torch` / `funasr` / `scikit-learn` / `soundfile` / `ffmpeg` 就停下来，按它打印的命令装好再继续。不要跳过这一步假装能跑。

装环境用 `uv`（不依赖 conda），在 DSH 所在的服务器上：

```bash
cd "$SCRIBE_HOME"
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python torch torchaudio --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv/bin/python -r requirements.txt
```

`ffmpeg` 单独装：Debian/Ubuntu `sudo apt install ffmpeg`，或 `conda install -c conda-forge ffmpeg`。第一次跑 pipeline 会从 ModelScope 下载约 2 GB 模型；这是唯一的联网动作。

## 1. 要名单

问用户这场会有谁发言。最好让他用仓库里的 `web/seating.html` 摆好座位、导出 JSON：

- 直接用浏览器打开 `web/seating.html`（离线单页，不联网），拖人、拖录音笔，点「下载 JSON」。
- 只有名单也可以：每行一个名字的 txt，或 `{"people": ["名字", ...]}`。
- 名单里的人数会作为说话人类数；名字自动进热词；声纹只在名单内比对。

没有名单也能跑，但人数只能估计，核对时要说明这一点。

## 2. 跑脚本

在 DSH 所在的服务器上：

```bash
"$SCRIBE_PY" "$SCRIBE_HOME/scribe/pipeline.py" \
  "录音.m4a" "会议A/out" \
  --attendees "座位.json" \
  --voiceprints "声纹库.npz" \
  --hotwords "热词.txt"
```

不需要的选项可以省略。输出目录里会出现 `named_k*.txt`、`review_k*.md`、`speakers_k*.md`、`summary.json`、`emb.npz`、`labels_k*.npy`。

远程模式：如果 DSH 在 A 机、转写想放到 B 机（只限用户自己的服务器）。成功时删远端目录；失败时只保证删录音，名单、已生成的转写/emb 和带上去的声纹库可能留在运行目录，排查后请手动删；SSH 中途断开时清理可能不执行。

```bash
SCRIBE_HOST=myserver \
SCRIBE_PY='~/miniforge3/envs/scribe/bin/python' \
bash "$SCRIBE_HOME/scribe/run_remote.sh" "录音.m4a" "会议A" "座位.json"
```

## 3. 读稿

通读 `named_k*.txt` 和 `review_k*.md`。

- **✓** 是声纹自动填的名字：分数 ≥ 0.80 且领先第二名 ≥ 0.05。
- **?** 要靠上下文判断：谁点了谁的名、谁在做哪部分汇报、谁在接谁的话。
- 同一个人常被拆成“长段发言”和“短句接话”两类；声纹对上同一个名字时，两类会自动合并。
- 零碎的“对/行/嗯”会单独聚成一类，归为“多人短句”，不要硬起名字。

## 4. 发核对表给用户

每一类写清：建议的名字、依据（声纹分数、上下文证据）、把握高低。另外列出：

- 没对上名字的类；
- 名单里没找到声音的人；
- 座位距离和音量只作旁证，不要当成硬证据。

然后**等用户改正**。不要在这一步之后直接写正式纪要。

## 5. 写纪要

用户确认后，按议题写纪要和待办。规则：

- 人名用用户确认的写法；拿不准的名字和数字标“（音）”，集中列进“待确认”。
- 不要把“? 类”写成确定的人；宁可写“一位同事”。
- 纪要只写会上说过的内容，不补背景、不编结论。

## 6. 回写声纹库

把确认结果写成 `说话人对名.json`（格式见 `examples/mapping.example.json`）：

```json
{
  "S0": { "name": "老周", "confirmed": true },
  "S1": { "name": "老周", "confirmed": true },
  "S2": null,
  "S3": { "name": "Amy", "confirmed": true }
}
```

`null` 表示零碎杂类，不入库；`confirmed: false` 表示名字是推断的。然后：

```bash
"$SCRIBE_PY" "$SCRIBE_HOME/scribe/voiceprint.py" build \
  "会议A/out/emb.npz" "会议A/out/labels_k5.npy" "说话人对名.json" \
  "会议A" "声纹库.npz" --lib "声纹库.npz"
```

`K` 要换成这次实际用的类数（看 `summary.json` 的 `primary`）。

## 7. 改了名单或声纹库，不必重跑识别

```bash
"$SCRIBE_PY" "$SCRIBE_HOME/scribe/pipeline.py" - "会议A/out" \
  --reuse --attendees "座位.json" --voiceprints "声纹库.npz"
```

## 经验

- 收音器放桌子中间。说话声音小的人，让他坐得离收音器近一点。
- 音量和座位距离只能当旁证：房间混响和每个人嗓门的大小，都会让距离和音量对不上。
- 很多设备的“双声道”其实是同一路声音复制了一份，脚本会自动识别，按单声道处理。
- 跨会议还没验证过。换房间、座位或设备后分数会下降；0.80 这条线只是第一版，用几次之后按实际分数再调。
- 声纹库不要发给别人，也不要上传；建库前先征得参会人同意。
