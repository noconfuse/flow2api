"""Model name resolver - converts simplified model names + generationConfig params to internal MODEL_CONFIG keys.

When upstream services (e.g. New API) send requests with a generic model name
along with generationConfig containing aspectRatio / imageSize, this module
resolves them to the specific internal model name used by flow2api.

Example:
    model = "gemini-3.0-pro-image"
    generationConfig.imageConfig.aspectRatio = "16:9"
    generationConfig.imageConfig.imageSize = "2k"
    → resolved to "gemini-3.0-pro-image-landscape-2k"
"""

import re
from typing import Optional, Dict, Any, Tuple, List
from ..core.logger import debug_logger

# ──────────────────────────────────────────────
# 简化模型名 → 基础模型名前缀 的映射
# ──────────────────────────────────────────────
IMAGE_BASE_MODELS = {
    # Gemini 3.0 Pro (GEM_PIX_2)
    "gemini-3.0-pro-image": "gemini-3.0-pro-image",
    # Gemini 3.1 Flash (NARWHAL)
    "gemini-3.1-flash-image": "gemini-3.1-flash-image",
    # Imagen 4.0 (IMAGEN_3_5)
    "imagen-4.0-generate-preview": "imagen-4.0-generate-preview",
}

# ──────────────────────────────────────────────
# aspectRatio 转换映射
# 支持 Gemini 原生格式 ("16:9") 和内部格式 ("landscape")
# ──────────────────────────────────────────────
ASPECT_RATIO_MAP = {
    # Gemini 标准 ratio 格式
    "16:9": "landscape",
    "9:16": "portrait",
    "1:1": "square",
    "4:3": "four-three",
    "3:4": "three-four",
    # 英文名直接映射
    "landscape": "landscape",
    "portrait": "portrait",
    "square": "square",
    "four-three": "four-three",
    "three-four": "three-four",
    "four_three": "four-three",
    "three_four": "three-four",
    # 大写形式
    "LANDSCAPE": "landscape",
    "PORTRAIT": "portrait",
    "SQUARE": "square",
}

# 每个基础模型支持的 aspectRatio 列表
# 如果请求的 ratio 不在支持列表中，降级到默认值
MODEL_SUPPORTED_ASPECTS = {
    "gemini-3.0-pro-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "gemini-3.1-flash-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "imagen-4.0-generate-preview": ["landscape", "portrait"],
}

# 每个基础模型支持的 imageSize（分辨率）列表
MODEL_SUPPORTED_SIZES = {
    "gemini-3.0-pro-image": ["2k", "4k"],
    "gemini-3.1-flash-image": ["2k", "4k"],
    "imagen-4.0-generate-preview": [],  # 不支持放大
}

# imageSize 归一化映射
IMAGE_SIZE_MAP = {
    "1k": "1k",
    "1K": "1k",
    "2k": "2k",
    "2K": "2k",
    "4k": "4k",
    "4K": "4k",
    "1080p": "1080p",
    "1080P": "1080p",
    "": "",
}

# 默认 aspectRatio
DEFAULT_ASPECT = "landscape"

OPENAI_IMAGE_SIZE_RE = re.compile(r"^(?P<w>\d{2,5})\s*[xX]\s*(?P<h>\d{2,5})$")

# OpenAI 常见 quality → imageSize 映射
# - 这里的 imageSize 是 flow2api 的“放大档位”，并不等价于 OpenAI 的像素尺寸；
#   但可用作“画质/清晰度”的近似映射。
OPENAI_QUALITY_MAP = {
    "low": None,
    "standard": None,
    "medium": "2k",
    "high": "4k",
    "hd": "4k",
    "ultra": "4k",
}

# 用于把 OpenAI size（如 1024x1792）映射到最接近的 flow2api aspect 选项
ASPECT_RATIO_FLOAT_MAP = {
    "landscape": 16 / 9,
    "portrait": 9 / 16,
    "square": 1.0,
    "four-three": 4 / 3,
    "three-four": 3 / 4,
}


