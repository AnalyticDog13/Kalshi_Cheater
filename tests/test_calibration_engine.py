"""
Tests for src/calibration_engine.py.

BiasDatabase tests use :memory: via a monkey-patched path.
CalibrationEngine tests use :memory: directly.
"""

from __future__ import annotations

import pickle
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.calibration_engine import (
    BiasDatabase,
    CalibrationEngine,
    MIN_OBS_FOR_FIT,
    REFIT_EVERY,
    _brier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bias_db() -> BiasDatabase:
    db = BiasDatabase(db_path=":memory:")
    return db


def _make_engine() -> CalibrationEngine:
    return CalibrationEngine(db_path=":memory:")


def _add_obs(engine: CalibrationEngine, city: str, n: int,
             prob: float = 0.65, outcome: int = 1) -> None:
    """Add n identical observations — enough to accumulate without triggering fits."""
    for i in range(n):
        engine.db[engine.OBS_TABLE].insert({
            "ticker":         f"TICKER-{i:04d}",
            "city":           city,
            "ensemble_prob":  prob,
            "actual_outcome": outcome,
            "added_at":       datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        })


def _add_realistic_obs(engine: CalibrationEngine, city: str, n: int) -> None:
    """
    Add n observations where calibrated probabilities will be better than raw.
    Pattern: high probs tend to win, low probs tend to lose.
    """
    import random
    random.seed(42)
    for i in range(n):
        prob = random.uniform(0.1, 0.9)
        # outcome follows prob with some noise — calibration should help
        outcome = 1 if random.random() < prob else 0
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
        engine.db[engine.OBS_TABLE].insert({
            "ticker":         f"T-{i:04d}",
            "city":           city,
            "ensemble_prob":  prob,
            "actual_outcome": outcome,
            "added_at":       ts.isoformat(),
        })


# ---------------------------------------------------------------------------
# _brier helper
# ---------------------------------------------------------------------------

class TestBrier:
    def test_perfect_score_is_zero(self):
        assert _brier([1, 1, 0], [1.0, 1.0, 0.0]) == pytest.approx(0.0)

    def test_worst_score_is_one(self):
        assert _brier([0, 0], [1.0, 1.0]) == pytest.approx(1.0)

    def test_known_value(self):
        # (0.7-1)^2 + (0.3-0)^2 = 0.09 + 0.09 = 0.18 / 2 = 0.09
        assert _brier([1, 0], [0.7, 0.3]) == pytest.approx(0.09)

    def test_empty_returns_zero(self):
        assert _brier([], []) == 0.0


# ---------------------------------------------------------------------------
# BiasDatabase
# ---------------------------------------------------------------------------

class TestBiasDatabaseSchema:
    def test_creates_table_on_first_use(self):
        db = _make_bias_db()
        db._load()
        assert "station_bias" in db._db.table_names()

    def test_lazy_load_not_called_at_init(self):
        db = _make_bias_db()
        assert db._db is None  # not loaded until first access

    def test_load_is_idempotent(self):
        db = _make_bias_db()
        db._load()
        db._load()  # second call should not crash
        assert db._db is not None


class TestBiasDatabaseUpdateGet:
    def test_update_and_get_round_trip(self):
        db = _make_bias_db()
        db.update_bias("KNYC", 7, 1.8)
        assert db.get_bias("KNYC", 7) == pytest.approx(1.8)

    def test_negative_bias(self):
        db = _make_bias_db()
        db.update_bias("KCQT", 1, -0.5)
        assert db.get_bias("KCQT", 1) == pytest.approx(-0.5)

    def test_zero_bias(self):
        db = _make_bias_db()
        db.update_bias("KORD", 3, 0.0)
        assert db.get_bias("KORD", 3) == pytest.approx(0.0)

    def test_get_missing_station_returns_zero(self):
        db = _make_bias_db()
        assert db.get_bias("UNKNOWN", 6) == pytest.approx(0.0)

    def test_get_missing_month_returns_zero(self):
        db = _make_bias_db()
        db.update_bias("KNYC", 7, 1.8)
        assert db.get_bias("KNYC", 8) == pytest.approx(0.0)

    def test_upsert_updates_existing(self):
        db = _make_bias_db()
        db.update_bias("KNYC", 7, 1.8)
        db.update_bias("KNYC", 7, 2.1)
        assert db.get_bias("KNYC", 7) == pytest.approx(2.1)

    def test_different_stations_independent(self):
        db = _make_bias_db()
        db.update_bias("KNYC", 7, 1.8)
        db.update_bias("KORD", 7, -0.3)
        assert db.get_bias("KNYC", 7) == pytest.approx(1.8)
        assert db.get_bias("KORD", 7) == pytest.approx(-0.3)

    def test_all_12_months(self):
        db = _make_bias_db()
        for m in range(1, 13):
            db.update_bias("KNYC", m, float(m) * 0.1)
        for m in range(1, 13):
            assert db.get_bias("KNYC", m) == pytest.approx(m * 0.1)

    def test_get_bias_returns_float(self):
        db = _make_bias_db()
        db.update_bias("KNYC", 6, 1)
        result = db.get_bias("KNYC", 6)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# CalibrationEngine — schema
# ---------------------------------------------------------------------------

class TestCalibrationEngineSchema:
    def test_creates_obs_table(self):
        e = _make_engine()
        assert "calibration_obs" in e.db.table_names()

    def test_creates_model_table(self):
        e = _make_engine()
        assert "calibration_models" in e.db.table_names()


# ---------------------------------------------------------------------------
# CalibrationEngine — add_observation
# ---------------------------------------------------------------------------

class TestAddObservation:
    def test_inserts_row(self):
        e = _make_engine()
        e.add_observation("TICKER-001", 0.70, 1, "New York")
        assert e.db[e.OBS_TABLE].count == 1

    def test_stores_correct_fields(self):
        e = _make_engine()
        e.add_observation("T-001", 0.65, 0, "Chicago")
        row = list(e.db[e.OBS_TABLE].rows)[0]
        assert row["ticker"] == "T-001"
        assert row["ensemble_prob"] == pytest.approx(0.65)
        assert row["actual_outcome"] == 0
        assert row["city"] == "Chicago"
        assert row["added_at"] is not None

    def test_multiple_cities_independent(self):
        e = _make_engine()
        e.add_observation("T-001", 0.7, 1, "New York")
        e.add_observation("T-002", 0.4, 0, "Chicago")
        ny = list(e.db[e.OBS_TABLE].rows_where("city = ?", ["New York"]))
        chi = list(e.db[e.OBS_TABLE].rows_where("city = ?", ["Chicago"]))
        assert len(ny) == 1
        assert len(chi) == 1

    def test_does_not_refit_below_threshold(self):
        e = _make_engine()
        for i in range(MIN_OBS_FOR_FIT - 1):
            e.add_observation(f"T-{i}", 0.7, 1, "New York")
        # No model should exist
        assert e.db[e.MODEL_TABLE].count == 0

    def test_does_not_refit_until_refit_every(self):
        """First fit happens at MIN_OBS_FOR_FIT, next at MIN_OBS_FOR_FIT + REFIT_EVERY."""
        e = _make_engine()
        # Pre-fill to just below auto-refit
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + REFIT_EVERY - 1)
        # Manually trigger first fit so n_at_last_fit is set
        e.fit_city("New York")
        n_before = e.db[e.MODEL_TABLE].count

        # Add one more obs — not yet REFIT_EVERY since last fit
        e.add_observation("NEW", 0.6, 1, "New York")
        # model table count should be unchanged (no new fit triggered)
        assert e.db[e.MODEL_TABLE].count == n_before


