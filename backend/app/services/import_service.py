import csv
import io
import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from smart_commissioning_core.db.repositories import ImportRepository
from sqlalchemy.engine import Engine

from app.core.db import get_engine
from app.core.runtime import IMPORT_FILES_ROOT, ensure_runtime_directories
from app.schemas.imports import (
    ImportBatchSummary,
    ImportErrorRecord,
    ImportErrorReport,
    ImportProfileSummary,
    ImportType,
)

ALLOWED_UNITS = {
    "",
    "degrees-celsius",
    "percent",
    "percent-relative-humidity",
    "parts-per-million",
    "minutes",
    "seconds",
    "no-units",
    "pa",
    "cfm",
    "kwh",
    "kw",
}


@dataclass(frozen=True)
class ImportProfile:
    import_type: ImportType
    description: str
    required_columns: tuple[str, ...]
    duplicate_key_fields: tuple[str, ...]
    row_validator: Callable[[dict[str, str], int], list[ImportErrorRecord]]

    def as_summary(self) -> ImportProfileSummary:
        return ImportProfileSummary(
            import_type=self.import_type,
            description=self.description,
            required_columns=list(self.required_columns),
            duplicate_key_fields=list(self.duplicate_key_fields),
        )


def _normalize_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _validate_ip(row: dict[str, str], row_number: int, field: str) -> list[ImportErrorRecord]:
    value = row.get(field, "").strip()
    if not value:
        return []
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return [ImportErrorRecord(row_number=row_number, field=field, code="invalid_ip", message=f"{field} is not a valid IP address.")]
    return []


def _validate_numeric(row: dict[str, str], row_number: int, field: str) -> list[ImportErrorRecord]:
    value = row.get(field, "").strip()
    if not value:
        return []
    if not value.isdigit():
        return [ImportErrorRecord(row_number=row_number, field=field, code="invalid_numeric", message=f"{field} must be numeric.")]
    return []


def _validate_units(row: dict[str, str], row_number: int, field: str) -> list[ImportErrorRecord]:
    value = row.get(field, "").strip().casefold()
    if value in ALLOWED_UNITS:
        return []
    return [ImportErrorRecord(row_number=row_number, field=field, code="invalid_unit", message=f"{field} is not a recognized unit.")]


def _validate_topic(row: dict[str, str], row_number: int, field: str) -> list[ImportErrorRecord]:
    value = row.get(field, "").strip()
    if not value:
        return []
    if " " in value or "/" not in value:
        return [ImportErrorRecord(row_number=row_number, field=field, code="invalid_topic", message=f"{field} must be a valid MQTT topic path.")]
    return []


def _base_row_validation(required_columns: tuple[str, ...], row: dict[str, str], row_number: int) -> list[ImportErrorRecord]:
    errors: list[ImportErrorRecord] = []
    for column in required_columns:
        if not row.get(column, "").strip():
            errors.append(
                ImportErrorRecord(
                    row_number=row_number,
                    field=column,
                    code="empty_required_field",
                    message=f"{column} must not be empty.",
                )
            )
    return errors


def _make_validator(
    required_columns: tuple[str, ...],
    extra_checks: tuple[Callable[[dict[str, str], int], list[ImportErrorRecord]], ...],
) -> Callable[[dict[str, str], int], list[ImportErrorRecord]]:
    def validator(row: dict[str, str], row_number: int) -> list[ImportErrorRecord]:
        errors = _base_row_validation(required_columns, row, row_number)
        for check in extra_checks:
            errors.extend(check(row, row_number))
        return errors

    return validator


def _field_check(field: str, fn: Callable[[dict[str, str], int, str], list[ImportErrorRecord]]) -> Callable[[dict[str, str], int], list[ImportErrorRecord]]:
    def wrapper(row: dict[str, str], row_number: int) -> list[ImportErrorRecord]:
        return fn(row, row_number, field)

    return wrapper


