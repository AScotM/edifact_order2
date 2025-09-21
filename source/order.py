#!/usr/bin/env python3
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from typing import Dict, List, Optional, TypedDict, Union, Any, cast
import copy

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Constants
UNA_SEGMENT = "UNA:+.? '"
ORDERS_MSG_TYPE = "ORDERS"
DATE_FORMAT = "102"

# Supported EDIFACT date formats
DATE_FORMATS: Dict[str, str] = {
    "102": "%Y%m%d",
    "203": "%Y%m%d%H%M",
}

class OrderItem(TypedDict):
    product_code: str
    description: str
    quantity: int
    price: Decimal
    unit: Optional[str]

class OrderParty(TypedDict, total=False):
    qualifier: str
    id: str
    name: Optional[str]
    address: Optional[str]
    contact: Optional[str]

class OrderData(TypedDict, total=False):
    message_ref: str
    order_number: str
    order_date: str
    parties: List[OrderParty]
    items: List[OrderItem]
    delivery_date: Optional[str]
    currency: Optional[str]
    delivery_location: Optional[str]
    payment_terms: Optional[str]
    tax_rate: Optional[Decimal]
    special_instructions: Optional[str]
    incoterms: Optional[str]

@dataclass
class EdifactConfig:
    una_segment: str = UNA_SEGMENT
    message_type: str = ORDERS_MSG_TYPE
    date_format: str = DATE_FORMAT
    version: str = "D"
    release: str = "96A"
    controlling_agency: str = "UN"
    decimal_rounding: str = "0.01"
    line_ending: str = "\n"  # can be set to "\r\n" if required

class SegmentGenerator:
    """Helper class for EDIFACT segment generation"""

    @staticmethod
    def escape_edifact(value: Optional[str]) -> str:
        if value is None:
            return ""
        s = str(value)
        # escape '?' first
        s = s.replace("?", "??")
        for char in ["'", "+", ":", "*"]:
            s = s.replace(char, f"?{char}")
        return s

    @classmethod
    def una(cls, config: EdifactConfig) -> str:
        return config.una_segment

    @classmethod
    def unh(cls, message_ref: str, config: EdifactConfig) -> str:
        return f"UNH+{cls.escape_edifact(message_ref)}+{config.message_type}:{config.version}:{config.release}:{config.controlling_agency}'"

    @classmethod
    def bgm(cls, order_number: str) -> str:
        return f"BGM+220+{cls.escape_edifact(order_number)}+9'"

    @classmethod
    def dtm(cls, qualifier: str, date: str, date_format: str) -> str:
        return f"DTM+{qualifier}:{cls.escape_edifact(date)}:{date_format}'"

    @classmethod
    def nad(cls, qualifier: str, party_id: str, name: Optional[str] = None) -> List[str]:
        base = f"NAD+{cls.escape_edifact(qualifier)}+{cls.escape_edifact(party_id)}::91"
        if name:
            return [f"{base}++{cls.escape_edifact(name)}'"]
        return [f"{base}'"]

    @classmethod
    def com(cls, contact: str, contact_type: str = "TE") -> str:
        return f"COM+{cls.escape_edifact(contact)}:{cls.escape_edifact(contact_type)}'"

    @classmethod
    def lin(cls, line_num: int, product_code: str) -> str:
        return f"LIN+{line_num}++{cls.escape_edifact(product_code)}:EN'"

    @classmethod
    def imd(cls, description: str) -> str:
        return f"IMD+F++:::{cls.escape_edifact(description)}'"

    @classmethod
    def qty(cls, quantity: int, unit: str = "EA") -> str:
        return f"QTY+21:{quantity}:{cls.escape_edifact(unit)}'"

    @classmethod
    def pri(cls, price: Decimal, config: EdifactConfig, unit: str = "EA") -> str:
        q = price.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        return f"PRI+AAA:{q}:{cls.escape_edifact(unit)}'"

    @classmethod
    def moa(cls, qualifier: str, amount: Decimal, config: EdifactConfig) -> str:
        q = amount.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        return f"MOA+{cls.escape_edifact(qualifier)}:{q}'"

    @classmethod
    def tax(cls, rate: Decimal, tax_type: str = "VAT", config: Optional[EdifactConfig] = None) -> str:
        if config:
            fmt_rate = rate.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        else:
            fmt_rate = rate
        return f"TAX+7+{cls.escape_edifact(tax_type)}+++:::{fmt_rate}'"

    @classmethod
    def loc(cls, qualifier: str, location: str) -> str:
        return f"LOC+{cls.escape_edifact(qualifier)}+{cls.escape_edifact(location)}:92'"

    @classmethod
    def pai(cls, terms: str) -> str:
        return f"PAI+{cls.escape_edifact(terms)}:3'"

    @classmethod
    def tod(cls, incoterms: str) -> str:
        return f"TOD+5++{cls.escape_edifact(incoterms)}'"

    @classmethod
    def unt(cls, segment_count: int, message_ref: str) -> str:
        return f"UNT+{segment_count}+{cls.escape_edifact(message_ref)}'"

