#!/usr/bin/env python3
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from typing import Dict, List, Optional, TypedDict, Union, Any, cast
import copy
from jsonschema import validate, ValidationError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

UNA_SEGMENT = "UNA:+.? '"
ORDERS_MSG_TYPE = "ORDERS"
DATE_FORMAT = "102"

DATE_FORMATS: Dict[str, str] = {
    "102": "%Y%m%d",
    "203": "%Y%m%d%H%M",
    "101": "%y%m%d",
    "204": "%Y%m%d%H%M%S",
}

CONTROL_CHAR_REGEX = re.compile(r'[\x00-\x1F\x7F]')
ESCAPE_CHARS = ["'", "+", ":", "*"]

ORDER_SCHEMA = {
    "type": "object",
    "required": ["message_ref", "order_number", "order_date", "parties", "items"],
    "properties": {
        "message_ref": {"type": "string", "maxLength": 14},
        "order_number": {"type": "string", "maxLength": 35},
        "order_date": {"type": "string"},
        "delivery_date": {"type": "string"},
        "currency": {"type": "string", "maxLength": 3},
        "delivery_location": {"type": "string", "maxLength": 35},
        "payment_terms": {"type": "string", "maxLength": 35},
        "tax_rate": {"type": "number"},
        "special_instructions": {"type": "string"},
        "incoterms": {"type": "string", "maxLength": 3}
    }
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
    line_ending: str = "\n"
    include_una: bool = True
    sender_id: str = "SENDER"
    receiver_id: str = "RECEIVER"
    max_segment_length: int = 2000
    max_field_length: int = 70
    allowed_qualifiers: List[str] = None
    
    def __post_init__(self):
        if self.allowed_qualifiers is None:
            self.allowed_qualifiers = ["BY", "SU", "DP", "IV", "CB"]
        
        if not all(len(q) == 2 for q in self.allowed_qualifiers):
            raise ValueError("All qualifiers must be 2 characters")
        
        if self.max_segment_length < 10:
            raise ValueError("max_segment_length must be at least 10")

class EdifactGenerationError(Exception):
    def __init__(self, message: str, code: str = "EDIFACT_001", details: Optional[Dict] = None):
        self.code = code
        self.details = details or {}
        super().__init__(f"{code}: {message}")

class SegmentGenerator:
    @staticmethod
    def escape_edifact(value: Optional[str]) -> str:
        if value is None:
            return ""
        s = str(value)
        s = CONTROL_CHAR_REGEX.sub('', s)
        s = s.replace("?", "??")
        for char in ESCAPE_CHARS:
            s = s.replace(char, f"?{char}")
        return s

    @classmethod
    def validate_segment_length(cls, segment: str, config: EdifactConfig) -> None:
        if len(segment) > config.max_segment_length:
            raise EdifactGenerationError(
                f"Segment too long: {len(segment)} > {config.max_segment_length}",
                "SEGMENT_001",
                {"segment": segment[:100], "length": len(segment)}
            )

    @classmethod
    def validate_decimal_precision(cls, value: Decimal, config: EdifactConfig) -> None:
        precision = Decimal(config.decimal_rounding)
        if value != value.quantize(precision, rounding=ROUND_HALF_UP):
            raise EdifactGenerationError(
                f"Decimal value {value} exceeds configured precision {precision}",
                "VALID_009"
            )

    @classmethod
    def unb(cls, config: EdifactConfig, message_ref: str) -> str:
        timestamp = datetime.now().strftime("%y%m%d%H%M")
        segment = f"UNB+UNOA:2+{cls.escape_edifact(config.sender_id)}+{cls.escape_edifact(config.receiver_id)}+{timestamp}+{cls.escape_edifact(message_ref)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def una(cls, config: EdifactConfig) -> str:
        return config.una_segment

    @classmethod
    def unz(cls, message_count: int = 1, message_ref: str = "", config: Optional[EdifactConfig] = None) -> str:
        segment = f"UNZ+{message_count}+{cls.escape_edifact(message_ref)}'"
        if config:
            cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def unh(cls, message_ref: str, config: EdifactConfig) -> str:
        segment = f"UNH+{cls.escape_edifact(message_ref)}+{config.message_type}:{config.version}:{config.release}:{config.controlling_agency}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def bgm(cls, order_number: str, document_type: str = "220", config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"BGM+{document_type}+{cls.escape_edifact(order_number)}+9'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def dtm(cls, qualifier: str, date: str, date_format: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"DTM+{qualifier}:{cls.escape_edifact(date)}:{date_format}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def nad(cls, qualifier: str, party_id: str, name: Optional[str] = None, config: Optional[EdifactConfig] = None) -> List[str]:
        if config is None:
            config = EdifactConfig()
        base = f"NAD+{cls.escape_edifact(qualifier)}+{cls.escape_edifact(party_id)}::91"
        if name:
            if len(name) > config.max_field_length:
                name = name[:config.max_field_length]
            segment = f"{base}++{cls.escape_edifact(name)}'"
        else:
            segment = f"{base}'"
        
        cls.validate_segment_length(segment, config)
        return [segment]

    @classmethod
    def com(cls, contact: str, contact_type: str = "TE", config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"COM+{cls.escape_edifact(contact)}:{cls.escape_edifact(contact_type)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def lin(cls, line_num: int, product_code: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"LIN+{line_num}++{cls.escape_edifact(product_code)}:EN'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def imd(cls, description: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        if len(description) > config.max_field_length:
            description = description[:config.max_field_length]
        segment = f"IMD+F++:::{cls.escape_edifact(description)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def qty(cls, quantity: int, unit: str = "EA", config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"QTY+21:{quantity}:{cls.escape_edifact(unit)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def pri(cls, price: Decimal, config: EdifactConfig, unit: str = "EA") -> str:
        cls.validate_decimal_precision(price, config)
        q = price.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        segment = f"PRI+AAA:{q}:{cls.escape_edifact(unit)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def moa(cls, qualifier: str, amount: Decimal, config: EdifactConfig) -> str:
        cls.validate_decimal_precision(amount, config)
        q = amount.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        segment = f"MOA+{cls.escape_edifact(qualifier)}:{q}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def tax(cls, rate: Decimal, tax_type: str = "VAT", config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        cls.validate_decimal_precision(rate, config)
        if config:
            fmt_rate = rate.quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)
        else:
            fmt_rate = rate
        segment = f"TAX+7+{cls.escape_edifact(tax_type)}+++:::{fmt_rate}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def loc(cls, qualifier: str, location: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"LOC+{cls.escape_edifact(qualifier)}+{cls.escape_edifact(location)}:92'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def pai(cls, terms: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"PAI+{cls.escape_edifact(terms)}:3'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def tod(cls, incoterms: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"TOD+5++{cls.escape_edifact(incoterms)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def unt(cls, segment_count: int, message_ref: str, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        segment = f"UNT+{segment_count}+{cls.escape_edifact(message_ref)}'"
        cls.validate_segment_length(segment, config)
        return segment

    @classmethod
    def ftx(cls, text: str, qualifier: str = "AAI", sequence: int = 1, config: Optional[EdifactConfig] = None) -> str:
        if config is None:
            config = EdifactConfig()
        if len(text) > config.max_field_length:
            text = text[:config.max_field_length]
        segment = f"FTX+{qualifier}+{sequence}+++{cls.escape_edifact(text)}'"
        cls.validate_segment_length(segment, config)
        return segment

def validate_date(date_str: str, date_format: str) -> bool:
    fmt = DATE_FORMATS.get(date_format)
    if not fmt:
        return False
    try:
        datetime.strptime(date_str, fmt)
        return True
    except (ValueError, TypeError):
        return False

def sanitize_input(data: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = {}
    for key, value in data.items():
        if isinstance(value, str):
            sanitized[key] = CONTROL_CHAR_REGEX.sub('', value)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_input(value)
        elif isinstance(value, list):
            sanitized[key] = [sanitize_input(item) if isinstance(item, dict) else item for item in value]
        else:
            sanitized[key] = value
    return sanitized

def validate_with_schema(data: Dict[str, Any]) -> None:
    try:
        validate(instance=data, schema=ORDER_SCHEMA)
    except ValidationError as e:
        raise EdifactGenerationError(f"Schema validation failed: {e.message}", "SCHEMA_001")

def validate_order_data(data: Dict[str, Any], config: EdifactConfig) -> OrderData:
    data_copy = sanitize_input(copy.deepcopy(data))
    
    validate_with_schema(data_copy)
    
    required_fields = ["message_ref", "order_number", "order_date", "parties", "items"]
    missing_fields = [field for field in required_fields if field not in data_copy]
    if missing_fields:
        raise EdifactGenerationError(
            f"Missing required fields: {', '.join(missing_fields)}",
            "VALID_001",
            {"missing_fields": missing_fields}
        )

    if not isinstance(data_copy["items"], list) or not data_copy["items"]:
        raise EdifactGenerationError("At least one item is required", "VALID_002")

    if not validate_date(data_copy["order_date"], config.date_format):
        raise EdifactGenerationError(
            f"Invalid order_date format for {config.date_format}",
            "VALID_003",
            {"date": data_copy["order_date"], "format": config.date_format}
        )

    if "delivery_date" in data_copy and data_copy.get("delivery_date") and not validate_date(data_copy["delivery_date"], config.date_format):
        raise EdifactGenerationError(
            f"Invalid delivery_date format for {config.date_format}",
            "VALID_004",
            {"date": data_copy["delivery_date"], "format": config.date_format}
        )

    try:
        converted_items: List[OrderItem] = []
        for idx, item in enumerate(data_copy["items"]):
            if len(item.get("product_code", "")) > 35:
                raise EdifactGenerationError(
                    f"Product code too long in item {idx}",
                    "VALID_007",
                    {"item_index": idx, "field": "product_code", "length": len(item["product_code"])}
                )
            
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

    for idx, p in enumerate(data_copy.get("parties", [])):
        if "qualifier" not in p or "id" not in p:
            raise EdifactGenerationError(
                f"Party {idx} must contain qualifier and id",
                "VALID_006",
                {"party_index": idx}
            )
        
        if p["qualifier"] not in config.allowed_qualifiers:
            raise EdifactGenerationError(
                f"Invalid qualifier '{p['qualifier']}' in party {idx}",
                "VALID_008",
                {"party_index": idx, "qualifier": p["qualifier"], "allowed": config.allowed_qualifiers}
            )

    return cast(OrderData, data_copy)

def validate_file_path(filename: str) -> None:
    safe_filename = os.path.basename(filename)
    if safe_filename != filename:
        raise EdifactGenerationError("Invalid filename provided", "IO_002")
    
    if not filename.lower().endswith(('.edi', '.edifact')):
        logger.warning("Recommended file extension is .edi or .edifact")

def generate_edifact_orders(
    data: Dict[str, Any],
    config: EdifactConfig = EdifactConfig(),
    output_file: Optional[str] = None,
) -> str:
    logger.info(f"Starting EDIFACT generation for order {data.get('order_number', 'Unknown')}")
    
    try:
        validated_data = validate_order_data(data, config)
    except EdifactGenerationError as e:
        logger.error(f"Validation failed: {e.code} - {e}")
        if e.details:
            logger.error(f"Details: {e.details}")
        raise

    segments: List[Union[str, List[str]]] = []

    if config.include_una:
        segments.append(SegmentGenerator.una(config))

    segments.append(SegmentGenerator.unb(config, validated_data["message_ref"]))
    segments.extend([
        SegmentGenerator.unh(validated_data["message_ref"], config),
        SegmentGenerator.bgm(validated_data["order_number"], "220", config),
        SegmentGenerator.dtm("137", validated_data["order_date"], config.date_format, config)
    ])

    if validated_data.get("delivery_date"):
        segments.append(SegmentGenerator.dtm("2", validated_data["delivery_date"], config.date_format, config))

    if validated_data.get("currency"):
        currency_segment = f"CUX+2:{SegmentGenerator.escape_edifact(validated_data['currency'])}:9'"
        SegmentGenerator.validate_segment_length(currency_segment, config)
        segments.append(currency_segment)

    for party in validated_data["parties"]:
        segments.extend(SegmentGenerator.nad(
            party["qualifier"],
            party["id"],
            party.get("name"),
            config
        ))
        if party.get("address"):
            segments.append(SegmentGenerator.com(party["address"], "AD", config))
        if party.get("contact"):
            segments.append(SegmentGenerator.com(party["contact"], "TE", config))

    total_amount = Decimal("0.00")
    for idx, item in enumerate(validated_data["items"], 1):
        quantity = int(item["quantity"])
        price: Decimal = item["price"]
        unit = item.get("unit", "EA") or "EA"
        line_total = (price * Decimal(quantity)).quantize(Decimal(config.decimal_rounding), rounding=ROUND_HALF_UP)

        segments.extend([
            SegmentGenerator.lin(idx, item["product_code"], config),
            SegmentGenerator.imd(item["description"], config),
            SegmentGenerator.qty(quantity, unit, config),
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
        segments.append(SegmentGenerator.loc("11", validated_data["delivery_location"], config))

    if validated_data.get("payment_terms"):
        segments.append(SegmentGenerator.pai(validated_data["payment_terms"], config))

    if validated_data.get("incoterms"):
        segments.append(SegmentGenerator.tod(validated_data["incoterms"], config))

    if validated_data.get("special_instructions"):
        instructions = validated_data["special_instructions"]
        chunks = [instructions[i:i+config.max_field_length] for i in range(0, len(instructions), config.max_field_length)]
        for i, chunk in enumerate(chunks, 1):
            segments.append(SegmentGenerator.ftx(chunk, "AAI", i, config))

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
    flat_segments.append(SegmentGenerator.unt(segment_count, validated_data["message_ref"], config))
    flat_segments.append(SegmentGenerator.unz(1, validated_data["message_ref"], config))

    edifact_message = config.line_ending.join(flat_segments)

    logger.debug(f"Generated {len(flat_segments)} segments")

    if output_file:
        try:
            validate_file_path(output_file)
            with open(output_file, "w", encoding="utf-8", newline="") as f:
                f.write(edifact_message)
            logger.info(f"EDIFACT message written to {output_file}")
        except IOError as e:
            logger.error(f"Failed to write file: {e}")
            raise EdifactGenerationError("File write failed", "IO_001") from e

    return edifact_message

if __name__ == "__main__":
    from datetime import datetime, timedelta

    sample_order = {
        "message_ref": "ORD0001",
        "order_number": "2025-0509-A",
        "order_date": datetime.now().strftime(DATE_FORMATS["102"]),
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
        "delivery_date": (datetime.now() + timedelta(days=7)).strftime(DATE_FORMATS["102"]),
        "currency": "USD",
        "delivery_location": "WAREHOUSE1",
        "payment_terms": "NET30",
        "tax_rate": Decimal("7.5"),
        "special_instructions": "Please deliver during business hours 9AM-5PM. Contact John Doe at extension 123 for delivery coordination.",
        "incoterms": "FOB"
    }

    enhanced_config = EdifactConfig(
        version="4",
        release="22A",
        controlling_agency="ISO",
        line_ending="\r\n",
        sender_id="BUYER123",
        receiver_id="SUPPLIER456",
        max_field_length=70,
        max_segment_length=2000
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
        if e.details:
            print(f"Error details: {e.details}")
