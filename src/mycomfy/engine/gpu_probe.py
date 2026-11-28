"""由引擎版本自己的 Python 执行 CUDA 和可选 SageAttention 算子验收。"""

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path


def verify_gpu(verify_sageattention):
    """读取当前可见 GPU 的 Torch 环境，按需运行与 H3 节点一致的无掩码注意力。"""
    import torch

    gpu = os.environ["CUDA_VISIBLE_DEVICES"]
    if not torch.cuda.is_available():
        raise RuntimeError(f"引擎虚拟环境未检测到 CUDA，GPU={gpu}")
    details = {"torch": torch.__version__, "cuda": torch.version.cuda}
    if not verify_sageattention:
        return details

    device = torch.cuda.get_device_name()
    capability = ".".join(map(str, torch.cuda.get_device_capability()))
    try:
        from sageattention import sageattn

        # KJNodes 的 auto 模式将 H3 的 BF16 张量以 HND 布局送入 sageattn。
        q, k, v = torch.randn(3, 1, 2, 128, 128, device="cuda", dtype=torch.bfloat16).unbind()
        out = sageattn(q, k, v, tensor_layout="HND", is_causal=False, attn_mask=None)
        torch.cuda.synchronize()
        if out.shape != q.shape or not torch.isfinite(out).all().item():
            raise RuntimeError("注意力输出形状错误或包含非有限数值")
        details.update(sageattention=version("sageattention"), compute_capability=capability)
    except Exception as exc:
        raise RuntimeError(
            f"SageAttention 验证失败：GPU={gpu} ({device}, SM {capability})，"
            f"Torch={torch.__version__}，CUDA={torch.version.cuda}。"
            "请核对 engine.sageattention 中的版本或 wheel 与以上环境是否匹配。"
        ) from exc
    return details


if __name__ == "__main__":
    # 使用独立结果文件，允许第三方 CUDA 扩展向标准输出打印诊断信息。
    result = verify_gpu("--sageattention" in sys.argv[2:])
    Path(sys.argv[1]).write_text(json.dumps(result), encoding="utf-8")
