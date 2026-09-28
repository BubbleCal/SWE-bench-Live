import unittest

from metabench import usage


class UsageTest(unittest.TestCase):
    def test_subsets_are_not_double_counted(self):
        result = usage.normalize({"input_tokens": 100, "cached_input_tokens": 80,
                                  "output_tokens": 40, "reasoning_output_tokens": 30})
        self.assertEqual(result["total_tokens"], 140)
        self.assertEqual(result["uncached_input_tokens"], 20)
        self.assertEqual(result["non_reasoning_output_tokens"], 10)
        self.assertIsNone(result["cost_usd"])

    def test_unknown_usage_propagates(self):
        result = usage.add(usage.empty(), usage.normalize({"input_tokens": 10, "output_tokens": 5}))
        self.assertEqual(result["total_tokens"], 15)
        self.assertIsNone(result["cached_input_tokens"])
        self.assertIsNone(usage.add(result, usage.normalize({}))["total_tokens"])

    def test_invalid_counters_are_rejected(self):
        for value in ({"input_tokens": -1}, {"output_tokens": 1.5},
                      {"input_tokens": 10, "cached_input_tokens": 11},
                      {"output_tokens": 10, "reasoning_output_tokens": 11}):
            with self.assertRaises(ValueError):
                usage.normalize(value)