class EdifactGenerationError(Exception):
    def __init__(self, message: str, code: str = "EDIFACT_001"):
        self.code = code
        super().__init__(f"{code}: {message}")

def validate_date(date_str: str, date_format: str) -> bool:
    try:
        fmt = DATE_FORMATS.get(date_format)
        if not fmt:
            return False
        datetime.strptime(date_str, fmt)
        return True
    except (ValueError, TypeError):
        return False

def validate_order_data(data: Dict[str, Any], config: EdifactConfig) -> OrderData:
    data_copy = copy.deepcopy(data)
    required_fields = ["message_ref", "order_number", "order_date", "parties", "items"]
    if not all(field in data_copy for field in required_fields):
        raise EdifactGenerationError("Missing required fields", "VALID_001")

    if not isinstance(data_copy["items"], list) or not data_copy["items"]:
        raise EdifactGenerationError("At least one item is required", "VALID_002")

    if not validate_date(data_copy["order_date"], config.date_format):
        raise EdifactGenerationError(f"Invalid order_date format for {config.date_format}", "VALID_003")

    if "delivery_date" in data_copy and data_copy.get("delivery_date") and not validate_date(data_copy["delivery_date"], config.date_format):
        raise EdifactGenerationError(f"Invalid delivery_date format for {config.date_format}", "VALID_004")

    try:
        converted_items: List[OrderItem] = []
        for item in data_copy["items"]:
            converted_item: OrderItem = {
                "product_code": str(item["product_code"]),
                "description": str(item.get("description", "")),
                "quantity": int(item["quantity"]),
                "price": Decimal(str(item["price"])),
                "unit": str(item.get("unit", "EA"))
            }
            converted_items.append(converted_item)
        data_copy["items"] = converted_items

        if "tax_rate" in data_copy and data_copy.get("tax_rate") is not None:
            data_copy["tax_rate"] = Decimal(str(data_copy["tax_rate"]))
    except (ValueError, TypeError, KeyError) as e:
        raise EdifactGenerationError(f"Invalid numeric format: {str(e)}", "VALID_005")

    for p in data_copy.get("parties", []):
        if "qualifier" not in p or "id" not in p:
            raise EdifactGenerationError("Party entries must contain qualifier and id", "VALID_006")

    return cast(OrderData, data_copy)