# ──────────────────────────────────────────────
# 视频模型简化名映射
# ──────────────────────────────────────────────
VIDEO_BASE_MODELS = {
    # T2V models
    "veo_3_1_t2v_fast": {
        "landscape": "veo_3_1_t2v_fast_landscape",
        "portrait": "veo_3_1_t2v_fast_portrait",
    },
    "veo_3_1_t2v_fast_4s": {
        "landscape": "veo_3_1_t2v_fast_4s",
        "portrait": "veo_3_1_t2v_fast_portrait_4s",
    },
    "veo_3_1_t2v_fast_6s": {
        "landscape": "veo_3_1_t2v_fast_6s",
        "portrait": "veo_3_1_t2v_fast_portrait_6s",
    },
    "veo_3_1_t2v_fast_ultra": {
        "landscape": "veo_3_1_t2v_fast_ultra",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra",
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_t2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_t2v": {
        "landscape": "veo_3_1_t2v_landscape",
        "portrait": "veo_3_1_t2v_portrait",
    },
    "veo_3_1_t2v_4s": {
        "landscape": "veo_3_1_t2v_4s",
        "portrait": "veo_3_1_t2v_portrait_4s",
    },
    "veo_3_1_t2v_6s": {
        "landscape": "veo_3_1_t2v_6s",
        "portrait": "veo_3_1_t2v_portrait_6s",
    },
    "veo_3_1_t2v_4s_4k": {
        "landscape": "veo_3_1_t2v_4s_4k",
        "portrait": "veo_3_1_t2v_portrait_4s_4k",
    },
    "veo_3_1_t2v_4s_1080p": {
        "landscape": "veo_3_1_t2v_4s_1080p",
        "portrait": "veo_3_1_t2v_portrait_4s_1080p",
    },
    "veo_3_1_t2v_6s_4k": {
        "landscape": "veo_3_1_t2v_6s_4k",
        "portrait": "veo_3_1_t2v_portrait_6s_4k",
    },
    "veo_3_1_t2v_6s_1080p": {
        "landscape": "veo_3_1_t2v_6s_1080p",
        "portrait": "veo_3_1_t2v_portrait_6s_1080p",
    },
    "veo_3_1_t2v_4k": {
        "landscape": "veo_3_1_t2v_4k",
        "portrait": "veo_3_1_t2v_portrait_4k",
    },
    "veo_3_1_t2v_1080p": {
        "landscape": "veo_3_1_t2v_1080p",
        "portrait": "veo_3_1_t2v_portrait_1080p",
    },
    "veo_3_1_t2v_lite": {
        "landscape": "veo_3_1_t2v_lite_landscape",
        "portrait": "veo_3_1_t2v_lite_portrait",
    },
    "veo_3_1_t2v_lite_4s": {
        "landscape": "veo_3_1_t2v_lite_4s_landscape",
        "portrait": "veo_3_1_t2v_lite_4s_portrait",
    },
    "veo_3_1_t2v_lite_6s": {
        "landscape": "veo_3_1_t2v_lite_6s_landscape",
        "portrait": "veo_3_1_t2v_lite_6s_portrait",
    },
    "omni-flash-t2v": {
        "landscape": "omni-flash-t2v_landscape",
        "portrait": "omni-flash-t2v_portrait",
    },
    "omni-flash-t2v_4s": {
        "landscape": "omni-flash-t2v_landscape_4s",
        "portrait": "omni-flash-t2v_portrait_4s",
    },
    "omni-flash-t2v_6s": {
        "landscape": "omni-flash-t2v_landscape_6s",
        "portrait": "omni-flash-t2v_portrait_6s",
    },
    "omni-flash-t2v_8s": {
        "landscape": "omni-flash-t2v_landscape_8s",
        "portrait": "omni-flash-t2v_portrait_8s",
    },
    "omni-flash-t2v_10s": {
        "landscape": "omni-flash-t2v_landscape_10s",
        "portrait": "omni-flash-t2v_portrait_10s",
    },
    "omni-flash-edit": {
        "landscape": "omni-flash-edit_landscape",
        "portrait": "omni-flash-edit_portrait",
    },
    "omni-flash-r2v": {
        "landscape": "omni-flash-r2v_landscape",
        "portrait": "omni-flash-r2v_portrait",
    },
    "omni-flash-r2v_8s": {
        "landscape": "omni-flash-r2v_landscape_8s",
        "portrait": "omni-flash-r2v_portrait_8s",
    },
    "omni-flash-r2v_4s": {
        "landscape": "omni-flash-r2v_landscape_4s",
        "portrait": "omni-flash-r2v_portrait_4s",
    },
    "omni-flash-r2v_6s": {
        "landscape": "omni-flash-r2v_landscape_6s",
        "portrait": "omni-flash-r2v_portrait_6s",
    },
    "omni-flash-r2v_10s": {
        "landscape": "omni-flash-r2v_landscape_10s",
        "portrait": "omni-flash-r2v_portrait_10s",
    },
    # I2V models
    "veo_3_1_i2v_s_fast_fl": {
        "landscape": "veo_3_1_i2v_s_fast_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_fl",
    },
    "veo_3_1_i2v_s_fast_4s_fl": {
        "landscape": "veo_3_1_i2v_s_fast_4s_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_4s_fl",
    },
    "veo_3_1_i2v_s_fast_6s_fl": {
        "landscape": "veo_3_1_i2v_s_fast_6s_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_6s_fl",
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_i2v_s": {
        "landscape": "veo_3_1_i2v_s_landscape",
        "portrait": "veo_3_1_i2v_s_portrait",
    },
    "veo_3_1_i2v_s_4s": {
        "landscape": "veo_3_1_i2v_s_4s",
        "portrait": "veo_3_1_i2v_s_portrait_4s",
    },
    "veo_3_1_i2v_s_6s": {
        "landscape": "veo_3_1_i2v_s_6s",
        "portrait": "veo_3_1_i2v_s_portrait_6s",
    },
    "veo_3_1_i2v_s_4s_4k": {
        "landscape": "veo_3_1_i2v_s_4s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_4s_4k",
    },
    "veo_3_1_i2v_s_4s_1080p": {
        "landscape": "veo_3_1_i2v_s_4s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_4s_1080p",
    },
    "veo_3_1_i2v_s_6s_4k": {
        "landscape": "veo_3_1_i2v_s_6s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_6s_4k",
    },
    "veo_3_1_i2v_s_6s_1080p": {
        "landscape": "veo_3_1_i2v_s_6s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_6s_1080p",
    },
    "veo_3_1_i2v_s_4k": {
        "landscape": "veo_3_1_i2v_s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_4k",
    },
    "veo_3_1_i2v_s_1080p": {
        "landscape": "veo_3_1_i2v_s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_1080p",
    },
    "veo_3_1_i2v_lite": {
        "landscape": "veo_3_1_i2v_lite_landscape",
        "portrait": "veo_3_1_i2v_lite_portrait",
    },
    "veo_3_1_i2v_lite_4s": {
        "landscape": "veo_3_1_i2v_lite_4s_landscape",
        "portrait": "veo_3_1_i2v_lite_4s_portrait",
    },
    "veo_3_1_i2v_lite_6s": {
        "landscape": "veo_3_1_i2v_lite_6s_landscape",
        "portrait": "veo_3_1_i2v_lite_6s_portrait",
    },
    "veo_3_1_interpolation_lite": {
        "landscape": "veo_3_1_interpolation_lite_landscape",
        "portrait": "veo_3_1_interpolation_lite_portrait",
    },
    "veo_3_1_interpolation_lite_4s": {
        "landscape": "veo_3_1_interpolation_lite_4s_landscape",
        "portrait": "veo_3_1_interpolation_lite_4s_portrait",
    },
    "veo_3_1_interpolation_lite_6s": {
        "landscape": "veo_3_1_interpolation_lite_6s_landscape",
        "portrait": "veo_3_1_interpolation_lite_6s_portrait",
    },
    # R2V models
    "veo_3_1_r2v_fast": {
        "landscape": "veo_3_1_r2v_fast",
        "portrait": "veo_3_1_r2v_fast_portrait",
    },
    "veo_3_1_r2v_fast_ultra": {
        "landscape": "veo_3_1_r2v_fast_ultra",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra",
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_r2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    },
    # Extend models (视频续写)
    "veo_3_1_extend": {
        "landscape": "veo_3_1_extend",
        "portrait": "veo_3_1_extend_portrait",
    },
}


