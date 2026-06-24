"""Load optional domain knowledge without coupling it to generic parsing."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DomainProfile:
    name: str
    body_section_titles: tuple[str, ...] = ()
    strict_subsection_terms: tuple[str, ...] = ()
    image_table_keywords: tuple[str, ...] = ()
    major_heading_keywords: tuple[str, ...] = ()

    @property
    def body_section_keys(self) -> frozenset[str]:
        return frozenset(re.sub(r"\s+", "", value) for value in self.body_section_titles)


def _string_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    values = payload.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"Domain profile field {key!r} must be an array of strings.")
    return tuple(value.strip() for value in values if value.strip())


def _profile_from_payload(payload: dict[str, Any], source: str) -> DomainProfile:
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        raise ValueError(f"Domain profile {source} must contain a [profile] table.")
    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Domain profile {source} must define profile.name.")
    return DomainProfile(
        name=name.strip(),
        body_section_titles=_string_tuple(profile, "body_section_titles"),
        strict_subsection_terms=_string_tuple(profile, "strict_subsection_terms"),
        image_table_keywords=_string_tuple(profile, "image_table_keywords"),
        major_heading_keywords=_string_tuple(profile, "major_heading_keywords"),
    )


def load_domain_profile(value: str | Path = "generic") -> DomainProfile:
    requested = str(value)
    if requested in {"generic", "tcm"}:
        resource = files("mineru_documents_markdown").joinpath("domains", f"{requested}.toml")
        with resource.open("rb") as handle:
            return _profile_from_payload(tomllib.load(handle), requested)
    path = Path(requested).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Domain profile not found: {path}")
    with path.open("rb") as handle:
        return _profile_from_payload(tomllib.load(handle), str(path))


def strict_subsection_like(text: str, profile: DomainProfile) -> bool:
    compact = re.sub(r"\s+", "", text)
    for term in profile.strict_subsection_terms:
        if compact == term:
            return True
        if re.fullmatch(rf"[（(]?[一二三四五六七八九十百\d]+[）)]{re.escape(term)}", compact):
            return True
    return False
