"""五图 H3 输出节点。部署器把此目录作为独立的 ComfyUI 自定义节点包加载。"""

import logging
import os
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


class MyComfySegmentedH3Video:
    """顺序采样四段 H3 视频，每段编码落盘后释放画面，只向 ComfyUI 提交最终 MP4。"""

    CATEGORY = "video/MyComfy"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ()

    @classmethod
    def INPUT_TYPES(cls):
        """声明五张同尺寸关键帧、四段独立提示词以及平台绑定的输出前缀。"""
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE",),
                **{f"image_{n}": ("IMAGE",) for n in range(1, 6)},
                **{
                    name: ("STRING", {"multiline": True, "default": ""})
                    for name in (
                        "prompt",
                        "shot_1_prompt",
                        "shot_2_prompt",
                        "shot_3_prompt",
                        "shot_4_prompt",
                    )
                },
                "seed": ("INT", {"default": 2026112301, "min": 0, "max": 2**64 - 1}),
                "filename_prefix": ("STRING", {"default": "video/H3_FiveImage_Segmented"}),
            }
        }

    def run(
        self,
        model,
        clip,
        vae,
        image_1,
        image_2,
        image_3,
        image_4,
        image_5,
        prompt,
        shot_1_prompt,
        shot_2_prompt,
        shot_3_prompt,
        shot_4_prompt,
        seed,
        filename_prefix,
    ):
        """在一个 ComfyUI 请求中完成四段编码，中途失败或取消时清理临时视频。"""
        import folder_paths
        from comfy.model_management import throw_exception_if_processing_interrupted

        from .segment import generate_segment
        from .video import concat_segments, encode_segment

        images = (image_1, image_2, image_3, image_4, image_5)
        shots = (shot_1_prompt, shot_2_prompt, shot_3_prompt, shot_4_prompt)
        height, width = image_1.shape[1:3]
        if min(height, width) < 32 or height % 32 or width % 32:
            raise ValueError("H3 的关键帧宽高必须为 32 的正整数倍")
        if any(tuple(image.shape[1:3]) != (height, width) for image in images):
            raise ValueError("五张关键帧必须先缩放到相同尺寸")
        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory(), width, height
        )
        scratch = Path(folder_paths.get_temp_directory())
        scratch.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="mycomfy-h3-", dir=scratch) as work:
            segments = []
            for i, motion in enumerate(shots):
                throw_exception_if_processing_interrupted()
                logging.getLogger(__name__).info("MiniMax H3 分段生成 %d/4", i + 1)
                part = Path(work) / f"segment-{i + 1}.mp4"
                # 只有进入当前段时才创建张量。发生编码错误时也关闭生成器，释放本段引用。
                with closing(
                    generate_segment(
                        model,
                        clip,
                        vae,
                        images[i],
                        images[i + 1],
                        f"{prompt} {motion}".strip(),
                        (seed + i) % 2**64,
                        width=width,
                        height=height,
                        final=(i == 3),
                    )
                ) as frames:
                    encode_segment(part, width, height, frames)
                segments.append(part)

            throw_exception_if_processing_interrupted()
            # 在最终输出所在文件系统完成封装。恢复扫描只应发现一个完整、可读的 MP4。
            with TemporaryDirectory(prefix=".mycomfy-join-", dir=folder) as staging:
                joined = Path(staging) / "joined.mp4"
                concat_segments(segments, joined)
                throw_exception_if_processing_interrupted()
                while True:
                    filename = f"{name}_{counter:05}_.mp4"
                    try:
                        os.link(joined, Path(folder) / filename)
                        break
                    except FileExistsError:
                        counter += 1

        output = {"filename": filename, "subfolder": subfolder, "type": "output"}
        return {"ui": {"images": [output], "animated": [True]}, "result": ()}


NODE_CLASS_MAPPINGS = {"MyComfySegmentedH3Video": MyComfySegmentedH3Video}
NODE_DISPLAY_NAME_MAPPINGS = {"MyComfySegmentedH3Video": "MyComfy · 五图 H3 逐段编码"}
