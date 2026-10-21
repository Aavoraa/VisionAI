"""Application configuration loaded from environment variables (prefix ``VISION_``)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VISION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # `VISION_X=` (empty) means "use the default", e.g. auto-derived signup/cookie settings.
        env_ignore_empty=True,
    )

    # --- Application ---
    app_name: str = "Vision AI API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Infrastructure (defaults are for local development only; SecretStr keeps
    # credentials out of repr/logs) ---
    database_url: SecretStr = SecretStr("postgresql+asyncpg://postgres:postgres@localhost:5432/vision_api")
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_echo: bool = False
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_socket_timeout: float = Field(default=1.0, gt=0, le=30)

    # --- Rate limiting (fixed window, per user) ---
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=10, ge=1, le=100_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    # When Redis is unreachable: True = allow requests (availability), False = reject with 503.
    rate_limit_fail_open: bool = True

    # --- Dashboard accounts & sessions ---
    # None = allowed outside production, disabled in production (accounts via scripts/create_admin.py).
    allow_signup: bool | None = None
    session_cookie_name: str = Field(default="vision_session", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    csrf_cookie_name: str = Field(default="vision_csrf", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 90)  # absolute lifetime
    session_idle_timeout_hours: int = Field(default=24, ge=1, le=24 * 90)
    # None = Secure cookies in production only (local development runs over plain http).
    session_cookie_secure: bool | None = None
    login_rate_limit_attempts: int = Field(default=10, ge=1, le=1000)  # per email; per IP is 3x
    login_rate_limit_window_seconds: int = Field(default=900, ge=1, le=86_400)
    max_api_keys_per_user: int = Field(default=25, ge=1, le=1000)  # active keys

    # --- Inference ---
    # Mock by default so tests and local startup never download weights.
    # Real weights: VISION_USE_MOCK_MODEL=false (+ `pip install torch torchvision transformers accelerate`).
    use_mock_model: bool = True
    model_name: str = "HuggingFaceTB/SmolVLM-256M-Instruct"
    model_device: str = "auto"
    # "llama_cpp": a GGUF model served by a local llama.cpp `llama-server` (e.g. PaddleOCR-VL);
    # model_name is then only a label, and llama_cpp_url says where the server listens.
    model_backend: Literal["transformers", "llama_cpp"] = "transformers"
    llama_cpp_url: str = "http://127.0.0.1:8080"
    model_timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    # Invoice extraction makes many model calls per document (header, tables, bands, footer,
    # locator, cross-check), so it has its own budget for the whole document.
    invoice_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    max_images_per_request: int = Field(default=4, ge=1, le=16)

    # --- Image processing limits ---
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
    max_image_pixels: int = Field(default=40_000_000, gt=0, le=200_000_000)
    max_image_side: int = Field(default=1536, ge=64, le=8192)
    allowed_image_formats: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "GIF"})
    output_jpeg_quality: int = Field(default=90, ge=50, le=100)

    # --- Documents (invoice extraction) ---
    max_document_bytes: int = Field(default=20 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    max_document_pages: int = Field(default=10, ge=1, le=100)
    # Pages keep more resolution than /v1/vision images: the OCR model does its own (single)
    # resize, and shrinking twice measurably hurt table recognition on dense invoices.
    max_document_side: int = Field(default=2560, ge=512, le=8192)
    # Optional reasoning model (Phase 2) that cross-checks the extracted fields: a second
    # llama-server with e.g. Qwen3-VL-2B. Empty: rules only.
    extraction_llm_url: str | None = None
    extraction_llm_name: str = "Qwen3-VL-2B-Instruct"
    extraction_llm_timeout_seconds: float = Field(default=300.0, gt=0, le=900)
    # Hybrid (see invoice_fallback): when the item or total checks fail or cannot run, a larger
    # vision model (e.g. Qwen3-VL-8B on its own llama-server) reads the page images directly; its
    # lines / totals are used only where they make more checks pass. Empty: off.
    invoice_fallback_llm_url: str | None = None
    invoice_fallback_llm_name: str = "Qwen3-VL-8B-Instruct"
    invoice_fallback_timeout_seconds: float = Field(default=600.0, gt=0, le=1800)
    # Log each invoice's time per pipeline stage and model calls (internal; never in API responses).
    invoice_profile_log: bool = True
    # Performance: overlap independent calls on the OCR and reasoning servers, and zoom only into
    # the blocks whose fields need it (see invoice_extraction / invoice_reread). False = the old way.
    invoice_parallel_calls: bool = True
    # PaddleOCR requests in flight at once (llama-server slots); 1 = one at a time.
    llama_cpp_concurrency: int = Field(default=1, ge=1, le=4)
    invoice_targeted_blocks: bool = True
    # Zoomed crops (need the reasoning model to locate boxes, see invoice_reread): read the
    # seller / buyer / invoice-details / totals blocks enlarged, then re-read up to N fields
    # that are still flagged (0 = off). The scale caps the enlargement; the OCR model shrinks
    # anything above ~1 megapixel, so larger values only help small crops.
    invoice_region_crops: bool = True
    invoice_reread_max_fields: int = Field(default=4, ge=0, le=12)
    invoice_reread_scale: float = Field(default=2.0, ge=1.0, le=4.0)
    # Item rows that fail their arithmetic (quantity x rate = amount, lines adding up) are read
    # again in shorter bands; a re-read row is kept only if its arithmetic holds.
    invoice_reread_items: bool = True
    # Phase 4 routing (heuristic confidence, see invoice_confidence): at or above auto-approve
    # with all checks passed -> approved; at or above review -> needs review; else reprocess.
    invoice_auto_approve_confidence: float = Field(default=0.90, ge=0, le=1)
    invoice_review_confidence: float = Field(default=0.70, ge=0, le=1)
    # Uploaded invoices and their page images (one folder per invoice). Keep it private.
    document_storage_dir: str = "./data/invoices"
    # Delete uploaded files and page images this many days after upload (extracted data is
    # kept). Empty: keep them.
    invoice_file_retention_days: int | None = Field(default=None, ge=1, le=36500)
    # Optional: POST each approved invoice (automatically or by a reviewer) to this URL, signed
    # with HMAC-SHA256 of the body using the secret (header X-Vision-Signature: sha256=<hex>).
    invoice_webhook_url: str | None = None
    invoice_webhook_secret: SecretStr | None = None

    # --- Remote image fetching (SSRF-protected) ---
    fetch_allowed_schemes: frozenset[str] = frozenset({"https", "http"})
    fetch_allowed_ports: frozenset[int] = frozenset({80, 443})
    fetch_connect_timeout: float = Field(default=3.0, gt=0, le=60)
    fetch_read_timeout: float = Field(default=5.0, gt=0, le=120)
    fetch_total_timeout: float = Field(default=5.0, gt=0, le=300)
    fetch_max_redirects: int = Field(default=3, ge=0, le=10)
    fetch_max_url_length: int = Field(default=2048, ge=64, le=8192)
    fetch_user_agent: str = "VisionAPI-Fetcher/1.0"

    @field_validator("allowed_image_formats")
    @classmethod
    def _normalise_formats(cls, value: frozenset[str]) -> frozenset[str]:
        supported = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"}
        normalised = frozenset(fmt.upper() for fmt in value)
        unknown = normalised - supported
        if unknown:
            raise ValueError(f"unsupported image formats: {sorted(unknown)}")
        if not normalised:
            raise ValueError("at least one image format must be allowed")
        return normalised

    @field_validator("llama_cpp_url", "extraction_llm_url", "invoice_fallback_llm_url", "invoice_webhook_url")
    @classmethod
    def _validate_llama_cpp_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator("fetch_allowed_schemes")
    @classmethod
    def _validate_schemes(cls, value: frozenset[str]) -> frozenset[str]:
        normalised = frozenset(s.lower() for s in value)
        if not normalised or not normalised <= {"http", "https"}:
            raise ValueError("fetch_allowed_schemes may only contain 'http' and 'https'")
        return normalised

    @model_validator(mode="after")
    def _idle_within_ttl(self) -> "Settings":
        if self.session_idle_timeout_hours > self.session_ttl_hours:
            raise ValueError("session_idle_timeout_hours must not exceed session_ttl_hours")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def signup_enabled(self) -> bool:
        return self.allow_signup if self.allow_signup is not None else not self.is_production

    @property
    def cookie_secure(self) -> bool:
        return self.session_cookie_secure if self.session_cookie_secure is not None else self.is_production


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
