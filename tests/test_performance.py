import copy
import unittest
import json
import tempfile
from pathlib import Path

from metabench.checkpoint_eval import performance_scores
from metabench.performance import rescore_rows, rescore_file, PERFORMANCE_POLICY_ID
from metabench.charts import chart_data


def sample(value):
    return {"valid": True, "metrics": [{"name": "latency", "unit": "ns/op", "value": value}]}


class RelativePerformanceTest(unittest.TestCase):
    def metric(self, ratio, tolerance=.05):
        return performance_scores([{"reference": sample(100 * ratio), "candidate": sample(100)}
                                   for _ in range(7)], 7, tolerance)[0]

    def test_parity_has_a_neutral_score_and_speedups_keep_improving(self):
        self.assertEqual(self.metric(1)["score"], 50)
        scores = [self.metric(r)["score"] for r in (.25, .5, 1, 2, 4, 10)]
        self.assertEqual(scores, sorted(set(scores)))
        self.assertAlmostEqual(self.metric(2)["score"], 100 * 2 / 3)
        self.assertAlmostEqual(self.metric(.5)["score"], 100 / 3)
        self.assertLess(self.metric(10)["score"], 100)

    def test_reciprocal_speed_changes_are_symmetric_and_have_no_tolerance_boost(self):
        for ratio in (.1, .95, 1.05, 2, 100):
            self.assertAlmostEqual(self.metric(ratio)["score"] + self.metric(1 / ratio)["score"], 100)
        self.assertEqual(self.metric(1, 0)["score"], self.metric(1, .05)["score"])
        self.assertLess(self.metric(1 / 1.05)["score"], 50)

    def test_score_uses_median_of_paired_ratios_and_preserves_raw_samples(self):
        pairs = [{"reference": sample(r), "candidate": sample(c)}
                 for r, c in [(1, 1), (100, 1), (1, 100), (100, 100), (1, 1), (100, 100), (1, 1)]]
        original = copy.deepcopy(pairs)
        result = performance_scores(pairs, 7, .05)[0]
        self.assertEqual(result["speedup"], 1)
        self.assertEqual(result["score"], 50)
        self.assertEqual(pairs, original)

    def test_invalid_samples_never_receive_a_score(self):
        for value in (0, -1, float('nan'), float('inf'), True):
            pairs=[{"reference": sample(100), "candidate": sample(value)} for _ in range(7)]
            self.assertIsNone(performance_scores(pairs, 7, .05))


class RescoreTest(unittest.TestCase):
    def row(self):
        return {"suite_id":"suite", "config_id":"config", "model":"fixture", "reasoning":"high",
                "task_id":"task", "step":1, "repeat":0, "status":"submitted", "score_eligible":True,
                "critical_pass":True, "performance_status":"measured", "score":90.5,
                "scores":{"functional":100,"performance":52.5,"future_evolution":100},
                "usage":{"total_tokens":100}, "patch_hash":"unchanged", "wall_seconds":20,
                "performance_metrics":[{"name":"latency","unit":"ns/op","score":52.5,
                                        "reference_samples":[100]*7,"candidate_samples":[200]*7}]}

    def test_rescore_reuses_samples_and_updates_total_without_modifying_source(self):
        row=self.row();original=copy.deepcopy(row)
        result=rescore_rows([row])[0]
        self.assertEqual(row,original)
        self.assertAlmostEqual(result['scores']['performance'],100/3)
        self.assertAlmostEqual(result['score'],80+20/3)
        for field in ('usage','patch_hash','wall_seconds','suite_id'):
            self.assertEqual(result[field],original[field])
        self.assertEqual(result['performance_rescoring']['source_scores'],original['scores'])
        self.assertEqual(result['performance_metrics'][0]['reference_samples'],[100]*7)
        self.assertEqual(rescore_rows([result]),[result])

    def test_exclusion_and_failed_measurements_are_preserved(self):
        row=self.row();row.update(critical_pass=False,performance_status='excluded_incorrect_solution',performance_metrics=[])
        row['scores'].update(functional=0,performance=None);row['score']=0
        result=rescore_rows([row])[0]
        self.assertIsNone(result['scores']['performance']);self.assertEqual(result['score'],0)
        row=self.row();row.update(performance_status='measurement_failed',performance_metrics=[])
        row['scores']['performance']=None;row['score']=None
        self.assertIsNone(rescore_rows([row])[0]['score'])

    def test_missing_samples_are_not_reconstructed_from_the_old_score(self):
        row=self.row();row['performance_metrics'][0]['candidate_samples']=[]
        with self.assertRaisesRegex(ValueError,'misaligned'):
            rescore_rows([row])

    def test_cli_output_preserves_input_and_records_policy_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'raw.jsonl';out=Path(directory)/'rescored.jsonl'
            raw=json.dumps(self.row())+'\n';source.write_text(raw)
            rescore_file(source,out)
            self.assertEqual(source.read_text(),raw)
            self.assertEqual(json.loads(out.read_text())['performance_policy_id'],PERFORMANCE_POLICY_ID)
            self.assertEqual(json.loads(out.with_suffix('.jsonl.policy.json').read_text())['model_calls'],0)
            with self.assertRaisesRegex(ValueError,'different file'):
                rescore_file(source,source)

    def test_charts_reject_mixing_old_and_new_score_policies(self):
        old=self.row();new=rescore_rows([self.row()])[0];new['task_id']='another'
        with self.assertRaisesRegex(ValueError,'performance scoring policy'):
            chart_data([old,new])
        self.assertEqual(chart_data([new])['performance_policy_id'],PERFORMANCE_POLICY_ID)


if __name__ == "__main__":
    unittest.main()
