"""Closed internal adapter registration with duplicate protection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from hansard_annotator.adapters.australia.openaustralia_hansard import (
    OpenAustraliaHansardAdapter,
)
from hansard_annotator.adapters.base import SourceAdapter


class AdapterRegistryError(ValueError):
    pass


class AdapterRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter]) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            if adapter.adapter_key in self._adapters:
                raise AdapterRegistryError(
                    f"duplicate adapter key: {adapter.adapter_key}"
                )
            self._adapters[adapter.adapter_key] = adapter

    def list(self) -> tuple[SourceAdapter, ...]:
        return tuple(self._adapters[key] for key in sorted(self._adapters))

    def get(self, key: str) -> SourceAdapter:
        try:
            return self._adapters[key]
        except KeyError as error:
            raise AdapterRegistryError(f"unsupported adapter key: {key}") from error


_REGISTRY = AdapterRegistry(
    (cast(SourceAdapter, OpenAustraliaHansardAdapter()),)
)


def list_adapters() -> tuple[SourceAdapter, ...]:
    return _REGISTRY.list()


def get_adapter(key: str) -> SourceAdapter:
    return _REGISTRY.get(key)
