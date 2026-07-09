"""
validate.py – ABI and action validation layer for GasNinja.

Catches bad addresses, malformed ABIs, type mismatches, and other
issues BEFORE the payload reaches the multicall engine. This prevents
wasted gas from Groq hallucinations or malicious requester payloads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from web3 import Web3

logger = logging.getLogger("validate")

# Solidity type → Python type mapping for basic validation.
_SOLIDITY_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "address": str,
    "bool": (bool, int),
    "string": str,
    "bytes": (str, bytes),
}


@dataclass
class ValidationError:
    """A single validation issue."""

    action_index: int
    field: str
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class ValidationResult:
    """Aggregated validation outcome."""

    valid: bool = True
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    def add_error(self, index: int, fld: str, msg: str) -> None:
        self.errors.append(ValidationError(index, fld, msg, "error"))
        self.valid = False

    def add_warning(self, index: int, fld: str, msg: str) -> None:
        self.warnings.append(ValidationError(index, fld, msg, "warning"))

    def summary(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"  ❌ action[{e.action_index}].{e.field}: {e.message}")
        for w in self.warnings:
            lines.append(f"  ⚠️  action[{w.action_index}].{w.field}: {w.message}")
        return "\n".join(lines) if lines else "  ✅ All actions valid."


# ──────────────────────────────────────────────
# Individual validators
# ──────────────────────────────────────────────

def _validate_address(index: int, target: Any, result: ValidationResult) -> None:
    """Check that target is a valid Ethereum address."""
    if not isinstance(target, str):
        result.add_error(index, "target", f"Expected string, got {type(target).__name__}")
        return

    if not re.match(r"^0x[0-9a-fA-F]{40}$", target):
        result.add_error(index, "target", f"Invalid address format: {target!r}")
        return

    # Check checksum if it's mixed-case (i.e. not all-lower or all-upper hex).
    hex_part = target[2:]
    if hex_part != hex_part.lower() and hex_part != hex_part.upper():
        try:
            checksummed = Web3.to_checksum_address(target)
            if checksummed != target:
                result.add_warning(
                    index, "target",
                    f"Checksum mismatch — expected {checksummed}, got {target}. Will auto-correct.",
                )
        except Exception:
            result.add_error(index, "target", f"Address failed checksum validation: {target}")


def _validate_abi(index: int, abi: Any, function_name: str, result: ValidationResult) -> dict | None:
    """
    Validate the ABI is a list, contains the target function, and return
    the matching ABI entry (or None on failure).
    """
    if not isinstance(abi, list):
        result.add_error(index, "abi", f"Expected list, got {type(abi).__name__}")
        return None

    if len(abi) == 0:
        result.add_error(index, "abi", "ABI is empty")
        return None

    # Find the function entry.
    matching = [
        entry for entry in abi
        if isinstance(entry, dict)
        and entry.get("name") == function_name
        and entry.get("type", "function") == "function"
    ]

    if not matching:
        available = [e.get("name") for e in abi if isinstance(e, dict) and e.get("type", "function") == "function"]
        result.add_error(
            index, "abi",
            f"Function '{function_name}' not found in ABI. Available: {available}",
        )
        return None

    if len(matching) > 1:
        result.add_warning(index, "abi", f"Multiple overloads of '{function_name}' found — using the first.")

    return matching[0]


def _validate_args(
    index: int, abi_entry: dict, args: Any, result: ValidationResult,
) -> None:
    """Validate argument count and basic type compatibility."""
    if not isinstance(args, list):
        result.add_error(index, "args", f"Expected list, got {type(args).__name__}")
        return

    inputs = abi_entry.get("inputs", [])
    if len(args) != len(inputs):
        result.add_error(
            index, "args",
            f"Expected {len(inputs)} arg(s) ({[i.get('name','?') for i in inputs]}), got {len(args)}",
        )
        return

    # Basic type checks.
    for i, (arg, inp) in enumerate(zip(args, inputs)):
        sol_type: str = inp.get("type", "")
        param_name: str = inp.get("name", f"arg{i}")

        # Address check.
        if sol_type == "address":
            if not isinstance(arg, str) or not re.match(r"^0x[0-9a-fA-F]{40}$", str(arg)):
                result.add_error(
                    index, f"args[{i}]",
                    f"Param '{param_name}' expects address, got {arg!r}",
                )

        # uint / int check — must be an integer or integer-string.
        elif sol_type.startswith("uint") or sol_type.startswith("int"):
            if isinstance(arg, str):
                try:
                    int(arg)
                except ValueError:
                    result.add_error(
                        index, f"args[{i}]",
                        f"Param '{param_name}' expects {sol_type}, got non-numeric string {arg!r}",
                    )
            elif not isinstance(arg, int):
                result.add_error(
                    index, f"args[{i}]",
                    f"Param '{param_name}' expects {sol_type}, got {type(arg).__name__}",
                )

        # Bool check.
        elif sol_type == "bool":
            if not isinstance(arg, (bool, int)):
                result.add_error(
                    index, f"args[{i}]",
                    f"Param '{param_name}' expects bool, got {type(arg).__name__}",
                )

        # bytes / bytesN check.
        elif sol_type.startswith("bytes"):
            if isinstance(arg, str):
                if not arg.startswith("0x"):
                    result.add_warning(
                        index, f"args[{i}]",
                        f"Param '{param_name}' ({sol_type}): string arg doesn't start with 0x",
                    )


def _validate_allow_failure(index: int, action: dict, result: ValidationResult) -> None:
    """Check allowFailure is a bool if present."""
    if "allowFailure" in action:
        val = action["allowFailure"]
        if not isinstance(val, bool):
            result.add_warning(
                index, "allowFailure",
                f"Expected bool, got {type(val).__name__} — will coerce to bool({val}).",
            )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def validate_actions(actions: list[dict[str, Any]]) -> ValidationResult:
    """
    Validate an entire list of action dicts before encoding.

    Returns a ValidationResult — check .valid before proceeding.
    Logged at INFO level with a summary.
    """
    result = ValidationResult()

    if not isinstance(actions, list):
        result.add_error(0, "actions", f"Expected list, got {type(actions).__name__}")
        return result

    if len(actions) == 0:
        result.add_error(0, "actions", "Actions list is empty")
        return result

    REQUIRED_KEYS = {"target", "abi", "function"}

    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            result.add_error(idx, "action", f"Expected dict, got {type(action).__name__}")
            continue

        # Check required keys.
        missing = REQUIRED_KEYS - set(action.keys())
        if missing:
            result.add_error(idx, "keys", f"Missing required keys: {missing}")
            continue

        # Validate each field.
        _validate_address(idx, action["target"], result)

        fn_name = action["function"]
        if not isinstance(fn_name, str) or not fn_name:
            result.add_error(idx, "function", f"Function name must be a non-empty string, got {fn_name!r}")
            continue

        abi_entry = _validate_abi(idx, action["abi"], fn_name, result)
        if abi_entry and "args" in action:
            _validate_args(idx, abi_entry, action["args"], result)

        _validate_allow_failure(idx, action, result)

    # Log summary.
    if result.valid:
        logger.info("✅ Validation passed for %d action(s)", len(actions))
    else:
        logger.warning(
            "❌ Validation failed (%d error(s), %d warning(s)):\n%s",
            len(result.errors),
            len(result.warnings),
            result.summary(),
        )

    return result