def generate_edifact_orders(
    data: Dict[str, Any],
    config: EdifactConfig = EdifactConfig(),
    output_file: Optional[str] = None,
) -> str:
    try:
        validated_data = validate_order_data(data, config)
    except EdifactGenerationError as e:
        logger.error(f"Validation failed: {e}")
        raise

    segments: List[Union[str, List[str]]] = [
        SegmentGenerator.una(config),
        SegmentGenerator.unh(validated_data["message_ref"], config),
        SegmentGenerator.bgm(validated_data["order_number"]),
        SegmentGenerator.dtm("137", validated_data["order_date"], config.date_format)
    ]

    if validated_data.get("delivery_date"):
        segments.append(SegmentGenerator.dtm("2", validated_data["delivery_date"], config.date_format))

    if validated_data.get("currency"):
        segments.append(f"CUX+2:{SegmentGenerator.escape_edifact(validated_data['currency'])}:9'")

    for party in validated_data["parties"]:
        segments.extend(SegmentGenerator.nad(
            party["qualifier"],
            party["id"],
            party.get("name")
        ))
        if party.get("address"):
            segments.append(SegmentGenerator.com(party["address"], "AD"))
        if party.get("contact"):
            segments.append(SegmentGenerator.com(party["contact"], "TE"))

    total_amount = Decimal("0.00")
    for idx, item in enumerate(validated_data["items"], 1):
        quantity = int(item["quantity"])
        price: Decimal = item["price"]
        unit = item.get("unit", "EA") or "EA"
        line_total = (price * Decimal(quantity)).quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)

        segments.extend([
            SegmentGenerator.lin(idx, item["product_code"]),
            SegmentGenerator.imd(item["description"]),
            SegmentGenerator.qty(quantity, unit),
            SegmentGenerator.pri(price, config, unit)
        ])
        total_amount += line_total

    if validated_data.get("tax_rate") is not None:
        tax_rate: Decimal = validated_data["tax_rate"]
        tax_amount = (total_amount * tax_rate / Decimal("100")).quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        segments.extend([
            SegmentGenerator.tax(tax_rate, "VAT", config),
            SegmentGenerator.moa("124", tax_amount, config)
        ])
        total_amount += tax_amount

    if validated_data.get("delivery_location"):
        segments.append(SegmentGenerator.loc("11", validated_data["delivery_location"]))

    if validated_data.get("payment_terms"):
        segments.append(SegmentGenerator.pai(validated_data["payment_terms"]))

    if validated_data.get("incoterms"):
        segments.append(SegmentGenerator.tod(validated_data["incoterms"]))

    segments.append(SegmentGenerator.moa("79", total_amount, config))

    flat_segments: List[str] = []
    for seg in segments:
        if isinstance(seg, list):
            flat_segments.extend(seg)
        else:
            flat_segments.append(seg)

    unh_index = None
    for i, s in enumerate(flat_segments):
        if s.startswith("UNH+"):
            unh_index = i
            break

    if unh_index is None:
        raise EdifactGenerationError("UNH segment missing", "GEN_001")

    segment_count = len(flat_segments) - unh_index + 1
    flat_segments.append(SegmentGenerator.unt(segment_count, validated_data["message_ref"]))

    edifact_message = config.line_ending.join(flat_segments)

    if output_file:
        try:
            with open(output_file, "w", encoding="utf-8", newline="") as f:
                f.write(edifact_message)
            logger.info(f"EDIFACT message written to {output_file}")
        except IOError as e:
            logger.error(f"Failed to write file: {e}")
            raise EdifactGenerationError("File write failed", "IO_001") from e

    return edifact_message

# Example usage
if __name__ == "__main__":
    sample_order = {
        "message_ref": "ORD0001",
        "order_number": "2025-0509-A",
        "order_date": "20250509",
        "parties": [
            {
                "qualifier": "BY",
                "id": "1234567890123",
                "name": "Buyer Corp",
                "contact": "+123456789"
            },
            {
                "qualifier": "SU",
                "id": "3210987654321",
                "address": "Industrial?Park",
                "contact": "supplier@example.com"
            },
        ],
        "items": [
            {
                "product_code": "ITEM001",
                "description": "Widget A (Special)",
                "quantity": 10,
                "price": Decimal("12.50"),
                "unit": "EA"
            },
        ],
        "delivery_date": "20250515",
        "currency": "USD",
        "delivery_location": "WAREHOUSE1",
        "payment_terms": "NET30",
        "tax_rate": Decimal("7.5"),
        "incoterms": "FOB"
    }

    enhanced_config = EdifactConfig(
        version="4",
        release="22A",
        controlling_agency="ISO",
        line_ending="\r\n"
    )

    try:
        message = generate_edifact_orders(
            sample_order,
            config=enhanced_config,
            output_file="orders.edi"
        )
        print("\nGenerated EDIFACT ORDERS:\n", message)
    except EdifactGenerationError as e:
        print(f"Generation failed: {e.code} - {str(e)}")
