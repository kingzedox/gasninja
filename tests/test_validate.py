"""
tests/test_validate.py – Unit tests for the ABI/action validation layer.

Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import pytest
from validate import validate_actions, ValidationResult


# ──────────────────────────────────────────────
# Fixtures: valid action templates
# ──────────────────────────────────────────────

def _approve_action(
    target: str = "0x4200000000000000000000000000000000000006",
    spender: str = "0xcA11bde05977b3631167028862bE2a173976CA11",
    amount: int = 2**256 - 1,
    **overrides,
) -> dict:
    """A standard ERC-20 approve action."""
    action = {
        "target": target,
        "abi": [
            {
                "name": "approve",
                "type": "function",
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
            }
        ],
        "function": "approve",
        "args": [spender, amount],
        "allowFailure": False,
    }
    action.update(overrides)
    return action


def _transfer_action(
    target: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    to: str = "0x0000000000000000000000000000000000000001",
    amount: int = 1000000,
) -> dict:
    """A standard ERC-20 transfer action."""
    return {
        "target": target,
        "abi": [
            {
                "name": "transfer",
                "type": "function",
                "inputs": [
                    {"name": "to", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
            }
        ],
        "function": "transfer",
        "args": [to, amount],
        "allowFailure": False,
    }


# ──────────────────────────────────────────────
# Tests: Happy path
# ──────────────────────────────────────────────

class TestValidActions:
    def test_single_approve(self):
        result = validate_actions([_approve_action()])
        assert result.valid
        assert len(result.errors) == 0

    def test_multi_action_bundle(self):
        actions = [_approve_action(), _transfer_action()]
        result = validate_actions(actions)
        assert result.valid
        assert len(result.errors) == 0

    def test_string_int_amount_accepted(self):
        """uint256 args can be passed as integer-strings (from Groq)."""
        action = _approve_action(amount="115792089237316195423570985008687907853269984665640564039457584007913129639935")
        result = validate_actions([action])
        assert result.valid


# ──────────────────────────────────────────────
# Tests: Address validation
# ──────────────────────────────────────────────

class TestAddressValidation:
    def test_invalid_address_format(self):
        action = _approve_action(target="not-an-address")
        result = validate_actions([action])
        assert not result.valid
        assert any("Invalid address" in e.message for e in result.errors)

    def test_short_address(self):
        action = _approve_action(target="0x1234")
        result = validate_actions([action])
        assert not result.valid

    def test_non_string_target(self):
        action = _approve_action()
        action["target"] = 12345
        result = validate_actions([action])
        assert not result.valid
        assert any("Expected string" in e.message for e in result.errors)


# ──────────────────────────────────────────────
# Tests: ABI validation
# ──────────────────────────────────────────────

class TestABIValidation:
    def test_empty_abi(self):
        action = _approve_action()
        action["abi"] = []
        result = validate_actions([action])
        assert not result.valid
        assert any("ABI is empty" in e.message for e in result.errors)

    def test_wrong_function_name(self):
        action = _approve_action()
        action["function"] = "nonexistent"
        result = validate_actions([action])
        assert not result.valid
        assert any("not found in ABI" in e.message for e in result.errors)

    def test_abi_not_a_list(self):
        action = _approve_action()
        action["abi"] = "not a list"
        result = validate_actions([action])
        assert not result.valid

    def test_function_name_empty_string(self):
        action = _approve_action()
        action["function"] = ""
        result = validate_actions([action])
        assert not result.valid


# ──────────────────────────────────────────────
# Tests: Argument validation
# ──────────────────────────────────────────────

class TestArgValidation:
    def test_wrong_arg_count(self):
        action = _approve_action()
        action["args"] = ["0xcA11bde05977b3631167028862bE2a173976CA11"]  # Missing amount
        result = validate_actions([action])
        assert not result.valid
        assert any("Expected 2 arg" in e.message for e in result.errors)

    def test_extra_args(self):
        action = _approve_action()
        action["args"].append("extra")
        result = validate_actions([action])
        assert not result.valid

    def test_bad_address_arg(self):
        action = _approve_action(spender="not-an-address")
        result = validate_actions([action])
        assert not result.valid
        assert any("expects address" in e.message for e in result.errors)

    def test_non_numeric_uint_string(self):
        action = _approve_action(amount="not_a_number")
        result = validate_actions([action])
        assert not result.valid
        assert any("non-numeric" in e.message for e in result.errors)

    def test_args_not_a_list(self):
        action = _approve_action()
        action["args"] = "not a list"
        result = validate_actions([action])
        assert not result.valid


# ──────────────────────────────────────────────
# Tests: Missing keys
# ──────────────────────────────────────────────

class TestMissingKeys:
    def test_missing_target(self):
        action = _approve_action()
        del action["target"]
        result = validate_actions([action])
        assert not result.valid
        assert any("Missing required" in e.message for e in result.errors)

    def test_missing_abi(self):
        action = _approve_action()
        del action["abi"]
        result = validate_actions([action])
        assert not result.valid

    def test_missing_function(self):
        action = _approve_action()
        del action["function"]
        result = validate_actions([action])
        assert not result.valid


# ──────────────────────────────────────────────
# Tests: Edge cases
# ──────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_actions_list(self):
        result = validate_actions([])
        assert not result.valid
        assert any("empty" in e.message for e in result.errors)

    def test_actions_not_a_list(self):
        result = validate_actions("not a list")  # type: ignore
        assert not result.valid

    def test_action_not_a_dict(self):
        result = validate_actions(["not a dict"])
        assert not result.valid

    def test_allow_failure_non_bool_warns(self):
        action = _approve_action()
        action["allowFailure"] = "yes"
        result = validate_actions([action])
        # Should still be valid but with a warning.
        assert result.valid
        assert len(result.warnings) > 0
        assert any("coerce" in w.message for w in result.warnings)


# ──────────────────────────────────────────────
# Tests: Gas savings calculator
# ──────────────────────────────────────────────

class TestGasSavings:
    def test_savings_calculation(self):
        from multicall_engine import calculate_gas_savings

        # 3 calls, each with 100 bytes of calldata (all non-zero → 16 gas each).
        mock_calls = [
            ("0x" + "00" * 20, False, bytes([0xFF] * 100)),
            ("0x" + "00" * 20, False, bytes([0xFF] * 100)),
            ("0x" + "00" * 20, False, bytes([0xFF] * 100)),
        ]

        # Suppose bundled tx used 150 000 gas.
        savings = calculate_gas_savings(mock_calls, bundled_gas_used=150_000)

        # individual = 3 × 21000 + 3 × 100 × 16 = 63000 + 4800 = 67800
        assert savings.individual_gas_total == 67_800
        assert savings.bundled_gas_used == 150_000
        assert savings.num_calls == 3
        # In this contrived case, bundled is more expensive (no real execution
        # gas counted in individual estimate), so saved = 0.
        assert savings.gas_saved == 0
        assert savings.savings_pct == 0.0

    def test_savings_when_bundled_is_cheaper(self):
        from multicall_engine import calculate_gas_savings

        # 5 calls with some calldata.
        mock_calls = [
            ("0x" + "00" * 20, False, bytes([0xFF] * 200))
            for _ in range(5)
        ]

        # individual = 5 × 21000 + 5 × 200 × 16 = 105000 + 16000 = 121000
        # Bundled used only 80 000 gas.
        savings = calculate_gas_savings(mock_calls, bundled_gas_used=80_000)

        assert savings.individual_gas_total == 121_000
        assert savings.bundled_gas_used == 80_000
        assert savings.gas_saved == 41_000
        assert savings.savings_pct == pytest.approx(33.88, abs=0.01)

    def test_zero_byte_calldata_costs_less(self):
        from multicall_engine import calculate_gas_savings

        # All zero bytes → 4 gas each instead of 16.
        mock_calls = [
            ("0x" + "00" * 20, False, bytes([0x00] * 100)),
        ]

        savings = calculate_gas_savings(mock_calls, bundled_gas_used=10_000)
        # individual = 1 × 21000 + 100 × 4 = 21400
        assert savings.individual_gas_total == 21_400