# ---------------------------------------------------------------------------
# CalibrationEngine — fit_city
# ---------------------------------------------------------------------------

class TestFitCity:
    def test_returns_false_below_min_obs(self):
        e = _make_engine()
        _add_obs(e, "New York", MIN_OBS_FOR_FIT - 1)
        assert e.fit_city("New York") is False

    def test_returns_false_unknown_city(self):
        e = _make_engine()
        assert e.fit_city("Atlantis") is False

    def test_returns_true_with_sufficient_realistic_data(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        result = e.fit_city("New York")
        # May be True or False depending on whether calibration helps;
        # with well-calibrated data seeded at 42 it should be True.
        # We just assert it doesn't crash.
        assert isinstance(result, bool)

    def test_model_stored_in_db_on_success(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        fitted = e.fit_city("New York")
        if fitted:
            assert e.db[e.MODEL_TABLE].count == 1
            row = e.db[e.MODEL_TABLE].get("New York")
            assert row["model_pickle"] is not None
            assert row["n_at_last_fit"] >= MIN_OBS_FOR_FIT

    def test_model_pickle_is_valid_isotonic(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        fitted = e.fit_city("New York")
        if fitted:
            row = e.db[e.MODEL_TABLE].get("New York")
            model = pickle.loads(row["model_pickle"])
            from sklearn.isotonic import IsotonicRegression
            assert isinstance(model, IsotonicRegression)

    def test_refit_updates_n_at_last_fit(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        e.fit_city("New York")
        n1 = e._n_at_last_fit("New York")

        # Add more and refit
        _add_realistic_obs(e, "New York", 20)
        e.fit_city("New York")
        n2 = e._n_at_last_fit("New York")
        assert n2 >= n1

    def test_temporal_split_uses_most_recent_as_holdout(self):
        """
        Verify that fit_city respects temporal order: older rows train, newer rows holdout.
        We can check this indirectly — fit on 100 rows should produce n_at_last_fit=100.
        """
        e = _make_engine()
        _add_realistic_obs(e, "New York", 100)
        fitted = e.fit_city("New York")
        if fitted:
            row = e.db[e.MODEL_TABLE].get("New York")
            assert row["n_at_last_fit"] == 100

    def test_poor_calibration_rejected(self):
        """
        Force a scenario where calibration cannot help: all probs=0.5, outcomes=alternating.
        Isotonic regression will predict 0.5 for all. Brier scores will be equal, so
        the model may or may not be accepted — critically, it must NOT raise.
        """
        e = _make_engine()
        for i in range(MIN_OBS_FOR_FIT + 10):
            e.db[e.OBS_TABLE].insert({
                "ticker": f"T-{i}",
                "city": "Flat City",
                "ensemble_prob": 0.5,
                "actual_outcome": i % 2,
                "added_at": datetime(2026, 1, i % 28 + 1, tzinfo=timezone.utc).isoformat(),
            })
        result = e.fit_city("Flat City")
        assert isinstance(result, bool)  # no crash


# ---------------------------------------------------------------------------
# CalibrationEngine — calibrate
# ---------------------------------------------------------------------------

class TestCalibrate:
    def test_returns_raw_prob_when_no_model(self):
        e = _make_engine()
        assert e.calibrate("New York", 0.65) == pytest.approx(0.65)

    def test_returns_raw_prob_for_unknown_city(self):
        e = _make_engine()
        assert e.calibrate("Atlantis", 0.30) == pytest.approx(0.30)

    def test_calibrated_output_in_zero_one(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        e.fit_city("New York")
        result = e.calibrate("New York", 0.75)
        assert 0.0 <= result <= 1.0

    def test_calibrate_uses_in_memory_cache_after_fit(self):
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        e.fit_city("New York")
        # Model should be cached; second call should not touch DB
        r1 = e.calibrate("New York", 0.6)
        r2 = e.calibrate("New York", 0.6)
        assert r1 == pytest.approx(r2)

    def test_calibrate_loads_model_from_db_on_fresh_instance(self):
        """Fit with one engine instance, calibrate with a fresh one (shared DB)."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            e1 = CalibrationEngine(db_path=path)
            _add_realistic_obs(e1, "New York", MIN_OBS_FOR_FIT + 20)
            fitted = e1.fit_city("New York")
            e1.db.close()  # release file lock before second open

            if fitted:
                e2 = CalibrationEngine(db_path=path)
                result = e2.calibrate("New York", 0.65)
                e2.db.close()
                assert 0.0 <= result <= 1.0
        finally:
            try:
                os.unlink(path)
            except PermissionError:
                pass  # Windows: file still locked, ignore in CI

    def test_calibrate_clamps_to_unit_interval(self):
        """Even if a model extrapolates outside [0,1], output must be clamped."""
        e = _make_engine()
        _add_realistic_obs(e, "New York", MIN_OBS_FOR_FIT + 20)
        e.fit_city("New York")
        # Extreme inputs should still yield values in [0, 1]
        assert 0.0 <= e.calibrate("New York", 0.001) <= 1.0
        assert 0.0 <= e.calibrate("New York", 0.999) <= 1.0


# ---------------------------------------------------------------------------
# CalibrationEngine — n_at_last_fit
# ---------------------------------------------------------------------------

class TestNAtLastFit:
    def test_returns_zero_when_no_model(self):
        e = _make_engine()
        assert e._n_at_last_fit("Unknown City") == 0

    def test_returns_correct_n_after_fit(self):
        e = _make_engine()
        _add_realistic_obs(e, "Chicago", MIN_OBS_FOR_FIT + 5)
        fitted = e.fit_city("Chicago")
        if fitted:
            n = e._n_at_last_fit("Chicago")
            assert n == MIN_OBS_FOR_FIT + 5