def _extract_generation_params(request) -> Tuple[Optional[str], Optional[str]]:
    """从请求中提取 aspectRatio 和 imageSize 参数。

    优先级：
    1. request.generationConfig.imageConfig (顶层 Gemini 参数)
    2. extra fields 中的 generationConfig (extra_body 透传)
    3. OpenAI 风格字段（size/quality）兼容：可在 generationConfig/imageConfig 或顶层 extra 中出现

    Returns:
        (aspect_ratio, image_size) 归一化后的值
    """
    def _normalize_str(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text if text else None

    def _read_value(obj: Any, *keys: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            for key in keys:
                if key in obj:
                    return obj.get(key)
            return None

        for key in keys:
            if hasattr(obj, key):
                value = getattr(obj, key, None)
                if value is not None:
                    return value

        extra = getattr(obj, "__pydantic_extra__", None) or {}
        for key in keys:
            if key in extra:
                return extra.get(key)
        return None

    def _normalize_aspect_ratio(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = (
            raw.replace("：", ":")
            .replace("/", ":")
            .replace("x", ":")
            .replace("X", ":")
            .replace(" ", "")
            .strip()
        )

        mapped = ASPECT_RATIO_MAP.get(token)
        if mapped:
            return mapped
        mapped = ASPECT_RATIO_MAP.get(token.lower())
        if mapped:
            return mapped
        mapped = ASPECT_RATIO_MAP.get(token.upper())
        if mapped:
            return mapped
        return token

    def _normalize_image_size(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = raw.replace(" ", "").strip()
        mapped = IMAGE_SIZE_MAP.get(token)
        if mapped is not None:
            return mapped or None
        mapped = IMAGE_SIZE_MAP.get(token.lower())
        if mapped is not None:
            return mapped or None
        mapped = IMAGE_SIZE_MAP.get(token.upper())
        if mapped is not None:
            return mapped or None
        return token.lower()

    def _aspect_from_openai_size(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        match = OPENAI_IMAGE_SIZE_RE.match(raw)
        if not match:
            return None

        try:
            width = int(match.group("w"))
            height = int(match.group("h"))
        except Exception:
            return None

        if width <= 0 or height <= 0:
            return None

        ratio = width / height
        best = min(
            ASPECT_RATIO_FLOAT_MAP.items(),
            key=lambda item: abs(ratio - item[1]),
        )[0]
        return best

    def _image_size_from_openai_quality(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = raw.strip().lower()
        if token in IMAGE_SIZE_MAP:
            return _normalize_image_size(token)

        mapped = OPENAI_QUALITY_MAP.get(token)
        if mapped:
            return mapped
        return None

    def _apply_image_config(image_config: Any, aspect_ratio: Optional[str], image_size: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        # 显式 aspectRatio/imageSize
        if not aspect_ratio:
            aspect_ratio = _normalize_aspect_ratio(
                _read_value(image_config, "aspectRatio", "aspect_ratio", "aspect")
            )
        if not image_size:
            image_size = _normalize_image_size(
                _read_value(image_config, "imageSize", "image_size", "resolution")
            )

        # OpenAI size/quality
        if not aspect_ratio:
            aspect_ratio = _aspect_from_openai_size(_read_value(image_config, "size"))
        if not image_size:
            image_size = _image_size_from_openai_quality(
                _read_value(image_config, "quality", "imageQuality", "image_quality")
            )

        return aspect_ratio, image_size

    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None

    # 1) 优先从 request.generationConfig 解析
    gen_config = getattr(request, "generationConfig", None)
    if gen_config is not None:
        image_config = _read_value(gen_config, "imageConfig", "image_config")
        if image_config is not None:
            aspect_ratio, image_size = _apply_image_config(
                image_config, aspect_ratio, image_size
            )

        # 有些上游会把字段放在 generationConfig 顶层
        if not aspect_ratio:
            aspect_ratio = _normalize_aspect_ratio(
                _read_value(gen_config, "aspectRatio", "aspect_ratio")
            )
        if not image_size:
            image_size = _normalize_image_size(
                _read_value(gen_config, "imageSize", "image_size")
            )

        if not aspect_ratio:
            aspect_ratio = _aspect_from_openai_size(_read_value(gen_config, "size"))
        if not image_size:
            image_size = _image_size_from_openai_quality(_read_value(gen_config, "quality"))

    # 2) 顶层没有时，再尝试从 extra fields (Pydantic extra="allow") 中透传的 generationConfig
    if (aspect_ratio is None or image_size is None) and hasattr(request, "__pydantic_extra__"):
        extra = request.__pydantic_extra__ or {}
        gen_config_raw = extra.get("generationConfig")
        if not isinstance(gen_config_raw, dict):
            extra_body = extra.get("extra_body") or extra.get("extraBody")
            if isinstance(extra_body, dict):
                gen_config_raw = extra_body.get("generationConfig")

        if isinstance(gen_config_raw, dict):
            image_config_raw = (
                gen_config_raw.get("imageConfig")
                or gen_config_raw.get("image_config")
                or {}
            )
            if image_config_raw:
                aspect_ratio, image_size = _apply_image_config(
                    image_config_raw, aspect_ratio, image_size
                )

            if aspect_ratio is None:
                aspect_ratio = _normalize_aspect_ratio(
                    gen_config_raw.get("aspectRatio") or gen_config_raw.get("aspect_ratio")
                )
            if image_size is None:
                image_size = _normalize_image_size(
                    gen_config_raw.get("imageSize") or gen_config_raw.get("image_size")
                )

            if aspect_ratio is None:
                aspect_ratio = _aspect_from_openai_size(gen_config_raw.get("size"))
            if image_size is None:
                image_size = _image_size_from_openai_quality(gen_config_raw.get("quality"))

    # 3) OpenAI 风格 size/quality（顶层 extra）兼容
    if (aspect_ratio is None or image_size is None) and hasattr(request, "__pydantic_extra__"):
        extra = request.__pydantic_extra__ or {}
        if aspect_ratio is None:
            aspect_ratio = _aspect_from_openai_size(extra.get("size"))
        if image_size is None:
            image_size = _image_size_from_openai_quality(extra.get("quality"))

        # 一些上游可能直接传 aspect_ratio/image_size
        if aspect_ratio is None:
            aspect_ratio = _normalize_aspect_ratio(extra.get("aspect_ratio") or extra.get("aspectRatio"))
        if image_size is None:
            image_size = _normalize_image_size(extra.get("image_size") or extra.get("imageSize"))

    return aspect_ratio, image_size


def resolve_model_name(
    model: str, request=None, model_config: Dict[str, Any] = None
) -> str:
    """将简化模型名 + generationConfig 参数解析为内部 MODEL_CONFIG key。

    如果 model 已经是有效的 MODEL_CONFIG key，直接返回。
    如果 model 是简化名（基础模型名），则根据 generationConfig 中的
    aspectRatio / imageSize 拼接出完整的内部模型名。

    Args:
        model: 请求中的模型名
        request: ChatCompletionRequest 实例（用于提取 generationConfig）
        model_config: MODEL_CONFIG 字典（用于验证解析后的模型名）

    Returns:
        解析后的内部模型名
    """
    # ────── 图片模型解析 ──────
    if model in IMAGE_BASE_MODELS:
        base = IMAGE_BASE_MODELS[model]
        aspect_ratio, image_size = (
            _extract_generation_params(request) if request else (None, None)
        )

        # 默认 aspect ratio
        if not aspect_ratio:
            aspect_ratio = DEFAULT_ASPECT

        # 检查支持的 aspect ratio
        supported_aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        if aspect_ratio not in supported_aspects and supported_aspects:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] 模型 {base} 不支持 aspectRatio={aspect_ratio}，"
                f"降级到 {DEFAULT_ASPECT}"
            )
            aspect_ratio = DEFAULT_ASPECT

        # 拼接模型名
        resolved = f"{base}-{aspect_ratio}"

        # 检查支持的 imageSize
        if image_size and image_size != "1k":
            supported_sizes = MODEL_SUPPORTED_SIZES.get(base, [])
            if image_size in supported_sizes:
                resolved = f"{resolved}-{image_size}"
            else:
                debug_logger.log_warning(
                    f"[MODEL_RESOLVER] 模型 {base} 不支持 imageSize={image_size}，忽略"
                )

        # 最终验证
        if model_config and resolved not in model_config:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] 解析后的模型名 {resolved} 不在 MODEL_CONFIG 中，"
                f"回退到原始模型名 {model}"
            )
            return model

        debug_logger.log_info(
            f"[MODEL_RESOLVER] 模型名转换: {model} → {resolved} "
            f"(aspectRatio={aspect_ratio}, imageSize={image_size or 'default'})"
        )
        return resolved

    # ────── 视频模型解析 ──────
    if model in VIDEO_BASE_MODELS:
        aspect_ratio, image_size = (
            _extract_generation_params(request) if request else (None, None)
        )

        # 视频默认横屏
        if not aspect_ratio or aspect_ratio not in ("landscape", "portrait"):
            aspect_ratio = "landscape"

        if image_size in ("4k", "1080p") and f"{model}_{image_size}" in VIDEO_BASE_MODELS:
            model = f"{model}_{image_size}"

        orientation_map = VIDEO_BASE_MODELS[model]
        resolved = orientation_map.get(aspect_ratio)

        if resolved and model_config and resolved in model_config:
            debug_logger.log_info(
                f"[MODEL_RESOLVER] 视频模型名转换: {model} → {resolved} "
                f"(aspectRatio={aspect_ratio})"
            )
            return resolved

        debug_logger.log_warning(
            f"[MODEL_RESOLVER] 视频模型 {model} 解析失败 (aspect={aspect_ratio})，"
            f"使用原始模型名"
        )
        return model

    # 如果已经是有效的 MODEL_CONFIG key，直接返回
    if model_config and model in model_config:
        return model

    # 未知模型名，原样返回（由下游 MODEL_CONFIG 校验报错）
    return model


def get_base_model_aliases() -> Dict[str, str]:
    """返回所有简化模型名（别名）及其描述，用于 /v1/models 接口展示。"""
    aliases = {}

    for alias, base in IMAGE_BASE_MODELS.items():
        aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        sizes = MODEL_SUPPORTED_SIZES.get(base, [])
        desc_parts = [f"aspects: {', '.join(aspects)}"]
        if sizes:
            desc_parts.append(f"sizes: {', '.join(sizes)}")
        aliases[alias] = f"Image generation (alias) - {'; '.join(desc_parts)}"

    for alias in VIDEO_BASE_MODELS:
        aliases[alias] = (
            "Video generation (alias) - supports landscape/portrait via generationConfig"
        )

    return aliases


BATCH_DURATION_SUFFIX_RE = re.compile(r"_(4s|6s|8s|10s)$", re.IGNORECASE)
BATCH_DURATION_ORDER = {
    "4s": 4,
    "5s": 5,
    "6s": 6,
    "8s": 8,
    "10s": 10,
}
BATCH_ASPECT_DISPLAY_MAP = {
    "landscape": "16:9",
    "portrait": "9:16",
}
BATCH_ASPECT_INTERNAL_MAP = {
    "16:9": "landscape",
    "9:16": "portrait",
    "landscape": "landscape",
    "portrait": "portrait",
}


def _normalize_batch_duration_choice(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text


def _normalize_batch_aspect_choice(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "16:9"
    return BATCH_ASPECT_INTERNAL_MAP.get(text, BATCH_ASPECT_INTERNAL_MAP.get(text.lower(), ""))


def _humanize_batch_family_name(family_key: str) -> str:
    tokens = re.split(r"[-_]+", str(family_key or "").strip())
    replacements = {
        "veo": "Veo",
        "omni": "Omni",
        "flash": "Flash",
        "lite": "Lite",
        "fast": "Fast",
        "ultra": "Ultra",
        "relaxed": "Relaxed",
        "edit": "Edit",
    }
    normalized = [
        replacements.get(token.lower(), token.upper() if token.isdigit() else token)
        for token in tokens
        if token and token.lower() not in {"t2v", "r2v", "i2v"}
    ]
    return " ".join(normalized) or family_key


def get_batch_video_model_catalog(model_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return batch-friendly video model families resolved from shared aliases."""
    families: Dict[str, Dict[str, Any]] = {}

    for alias, orientations in VIDEO_BASE_MODELS.items():
        landscape_model = orientations.get("landscape")
        if not landscape_model or landscape_model not in model_config:
            continue

        config = model_config.get(landscape_model) or {}
        if config.get("type") != "video":
            continue

        video_type = str(config.get("video_type") or "").strip().lower()
        if video_type not in {"t2v", "r2v", "edit"}:
            continue

        family_key = BATCH_DURATION_SUFFIX_RE.sub("", alias)
        duration_key = alias[len(family_key):].lstrip("_")
        if not duration_key:
            continue
        family_entry = families.setdefault(
            family_key,
            {
                "key": family_key,
                "label": _humanize_batch_family_name(family_key),
                "task_types": set(),
                "duration_options": set(),
                "aspect_ratio_options": set(),
                "variants": {},
            },
        )
        family_entry["task_types"].add(video_type)
        family_entry["duration_options"].add(duration_key)
        variant_entry = family_entry["variants"].setdefault(duration_key, {})
        for aspect_key, target_model in orientations.items():
            if target_model in model_config and aspect_key in BATCH_ASPECT_DISPLAY_MAP:
                family_entry["aspect_ratio_options"].add(BATCH_ASPECT_DISPLAY_MAP[aspect_key])
                variant_entry[aspect_key] = target_model

    def _sort_duration_key(value: str) -> Tuple[int, str]:
        normalized = _normalize_batch_duration_choice(value)
        return (BATCH_DURATION_ORDER.get(normalized, 999), normalized)

    items: List[Dict[str, Any]] = []
    for family_key in sorted(families.keys()):
        entry = families[family_key]
        duration_options = sorted(entry["duration_options"], key=_sort_duration_key)
        aspect_ratio_options = [item for item in ("16:9", "9:16") if item in entry["aspect_ratio_options"]]
        items.append(
            {
                "key": entry["key"],
                "label": entry["label"],
                "task_types": sorted(entry["task_types"]),
                "duration_options": duration_options,
                "aspect_ratio_options": aspect_ratio_options,
                "variants": entry["variants"],
            }
        )
    return items


def resolve_batch_video_model(
    *,
    model_family: str,
    task_type: Optional[str],
    duration: Optional[str],
    aspect_ratio: Optional[str],
    model_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve batch model family + duration + aspect to an internal model key."""
    normalized_family = str(model_family or "").strip()
    if not normalized_family:
        return {"resolved_model": None, "error": "model 不能为空"}

    normalized_task_type = str(task_type or "").strip().lower()
    normalized_duration = _normalize_batch_duration_choice(duration)
    normalized_aspect = _normalize_batch_aspect_choice(aspect_ratio)
    if not normalized_aspect:
        return {"resolved_model": None, "error": f"不支持的 aspect_ratio: {aspect_ratio}"}

    if normalized_family in model_config:
        config = model_config.get(normalized_family) or {}
        if config.get("type") != "video":
            return {"resolved_model": None, "error": f"模型不是视频模型: {normalized_family}"}
        video_type = str(config.get("video_type") or "").strip().lower()
        if normalized_task_type and normalized_task_type != video_type:
            return {"resolved_model": None, "error": f"{normalized_family} 不支持任务类型 {normalized_task_type}"}
        internal_aspect = str(config.get("aspect_ratio") or "")
        aspect_display = "9:16" if "PORTRAIT" in internal_aspect else "16:9"
        return {
            "resolved_model": normalized_family,
            "resolved_duration": normalized_duration,
            "resolved_aspect_ratio": aspect_display,
            "task_type": video_type,
        }

    catalog = {item["key"]: item for item in get_batch_video_model_catalog(model_config)}
    family_entry = catalog.get(normalized_family)
    if not family_entry:
        return {"resolved_model": None, "error": f"不支持的模型家族: {normalized_family}"}

    if normalized_task_type and normalized_task_type not in family_entry["task_types"]:
        return {"resolved_model": None, "error": f"{normalized_family} 不支持任务类型 {normalized_task_type}"}

    duration_options = family_entry["duration_options"]
    chosen_duration = normalized_duration
    if not chosen_duration:
        return {"resolved_model": None, "error": "duration 不能为空"}
    if chosen_duration not in duration_options:
        return {"resolved_model": None, "error": f"{normalized_family} 不支持时长 {duration or '-'}"}

    aspect_key = normalized_aspect
    variant_entry = family_entry["variants"].get(chosen_duration) or {}
    resolved_model = variant_entry.get(aspect_key)
    if not resolved_model:
        return {"resolved_model": None, "error": f"{normalized_family} 不支持横竖版 {aspect_ratio or '-'}"}

    return {
        "resolved_model": resolved_model,
        "resolved_duration": chosen_duration,
        "resolved_aspect_ratio": BATCH_ASPECT_DISPLAY_MAP.get(aspect_key, "16:9"),
        "task_type": normalized_task_type or (family_entry["task_types"][0] if family_entry["task_types"] else ""),
    }
