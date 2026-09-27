import copy
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from metabench.charts import chart_data
from metabench.report import write_report


def row(task="a", step=1, repeat=0, reasoning="high", config=None, **fields):
    return {"suite_id": "frozen", "model": "test-model", "reasoning": reasoning,
            "config_id": config or reasoning, "task_id": task, "step": step, "repeat": repeat,
            "status": "submitted", "score_eligible": True, "critical_pass": True,
            "score": 100, "scores": {"correctness": 100}, **fields}


def points(rows, dimension="correctness", config="high"):
    data = chart_data(rows)
    return next(c for c in data["charts"] if c["dimension"] == dimension)["points"][config]


class ChartsTest(unittest.TestCase):
    def test_tasks_have_equal_weight_despite_unequal_repeat_counts(self):
        rows = [row(repeat=i) for i in range(3)]
        rows.append(row(task="b", score=0, scores={"correctness": 0}))
        point = points(rows)[0]
        self.assertEqual(point["score"], 50)
        self.assertEqual(point["scored_tasks"], 2)

    def test_absent_rounds_errors_and_ineligible_rows_are_gaps(self):
        rows = [row(step=1), row(step=3)]
        self.assertEqual([p["score"] for p in points(rows)], [100, None, 100])
        for change in ({"status": "agent_error"}, {"score_eligible": False}, {"scores": {"correctness": None}}):
            self.assertEqual([p["score"] for p in points([*rows, row(step=2, **change)])], [100, None, 100])
        # A missing repeat must not be hidden by a successful repeat.
        rows = [row(step=1, repeat=0), row(step=1, repeat=1), row(step=2, repeat=0)]
        self.assertIsNone(points(rows)[1]["score"])

    def test_excluded_performance_is_distinct_from_unknown_or_inapplicable(self):
        rows = [row(scores={"performance": 90}),
                row(task="b", scores={"performance": None}, performance_status="excluded_incorrect_solution"),
                row(task="c", scores={"correctness": 100})]
        point = points(rows, "performance")[0]
        self.assertEqual(point["score"], 90)
        self.assertEqual(point["scored_tasks"], 1)
        self.assertEqual(point["applicable_tasks"], 2)
        self.assertEqual(point["excluded_tasks"], ["b"])
        self.assertEqual(point["not_applicable_tasks"], ["c"])
        del rows[1]["performance_status"]
        self.assertIsNone(points(rows, "performance")[0]["score"])
        rows[1]["dimension_statuses"] = {"performance": "excluded"}
        self.assertEqual(points(rows, "performance")[0]["score"], 90)
        # Do not silently select only the correct repeat of a partly excluded task.
        rows.append(row(task="b", repeat=1, scores={"performance": 100}))
        self.assertEqual(points(rows, "performance")[0]["score"], 90)

    def test_configurations_and_task_coverage_are_not_pooled(self):
        rows = [row(config="same-prefix-first", score=0, scores={"correctness": 0}),
                row(config="same-prefix-second"), row(reasoning="max"), row(reasoning="xhigh")]
        data = chart_data(rows)
        self.assertEqual([s["reasoning"] for s in data["series"]], ["high", "high", "xhigh", "max"])
        self.assertEqual(len({s["label"] for s in data["series"]}), 4)
        self.assertEqual(points(rows, config="same-prefix-first")[0]["score"], 0)
        rows.append(row(task="b", config="same-prefix-second"))
        self.assertIsNone(points(rows, config="same-prefix-first")[0]["score"])

    def test_declared_dimensions_survive_all_failed_generation(self):
        failed = row(status="infrastructure_error", score=None, scores={}, score_eligible=False,
                     score_dimensions=["correctness", "future_evolution"])
        data = chart_data([failed])
        self.assertEqual(len(data["charts"]), 2)
        self.assertIsNone(points([failed], "future_evolution")[0]["score"])

    def test_duplicate_mixed_suite_and_invalid_scores_are_rejected(self):
        for rows in ([row(), row()], [row(), row(suite_id="different")],
                     [row(scores={"correctness": float("nan")})],
                     [row(scores={"correctness": -1})], [row(scores={"correctness": True})]):
            with self.assertRaises(ValueError):
                chart_data(rows)

    def test_cli_writes_one_figure_per_dimension_and_preserves_checkpoint_values(self):
        rows = [row(step=step, reasoning=effort, scores={"functional": value, "performance": 98,
                                                       "future_evolution": 100})
                for effort in ("high", "xhigh", "max")
                for step, value in ((1, 80), (2, 100), (3, 40))]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "results.jsonl"
            source.write_text("".join(json.dumps(r) + "\n" for r in rows))
            out = root / "comparison report.md"
            completed = subprocess.run([sys.executable, "-m", "metabench", "report", str(source), "--out", str(out)],
                                       text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            assets = root / "comparison report.charts"
            data = json.loads((assets / "chart-data.json").read_text())
            self.assertEqual(len(data["charts"]), 3)
            self.assertEqual(len(data["series"]), 3)
            self.assertEqual(out.read_text().count("!["), 3)
            self.assertIn("comparison%20report.charts/", out.read_text())
            self.assertEqual([p["score"] for p in data["charts"][0]["points"]["high"]], [80, 100, 40])
            for chart in data["charts"]:
                self.assertTrue((assets / chart["png"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(ET.parse(assets / chart["svg"]).getroot().tag, "{http://www.w3.org/2000/svg}svg")
            # Regeneration retires only images listed in our old manifest.
            (assets / "user-notes.txt").write_text("keep me")
            fewer = copy.deepcopy(rows)
            for item in fewer:
                del item["scores"]["future_evolution"]
            write_report(fewer, out)
            self.assertEqual(len(list(assets.glob("*.png"))), 2)
            self.assertEqual((assets / "user-notes.txt").read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
