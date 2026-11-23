"""用 ComfyUI v0.33.0 的 H3 节点采样两个关键帧间的一段视频，逐帧交给编码器。"""


def _sample_segment(model, clip, vae, first, last, prompt, seed, width, height, steps):
    """复用 H3 首尾帧条件和原工作流的采样器，仅返回需要解码的视频／音频联合潜变量。"""
    from comfy.model_management import throw_exception_if_processing_interrupted
    from comfy_extras.nodes_custom_sampler import (
        BasicGuider,
        BasicScheduler,
        KSamplerSelect,
        RandomNoise,
        SamplerCustomAdvanced,
    )
    from comfy_extras.nodes_minimax_h3 import MiniMaxH3ImageToVideo

    # 124 符合 H3 的 17k+5 帧网格。模型使用第 0 和第 123 帧作为首尾条件。
    throw_exception_if_processing_interrupted()
    conditioned = MiniMaxH3ImageToVideo.execute(
        clip, vae, prompt, width, height, 124, first_frame=first, last_frame=last
    )
    guider = BasicGuider.execute(model, conditioned[0])[0]
    noise = RandomNoise.execute(seed)[0]
    sampler = KSamplerSelect.execute("res_multistep")[0]
    sigmas = BasicScheduler.execute(model, "simple", steps, 1.0)[0]

    # 原生采样器负责每步进度和中断。直接调用节点方法，不向图执行器登记中间结果。
    throw_exception_if_processing_interrupted()
    return SamplerCustomAdvanced.execute(noise, guider, sampler, sigmas, conditioned[1])[0]


def _rgb24(frame):
    """按 ComfyUI 的 8 位视频量化规则，将一张 H×W×3 图像转成紧密排列的 RGB 字节。"""
    return (frame * 255).clamp(0, 255).byte().cpu().numpy().tobytes()


def generate_segment(
    model, clip, vae, first, last, prompt, seed, *, width, height, final=False, steps=20
):
    """生成一段 120 帧的 RGB24 字节流。末段额外输出尾图，供四段拼为 24 fps 的 20 秒视频。"""
    from comfy.model_management import throw_exception_if_processing_interrupted
    from nodes import VAEDecode

    # 上游已把五张图按同一规则裁到目标画布；这里只取首帧的 RGB 通道，避免再次缩放。
    throw_exception_if_processing_interrupted()
    first = first[:1, :, :, :3]
    last = last[:1, :, :, :3]
    yield _rgb24(first[0])

    samples = _sample_segment(model, clip, vae, first, last, prompt, seed, width, height, steps)
    throw_exception_if_processing_interrupted()
    # VAEDecode 从联合潜变量取视频分支；解码峰值只包含当前段，帧切片与量化逐张进行。
    images = VAEDecode().decode(vae, samples)[0]
    del samples
    if (
        len(images.shape) != 4
        or images.shape[0] < 121
        or tuple(images.shape[1:]) != (height, width, 3)
    ):
        raise ValueError(f"H3 视频 VAE 输出尺寸或帧数异常：{tuple(images.shape)}")

    # 前三段各取 [2:121] 共 119 帧；末段取 [3:121] 共 118 帧，为第 480 帧的原图留位。
    for index in range(3 if final else 2, 121):
        throw_exception_if_processing_interrupted()
        yield _rgb24(images[index])
    del images
    if final:
        throw_exception_if_processing_interrupted()
        yield _rgb24(last[0])
