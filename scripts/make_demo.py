"""用模型自带的示例音频拼一段「多人轮流说话」的演示录音，不需要你自己的录音就能把流程跑通。

python scripts/make_demo.py demo/demo.wav

会顺带下载（或复用缓存里的）CAM++ / VAD / SeACo-Paraformer 模型；示例音频来自这些模型仓库自带的 example 文件。
拼出来的录音大约 1.6 分钟，里面有 5 个不同的声音，其中 2 个人各说了两段：
  演示甲（两段）、演示乙（两段）、演示丙（一段长讲话）、演示丁（主持人插话）、演示戊（一句话）。
所以跑的时候用 K=5，或者传 examples/demo_attendees.txt。
"""
import os, sys
import numpy as np
import soundfile as sf
from modelscope.hub.snapshot_download import snapshot_download

SR = 16000


def load(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim == 2: x = x.mean(1)
    assert sr == SR, f"{path}: {sr} Hz"
    return x


def main(out):
    spk = snapshot_download("iic/speech_campplus_sv_zh-cn_16k-common")
    vad = snapshot_download("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
    asr = snapshot_download("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
    parts = [os.path.join(spk, "examples", "speaker1_a_cn_16k.wav"),
             os.path.join(spk, "examples", "speaker2_a_cn_16k.wav"),
             os.path.join(vad, "example", "vad_example.wav"),
             os.path.join(spk, "examples", "speaker1_b_cn_16k.wav"),
             os.path.join(asr, "example", "asr_example.wav"),
             os.path.join(spk, "examples", "speaker2_a_cn_16k.wav")]
    gap = np.zeros(int(0.6 * SR), dtype=np.float32)
    x = np.concatenate([np.concatenate([load(p), gap]) for p in parts])
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    sf.write(out, x, SR)
    print(f"写好了 {out}：{len(x)/SR/60:.1f} 分钟")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo/demo.wav")
