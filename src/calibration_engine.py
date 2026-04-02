"""
Calibration Engine.

Two responsibilities:
1. BiasDatabase — per-station, per-month temperature bias corrections stored
   in SQLite. The ensemble sometimes runs systematically hot or cold at a
   specific station; this corrects for that before probability computation.

2. CalibrationEngine — isotonic regression calibration of ensemble probability
   estimates. After enough resolved markets accumulate, fits a monotone
   calibration curve so that "70% probability" actually means 70% win rate.

Design decisions:
  - Both use sqlite-utils, no async.
  - BiasDatabase lazy-loads on first get_bias() call; silently returns 0.0 if
    the DB file does not exist yet (fresh install, no data collected yet).
  - CalibrationEngine requires MIN_OBS_FOR_FIT (80) resolved markets per city
    before fitting. Below that threshold, calibrate() returns raw_prob unchanged.
  - Holdout validation uses a TEMPORAL SPLIT (most recent 20%), never random.
    If the fitted model's Brier score on the holdout is worse than the
    uncalibrated Brier, the model is rejected and not stored.
  - Refit is triggered when observations since last fit reach REFIT_EVERY (20).
  - Isotonic regression model is persisted as pickle bytes in the DB so it
    survives process restarts without re-fitting.
"""

from __future__ import annotations

import logging
import math
import pickle
from datetime import datetime, timezone
from typing import Optional

from sqlite_utils import Database

logger = logging.getLogger(__name__)

MIN_OBS_FOR_FIT: int = 80
REFIT_EVERY: int = 20


# ---------------------------------------------------------------------------
# BiasDatabase
# ---------------------------------------------------------------------------

class BiasDatabase:
    """
    Per-station, per-month temperature bias corrections (°F).

    Positive bias_f means the ensemble runs hot relative to observations
    (e.g. +1.8 → subtract 1.8°F from ensemble members before computing
    P(max ≥ threshold)).

    Schema:
        station_id TEXT, month INTEGER, bias_f REAL,
        sample_count INTEGER,  -- number of matched data points used
        updated_at TEXT,
        PRIMARY KEY (station_id, month)

    Lazy-loads: the DB file is opened only on first get_bias() or update_bias()
    call. Returns 0.0 without error if the file does not exist.

    get_bias() only returns a non-zero correction when sample_count >= MIN_SAMPLE_COUNT
    (30), so fresh installs and thin-data stations safely fall back to no correction.
    """

    TABLE = "station_bias"
    MIN_SAMPLE_COUNT = 30  # don't apply corrections based on fewer than this

    def __init__(self, db_path: str = "./data/station_bias.db") -> None:
        self._db_path = db_path
        self._db: Database | None = None

    def _load(self) -> Database:
        """Open or create the DB on first use."""
        if self._db is None:
            self._db = Database(self._db_path)
            if self.TABLE not in self._db.table_names():
                self._db[self.TABLE].create(
                    {
                        "station_id":   str,
                        "month":        int,
                        "bias_f":       float,
                        "sample_count": int,
                        "updated_at":   str,
                    },
                    pk=("station_id", "month"),
                )
                logger.info("Created station_bias table in %s", self._db_path)
            else:
                # Migrate existing DBs that are missing sample_count (schema v1)
                existing_cols = {c.name for c in self._db[self.TABLE].columns}
                if "sample_count" not in existing_cols:
                    self._db[self.TABLE].add_column("sample_count", int, not_null_default=0)
                    logger.info("Migrated station_bias table: added sample_count column")
        return self._db

    def update_bias(
        self,
        station_id: str,
        month: int,
        bias_f: float,
        sample_count: int = 0,
    ) -> None:
        """
        Upsert a bias correction for (station_id, month).

        Args:
            station_id:   NWS station identifier (e.g. "KNYC")
            month:        calendar month 1–12
            bias_f:       bias in °F (positive = ensemble runs hot)
            sample_count: number of matched data points this bias is based on.
                          Corrections with fewer than MIN_SAMPLE_COUNT points are
                          stored but silently ignored by get_bias().
        """
        db = self._load()
        db[self.TABLE].upsert(
            {
                "station_id":   station_id.upper(),
                "month":        month,
                "bias_f":       bias_f,
                "sample_count": sample_count,
                "updated_at":   datetime.now(timezone.utc).isoformat(),
            },
            pk=("station_id", "month"),
        )
        logger.debug(
            "Updated bias for %s month=%d: %.3f°F (n=%d)",
            station_id, month, bias_f, sample_count,
        )

    def get_bias(self, station_id: str, month: int) -> float:
        """
        Return the bias correction (°F) for a station/month pair.

        Returns 0.0 if:
          - The DB file does not exist yet
          - No entry found for this station/month combination
          - sample_count was explicitly recorded and is < MIN_SAMPLE_COUNT
          - Any read error

        Note on sample_count filtering: rows written with sample_count=0
        (the default) are treated as "count not tracked" and are returned
        without filtering. Rows written with a positive sample_count that
        falls below MIN_SAMPLE_COUNT are suppressed. This allows
        compute_station_bias.py to gate on real data quality while keeping
        tests and legacy rows working.

        Args:
            station_id: NWS station identifier (case-insensitive)
            month:      calendar month 1–12
        """
        try:
            db = self._load()
            row = db[self.TABLE].get((station_id.upper(), month))
            sc = row.get("sample_count") or 0
            # Only suppress if count was explicitly tracked and is too small
            if sc > 0 and sc < self.MIN_SAMPLE_COUNT:
                return 0.0
            return float(row["bias_f"])
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# CalibrationEngine
# ---------------------------------------------------------------------------

