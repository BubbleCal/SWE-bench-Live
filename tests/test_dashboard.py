import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from metabench.dashboard import dashboard_data, dashboard_html, dashboard_server, results_loader
from metabench.report import write_report


def row(task="a", step=1, **fields):
    return {"suite_id":"suite", "config_id":"config", "model":"model", "reasoning":"high",
            "task_id":task, "repeat":0, "step":step, "score_eligible":True, "status":"submitted",
            "scores":{"functional":100, "performance":None, "future_evolution":None}, **fields}


class DashboardTest(unittest.TestCase):
    def test_common_task_group_keeps_full_suite_missing_results_as_gaps(self):
        rows = [row(), row(task="b", scores={"functional": 0}),
                row(config_id="historical", model="historical", scores={"functional": 80})]
        metadata = {"task_groups": [{"id": "common", "label": "Common tasks", "task_ids": ["a"]}]}
        data = dashboard_data(rows, metadata)
        full = data["charts"][0]["points"]
        common = data["task_groups"][0]["charts"][0]["points"]
        self.assertEqual(full["config"][0]["score"], 50)
        self.assertIsNone(full["historical"][0]["score"])
        self.assertEqual(full["historical"][0]["missing_tasks"], ["b"])
        self.assertEqual(common["config"][0]["score"], 100)
        self.assertEqual(common["historical"][0]["score"], 80)
        self.assertEqual(common["historical"][0]["applicable_tasks"], 1)
        for ids in ([], ["a", "a"], ["missing"]):
            with self.assertRaises(ValueError):
                dashboard_data(rows, {"task_groups": [{"id": "common", "label": "Common tasks", "task_ids": ids}]})

    def test_partial_tasks_remain_gaps_but_can_be_inspected_individually(self):
        rows=[row(),row(task="b",config_id="other",model="other")]
        data=dashboard_data(rows)
        self.assertIsNone(data["charts"][0]["points"]["config"][0]["score"])
        self.assertEqual(data["task_charts"]["a"][0]["points"]["config"][0]["score"],100)
        self.assertEqual(len(data["charts"]),2)

    def test_export_is_standalone_and_does_not_embed_transcripts_or_executable_labels(self):
        unsafe="</script><script>alert(1)</script>"
        data=dashboard_data([row(model=unsafe,transcript="secret transcript",patch="secret patch")])
        html=dashboard_html(data)
        self.assertNotIn(unsafe,html)
        self.assertNotIn("secret transcript",html)
        self.assertNotIn("secret patch",html)
        embedded=html.split('<script id="metabench-data" type="application/json">',1)[1].split('</script>',1)[0]
        self.assertEqual(json.loads(embedded)["data"]["series"][0]["model"],unsafe)
        self.assertFalse(json.loads(embedded)["live"])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"report.html"
            write_report([row()],path)
            self.assertTrue(path.read_text().startswith("<!doctype html>"))
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_live_server_refreshes_files_and_rejects_partial_or_mixed_suite_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"rows.jsonl"
            path.write_text(json.dumps(row())+'\n')
            with dashboard_server(results_loader([path]),port=0) as server:
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                url=f"http://127.0.0.1:{server.server_port}"
                try:
                    first=json.load(urlopen(url+'/data.json'))
                    path.write_text(json.dumps(row())+'\n'+json.dumps(row(step=2))+'\n')
                    second=json.load(urlopen(url+'/data.json'))
                    self.assertNotEqual(first['version'],second['version'])
                    self.assertEqual(second['steps'],[1,2])
                    with urlopen(url) as response:
                        self.assertIn('no-store',response.headers['Cache-Control'])
                        self.assertNotIn('Access-Control-Allow-Origin',response.headers)
                    for invalid in ('{"partial":', json.dumps(row())+'\n'+json.dumps(row(step=2,suite_id='other'))):
                        path.write_text(invalid)
                        with self.assertRaises(HTTPError) as error:urlopen(url+'/data.json')
                        self.assertEqual(error.exception.code,503)
                        error.exception.close()
                    with self.assertRaises(HTTPError) as error:urlopen(url+'/../../pyproject.toml')
                    self.assertEqual(error.exception.code,404)
                    error.exception.close()
                finally:
                    server.shutdown();worker.join()


if __name__=='__main__':unittest.main()
