"""HuggingFace 本地缓存探测：模型已缓存时自动切离线，避免每次启动都往返 HF Hub。

背景：`sentence-transformers` 每次加载都会对 Hub 发一批 HEAD/GET 做版本校验，
即使权重已在本地。实测这让冷启动耗时 13.8s，并且要求联网才能起服务，
与「无外网环境也能跑」的要求冲突（见 docs/review-v1-audit.md §2.14）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("hf-cache")


def _hub_cache_dir() -> Path:
    """HF 缓存根目录：优先 HF_HOME，其次 HUGGINGFACE_HUB_CACHE，最后默认 ~/.cache/huggingface。"""
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        return Path(hub)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "huggingface" / "hub"


def is_model_cached(model_name: str) -> bool:
    """模型目录是否已存在于本地 HF 缓存（`org/name` → `models--org--name`）。"""
    slug = "models--" + model_name.replace("/", "--")
    return (_hub_cache_dir() / slug).is_dir()


def prefer_offline_if_cached(model_name: str) -> None:
    """模型已缓存则设置 HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE。

    只在用户未显式设置 HF_HUB_OFFLINE 时生效（显式设置永远优先），
    必须在 `import sentence_transformers` **之前**调用。
    """
    if "HF_HUB_OFFLINE" in os.environ:
        return
    try:
        if not is_model_cached(model_name):
            return
    except Exception:  # 权限等异常不应影响启动
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    logger.info("检测到本地已缓存 %s，切换为离线加载", model_name)