class CalibrationEngine:
    """
    Isotonic regression calibration of ensemble probability estimates.

    After MIN_OBS_FOR_FIT (80) resolved markets accumulate per city, fits a
    monotone calibration curve using scikit-learn's IsotonicRegression.
    Calibrate() applies that curve to raw ensemble probabilities. If no
    fitted model exists for a city, the raw probability is returned unchanged.

    Refits are triggered automatically in add_observation() when
    REFIT_EVERY (20) new observations have been added since the last fit.
    """

    OBS_TABLE   = "calibration_obs"
    MODEL_TABLE = "calibration_models"

    def __init__(self, db_path: str = "./data/calibration.db") -> None:
        self._db_path = db_path
        self.db = Database(db_path)
        self._ensure_schema()
        # In-memory cache: city → fitted IsotonicRegression (or None)
        self._models: dict[str, object] = {}

    def _ensure_schema(self) -> None:
        if self.OBS_TABLE not in self.db.table_names():
            self.db[self.OBS_TABLE].create(
                {
                    "id":            int,
                    "ticker":        str,
                    "city":          str,
                    "ensemble_prob": float,
                    "actual_outcome": int,
                    "added_at":      str,
                },
                pk="id",
            )
        if self.MODEL_TABLE not in self.db.table_names():
            self.db[self.MODEL_TABLE].create(
                {
                    "city":           str,
                    "n_at_last_fit":  int,
                    "model_pickle":   bytes,
                    "holdout_brier":  float,
                    "fitted_at":      str,
                },
                pk="city",
            )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_observation(
        self,
        ticker: str,
        ensemble_prob: float,
        actual_outcome: int,
        city: str,
    ) -> None:
        """
        Record a resolved market observation.

        Automatically triggers fit_city() if REFIT_EVERY new observations
        have accumulated since the last fit for this city.

        Args:
            ticker:         Kalshi market ticker (for deduplication reference)
            ensemble_prob:  raw ensemble probability at trade time (0–1)
            actual_outcome: 1 if condition was true (e.g. temp exceeded threshold), 0 otherwise
            city:           city name matching station_mapping.yaml (e.g. "New York")
        """
        self.db[self.OBS_TABLE].insert(
            {
                "ticker":         ticker,
                "city":           city,
                "ensemble_prob":  ensemble_prob,
                "actual_outcome": actual_outcome,
                "added_at":       datetime.now(timezone.utc).isoformat(),
            }
        )

        # Check if a refit is due
        n_total = self.db[self.OBS_TABLE].count_where("city = ?", [city])
        n_at_last_fit = self._n_at_last_fit(city)

        if n_total >= MIN_OBS_FOR_FIT and (n_total - n_at_last_fit) >= REFIT_EVERY:
            logger.info(
                "Triggering refit for %s: %d total obs, %d since last fit",
                city, n_total, n_total - n_at_last_fit,
            )
            self.fit_city(city)

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit_city(self, city: str) -> bool:
        """
        Fit (or refit) the isotonic regression calibration model for a city.

        Process:
          1. Fetch all observations for city ordered by added_at ASC
          2. Require MIN_OBS_FOR_FIT; skip if insufficient
          3. Temporal train/holdout split: train=first 80%, holdout=last 20%
          4. Fit IsotonicRegression on train set
          5. Compute Brier score on holdout: calibrated vs uncalibrated
          6. Accept model only if holdout Brier improves (or stays equal)
          7. Persist accepted model to DB; update in-memory cache

        Returns:
            True if a model was fitted and accepted, False otherwise.
        """
        from sklearn.isotonic import IsotonicRegression  # lazy import

        rows = list(
            self.db[self.OBS_TABLE].rows_where(
                "city = ?", [city], order_by="added_at ASC"
            )
        )
        n = len(rows)

        if n < MIN_OBS_FOR_FIT:
            logger.info(
                "fit_city(%s): only %d observations, need %d. Skipping.",
                city, n, MIN_OBS_FOR_FIT,
            )
            return False

        probs    = [r["ensemble_prob"] for r in rows]
        outcomes = [r["actual_outcome"] for r in rows]

        # Temporal split — hold out most recent 20%
        split = max(1, int(n * 0.8))
        train_probs,   train_outcomes   = probs[:split],  outcomes[:split]
        holdout_probs, holdout_outcomes = probs[split:],  outcomes[split:]

        # Fit
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(train_probs, train_outcomes)

        # Holdout Brier: calibrated vs uncalibrated
        cal_brier   = _brier(holdout_outcomes, list(iso.predict(holdout_probs)))
        raw_brier   = _brier(holdout_outcomes, holdout_probs)

        if cal_brier > raw_brier:
            logger.warning(
                "fit_city(%s): calibrated Brier %.4f > raw %.4f on holdout. "
                "Model rejected — using raw probabilities.",
                city, cal_brier, raw_brier,
            )
            return False

        # Persist
        model_bytes = pickle.dumps(iso)
        self.db[self.MODEL_TABLE].upsert(
            {
                "city":          city,
                "n_at_last_fit": n,
                "model_pickle":  model_bytes,
                "holdout_brier": cal_brier,
                "fitted_at":     datetime.now(timezone.utc).isoformat(),
            },
            pk="city",
        )
        self._models[city] = iso

        logger.info(
            "fit_city(%s): model accepted. train=%d holdout=%d "
            "raw_brier=%.4f cal_brier=%.4f",
            city, split, n - split, raw_brier, cal_brier,
        )
        return True

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def calibrate(self, city: str, raw_prob: float) -> float:
        """
        Apply the calibration model for a city to a raw ensemble probability.

        Returns raw_prob unchanged if:
          - No fitted model exists for this city
          - Fewer than MIN_OBS_FOR_FIT observations have been recorded

        Args:
            city:     city name (must match what was used in add_observation)
            raw_prob: raw ensemble probability (0–1)

        Returns:
            Calibrated probability in [0, 1].
        """
        model = self._get_model(city)
        if model is None:
            return raw_prob

        calibrated = float(model.predict([raw_prob])[0])
        return max(0.0, min(1.0, calibrated))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _n_at_last_fit(self, city: str) -> int:
        """Return n_at_last_fit from DB, or 0 if no model has been fitted yet."""
        try:
            row = self.db[self.MODEL_TABLE].get(city)
            return int(row["n_at_last_fit"])
        except Exception:
            return 0

    def _get_model(self, city: str):
        """Return the fitted IsotonicRegression for a city, loading from DB if needed."""
        if city in self._models:
            return self._models[city]

        try:
            row = self.db[self.MODEL_TABLE].get(city)
            model = pickle.loads(row["model_pickle"])
            self._models[city] = model
            return model
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Internal math helpers
# ---------------------------------------------------------------------------

def _brier(outcomes: list[int], probs: list[float]) -> float:
    """Brier score: mean((prob - outcome)^2). Lower is better."""
    if not outcomes:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(outcomes)
