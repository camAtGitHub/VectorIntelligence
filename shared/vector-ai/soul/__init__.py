"""Soul melting pot: one hot scratch for presence, face, eye, speech clocks."""
from .eye import EyeService, parse_ambient_llm_raw
from .face import FaceService
from .soul import Soul
from .types import FaceSighting, VisionDigest, build_soul_snapshot, face_current_dict
from .view import format_soul_for_prompt, it_has_been_since, llm_view, time_since_compact

__all__ = [
    "Soul",
    "FaceService",
    "EyeService",
    "FaceSighting",
    "VisionDigest",
    "build_soul_snapshot",
    "face_current_dict",
    "parse_ambient_llm_raw",
    "llm_view",
    "format_soul_for_prompt",
    "it_has_been_since",
    "time_since_compact",
]
# growth / growth_prompts imported on demand (llm dependency) to keep light imports.
