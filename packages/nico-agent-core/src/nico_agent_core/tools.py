"""Frozen per-run tool definitions and execution bindings."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import jsonschema
from nico_contracts.tools import ToolDefinition


@dataclass(frozen=True)
class ToolBinding:
    definition: ToolDefinition
    handler: Callable[[dict], Awaitable[dict]]
    parallel: bool = False


class FrozenToolSnapshot:
    def __init__(self, bindings: tuple[ToolBinding, ...],
                 denied_write_names: frozenset[str] = frozenset()):
        self._bindings = {}
        self._denied_write_names = denied_write_names
        for binding in bindings:
            definition = json.loads(json.dumps(binding.definition, ensure_ascii=False))
            function = definition.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ValueError("invalid tool definition")
            name = function["name"]
            if not name or name in self._bindings or not isinstance(function.get("parameters"), dict):
                raise ValueError("duplicate or invalid tool definition")
            self._bindings[name] = (json.dumps(definition, ensure_ascii=False),
                                    binding.handler, binding.parallel)
        if self._denied_write_names & self._bindings.keys():
            raise ValueError("denied tool is also executable")

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(json.loads(value) for value, _, _ in self._bindings.values())

    async def execute(self, name: str, args: dict) -> dict:
        binding = self._bindings.get(name)
        if binding is None:
            return {"error": ("tool_write_disabled" if name in self._denied_write_names
                              else "tool_not_allowed")}
        definition, handler, _ = binding
        try:
            jsonschema.validate(args, json.loads(definition)["function"]["parameters"])
        except jsonschema.ValidationError:
            return {"error": "invalid_arguments"}
        return await handler(args)

    def can_execute_in_parallel(self, name: str) -> bool:
        binding = self._bindings.get(name)
        return bool(binding and binding[2])