PROFILES: dict[ImportType, ImportProfile] = {
    "ip_register": ImportProfile(
        import_type="ip_register",
        description="Expected network-connected device register for IP discovery.",
        required_columns=(
            "Project/site",
            "System",
            "Asset ID",
            "Asset name",
            "Expected IP address",
            "Expected hostname",
            "Expected services/ports",
        ),
        duplicate_key_fields=("Asset ID", "Expected IP address"),
        row_validator=_make_validator(
            (
                "Project/site",
                "System",
                "Asset ID",
                "Asset name",
                "Expected IP address",
                "Expected hostname",
                "Expected services/ports",
            ),
            (_field_check("Expected IP address", _validate_ip),),
        ),
    ),
    "bacnet_register": ImportProfile(
        import_type="bacnet_register",
        description="Expected BACnet device register for BACnet discovery.",
        required_columns=(
            "Project/site",
            "System",
            "Asset ID",
            "Asset name",
            "BACnet device instance",
            "BACnet network",
            "IP address",
        ),
        duplicate_key_fields=("Asset ID", "BACnet device instance"),
        row_validator=_make_validator(
            (
                "Project/site",
                "System",
                "Asset ID",
                "Asset name",
                "BACnet device instance",
                "BACnet network",
                "IP address",
            ),
            (
                _field_check("IP address", _validate_ip),
                _field_check("BACnet device instance", _validate_numeric),
                _field_check("BACnet network", _validate_numeric),
            ),
        ),
    ),
    "mqtt_register": ImportProfile(
        import_type="mqtt_register",
        description="Expected MQTT asset and topic register for MQTT discovery.",
        required_columns=(
            "Project/site",
            "System",
            "Asset ID",
            "Asset name",
            "Expected topic",
            "Payload type",
            "Expected schema version",
            "Expected points",
            "Expected units",
            "Expected reporting interval",
            "Source protocol",
            "Notes",
        ),
        duplicate_key_fields=("Asset ID", "Expected topic"),
        row_validator=_make_validator(
            (
                "Project/site",
                "System",
                "Asset ID",
                "Asset name",
                "Expected topic",
                "Payload type",
                "Expected schema version",
                "Expected points",
                "Expected units",
                "Expected reporting interval",
                "Source protocol",
                "Notes",
            ),
            (
                _field_check("Expected topic", _validate_topic),
                _field_check("Expected reporting interval", _validate_numeric),
            ),
        ),
    ),
    "asset_validation": ImportProfile(
        import_type="asset_validation",
        description="Asset-level validation file for expected online and integration status.",
        required_columns=(
            "Project/site",
            "System",
            "Asset ID",
            "Asset name",
            "Source protocol",
            "Expected online status",
            "Expected topic or device reference",
            "Location",
        ),
        duplicate_key_fields=("Asset ID",),
        row_validator=_make_validator(
            (
                "Project/site",
                "System",
                "Asset ID",
                "Asset name",
                "Source protocol",
                "Expected online status",
                "Expected topic or device reference",
                "Location",
            ),
            (),
        ),
    ),
    "bacnet_points": ImportProfile(
        import_type="bacnet_points",
        description="Expected BACnet point validation register.",
        required_columns=(
            "Asset ID",
            "Device instance",
            "BACnet network",
            "Object type",
            "Object instance",
            "Object name",
            "Expected point name",
            "Expected units",
            "Expected value type",
            "Required/optional flag",
        ),
        duplicate_key_fields=("Asset ID", "Object instance", "Expected point name"),
        row_validator=_make_validator(
            (
                "Asset ID",
                "Device instance",
                "BACnet network",
                "Object type",
                "Object instance",
                "Object name",
                "Expected point name",
                "Expected units",
                "Expected value type",
                "Required/optional flag",
            ),
            (
                _field_check("Device instance", _validate_numeric),
                _field_check("BACnet network", _validate_numeric),
                _field_check("Object instance", _validate_numeric),
                _field_check("Expected units", _validate_units),
            ),
        ),
    ),
    "mqtt_points": ImportProfile(
        import_type="mqtt_points",
        description="Expected MQTT point extraction validation register.",
        required_columns=(
            "Asset ID",
            "Topic",
            "Payload type",
            "JSON path or field name",
            "Expected point name",
            "Expected units",
            "Expected value type",
            "Required/optional flag",
            "Expected reporting interval",
        ),
        duplicate_key_fields=("Asset ID", "JSON path or field name", "Expected point name"),
        row_validator=_make_validator(
            (
                "Asset ID",
                "Topic",
                "Payload type",
                "JSON path or field name",
                "Expected point name",
                "Expected units",
                "Expected value type",
                "Required/optional flag",
                "Expected reporting interval",
            ),
            (
                _field_check("Topic", _validate_topic),
                _field_check("Expected units", _validate_units),
                _field_check("Expected reporting interval", _validate_numeric),
            ),
        ),
    ),
    "mapping": ImportProfile(
        import_type="mapping",
        description="BACnet-to-MQTT mapping validation file.",
        required_columns=(
            "Asset ID",
            "BACnet device instance",
            "BACnet object type",
            "BACnet object instance",
            "BACnet object name",
            "BACnet units",
            "MQTT topic",
            "MQTT field/path",
            "MQTT units",
            "Tolerance",
            "Mapping required flag",
        ),
        duplicate_key_fields=("Asset ID", "BACnet object instance", "MQTT field/path"),
        row_validator=_make_validator(
            (
                "Asset ID",
                "BACnet device instance",
                "BACnet object type",
                "BACnet object instance",
                "BACnet object name",
                "BACnet units",
                "MQTT topic",
                "MQTT field/path",
                "MQTT units",
                "Tolerance",
                "Mapping required flag",
            ),
            (
                _field_check("BACnet device instance", _validate_numeric),
                _field_check("BACnet object instance", _validate_numeric),
                _field_check("MQTT topic", _validate_topic),
                _field_check("BACnet units", _validate_units),
                _field_check("MQTT units", _validate_units),
            ),
        ),
    ),
    "tolerances": ImportProfile(
        import_type="tolerances",
        description="Point-level tolerances used by comparison validation.",
        required_columns=("Asset ID", "Point name", "Tolerance"),
        duplicate_key_fields=("Asset ID", "Point name"),
        row_validator=_make_validator(
            ("Asset ID", "Point name", "Tolerance"),
            (_field_check("Tolerance", _validate_numeric),),
        ),
    ),
}

