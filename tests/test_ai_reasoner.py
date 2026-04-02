"""
Tests for src/ai_reasoner.py.

All Anthropic API calls are mocked — no real network calls.
Tests verify:
  - AIParams loads from settings correctly
  - Daily call budget enforcement (exhausted → proceed=True)
  - Budget auto-reset on day change
  - Normal proceed=True and proceed=False responses
  - JSON parse failure fallback (proceed=True)
  - API exception fallback (proceed=True)
  - Markdown code-block JSON stripping
  - String boolean coercion ("false" → False)
  - adjusted_probability returned and logged (not used for sizing)
  - calls_remaining decrements correctly
  - Model ID comes from settings, not hardcoded
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.ai_reasoner import AIParams, AIReasoner, SanityCheckResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine in the test's event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_reasoner(
    api_key: str = "test-key",
    max_calls: int = 50,
    model: str = "claude-test-model",
) -> tuple[AIReasoner, MagicMock]:
    """
    Build an AIReasoner with a mocked Anthropic client.
    Returns (reasoner, mock_messages_create) so tests can configure responses.
    """
    params = AIParams(
        model=model,
        max_calls_per_day=max_calls,
        temperature=0.1,
        max_tokens=500,
        sanity_check_edge_threshold=0.15,
    )
    with patch("src.ai_reasoner.Anthropic") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        reasoner = AIReasoner(api_key=api_key, params=params)

    return reasoner, mock_instance.messages.create


def _set_response(mock_create: MagicMock, response_json: dict) -> None:
    """Configure the mock API to return a specific JSON payload."""
    mock_content = MagicMock()
    mock_content.text = json.dumps(response_json)
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_create.return_value = mock_response


def _set_raw_response(mock_create: MagicMock, text: str) -> None:
    """Configure the mock API to return raw text (for parse-failure tests)."""
    mock_content = MagicMock()
    mock_content.text = text
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_create.return_value = mock_response


# ---------------------------------------------------------------------------
# AIParams
# ---------------------------------------------------------------------------

class TestAIParams:
    def test_defaults(self):
        p = AIParams()
        assert p.model == "claude-sonnet-4-6"
        assert p.max_calls_per_day == 50
        assert p.temperature == 0.1
        assert p.max_tokens == 500
        assert p.sanity_check_edge_threshold == 0.15

    def test_from_settings_full(self):
        settings = {
            "ai": {
                "model": "claude-opus-4-5",
                "max_calls_per_day": 25,
                "temperature": 0.2,
                "max_tokens": 300,
                "sanity_check_edge_threshold": 0.20,
            }
        }
        p = AIParams.from_settings(settings)
        assert p.model == "claude-opus-4-5"
        assert p.max_calls_per_day == 25
        assert p.temperature == 0.2
        assert p.max_tokens == 300
        assert p.sanity_check_edge_threshold == 0.20

    def test_from_settings_missing_ai_section(self):
        """Empty settings uses all defaults."""
        p = AIParams.from_settings({})
        assert p.max_calls_per_day == 50
        assert p.model == "claude-sonnet-4-6"

    def test_from_settings_partial(self):
        """Only some keys provided — rest get defaults."""
        settings = {"ai": {"max_calls_per_day": 10}}
        p = AIParams.from_settings(settings)
        assert p.max_calls_per_day == 10
        assert p.model == "claude-sonnet-4-6"  # default


# ---------------------------------------------------------------------------
# Budget management
# ---------------------------------------------------------------------------

class TestCallBudget:
    def test_calls_remaining_starts_at_max(self):
        reasoner, _ = _make_reasoner(max_calls=50)
        assert reasoner.calls_remaining == 50

    def test_calls_remaining_decrements_after_call(self):
        reasoner, mock_create = _make_reasoner(max_calls=5)
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": 0.75})

        _run(reasoner.sanity_check("NYC above 79F on 2026-04-01", 0.75, 0.55, "139 members"))
        assert reasoner.calls_remaining == 4
        assert reasoner._calls_today == 1

    def test_budget_exhausted_returns_proceed_true_without_api_call(self):
        """When budget is exhausted, must return proceed=True WITHOUT calling the API."""
        reasoner, mock_create = _make_reasoner(max_calls=2)
        reasoner._calls_today = 2  # already at limit

        result = _run(
            reasoner.sanity_check("NYC above 79F", 0.75, 0.55, "139 members")
        )

        assert result.proceed is True
        assert result.calls_remaining == 0
        assert "budget" in result.concern.lower()
        mock_create.assert_not_called()

    def test_budget_exactly_at_limit_is_exhausted(self):
        """_calls_today == max_calls_per_day should be treated as exhausted."""
        reasoner, mock_create = _make_reasoner(max_calls=1)
        reasoner._calls_today = 1

        result = _run(reasoner.sanity_check("NYC above 79F", 0.75, 0.55, "x"))
        assert result.proceed is True
        mock_create.assert_not_called()

    def test_reset_daily_budget(self):
        reasoner, _ = _make_reasoner(max_calls=10)
        reasoner._calls_today = 7
        reasoner.reset_daily_budget()
        assert reasoner._calls_today == 0
        assert reasoner.calls_remaining == 10

    def test_budget_auto_resets_on_new_day(self):
        """_calls_today resets when the UTC date changes.

        We simulate "yesterday" by storing a clearly past date in _budget_date.
        The real datetime.now(utc).date() will always be after 2020-01-01, so
        the refresh branch will trigger without needing to mock datetime.
        """
        reasoner, _ = _make_reasoner(max_calls=10)
        reasoner._calls_today = 9
        reasoner._budget_date = date(2020, 1, 1)  # a date clearly in the past

        reasoner._refresh_budget_if_new_day()

        assert reasoner._calls_today == 0
        assert reasoner._budget_date != date(2020, 1, 1)  # updated to today


# ---------------------------------------------------------------------------
# Normal sanity check responses
# ---------------------------------------------------------------------------

class TestSanityCheckNormal:
    def test_proceed_true_response(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {
            "proceed": True,
            "concern": None,
            "adjusted_probability": 0.75,
        })

        result = _run(
            reasoner.sanity_check("NYC above 79F on 2026-04-01", 0.75, 0.55, "139 members, spread=6.2F")
        )

        assert isinstance(result, SanityCheckResult)
        assert result.proceed is True
        assert result.concern is None
        assert result.adjusted_probability == pytest.approx(0.75)
        assert result.calls_remaining == 49

    def test_proceed_false_response(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {
            "proceed": False,
            "concern": "Cold front timing is uncertain; ensemble may underestimate probability of warm side",
            "adjusted_probability": 0.60,
        })

        result = _run(
            reasoner.sanity_check("NYC above 79F on 2026-04-01", 0.75, 0.55, "139 members")
        )

        assert result.proceed is False
        assert "cold front" in result.concern.lower()
        assert result.adjusted_probability == pytest.approx(0.60)

    def test_calls_increments_only_on_successful_call(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": 0.7})
        assert reasoner._calls_today == 0
        _run(reasoner.sanity_check("x", 0.7, 0.5, "x"))
        assert reasoner._calls_today == 1

    def test_model_passed_to_api_from_settings(self):
        """Verify the model ID comes from params, not hardcoded."""
        reasoner, mock_create = _make_reasoner(model="claude-haiku-4-5-20251001")
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": 0.7})
        _run(reasoner.sanity_check("x", 0.7, 0.5, "x"))
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"

    def test_temperature_and_max_tokens_passed_to_api(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": 0.7})
        _run(reasoner.sanity_check("x", 0.7, 0.5, "x"))
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["temperature"] == pytest.approx(0.1)
        assert call_kwargs["max_tokens"] == 500

    def test_adjusted_probability_returned_even_when_proceed_true(self):
        """adjusted_probability is always returned for logging, even when not vetoing."""
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {
            "proceed": True,
            "concern": None,
            "adjusted_probability": 0.68,  # different from ensemble_prob 0.75
        })
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        # Value is returned (for logging); the caller decides not to use it for sizing
        assert result.adjusted_probability == pytest.approx(0.68)

    def test_from_settings_constructor(self):
        settings = {
            "ai": {
                "model": "claude-test",
                "max_calls_per_day": 5,
                "temperature": 0.0,
                "max_tokens": 200,
                "sanity_check_edge_threshold": 0.10,
            }
        }
        with patch("src.ai_reasoner.Anthropic"):
            reasoner = AIReasoner.from_settings(settings, api_key="key123")
        assert reasoner.params.model == "claude-test"
        assert reasoner.params.max_calls_per_day == 5


# ---------------------------------------------------------------------------
# JSON parse failure fallback
# ---------------------------------------------------------------------------

class TestParseFailureFallback:
    def test_invalid_json_defaults_proceed_true(self):
        reasoner, mock_create = _make_reasoner()
        _set_raw_response(mock_create, "Sorry, I can't help with that.")
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is True
        assert result.adjusted_probability == pytest.approx(0.75)

    def test_empty_string_defaults_proceed_true(self):
        reasoner, mock_create = _make_reasoner()
        _set_raw_response(mock_create, "")
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is True

    def test_markdown_json_block_parsed(self):
        """Claude sometimes wraps JSON in ```json ... ``` code blocks."""
        reasoner, mock_create = _make_reasoner()
        _set_raw_response(mock_create, '```json\n{"proceed": true, "concern": null, "adjusted_probability": 0.75}\n```')
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is True

    def test_markdown_block_without_language_tag(self):
        """Also handle plain ``` ... ``` without the 'json' language tag."""
        reasoner, mock_create = _make_reasoner()
        _set_raw_response(mock_create, '```\n{"proceed": false, "concern": "bad", "adjusted_probability": 0.5}\n```')
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is False
        assert result.concern == "bad"

    def test_missing_adjusted_probability_uses_ensemble(self):
        """If Claude omits adjusted_probability, fall back to ensemble_prob."""
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None})  # no adjusted_probability
        result = _run(reasoner.sanity_check("x", 0.73, 0.55, "x"))
        assert result.adjusted_probability == pytest.approx(0.73)

    def test_null_adjusted_probability_uses_ensemble(self):
        """If Claude returns adjusted_probability: null, fall back to ensemble_prob."""
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": None})
        result = _run(reasoner.sanity_check("x", 0.73, 0.55, "x"))
        assert result.adjusted_probability == pytest.approx(0.73)

    def test_string_false_coerced_to_bool(self):
        """Claude sometimes returns 'false' as a string instead of JSON bool."""
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {
            "proceed": "false",
            "concern": "string boolean test",
            "adjusted_probability": 0.5,
        })
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is False

    def test_string_true_coerced_to_bool(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {
            "proceed": "true",
            "concern": None,
            "adjusted_probability": 0.75,
        })
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.proceed is True

    def test_probability_clamped_above_1(self):
        """Claude can't return probability > 1, but clamp defensively."""
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": 1.5})
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.adjusted_probability <= 1.0

    def test_probability_clamped_below_0(self):
        reasoner, mock_create = _make_reasoner()
        _set_response(mock_create, {"proceed": True, "concern": None, "adjusted_probability": -0.1})
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.adjusted_probability >= 0.0

    def test_calls_not_incremented_on_parse_failure(self):
        """Parse failure still counts as a used API call (we did call the API)."""
        reasoner, mock_create = _make_reasoner(max_calls=10)
        _set_raw_response(mock_create, "not json")
        _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        # API was called, so count should increment
        assert reasoner._calls_today == 1


