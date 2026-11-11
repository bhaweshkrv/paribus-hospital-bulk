from app.csv_parser import parse_hospital_csv


def parse(text: str, max_rows: int = 20):
    return parse_hospital_csv(text.encode("utf-8"), max_rows=max_rows)


def test_parses_rows_with_optional_phone():
    result = parse(
        "name,address,phone\n"
        "General Hospital,1 Main St,555-0001\n"
        "City Clinic,2 Oak Ave,\n"
    )

    assert result.valid
    assert [(r.row, r.name, r.address, r.phone) for r in result.rows] == [
        (1, "General Hospital", "1 Main St", "555-0001"),
        (2, "City Clinic", "2 Oak Ave", None),
    ]


def test_phone_column_may_be_omitted_entirely():
    result = parse("name,address\nGeneral Hospital,1 Main St\n")

    assert result.valid
    assert result.rows[0].phone is None


def test_header_is_case_and_whitespace_insensitive_and_bom_is_stripped():
    content = "﻿ Name , ADDRESS ,Phone\nGeneral,1 Main St,555\n".encode("utf-8")

    result = parse_hospital_csv(content, max_rows=20)

    assert result.valid
    assert result.rows[0].name == "General"


def test_values_are_trimmed_and_quoted_commas_preserved():
    result = parse('name,address,phone\n  General  ,"1 Main St, Suite 4",  555  \n')

    assert result.rows[0].name == "General"
    assert result.rows[0].address == "1 Main St, Suite 4"
    assert result.rows[0].phone == "555"


def test_missing_required_column_is_a_file_level_error():
    result = parse("name,phone\nGeneral,555\n")

    assert not result.valid
    assert result.rows == []
    assert result.errors[0].row is None
    assert "address" in result.errors[0].message


def test_reports_every_invalid_row_not_just_the_first():
    result = parse("name,address\n,1 Main St\nGeneral,   \nFine,3 Elm St\n")

    assert not result.valid
    assert [(e.row, e.field) for e in result.errors] == [(1, "name"), (2, "address")]


def test_rejects_more_rows_than_the_limit():
    body = "".join(f"H{i},{i} Main St\n" for i in range(3))

    result = parse("name,address\n" + body, max_rows=2)

    assert not result.valid
    assert "3" in result.errors[0].message and "2" in result.errors[0].message


def test_accepts_exactly_the_row_limit():
    body = "".join(f"H{i},{i} Main St\n" for i in range(2))

    assert parse("name,address\n" + body, max_rows=2).valid


def test_header_only_file_is_rejected():
    result = parse("name,address,phone\n")

    assert not result.valid
    assert "no hospital rows" in result.errors[0].message.lower()


def test_empty_file_is_rejected():
    result = parse_hospital_csv(b"", max_rows=20)

    assert not result.valid
    assert "empty" in result.errors[0].message.lower()


def test_non_utf8_file_is_rejected():
    result = parse_hospital_csv("name,address\nCaf\xe9,1 St\n".encode("latin-1"), max_rows=20)

    assert not result.valid
    assert "utf-8" in result.errors[0].message.lower()


def test_row_with_extra_fields_is_rejected():
    result = parse("name,address,phone\nGeneral,1 Main St,555,unexpected\n")

    assert not result.valid
    assert result.errors[0].row == 1


def test_blank_lines_are_skipped():
    result = parse("name,address\n\nGeneral,1 Main St\n,,\n\nClinic,2 Oak Ave\n")

    assert result.valid
    assert [(r.row, r.name) for r in result.rows] == [(1, "General"), (2, "Clinic")]


def test_unknown_columns_are_ignored_with_a_warning():
    result = parse("name,address,website\nGeneral,1 Main St,example.com\n")

    assert result.valid
    assert "website" in result.warnings[0].message


def test_duplicate_rows_produce_a_warning_but_stay_valid():
    result = parse("name,address\nGeneral,1 Main St\ngeneral , 1 main st\n")

    assert result.valid
    assert len(result.rows) == 2
    assert result.warnings[0].row == 2


def test_field_beyond_csv_module_limit_is_reported_as_malformed():
    huge_address = "A" * 200_000  # over csv's default 128 KB field limit

    result = parse(f"name,address\nGeneral,{huge_address}\n")

    assert not result.valid
    assert result.errors[0].message.startswith("Malformed CSV")


def test_header_matching_ignores_case_but_values_keep_their_case():
    result = parse("NAME,Address\nSt. Mary's GENERAL,1 Main St\n")

    assert result.rows[0].name == "St. Mary's GENERAL"
