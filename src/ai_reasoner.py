"""
AI Reasoner.

Thin Claude API wrapper used SPARINGLY for two purposes:
1. Sanity-checking ensemble estimates when the edge is unusually large (>15pp).
   Called only when edge.recommendation == "SANITY_CHECK".
2. Parsing settlement criteria for new market series (called once per series, cached).

The AI is NEVER used for routine probability estimation — the ensemble handles that.

Hard budget: max_calls_per_day from settings.yaml ai.max_calls_per_day (default: 50).
When the budget is exhausted, all calls default to proceed=True so trading is not
blocked by a cost-control mechanism.

FUTURE IMPROVEMENT — adjusted_probability:
  The sanity_check() response includes an `adjusted_probability` field from Claude.
  Currently this value is LOGGED ONLY and not used to reprice the edge or position
  sizing (Option B: AI is a veto-only gatekeeper). If this system is ever improved,
  consider re-running compute_edge() with the adjusted_probability when:
    - proceed=True AND
    - adjusted_probability differs materially from ensemble_prob (e.g. >2pp)
  This would make the system use Claude's view of the probability for sizing, not
  just as a yes/no gate. Before implementing, validate that Claude's adjusted
  probabilities are better-calibrated than raw ensemble on historical data.
  Search "FUTURE IMPROVEMENT: adjusted_probability" across the codebase to find
  all relevant call sites.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from anthropic import Anthropic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AIParams:
    """
    Loaded from settings.yaml ai section.
    Model ID must come from settings — never hardcoded in source.
    """
    model: str = "claude-sonnet-4-6"
    max_calls_per_day: int = 50
    temperature: float = 0.1
    max_tokens: int = 500
    sanity_check_edge_threshold: float = 0.15

    @classmethod
    def from_settings(cls, settings: dict) -> "AIParams":
        ai = settings.get("ai", {})
        return cls(
            model=ai.get("model", "claude-sonnet-4-6"),
            max_calls_per_day=ai.get("max_calls_per_day", 50),
            temperature=ai.get("temperature", 0.1),
            max_tokens=ai.get("max_tokens", 500),
            sanity_check_edge_threshold=ai.get("sanity_check_edge_threshold", 0.15),
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SanityCheckResult:
    """
    Result of a single AI sanity check call.

    proceed:              True = go ahead and trade (or default on any error)
    concern:              Claude's stated concern, or None / error message
    adjusted_probability: Claude's suggested probability (logged only — not used
                          for sizing; see FUTURE IMPROVEMENT note at top of file)
    calls_remaining:      How many AI calls are left for today after this one
    """
    proceed: bool
    concern: str | None
    adjusted_probability: float
    calls_remaining: int


# ---------------------------------------------------------------------------
# AI Reasoner
# ---------------------------------------------------------------------------

class AIReasoner:
    """
    Thin wrapper around the Anthropic Claude API for edge-case reasoning.

    All public methods are async (uses asyncio.to_thread so the synchronous
    Anthropic client does not block the event loop).

    State: tracks today's call count and auto-resets at UTC midnight.

    The caller (MarketScanner / daemon) is responsible for injecting this
    instance. When ai_reasoner=None, the scanner passes SANITY_CHECK results
    through unchanged (the daemon/executor decides what to do with them).
    """

    def __init__(self, api_key: str, params: AIParams | None = None) -> None:
        self._client = Anthropic(api_key=api_key)
        self.params = params or AIParams()
        self._calls_today: int = 0
        self._budget_date: date = datetime.now(timezone.utc).date()

    @classmethod
    def from_settings(cls, settings: dict, api_key: str) -> "AIReasoner":
        """Convenience constructor — builds AIParams from settings dict."""
        return cls(api_key=api_key, params=AIParams.from_settings(settings))

    # ------------------------------------------------------------------
    # Budget management
    # ------------------------------------------------------------------

    def _refresh_budget_if_new_day(self) -> None:
        """Auto-reset call count at UTC midnight."""
        today = datetime.now(timezone.utc).date()
        if today != self._budget_date:
            logger.info(
                "AI call budget reset for new day %s (used %d calls yesterday)",
                today, self._calls_today,
            )
            self._calls_today = 0
            self._budget_date = today

    @property
    def calls_remaining(self) -> int:
        """How many AI calls remain for today (auto-refreshes on day change)."""
        self._refresh_budget_if_new_day()
        return max(0, self.params.max_calls_per_day - self._calls_today)

    def reset_daily_budget(self) -> None:
        """Manually reset the call counter. Used by daemon at day-start and in tests."""
        self._calls_today = 0
        self._budget_date = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------
    # Sanity check
    # ------------------------------------------------------------------

    async def sanity_check(
        self,
        market_description: str,
        ensemble_prob: float,
        market_price: float,
        ensemble_details: str,
    ) -> SanityCheckResult:
        """
        Ask Claude if there is a concrete reason the ensemble could be wrong.

        Called only when edge > sanity_check_edge_threshold (default 15pp).
        On ANY failure (budget exhausted, JSON parse error, API exception),
        defaults to proceed=True so a cost-control limit never silently
        blocks a valid trade.

        Args:
            market_description:  Human-readable description of the market
                                 (e.g. "NYC high temp above 79F on 2026-04-01")
            ensemble_prob:       Ensemble probability estimate (0–1)
            market_price:        Current Kalshi mid-price (0–1)
            ensemble_details:    Context string e.g. "139 members, spread=8.2F"

        Returns:
            SanityCheckResult with proceed=True on any error path.

        NOTE — FUTURE IMPROVEMENT: adjusted_probability:
            Claude returns an adjusted_probability in its JSON response.
            Currently this value is only logged — it does NOT affect edge
            calculation or position sizing (Option B: AI is veto-only).
            Search "FUTURE IMPROVEMENT: adjusted_probability" in this file
            and in market_scanner.py to find where this could be wired up.
        """
        self._refresh_budget_if_new_day()

        if self._calls_today >= self.params.max_calls_per_day:
            logger.warning(
                "AI call budget exhausted (%d/%d). Defaulting to proceed=True for: %s",
                self._calls_today, self.params.max_calls_per_day, market_description,
            )
            return SanityCheckResult(
                proceed=True,
                concern="AI call budget exhausted — proceeding on ensemble alone",
                adjusted_probability=ensemble_prob,
                calls_remaining=0,
            )

        edge = abs(ensemble_prob - market_price)
        prompt = self._build_prompt(
            market_description, ensemble_prob, market_price, ensemble_details, edge
        )

        try:
            response_text = await asyncio.to_thread(self._call_api, prompt)
            self._calls_today += 1
            result_dict = self._parse_response(response_text, ensemble_prob)

            adj_prob = result_dict.get("adjusted_probability", ensemble_prob)
            concern = result_dict.get("concern")
            proceed = result_dict.get("proceed", True)

            # FUTURE IMPROVEMENT: adjusted_probability
            # adj_prob is currently logged only. If Claude suggests a different
            # probability and proceed=True, we could re-run compute_edge() here
            # to reprice the trade. Requires evidence that Claude's adjusted
            # probabilities are better-calibrated than the raw ensemble first.
            if abs(adj_prob - ensemble_prob) > 0.02:
                logger.info(
                    "AI adjusted probability: %.3f → %.3f for %s (logged only, not repriced)",
                    ensemble_prob, adj_prob, market_description,
                )

            logger.info(
                "AI sanity check: proceed=%s concern=%s adj_prob=%.3f calls=%d/%d | %s",
                proceed, concern, adj_prob,
                self._calls_today, self.params.max_calls_per_day,
                market_description,
            )

            return SanityCheckResult(
                proceed=proceed,
                concern=concern,
                adjusted_probability=adj_prob,
                calls_remaining=self.calls_remaining,
            )

        except Exception as exc:
            logger.warning(
                "AI sanity check failed (%s: %s). Defaulting to proceed=True for: %s",
                type(exc).__name__, exc, market_description,
            )
            return SanityCheckResult(
                proceed=True,
                concern=f"AI call failed ({type(exc).__name__}) — proceeding on ensemble",
                adjusted_probability=ensemble_prob,
                calls_remaining=self.calls_remaining,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_api(self, prompt: str) -> str:
        """
        Synchronous Anthropic API call.
        Called via asyncio.to_thread to avoid blocking the event loop.
        """
        response = self._client.messages.create(
            model=self.params.model,
            max_tokens=self.params.max_tokens,
            temperature=self.params.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def _parse_response(self, text: str, fallback_prob: float) -> dict:
        """
        Parse JSON from Claude's response.

        Handles two common response formats:
          1. Raw JSON:           {"proceed": true, ...}
          2. Markdown code block: ```json\\n{...}\\n```

        Falls back to {"proceed": True, "concern": None} on any parse error
        so that a bad API response never silently blocks a trade.
        """
        try:
            stripped = text.strip()

            # Strip markdown code fences if present
            if stripped.startswith("```"):
                lines = stripped.splitlines()
                # Drop the opening ``` line and closing ``` line (if present)
                inner_lines = lines[1:]
                if inner_lines and inner_lines[-1].strip() == "```":
                    inner_lines = inner_lines[:-1]
                stripped = "\n".join(inner_lines)

            data = json.loads(stripped)

            # Coerce "proceed" to bool — Claude sometimes returns a string
            if isinstance(data.get("proceed"), str):
                data["proceed"] = data["proceed"].lower() not in ("false", "0", "no")

            # Ensure adjusted_probability has a value
            if "adjusted_probability" not in data or data["adjusted_probability"] is None:
                data["adjusted_probability"] = fallback_prob

            # Clamp probability to valid range
            data["adjusted_probability"] = max(
                0.0, min(1.0, float(data["adjusted_probability"]))
            )

            return data

        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Failed to parse AI response JSON (%s: %s). Raw response: %r",
                type(exc).__name__, exc, text,
            )
            return {
                "proceed": True,
                "concern": None,
                "adjusted_probability": fallback_prob,
            }

    def _build_prompt(
        self,
        market_description: str,
        ensemble_prob: float,
        market_price: float,
        ensemble_details: str,
        edge: float,
    ) -> str:
        """Build the sanity-check prompt. Kept as a method for testability."""
        return (
            "You are reviewing a weather prediction market trade.\n\n"
            f"Market: {market_description}\n"
            f"Ensemble probability (from {ensemble_details}): {ensemble_prob:.1%}\n"
            f"Current Kalshi market price: {market_price:.1%}\n"
            f"Edge: {edge:.1%}\n\n"
            "This is a large edge. Before we trade, is there any reason the ensemble "
            "forecast could be systematically wrong for THIS specific market? Consider:\n"
            "- Urban heat island effects at the settlement station\n"
            "- Known model biases for this region/season\n"
            "- Upcoming weather events the ensemble might handle poorly "
            "(e.g., boundary of a front)\n"
            "- Settlement criteria nuances\n\n"
            "Return ONLY JSON:\n"
            "{\n"
            '    "proceed": true | false,\n'
            '    "concern": null or "specific concern as a string",\n'
            f'    "adjusted_probability": {ensemble_prob:.4f} or adjusted value '
            "if you have a specific reason\n"
            "}\n\n"
            "Default to proceed=true. Only set proceed=false if you have a SPECIFIC, "
            "CONCRETE concern that would materially change the probability estimate."
        )
