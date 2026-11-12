"""Translate the RouterAI video request contract to BetterToken."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class VideoReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url", "video_url", "audio_url"]
    image_url: dict[str, str] | None = None
    video_url: dict[str, str] | None = None
    audio_url: dict[str, str] | None = None
    frame_type: Literal["first_frame", "last_frame"] | None = None

    @model_validator(mode="after")
    def validate_source(self):
        source = getattr(self, self.type)
        if not source or not source.get("url"):
            raise ValueError(f"{self.type}.url is required")
        for kind in ("image_url", "video_url", "audio_url"):
            if kind != self.type and getattr(self, kind) is not None:
                raise ValueError("Only the selected reference type may have a source")
        return self


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    duration: StrictInt | None = Field(default=None, ge=2, le=30)
    resolution: Literal["480p", "720p", "1080p"] | None = None
    aspect_ratio: Literal["16:9", "4:3", "1:1", "3:4", "9:16"] | None = None
    seed: StrictInt | None = None
    generate_audio: StrictBool = True
    frame_images: list[VideoReference] = Field(default_factory=list)
    input_references: list[VideoReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_media(self):
        for frame in self.frame_images:
            if frame.type != "image_url" or frame.frame_type is None:
                raise ValueError("frame_images require image_url and frame_type")
        if len({frame.frame_type for frame in self.frame_images}) != len(self.frame_images):
            raise ValueError("Duplicate frame_type")
        if any(reference.frame_type for reference in self.input_references):
            raise ValueError("frame_type belongs in frame_images")
        return self


def build_video_payload(body: VideoRequest, model: str, request_format: str | None) -> dict:
    if request_format in (None, "routerai_videos"):
        return {**body.model_dump(exclude_none=True), "model": model}
    if request_format != "bettertoken_videos":
        raise ValueError("Unsupported video request format")
    payload = {
        "model": model,
        "prompt": body.prompt,
        "operation": (
            "image_to_video" if body.frame_images
            else "reference_to_video" if body.input_references
            else "text_to_video"
        ),
        "output": {"format": "mp4"},
        "audio": {"mode": "on" if body.generate_audio else "off"},
    }
    if body.duration is not None:
        payload["duration"] = {"mode": "fixed", "seconds": body.duration}
    for name in ("resolution", "aspect_ratio"):
        if getattr(body, name) is not None:
            payload["output"][name] = getattr(body, name)
    if body.seed is not None:
        payload["seed"] = body.seed
    media = []
    for index, reference in enumerate(body.frame_images + body.input_references):
        media_type = reference.type.removesuffix("_url")
        media.append({
            "id": f"media-{index + 1}",
            "type": media_type,
            "role": reference.frame_type or f"reference_{media_type}",
            "source": getattr(reference, reference.type)["url"],
        })
    if media:
        payload["media"] = media
    return payload


def video_cost_usd(payload: dict) -> float | None:
    """Accept actual USD settlement only; never guess a per-video tariff."""
    usage = payload.get("usage")
    value = usage.get("cost") if isinstance(usage, dict) else None
    if value is None:
        billing = payload.get("billing")
        if not isinstance(billing, dict) or billing.get("currency") != "USD":
            return None
        value = billing.get("net_amount")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid video cost")
    cost = float(value)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Invalid video cost")
    return cost