EXAMPLE_ROWS: dict[ImportType, dict[str, str]] = {
    "ip_register": {
        "Project/site": "ElectraCom / Block B Plantroom",
        "System": "BMS",
        "Asset ID": "AHU-L03-017",
        "Asset name": "Level 3 AHU",
        "Expected IP address": "10.10.25.117",
        "Expected hostname": "ahu-l03-017",
        "Expected services/ports": "47808/udp, 443/tcp",
    },
    "bacnet_register": {
        "Project/site": "ElectraCom / Block B Plantroom",
        "System": "BMS",
        "Asset ID": "AHU-L03-017",
        "Asset name": "Level 3 AHU",
        "BACnet device instance": "1532117",
        "BACnet network": "1532",
        "IP address": "10.10.25.117",
    },
    "mqtt_register": {
        "Project/site": "ElectraCom / Block B Plantroom",
        "System": "BMS",
        "Asset ID": "MTR-ENERGY-009",
        "Asset name": "Energy Meter 9",
        "Expected topic": "electracom/sct/1532/meter/009/events/pointset",
        "Payload type": "pointset",
        "Expected schema version": "1.5.2",
        "Expected points": "energy_sensor",
        "Expected units": "kwh",
        "Expected reporting interval": "60",
        "Source protocol": "MQTT",
        "Notes": "Default commissioning import example.",
    },
    "asset_validation": {
        "Project/site": "ElectraCom / Block B Plantroom",
        "System": "BMS",
        "Asset ID": "AHU-L03-017",
        "Asset name": "Level 3 AHU",
        "Source protocol": "BACnet + MQTT",
        "Expected online status": "Online",
        "Expected topic or device reference": "1532117",
        "Location": "Level 3 plantroom",
    },
    "bacnet_points": {
        "Asset ID": "AHU-L03-017",
        "Device instance": "1532117",
        "BACnet network": "1532",
        "Object type": "analogInput",
        "Object instance": "300001",
        "Object name": "supply_air_temperature",
        "Expected point name": "supply_air_temperature_sensor",
        "Expected units": "degrees-celsius",
        "Expected value type": "number",
        "Required/optional flag": "required",
    },
    "mqtt_points": {
        "Asset ID": "AHU-L03-017",
        "Topic": "electracom/sct/1532/ahu/l03/events/pointset",
        "Payload type": "pointset",
        "JSON path or field name": "pointset.points.supply_air_temperature_sensor.present_value",
        "Expected point name": "supply_air_temperature_sensor",
        "Expected units": "degrees-celsius",
        "Expected value type": "number",
        "Required/optional flag": "required",
        "Expected reporting interval": "60",
    },
    "mapping": {
        "Asset ID": "AHU-L03-017",
        "BACnet device instance": "1532117",
        "BACnet object type": "analogInput",
        "BACnet object instance": "300001",
        "BACnet object name": "supply_air_temperature",
        "BACnet units": "degrees-celsius",
        "MQTT topic": "electracom/sct/1532/ahu/l03/events/pointset",
        "MQTT field/path": "pointset.points.supply_air_temperature_sensor.present_value",
        "MQTT units": "degrees-celsius",
        "Tolerance": "0.5",
        "Mapping required flag": "required",
    },
    "tolerances": {
        "Asset ID": "AHU-L03-017",
        "Point name": "supply_air_temperature_sensor",
        "Tolerance": "0.5",
    },
}


