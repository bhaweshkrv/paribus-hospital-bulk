"""Parse and validate hospital CSV uploads.

Validation is all-or-nothing by design: the whole file is checked before a
single upstream call is made, so a bad upload can never leave a half-created
batch behind. Every problem is reported at once rather than failing on the
first one, so the client can fix the file in a single pass.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import List, Optional

REQUIRED_COLUMNS = ("name", "address")
OPTIONAL_COLUMNS = ("phone",)
KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass(frozen=True)
class HospitalRow:
    row: int  # 1-based data row number (header and blank lines excluded)
    name: str
    address: str
    phone: Optional[str] = None


@dataclass(frozen=True)
class CsvIssue:
    message: str
    row: Optional[int] = None  # None means the issue concerns the whole file
    field: Optional[str] = None


@dataclass
class CsvParseResult:
    rows: List[HospitalRow] = field(default_factory=list)
    errors: List[CsvIssue] = field(default_factory=list)
    warnings: List[CsvIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _fail(message: str) -> CsvParseResult:
    return CsvParseResult(errors=[CsvIssue(message)])


def parse_hospital_csv(content: bytes, max_rows: int) -> CsvParseResult:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _fail("File is not valid UTF-8 text.")

    if not text.strip():
        return _fail("File is empty.")

    reader = csv.reader(io.StringIO(text))
    try:
        header = [column.strip().lower() for column in next(reader)]
        records = [record for record in reader if any(cell.strip() for cell in record)]
    except csv.Error as exc:
        return _fail(f"Malformed CSV: {exc}")

    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        return _fail(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected header: {','.join(KNOWN_COLUMNS)}."
        )

    result = CsvParseResult()
    unknown = [column for column in header if column and column not in KNOWN_COLUMNS]
    if unknown:
        result.warnings.append(CsvIssue(f"Ignoring unknown column(s): {', '.join(unknown)}."))

    if not records:
        result.errors.append(CsvIssue("File contains no hospital rows."))
        return result
    if len(records) > max_rows:
        result.errors.append(
            CsvIssue(f"File contains {len(records)} hospitals; the maximum is {max_rows}.")
        )
        return result

    index = {column: header.index(column) for column in KNOWN_COLUMNS if column in header}
    seen = {}
    for row_number, record in enumerate(records, start=1):
        if len(record) > len(header):
            result.errors.append(
                CsvIssue(
                    f"Row has {len(record)} fields but the header has {len(header)}.",
                    row=row_number,
                )
            )
            continue

        values = {
            column: (record[i].strip() if i < len(record) else "") for column, i in index.items()
        }
        row_errors = [
            CsvIssue(f"'{column}' is required.", row=row_number, field=column)
            for column in REQUIRED_COLUMNS
            if not values[column]
        ]
        if row_errors:
            result.errors.extend(row_errors)
            continue

        row = HospitalRow(
            row=row_number,
            name=values["name"],
            address=values["address"],
            phone=values.get("phone") or None,
        )
        key = (row.name.lower(), row.address.lower())
        if key in seen:
            result.warnings.append(
                CsvIssue(f"Duplicate of row {seen[key]} (same name and address).", row=row_number)
            )
        else:
            seen[key] = row_number
        result.rows.append(row)

    if result.errors:
        result.rows = []
    return result