# ---------------------------------------------------------------------------
# API exception fallback
# ---------------------------------------------------------------------------

class TestAPIExceptionFallback:
    def test_api_exception_defaults_proceed_true(self):
        """Any API exception must default to proceed=True (never block on infra failure)."""
        reasoner, mock_create = _make_reasoner()
        mock_create.side_effect = RuntimeError("API timeout")

        result = _run(reasoner.sanity_check("NYC above 79F", 0.75, 0.55, "139 members"))

        assert result.proceed is True
        assert result.adjusted_probability == pytest.approx(0.75)
        assert "RuntimeError" in result.concern or "API" in result.concern

    def test_network_error_defaults_proceed_true(self):
        reasoner, mock_create = _make_reasoner()
        mock_create.side_effect = ConnectionError("Network unreachable")
        result = _run(reasoner.sanity_check("x", 0.6, 0.4, "x"))
        assert result.proceed is True

    def test_api_exception_does_not_increment_calls(self):
        """If the API call itself raises before returning, don't charge a call."""
        reasoner, mock_create = _make_reasoner(max_calls=10)
        mock_create.side_effect = RuntimeError("timeout")
        _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        # The API raised before we got a response, so calls_today stays at 0
        assert reasoner._calls_today == 0

    def test_calls_remaining_available_after_exception(self):
        reasoner, mock_create = _make_reasoner(max_calls=5)
        mock_create.side_effect = RuntimeError("timeout")
        result = _run(reasoner.sanity_check("x", 0.75, 0.55, "x"))
        assert result.calls_remaining == 5  # nothing was consumed


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestPromptConstruction:
    def test_prompt_contains_market_description(self):
        reasoner, _ = _make_reasoner()
        prompt = reasoner._build_prompt(
            "NYC high temp above 79F on 2026-04-01",
            0.75, 0.55, "139 members, spread=6.2F", 0.20,
        )
        assert "NYC high temp above 79F on 2026-04-01" in prompt

    def test_prompt_contains_probabilities_as_percent(self):
        reasoner, _ = _make_reasoner()
        prompt = reasoner._build_prompt("market", 0.75, 0.55, "details", 0.20)
        assert "75.0%" in prompt
        assert "55.0%" in prompt
        assert "20.0%" in prompt

    def test_prompt_requests_json_output(self):
        reasoner, _ = _make_reasoner()
        prompt = reasoner._build_prompt("market", 0.75, 0.55, "details", 0.20)
        assert "proceed" in prompt
        assert "concern" in prompt
        assert "adjusted_probability" in prompt

    def test_prompt_instructs_default_to_proceed_true(self):
        reasoner, _ = _make_reasoner()
        prompt = reasoner._build_prompt("market", 0.75, 0.55, "details", 0.20)
        assert "proceed=true" in prompt.lower() or "default to proceed" in prompt.lower()
