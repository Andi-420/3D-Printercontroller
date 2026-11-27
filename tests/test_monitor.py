import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import PrinterState, deep_merge  # noqa: E402


class DeepMergeTest(unittest.TestCase):
    def test_nested_dicts_are_merged(self):
        state = {"print": {"bed_temper": 20, "nozzle_temper": 25}}
        deep_merge(state, {"print": {"bed_temper": 60}})
        self.assertEqual(state, {"print": {"bed_temper": 60, "nozzle_temper": 25}})

    def test_lists_with_id_are_merged_by_id(self):
        state = {"ams": [{"id": "0", "humidity": "3", "tray": [{"id": "0", "tray_type": "PLA", "remain": 80}]}]}
        deep_merge(state, {"ams": [{"id": "0", "tray": [{"id": "0", "remain": 79}]}]})
        tray = state["ams"][0]["tray"][0]
        self.assertEqual(tray, {"id": "0", "tray_type": "PLA", "remain": 79})
        self.assertEqual(state["ams"][0]["humidity"], "3")

    def test_new_ids_are_appended(self):
        state = {"ams": [{"id": "0"}]}
        deep_merge(state, {"ams": [{"id": "1", "temp": "25"}]})
        self.assertEqual([a["id"] for a in state["ams"]], ["0", "1"])

    def test_plain_lists_are_replaced(self):
        state = {"hms": [{"attr": 1, "code": 2}]}
        deep_merge(state, {"hms": []})
        self.assertEqual(state["hms"], [])

    def test_source_is_not_aliased(self):
        update = {"print": {"lights_report": [{"node": "chamber_light", "mode": "on"}]}}
        state = {}
        deep_merge(state, update)
        update["print"]["lights_report"][0]["mode"] = "off"
        self.assertEqual(state["print"]["lights_report"][0]["mode"], "on")


class PrinterStateTest(unittest.TestCase):
    def test_merge_bumps_version_and_counts(self):
        s = PrinterState("h", "S", demo=False)
        v0 = s.snapshot()["version"]
        s.merge({"print": {"mc_percent": 5}})
        snap = s.snapshot()
        self.assertGreater(snap["version"], v0)
        self.assertEqual(snap["meta"]["messages"], 1)
        self.assertEqual(snap["state"]["print"]["mc_percent"], 5)

    def test_wait_for_change_times_out_without_update(self):
        s = PrinterState("h", "S", demo=False)
        self.assertFalse(s.wait_for_change(s.snapshot()["version"], timeout=0.05))


if __name__ == "__main__":
    unittest.main()