class ImportService:
    """Import batches with database-backed metadata.

    Summary/accepted_rows/errors live in the database (ImportRepository); the
    uploaded original file is still written to disk under the imports root.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        ensure_runtime_directories()
        self._repository = ImportRepository(engine if engine is not None else get_engine())

    def list_profiles(self) -> list[ImportProfileSummary]:
        return [profile.as_summary() for profile in PROFILES.values()]

    def build_template(self, import_type: ImportType, file_type: str) -> bytes:
        profile = PROFILES[import_type]
        example = EXAMPLE_ROWS[import_type]
        if file_type == "csv":
            return self._build_csv_template(profile, example)
        if file_type == "xlsx":
            return self._build_xlsx_template(profile, example)
        raise ValueError("Template format must be csv or xlsx.")

    def create_import(
        self,
        *,
        import_type: ImportType,
        file_name: str,
        file_bytes: bytes,
        project_id: str | None,
        site_id: str | None,
    ) -> tuple[ImportBatchSummary, ImportErrorReport]:
        profile = PROFILES[import_type]
        file_type = self._detect_file_type(file_name)
        rows = self._parse_rows(file_type=file_type, file_bytes=file_bytes)

        import_id = f"imp_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{token_hex(4)}"
        stored_file_name = f"{import_id}_{Path(file_name).name}"
        stored_path = IMPORT_FILES_ROOT / stored_file_name
        stored_path.write_bytes(file_bytes)

        mapped_rows, missing_columns = self._canonicalize_rows(profile, rows)
        errors: list[ImportErrorRecord] = []
        accepted_rows: list[dict[str, str]] = []

        if missing_columns:
            errors.extend(
                ImportErrorRecord(
                    field=column,
                    code="missing_required_column",
                    message=f"Required column '{column}' is missing.",
                )
                for column in missing_columns
            )
        else:
            seen_keys: set[tuple[str, ...]] = set()
            for row_number, row in enumerate(mapped_rows, start=2):
                row_errors = profile.row_validator(row, row_number)
                duplicate_key = tuple(row.get(field, "").strip() for field in profile.duplicate_key_fields)
                if duplicate_key and any(duplicate_key):
                    if duplicate_key in seen_keys:
                        row_errors.append(
                            ImportErrorRecord(
                                row_number=row_number,
                                code="duplicate_row",
                                message=f"Duplicate record detected for key fields {', '.join(profile.duplicate_key_fields)}.",
                            )
                        )
                    else:
                        seen_keys.add(duplicate_key)
                if row_errors:
                    errors.extend(row_errors)
                else:
                    accepted_rows.append(row)

        status = self._status(
            total_rows=len(mapped_rows),
            accepted_rows=len(accepted_rows),
            missing_columns=missing_columns,
        )
        summary = ImportBatchSummary(
            import_id=import_id,
            import_type=import_type,
            file_name=Path(file_name).name,
            file_type=file_type,
            project_id=project_id,
            site_id=site_id,
            total_rows=len(mapped_rows),
            accepted_rows=len(accepted_rows),
            rejected_rows=max(len(mapped_rows) - len(accepted_rows), 0),
            status=status,
            missing_columns=missing_columns,
            stored_file_name=stored_file_name,
            created_at=datetime.now(UTC),
        )
        error_report = ImportErrorReport(import_id=import_id, errors=errors)

        self._repository.create(
            import_id=import_id,
            import_type=import_type,
            project_id=project_id,
            site_id=site_id,
            original_filename=Path(file_name).name,
            stored_file_path=str(stored_path),
            summary=summary.model_dump(mode="json"),
            accepted_rows=accepted_rows,
            errors=[error.model_dump(mode="json") for error in errors],
            created_at=summary.created_at,
        )

        return summary, error_report

    def get_import(self, import_id: str) -> ImportBatchSummary:
        return ImportBatchSummary.model_validate(self._repository.get_summary(import_id))

    def get_import_errors(self, import_id: str) -> ImportErrorReport:
        return ImportErrorReport.model_validate(self._repository.get_errors(import_id))

    def _detect_file_type(self, file_name: str) -> str:
        suffix = Path(file_name).suffix.lower()
        if suffix == ".csv":
            return "csv"
        if suffix == ".xlsx":
            return "xlsx"
        raise ValueError("Only CSV and XLSX imports are supported.")

    def _parse_rows(self, *, file_type: str, file_bytes: bytes) -> list[dict[str, str]]:
        if file_type == "csv":
            return self._parse_csv(file_bytes)
        if file_type == "xlsx":
            return self._parse_xlsx(file_bytes)
        raise ValueError(f"Unsupported file type: {file_type}")

    def _parse_csv(self, file_bytes: bytes) -> list[dict[str, str]]:
        text = file_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("CSV file is missing a header row.")
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {_normalize_header(key): str(value or "").strip() for key, value in raw_row.items() if key is not None}
            if any(value.strip() for value in row.values()):
                rows.append(row)
        return rows

    def _parse_xlsx(self, file_bytes: bytes) -> list[dict[str, str]]:
        workbook = load_workbook(filename=io.BytesIO(file_bytes), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        try:
            headers = [_normalize_header(value) for value in next(iterator)]
        except StopIteration as error:
            raise ValueError("XLSX file is empty.") from error
        rows: list[dict[str, str]] = []
        for values in iterator:
            row = {headers[index]: str(value or "").strip() for index, value in enumerate(values)}
            if any(value.strip() for value in row.values()):
                rows.append(row)
        return rows

    def _canonicalize_rows(self, profile: ImportProfile, rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
        if not rows:
            return [], list(profile.required_columns)

        first_row = rows[0]
        header_lookup = {_normalize_key(header): header for header in first_row.keys()}
        missing_columns = [column for column in profile.required_columns if _normalize_key(column) not in header_lookup]

        mapped_rows: list[dict[str, str]] = []
        for row in rows:
            mapped_row: dict[str, str] = {}
            for header, value in row.items():
                mapped_row[header] = value.strip()
            for required in profile.required_columns:
                source_header = header_lookup.get(_normalize_key(required))
                if source_header is not None:
                    mapped_row[required] = row.get(source_header, "").strip()
            mapped_rows.append(mapped_row)

        return mapped_rows, missing_columns

    def _status(self, *, total_rows: int, accepted_rows: int, missing_columns: list[str]) -> str:
        if missing_columns or total_rows == 0 or accepted_rows == 0:
            return "rejected"
        if accepted_rows == total_rows:
            return "accepted"
        return "partial"

    def _build_csv_template(self, profile: ImportProfile, example: dict[str, str]) -> bytes:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(profile.required_columns), lineterminator="\n")
        writer.writeheader()
        writer.writerow({column: example.get(column, "") for column in profile.required_columns})
        return buffer.getvalue().encode("utf-8-sig")

    def _build_xlsx_template(self, profile: ImportProfile, example: dict[str, str]) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Default import template"
        sheet.append(list(profile.required_columns))
        sheet.append([example.get(column, "") for column in profile.required_columns])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        header_fill = PatternFill(fill_type="solid", fgColor="DCEBFF")
        header_font = Font(bold=True, color="1D1D1F")
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font

        for column_cells in sheet.columns:
            values = [str(cell.value or "") for cell in column_cells]
            width = min(max(max(len(value) for value in values) + 2, 14), 42)
            sheet.column_dimensions[column_cells[0].column_letter].width = width

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
