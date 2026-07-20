"""Shared Pydantic request models for vector-ai HTTP routes."""
from typing import List, Optional

from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: str | list | None = ""


class ChatRequest(BaseModel):
    model:       Optional[str]   = None
    messages:    List[Message]
    stream:      Optional[bool]  = True
    max_tokens:  Optional[int]   = 2048
    temperature: Optional[float] = 1.0


class SensorReactionRequest(BaseModel):
    event:        str
    avoid:        Optional[List[str]] = None  # Recent phrases to avoid repeating


class AmbientRequest(BaseModel):
    image: str  # base64-encoded JPEG of what Vector is currently looking at


class MemoryAddRequest(BaseModel):
    text: str


class MemoryForgetRequest(BaseModel):
    target: str  # integer id or substring


class FaceSeenRequest(BaseModel):
    face_id: int
    name:    Optional[str] = None  # empty/missing = stranger


class AmbientQuietRequest(BaseModel):
    on: bool


class FaceIn(BaseModel):
    """Bounded face payload from chipper (local POST only)."""
    face_id: int = 0
    name: str = ""
    is_stranger: bool = False


class BehaviorTickRequest(BaseModel):
    occupied: bool = False
    face: Optional[FaceIn] = None
    on_charger: bool = False
    voice_recent: bool = False


class GreetingRequest(BaseModel):
    face_id: int
    name:    str
