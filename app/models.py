"""Pydantic schemas shared by the API, the vision layer and the card renderer."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONDITIONS = ("New", "Used", "Refurbished")


class Condition(str, Enum):
    new = "New"
    used = "Used"
    refurbished = "Refurbished"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Source(str, Enum):
    """Where a part number came from. Never silently upgrade this."""

    catalogue = "catalogue"
    model = "model"
    manual = "manual"


class VehicleFitment(BaseModel):
    """One vehicle application. Human-readable strings, not internal codes."""
    make: str = ""
    model: str = ""
    engine: str = ""
    years: str = ""

    def label(self) -> str:
        head = " ".join(x for x in (self.make, self.model) if x).strip()
        tail = " · ".join(x for x in (self.engine, self.years) if x)
        if head and tail:
            return f"{head} · {tail}"
        return head or tail

    def short_label(self) -> str:
        bits = [self.make, self.model, self.engine]
        return " ".join(b for b in bits if b)


class PartNumber(BaseModel):
    number: str
    kind: Literal["primary", "alternate", "superseded"] = "alternate"
    note: str = ""
    source: Source = Source.catalogue
    confidence: Confidence = Confidence.medium


class SearchSource(BaseModel):
    """A web page the identification was grounded in (Google Search results)."""

    title: str = ""
    url: str = ""


class IdentificationReport(BaseModel):
    """Module 1 output: what the engine believes the part is."""

    part_name: str = ""
    category: str = ""
    oem_part_number: str = ""
    alternate_numbers: list[str] = Field(default_factory=list)
    superseded_numbers: list[str] = Field(default_factory=list)
    number_confidence: Confidence = Confidence.low
    fitment: list[VehicleFitment] = Field(default_factory=list)
    technical_notes: list[str] = Field(default_factory=list)
    visual_evidence: list[str] = Field(default_factory=list)
    overall_confidence: Confidence = Confidence.low
    provider: str = "offline"
    model_name: str = ""
    needs_manual_review: bool = True
    warnings: list[str] = Field(default_factory=list)
    raw_model_output: str | None = None
    # Provenance: set when the model was allowed to search the web.
    grounded: bool = False
    search_queries: list[str] = Field(default_factory=list)
    sources: list[SearchSource] = Field(default_factory=list)

    def primary_fitment_label(self) -> str:
        if not self.fitment:
            return ""
        return self.fitment[0].label()


class InventoryPart(BaseModel):
    """Module 2 record: the stored, sellable article."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    part_name: str
    oem_part_number: str = ""
    alternate_numbers: list[str] = Field(default_factory=list)
    superseded_numbers: list[str] = Field(default_factory=list)
    fitment: list[VehicleFitment] = Field(default_factory=list)
    condition: Condition = Condition.new
    bin_location: str = ""
    quantity: int = 0
    unit_price: float = 0.0
    currency: str = "KES"
    category: str = ""
    technical_notes: list[str] = Field(default_factory=list)
    image_path: str = ""
    verified: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_received_at: datetime | None = None

    @field_validator("quantity")
    @classmethod
    def _qty_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("quantity cannot be negative")
        return value

    @field_validator("unit_price")
    @classmethod
    def _price_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("unit_price cannot be negative")
        return value

    def in_stock(self) -> bool:
        return self.quantity > 0

    def fitment_labels(self, limit: int = 3) -> list[str]:
        out = [f.label() for f in self.fitment if f.label()]
        return out[:limit]


class PartCreate(BaseModel):
    part_name: str
    oem_part_number: str = ""
    alternate_numbers: list[str] = Field(default_factory=list)
    superseded_numbers: list[str] = Field(default_factory=list)
    fitment: list[VehicleFitment] = Field(default_factory=list)
    condition: Condition = Condition.new
    bin_location: str = ""
    quantity: int = 0
    unit_price: float = 0.0
    currency: str = "KES"
    category: str = ""
    technical_notes: list[str] = Field(default_factory=list)
    image_path: str = ""
    verified: bool = False
    # Free text like "Jeep Grand Cherokee 3.6L V6 2011-2021". Parsed server-side
    # with the same parser the CSV importer uses, so "Land Rover" and
    # "Mercedes-Benz" are not split into a one-word make.
    fitment_text: str = ""


class PartUpdate(BaseModel):
    part_name: str | None = None
    oem_part_number: str | None = None
    alternate_numbers: list[str] | None = None
    superseded_numbers: list[str] | None = None
    fitment: list[VehicleFitment] | None = None
    condition: Condition | None = None
    bin_location: str | None = None
    quantity: int | None = None
    unit_price: float | None = None
    currency: str | None = None
    category: str | None = None
    technical_notes: list[str] | None = None
    image_path: str | None = None
    verified: bool | None = None
    fitment_text: str | None = None


class ShopProfileUpdate(BaseModel):
    shop_name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(default="", max_length=140)
    whatsapp_number: str = Field(default="", max_length=40)
    currency: str = Field(default="KES", min_length=1, max_length=8)
    location: str = Field(default="", max_length=120)
    social_links: dict[str, str] = Field(default_factory=dict)

    @field_validator("social_links")
    @classmethod
    def _valid_social_links(cls, value: dict[str, str]) -> dict[str, str]:
        from urllib.parse import urlsplit

        allowed = {"facebook", "instagram", "tiktok", "x"}
        links: dict[str, str] = {}
        for key, raw in value.items():
            if key not in allowed:
                raise ValueError(f"unsupported social link: {key}")
            url = str(raw or "").strip()
            if url:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise ValueError(f"{key} link must be a complete https:// URL")
                if len(url) > 500:
                    raise ValueError(f"{key} link is too long")
            links[key] = url
        return {key: links.get(key, "") for key in allowed}


class StockMovement(BaseModel):
    """Audit trail for every quantity change."""

    part_id: int
    delta: int
    reason: str = "adjustment"
    note: str = ""
    created_at: datetime | None = None


class ScanRecord(BaseModel):
    id: int | None = None
    image_path: str
    report: IdentificationReport
    matched_part_id: int | None = None
    created_at: datetime | None = None


class CardRequest(BaseModel):
    part_id: int | None = None
    part: PartCreate | None = None
    formats: list[Literal["story", "square", "wide", "bin_label"]] = Field(
        default_factory=lambda: ["story", "square", "wide"]
    )
    headline: str = ""
    image_path: str = ""
    caption: str = ""
    include_caption: bool = True


class CardAsset(BaseModel):
    format: str
    width: int
    height: int
    filename: str
    url: str


class CardResult(BaseModel):
    part: InventoryPart
    cards: list[CardAsset]
    caption: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IdentifyResponse(BaseModel):
    scan_id: int | None = None
    image_path: str
    image_url: str = ""
    report: IdentificationReport
    matched_part: InventoryPart | None = None
    candidates: list[InventoryPart] = Field(default_factory=list)
    report_markdown: str = ""
    lens_url: str = ""
    google_search_url: str = ""


class DemoImage(BaseModel):
    name: str
    path: str
    url: str


class ImageApproval(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
