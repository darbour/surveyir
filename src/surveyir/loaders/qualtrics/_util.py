"""Small helpers for reading loosely-typed QSF payloads."""

from __future__ import annotations

from typing import Any, Literal

from ...model import Diagnostic


class Payload:
    """A read-tracking view of a QSF dict.

    Every key read through ``get``/``pop`` (or marked with ``drop``) counts as
    consumed; ``rest()`` returns everything else so the loader can preserve it
    in ``extras``. This is how surveyir stays lossless.
    """

    __slots__ = ("data", "used")

    def __init__(self, data: Any) -> None:
        self.data: dict[str, Any] = data if isinstance(data, dict) else {}
        self.used: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.used.add(key)
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.data

    def drop(self, *keys: str) -> None:
        self.used.update(keys)

    def sub(self, key: str) -> Payload:
        return Payload(self.get(key))

    def rest(self) -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if k not in self.used}


class Diagnostics:
    def __init__(self) -> None:
        self.items: list[Diagnostic] = []

    def add(
        self,
        level: Literal["info", "warning", "error"],
        code: str,
        message: str,
        location: str | None = None,
    ) -> None:
        self.items.append(Diagnostic(level=level, code=code, message=message, location=location))

    def info(self, code: str, message: str, location: str | None = None) -> None:
        self.add("info", code, message, location)

    def warn(self, code: str, message: str, location: str | None = None) -> None:
        self.add("warning", code, message, location)

    def error(self, code: str, message: str, location: str | None = None) -> None:
        self.add("error", code, message, location)


def truthy(value: Any) -> bool:
    """Qualtrics encodes booleans as true/1/'true'/'on'/'ON'/'yes'."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "on", "yes"}
    return bool(value)


def to_number(value: Any) -> int | float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def to_int(value: Any) -> int | None:
    n = to_number(value)
    return int(n) if n is not None else None


def recode(value: Any) -> int | float | str | None:
    """Recode values are numeric in practice; keep non-numeric ones as strings."""
    if value is None or value == "":
        return None
    n = to_number(value)
    return n if n is not None else str(value)


def ordered_keys(mapping: dict[str, Any]) -> list[str]:
    """Qualtrics numeric-keyed dicts ('0', '1', ...) sorted numerically."""
    return sorted((k for k in mapping if k.isdigit()), key=int)


def as_mapping(value: Any) -> dict[str, Any]:
    """Qualtrics (PHP) writes id-keyed maps as JSON arrays when ids are 0..n-1.

    Normalize both shapes to ``{str(id): value}``; anything else becomes ``{}``.
    """
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value)}
    return {}


def first_str(value: Any) -> str:
    """A scalar field that some exports wrap in a one-element list."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return "" if value is None else str(value)
