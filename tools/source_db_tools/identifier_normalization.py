"""Shared identifier normalization helpers for canonical source/work records."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


DOI_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
ISBN_RE = re.compile(r"^[0-9]{9}[0-9Xx]$|^[0-9]{13}$")
ISSN_RE = re.compile(r"^[0-9]{7}[0-9Xx]$")
ORCID_RE = re.compile(r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[0-9Xx])$", re.IGNORECASE)
WIKIDATA_RE = re.compile(r"^(Q\d+)$", re.IGNORECASE)
SUPPORTED_SCHEMES = {"doi", "isbn", "issn", "orcid", "wikidata", "url", "http", "https", "local"}


def normalize_scheme(value: Any) -> str:
    return str(value or "").strip().lower()


def _result(
    *,
    scheme: str,
    raw_value: str,
    value: str | None,
    normalized_uri: str | None,
    validity_status: str,
    validation_warning: str | None,
) -> dict[str, Any]:
    normalized_value = value or raw_value.strip()
    stored_value = normalized_value if normalized_value else raw_value.strip()
    return {
        "scheme": scheme,
        "value": stored_value,
        "raw_value": raw_value,
        "normalized_value": normalized_value,
        "normalized_uri": normalized_uri,
        "validity_status": validity_status,
        "validation_warning": validation_warning,
    }


def identifier_storage_values(scheme: Any, value: Any) -> dict[str, Any]:
    raw_scheme = normalize_scheme(scheme)
    raw_value = str(value or "").strip()
    if not raw_scheme:
        return _result(
            scheme="",
            raw_value=raw_value,
            value=raw_value or None,
            normalized_uri=None,
            validity_status="invalid",
            validation_warning="missing identifier scheme",
        )
    if not raw_value:
        return _result(
            scheme=raw_scheme,
            raw_value=raw_value,
            value=None,
            normalized_uri=None,
            validity_status="invalid",
            validation_warning="missing identifier value",
        )
    if raw_scheme not in SUPPORTED_SCHEMES:
        return _result(
            scheme=raw_scheme,
            raw_value=raw_value,
            value=raw_value,
            normalized_uri=None,
            validity_status="unsupported_scheme",
            validation_warning=f"unsupported identifier scheme: {raw_scheme}",
        )

    if raw_scheme == "local":
        return _result(
            scheme="local",
            raw_value=raw_value,
            value=raw_value,
            normalized_uri=None,
            validity_status="valid",
            validation_warning=None,
        )

    if raw_scheme == "doi":
        match = DOI_RE.match(raw_value)
        if not match:
            return _result(
                scheme="doi",
                raw_value=raw_value,
                value=raw_value.lower(),
                normalized_uri=None,
                validity_status="invalid",
                validation_warning="DOI must match 10.<registrant>/<suffix>",
            )
        normalized = match.group(1).lower()
        return _result(
            scheme="doi",
            raw_value=raw_value,
            value=normalized,
            normalized_uri=f"https://doi.org/{normalized}",
            validity_status="valid",
            validation_warning=None,
        )

    if raw_scheme == "isbn":
        normalized = re.sub(r"[^0-9Xx]", "", raw_value).upper()
        if not ISBN_RE.fullmatch(normalized):
            return _result(
                scheme="isbn",
                raw_value=raw_value,
                value=normalized,
                normalized_uri=None,
                validity_status="invalid",
                validation_warning="ISBN must normalize to 10 or 13 characters",
            )
        return _result(
            scheme="isbn",
            raw_value=raw_value,
            value=normalized,
            normalized_uri=f"urn:isbn:{normalized}",
            validity_status="valid",
            validation_warning=None,
        )

    if raw_scheme == "issn":
        normalized = re.sub(r"[^0-9Xx]", "", raw_value).upper()
        if not ISSN_RE.fullmatch(normalized):
            return _result(
                scheme="issn",
                raw_value=raw_value,
                value=normalized,
                normalized_uri=None,
                validity_status="invalid",
                validation_warning="ISSN must normalize to 8 characters",
            )
        return _result(
            scheme="issn",
            raw_value=raw_value,
            value=normalized,
            normalized_uri=f"urn:issn:{normalized}",
            validity_status="valid",
            validation_warning=None,
        )

    if raw_scheme == "orcid":
        match = ORCID_RE.match(raw_value)
        if not match:
            return _result(
                scheme="orcid",
                raw_value=raw_value,
                value=raw_value,
                normalized_uri=None,
                validity_status="invalid",
                validation_warning="ORCID must match 0000-0000-0000-0000 form",
            )
        normalized = match.group(1).upper()
        return _result(
            scheme="orcid",
            raw_value=raw_value,
            value=normalized,
            normalized_uri=f"https://orcid.org/{normalized}",
            validity_status="valid",
            validation_warning=None,
        )

    if raw_scheme == "wikidata":
        match = WIKIDATA_RE.match(raw_value)
        if not match:
            return _result(
                scheme="wikidata",
                raw_value=raw_value,
                value=raw_value.upper(),
                normalized_uri=None,
                validity_status="invalid",
                validation_warning="Wikidata id must look like Q<number>",
            )
        normalized = match.group(1).upper()
        return _result(
            scheme="wikidata",
            raw_value=raw_value,
            value=normalized,
            normalized_uri=f"https://www.wikidata.org/entity/{normalized}",
            validity_status="valid",
            validation_warning=None,
        )

    url_value = raw_value if raw_scheme == "url" else f"{raw_scheme}://{raw_value}"
    parsed = urlparse(url_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _result(
            scheme="url",
            raw_value=raw_value,
            value=url_value,
            normalized_uri=None,
            validity_status="invalid",
            validation_warning="URL must use http or https with a host",
        )
    normalized = parsed.geturl()
    return _result(
        scheme="url",
        raw_value=raw_value,
        value=normalized,
        normalized_uri=normalized,
        validity_status="valid",
        validation_warning=None,
    )


def normalize_identifier_row(row: dict[str, Any]) -> dict[str, Any]:
    return identifier_storage_values(row.get("scheme"), row.get("value"))
