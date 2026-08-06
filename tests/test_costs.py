import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cccswitcher.costs import seven_day_report
from cccswitcher.pricing import estimate_cost


UTC = ZoneInfo("UTC")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


class SevenDayCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def report(self, **kwargs):
        return seven_day_report(
            claude_paths=[self.claude],
            codex_paths=[self.codex],
            now=datetime(2026, 7, 9, 12, tzinfo=UTC),
            tz=UTC,
            **kwargs,
        )

    def test_always_emits_seven_ordered_empty_days(self):
        report = self.report()
        self.assertEqual(report["start_date"], "2026-07-03")
        self.assertEqual(report["end_date"], "2026-07-09")
        self.assertEqual([d["date"] for d in report["days"]], [
            "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06",
            "2026-07-07", "2026-07-08", "2026-07-09",
        ])
        self.assertEqual(report["status"], "exact")
        self.assertEqual(report["cost"], 0.0)
        json.dumps(report)  # snapshot contract is JSON serializable

    def test_claude_request_dedup_attribution_and_exact_cost(self):
        record = {
            "type": "assistant",
            "requestId": "req-one",
            "timestamp": "2026-07-08T05:30:00Z",
            "email": "stable@example.test",
            "message": {
                "model": "unpriced-private-model",
                "usage": {"input_tokens": 100, "output_tokens": 20, "costUSD": 0.42},
            },
        }
        write_jsonl(self.claude / "main.jsonl", [record, {**record, "uuid": "stream-copy"}])
        write_jsonl(self.claude / "subagents" / "copy.jsonl", [record])
        report = self.report(account_labels={"claude": {"stable@example.test": "Work"}})
        self.assertEqual(report["event_count"], 1)
        self.assertEqual(report["deduplicated_events"], 2)
        self.assertEqual(report["cost"], 0.42)
        self.assertEqual(report["status"], "exact")
        self.assertEqual(report["accounts"][0]["account"], "Work")
        self.assertEqual(report["accounts"][0]["tokens"]["total"], 120)

    def test_configured_account_without_local_events_is_retained(self):
        report = self.report(account_labels={
            "claude": {"account-a": "Work", "account-b": "Personal"},
            "codex": {"account-c": "Team"},
        })
        self.assertEqual(
            [(row["provider"], row["account"], row["event_count"]) for row in report["accounts"]],
            [("claude", "Personal", 0), ("claude", "Work", 0), ("codex", "Team", 0)],
        )

    def test_codex_uses_turn_deltas_not_cumulative_totals(self):
        records = [
            {"type": "session_meta", "timestamp": "2026-07-08T10:00:00Z",
             "payload": {"id": "session-a", "account_id": "acct-1"}},
            {"type": "event_msg", "timestamp": "2026-07-08T10:01:00Z",
             "payload": {"type": "turn_context", "model": "gpt-5.1-codex"}},
            {"type": "event_msg", "timestamp": "2026-07-08T10:02:00Z", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                         "output_tokens": 100, "total_tokens": 1100},
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100},
                }}},
            # A repeated emission of the same cumulative snapshot is one event.
            {"type": "event_msg", "timestamp": "2026-07-08T10:02:01Z", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                         "output_tokens": 100, "total_tokens": 1100},
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100},
                }}},
            {"type": "event_msg", "timestamp": "2026-07-08T10:03:00Z", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 500, "cached_input_tokens": 100,
                                         "output_tokens": 50, "total_tokens": 550},
                    "total_token_usage": {"input_tokens": 1500, "output_tokens": 150},
                }}},
        ]
        write_jsonl(self.codex / "rollout.jsonl", records)
        report = self.report(account_labels={"codex": {"acct-1": "Personal"}})
        codex = report["providers"]["codex"]
        self.assertEqual(codex["event_count"], 2)
        self.assertEqual(codex["tokens"]["total"], 1650)
        self.assertEqual(report["deduplicated_events"], 1)
        self.assertEqual(report["accounts"][0]["account"], "Personal")
        self.assertEqual(codex["status"], "estimated")

    def test_timezone_boundaries_and_dst_use_local_calendar_dates(self):
        la = ZoneInfo("America/Los_Angeles")
        write_jsonl(self.claude / "events.jsonl", [
            {"timestamp": "2026-03-02T07:59:59Z", "requestId": "outside",
             "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 1}}},
            {"timestamp": "2026-03-02T08:00:00Z", "requestId": "inside",
             "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 2}}},
            # After the DST jump, this is still local Sunday March 8.
            {"timestamp": "2026-03-09T06:30:00Z", "requestId": "today",
             "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 3}}},
        ])
        report = seven_day_report(
            claude_paths=[self.claude], codex_paths=[self.codex],
            now=datetime(2026, 3, 8, 23, 30, tzinfo=la), tz=la,
        )
        self.assertEqual(report["start_date"], "2026-03-02")
        self.assertEqual(report["event_count"], 2)
        self.assertEqual(report["days"][0]["tokens"]["input"], 2)
        self.assertEqual(report["days"][-1]["tokens"]["input"], 3)

    def test_unknown_and_partial_pricing_are_not_presented_as_zero_or_total(self):
        write_jsonl(self.claude / "events.jsonl", [
            {"timestamp": "2026-07-09T01:00:00Z", "requestId": "known",
             "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 1_000_000}}},
            {"timestamp": "2026-07-09T02:00:00Z", "requestId": "unknown",
             "message": {"model": "future-secret-model", "usage": {"input_tokens": 10}}},
        ])
        report = self.report()
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["cost"])
        self.assertEqual(report["known_cost"], 3.0)
        unknown = next(m for m in report["models"] if m["model"] == "future-secret-model")
        self.assertEqual(unknown["status"], "unknown")
        self.assertIsNone(unknown["cost"])

    def test_specific_model_prices_precede_family_and_long_context_tier(self):
        base = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0}
        self.assertEqual(estimate_cost("codex", "gpt-5.4-mini", base), 1.5)
        self.assertEqual(estimate_cost("codex", "gpt-5.4-pro", base), 60.0)
        self.assertEqual(estimate_cost("claude", "claude-fable-5", base), 10.0)

    def test_provider_and_daily_views_sum_same_deduplicated_events(self):
        write_jsonl(self.claude / "one.jsonl", [{
            "timestamp": "2026-07-07T00:00:00Z", "requestId": "c1", "account_id": "a",
            "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 10, "costUSD": 1.0}},
        }])
        write_jsonl(self.codex / "one.jsonl", [
            {"type": "session_meta", "payload": {"id": "s"}, "timestamp": "2026-07-07T00:00:00Z"},
            {"type": "turn_context", "payload": {"type": "turn_context", "model": "gpt-5"}},
            {"type": "event_msg", "timestamp": "2026-07-07T01:00:00Z", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "cost_usd": 2.0},
                                                    "total_token_usage": {"input_tokens": 10}}}},
        ])
        report = self.report()
        self.assertEqual(report["cost"], 3.0)
        self.assertEqual(sum(p["cost"] for p in report["providers"].values()), 3.0)
        self.assertEqual(sum(day["cost"] for day in report["days"]), 3.0)
        self.assertEqual(sum(a["cost"] for a in report["accounts"]), 3.0)

    def test_malformed_lines_are_counted_without_losing_valid_usage(self):
        self.claude.mkdir()
        (self.claude / "bad.jsonl").write_text(
            "not-json\n" + json.dumps({"timestamp": "2026-07-09T01:00:00Z", "requestId": "ok",
            "message": {"model": "claude-sonnet-4", "usage": {"input_tokens": 1}}}) + "\n",
            encoding="utf-8",
        )
        report = self.report()
        self.assertEqual(report["parse_errors"], 1)
        self.assertEqual(report["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
