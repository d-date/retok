#!/usr/bin/env python3
"""Regression tests for retok. Stdlib unittest only, no third-party deps.

`retok` has no .py extension, so it is loaded via importlib/SourceFileLoader
instead of a normal import statement.
"""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

RETOK_PATH = Path(__file__).resolve().parent / "retok"


def _load_retok():
    loader = SourceFileLoader("retok_under_test", str(RETOK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


retok = _load_retok()


class TestCostCalculation(unittest.TestCase):
    def test_entry_cost_known_model(self):
        price = retok.PRICING["sonnet"]
        usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 300,  # no breakdown -> assumed 5m TTL
        }
        expected = (
            1000 * price["input"]
            + 500 * price["output"]
            + 2000 * price["input"] * retok.CACHE_READ
            + 300 * price["input"] * retok.CACHE_WRITE_5M
        ) / 1_000_000
        got = retok.entry_cost("claude-sonnet-4-5-20250514", usage)
        self.assertAlmostEqual(got, expected, places=9)

    def test_entry_cost_unknown_model_is_zero(self):
        usage = {"input_tokens": 1000, "output_tokens": 500}
        self.assertEqual(retok.entry_cost("claude-future-9000", usage), 0.0)
        self.assertIsNone(retok.model_family("claude-future-9000"))

    def test_codex_cost_known_model(self):
        price = dict(retok.OPENAI_PRICING)["gpt-5.4"]
        expected = (
            (2000 - 500) * price["input"]
            + 500 * price["input"] * retok.OPENAI_CACHE_READ
            + 1000 * price["output"]
        ) / 1_000_000
        got = retok.codex_cost("gpt-5.4", 2000, 500, 1000)
        self.assertAlmostEqual(got, expected, places=9)

    def test_codex_cost_unknown_model_is_zero(self):
        self.assertEqual(retok.codex_cost("gpt-9-nonexistent", 2000, 500, 1000), 0.0)
        self.assertIsNone(retok.openai_price("gpt-9-nonexistent"))


class TestAdviceThresholds(unittest.TestCase):
    def _totals(self, **overrides):
        totals = {"input": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
        totals.update(overrides)
        return totals

    def _session(self, **overrides):
        s = retok.new_session()
        s.update(overrides)
        s.setdefault("provider", "claude")
        return s

    def test_cache_hit_rate_advice_threshold(self):
        # denom > 1M and hit rate < 75% -> fires
        firing = self._totals(input=300_000, cache_read=200_000, cache_write=600_000)
        advice, hit = retok.build_advice({}, firing, {})
        self.assertLess(hit, 0.75)
        self.assertTrue(any(key == "adv_cache_hit" for _, key, _ in advice))

        # same shape, hit rate exactly at 75% -> does not fire (strict <)
        healthy = self._totals(input=200_000, cache_read=900_000, cache_write=100_000)
        advice2, hit2 = retok.build_advice({}, healthy, {})
        self.assertEqual(hit2, 0.75)
        self.assertFalse(any(key == "adv_cache_hit" for _, key, _ in advice2))

    def test_fat_context_advice_threshold(self):
        # context over the limit and enough prompts -> fires
        fires = {"s1": self._session(max_context=retok.LARGE_CONTEXT_THRESHOLD + 1, prompts=3)}
        advice, _ = retok.build_advice(fires, self._totals(), {})
        self.assertTrue(any(key == "adv_fat_ctx" for _, key, _ in advice))

        # same oversized context but too few prompts -> does not fire
        not_fires = {"s1": self._session(max_context=200_000, prompts=2)}
        advice2, _ = retok.build_advice(not_fires, self._totals(), {})
        self.assertFalse(any(key == "adv_fat_ctx" for _, key, _ in advice2))


class TestPremiumShareAdvice(unittest.TestCase):
    """The 'a single premium-tier model dominates cost' rule (adv_premium_share)."""

    def _totals(self, cost):
        return {"input": 0, "cache_read": 0, "cache_write": 0, "cost": cost}

    def _pm(self, cost):
        return {"cost": cost, "input": 0, "output": 0, "cache_read": 0,
                "cache_write": 0, "requests": 1}

    def test_fires_at_90pct_share_and_50_dollar_floor(self):
        per_model = {"claude-fable-5": self._pm(54.0),   # 90% of 60
                     "claude-haiku-4-5": self._pm(6.0)}
        advice, _ = retok.build_advice({}, self._totals(60.0), per_model)
        self.assertTrue(any(k == "adv_premium_share" for _, k, _ in advice))

    def test_does_not_fire_below_90pct_share(self):
        per_model = {"claude-fable-5": self._pm(53.4),   # 89% of 60
                     "claude-haiku-4-5": self._pm(6.6)}
        advice, _ = retok.build_advice({}, self._totals(60.0), per_model)
        self.assertFalse(any(k == "adv_premium_share" for _, k, _ in advice))

    def test_does_not_fire_below_50_dollar_floor(self):
        per_model = {"claude-fable-5": self._pm(40.0)}   # 100% share, total < $50
        advice, _ = retok.build_advice({}, self._totals(40.0), per_model)
        self.assertFalse(any(k == "adv_premium_share" for _, k, _ in advice))

    def test_does_not_fire_for_non_premium_model(self):
        # 95% share, but sonnet's input price isn't above the PRICING median
        per_model = {"claude-sonnet-4-5": self._pm(57.0),
                     "claude-haiku-4-5": self._pm(3.0)}
        advice, _ = retok.build_advice({}, self._totals(60.0), per_model)
        self.assertFalse(any(k == "adv_premium_share" for _, k, _ in advice))


class TestModelPortfolioStats(unittest.TestCase):
    def test_cost_share_and_cache_hit_rate(self):
        per_model = {
            "claude-sonnet-4-5": {"cost": 30.0, "input": 100, "output": 50,
                                   "cache_read": 300, "cache_write": 100, "requests": 5},
            "claude-haiku-4-5": {"cost": 10.0, "input": 400, "output": 50,
                                  "cache_read": 0, "cache_write": 0, "requests": 5},
        }
        portfolio = retok.model_portfolio(per_model, total_cost=40.0)
        self.assertAlmostEqual(portfolio["claude-sonnet-4-5"]["cost_share"], 0.75)
        self.assertAlmostEqual(portfolio["claude-haiku-4-5"]["cost_share"], 0.25)
        # sonnet: cache_read / (input + cache_read + cache_write) = 300/500
        self.assertAlmostEqual(portfolio["claude-sonnet-4-5"]["cache_hit_rate"], 0.6)
        self.assertAlmostEqual(portfolio["claude-haiku-4-5"]["cache_hit_rate"], 0.0)

    def test_zero_total_cost_does_not_divide_by_zero(self):
        per_model = {"m": {"cost": 0.0, "input": 0, "output": 0,
                            "cache_read": 0, "cache_write": 0, "requests": 0}}
        portfolio = retok.model_portfolio(per_model, total_cost=0.0)
        self.assertEqual(portfolio["m"]["cost_share"], 0.0)
        self.assertEqual(portfolio["m"]["cache_hit_rate"], 0.0)


class TestScanIntegration(unittest.TestCase):
    def test_scan_aggregates_tokens_and_flags_unknown_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj_dir = Path(tmp) / "myproject"
            proj_dir.mkdir()
            now = datetime.now(timezone.utc)

            def ts(minutes_ago):
                return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")

            entries = [
                {"type": "user", "timestamp": ts(5), "sessionId": "sess1",
                 "cwd": str(proj_dir),
                 "message": {"role": "user", "content": "please fix the bug"}},
                {"type": "assistant", "timestamp": ts(4), "sessionId": "sess1",
                 "cwd": str(proj_dir),
                 "message": {"id": "msg-1", "model": "claude-sonnet-4-5",
                             "usage": {"input_tokens": 100, "output_tokens": 50,
                                       "cache_read_input_tokens": 20,
                                       "cache_creation_input_tokens": 10},
                             "content": [{"type": "text", "text": "ok"}]}},
                {"type": "assistant", "timestamp": ts(3), "sessionId": "sess1",
                 "cwd": str(proj_dir),
                 "message": {"id": "msg-2", "model": "claude-future-9000",
                             "usage": {"input_tokens": 200, "output_tokens": 80},
                             "content": [{"type": "text", "text": "ok2"}]}},
            ]
            with open(proj_dir / "sess1.jsonl", "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry) + "\n")

            since = now - timedelta(days=1)
            sessions, _daily, _per_model, unknown_models, files_scanned = retok.scan(
                [tmp], [], since, None, None)

        self.assertEqual(files_scanned, 1)
        s = sessions["sess1"]
        self.assertEqual(s["prompts"], 1)
        self.assertEqual(s["requests"], 2)
        self.assertEqual(s["input"], 300)
        self.assertEqual(s["output"], 130)
        self.assertEqual(s["cache_read"], 20)
        self.assertEqual(s["cache_write"], 10)
        self.assertGreater(s["cost"], 0)  # only the known-model entry contributes
        self.assertEqual(dict(unknown_models), {"claude-future-9000": 1})


class TestLocaleKeys(unittest.TestCase):
    def test_all_locales_have_exactly_the_en_keys(self):
        en_keys = set(retok.EN.keys())
        locales_dir = RETOK_PATH.parent / "locales"
        locale_files = list(locales_dir.glob("*.json"))
        self.assertTrue(locale_files, "no locale files found")
        for path in locale_files:
            with self.subTest(locale=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(set(data.keys()), en_keys)


if __name__ == "__main__":
    unittest.main()
