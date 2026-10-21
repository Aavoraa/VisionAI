"""Model abstraction layer: every inference backend implements ``VisionModel``."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, TypedDict


class ModelOutput(TypedDict):
    text: str
    input_tokens: int
    output_tokens: int


class ModelError(Exception):
    """Inference failed. Messages are generic and safe to log; never shown verbatim to clients."""


class ModelUnavailableError(ModelError):
    """The backend cannot be loaded or reached."""


class ModelTimeoutError(ModelError):
    """Inference exceeded its deadline."""


class VisionModel(ABC):
    name: str
    #: Whether ``options["response_format"]`` (JSON schema) is enforced during decoding.
    supports_structured_output: bool = False
    #: Task-prompt models (e.g. Florence-2) accept only these prompts, mapped to a short
    #: description. ``None``: the prompt is a free-form instruction or question.
    tasks: Mapping[str, str] | None = None

    @abstractmethod
    async def analyze(self, images: list[bytes], prompt: str, options: dict[str, Any] | None = None) -> ModelOutput:
        """Run inference on sanitised image bytes.

        ``options`` may contain ``temperature`` (float), ``max_tokens`` (int),
        ``anti_repetition`` (bool: discourage repetition loops; backends may ignore it) and
        ``response_format`` (a JSON schema; only when ``supports_structured_output``).
        Raises ``ModelError`` on failure.
        """

    async def close(self) -> None:
        """Release resources (GPU memory, HTTP clients). Backends without resources keep this default."""
