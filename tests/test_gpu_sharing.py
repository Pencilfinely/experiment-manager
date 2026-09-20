"""Admission boundaries for shared GPUs; no Docker or real workloads required."""
import copy
import unittest

from expman.scheduler import eligible, select_device
from tests.test_core import gpu_task, snapshot


class GPUSharingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snapshot()
        self.snapshot["policy"].update(cpu_budget=8, ram_budget_mb=32000)

    def active(self, memory=4000, *, exclusive=False, gpu="GPU-1", cpu=1, ram=1024):
        task = gpu_task(memory, exclusive)
        task["resources"].update(cpu=cpu, ram_mb=ram)
        return {"spec": task, "gpu_uuid": gpu}

    def test_external_gpu_use_allows_shared_and_software_exclusive_tasks(self):
        # 10 GB belongs to another process; the remaining 6 GB covers our
        # 4 GB task and 2 GB reserve. Even exclusive only excludes our own jobs.
        self.snapshot["gpus"][0]["free_mb"] = 6000
        for exclusive in (False, True):
            with self.subTest(exclusive=exclusive):
                choice = select_device(gpu_task(4000, exclusive), self.snapshot, [])
                self.assertEqual(choice["gpu_uuid"], "GPU-1")

    def test_two_shared_tasks_start_on_the_same_partly_used_card(self):
        task = gpu_task(3000)
        self.snapshot["gpus"][0].update(free_mb=13000, max_jobs=2)
        first = select_device(task, self.snapshot, [])
        self.assertEqual(first["gpu_uuid"], "GPU-1")
        # The first task has now allocated its 3 GB. Its complete declared
        # budget remains reserved because telemetry provides no ownership.
        self.snapshot["gpus"][0]["free_mb"] = 10000
        active = [{"spec": task, "gpu_uuid": first["gpu_uuid"]}]
        second = select_device(task, self.snapshot, active)
        self.assertEqual(second["gpu_uuid"], first["gpu_uuid"])
        active.append({"spec": task, "gpu_uuid": second["gpu_uuid"]})
        self.assertIsNone(select_device(task, self.snapshot, active))

    def test_either_task_exclusive_blocks_software_colocation(self):
        for existing, incoming in ((False, True), (True, False), (True, True)):
            with self.subTest(existing=existing, incoming=incoming):
                active = [self.active(exclusive=existing)]
                self.assertIsNone(select_device(gpu_task(1000, incoming), self.snapshot, active))
        self.assertIsNotNone(select_device(gpu_task(1000), self.snapshot, [self.active()]))

    def test_exclusive_on_one_card_does_not_block_another_card(self):
        second = copy.deepcopy(self.snapshot["gpus"][0])
        second["uuid"] = "GPU-2"
        self.snapshot["gpus"].append(second)
        choice = select_device(gpu_task(1000, True), self.snapshot,
                               [self.active(exclusive=True)])
        self.assertEqual(choice["gpu_uuid"], "GPU-2")

    def test_unattributed_used_vram_cannot_replace_active_growth_reservation(self):
        active = [self.active(memory=6000)]
        self.snapshot["gpus"][0].update(free_mb=10000, reserve_mb=1000)
        self.assertIsNotNone(select_device(gpu_task(3000), self.snapshot, active))
        # All observed used VRAM could belong to external processes, while our
        # active task has not allocated yet. It still needs its full 6 GB.
        self.snapshot["gpus"][0]["free_mb"] -= 1
        self.assertIsNone(select_device(gpu_task(3000), self.snapshot, active))

    def test_gpu_reserve_is_enforced_at_exact_available_memory_boundary(self):
        self.snapshot["gpus"][0]["free_mb"] = 6000
        self.assertIsNotNone(select_device(gpu_task(4000), self.snapshot, []))
        self.snapshot["gpus"][0]["reserve_mb"] = 2001
        self.assertIsNone(select_device(gpu_task(4000), self.snapshot, []))

    def test_node_concurrency_and_zero_pause_apply_before_start(self):
        self.snapshot["policy"]["max_running"] = 1
        self.assertIsNotNone(select_device(gpu_task(1000), self.snapshot, []))
        # Node concurrency includes software tasks on other GPUs too.
        self.assertIsNone(select_device(gpu_task(1000), self.snapshot,
                                        [self.active(gpu="GPU-2")]))
        self.snapshot["policy"]["max_running"] = 0
        self.assertIsNone(select_device(gpu_task(1000), self.snapshot, []))

    def test_disabled_gpu_zero_is_never_selected_even_when_manually_requested(self):
        disabled = copy.deepcopy(self.snapshot["gpus"][0])
        disabled.update(uuid="GPU-0", max_jobs=0, free_mb=5000)
        self.snapshot["gpus"].insert(0, disabled)
        self.assertEqual(select_device(gpu_task(3000), self.snapshot, [])["gpu_uuid"], "GPU-1")
        self.snapshot["capabilities"] = ["scheduler-v2"]
        manual = gpu_task(3000)
        manual["scheduling"] = {"mode": "manual", "node_ids": ["node-a"], "gpu_uuids": ["GPU-0"]}
        self.assertFalse(eligible(manual, self.snapshot))
        self.assertIsNone(select_device(manual, self.snapshot, []))
        # Changing the target to an enabled card permits sharing, but does not
        # bypass memory admission when external usage increases afterward.
        manual["scheduling"]["gpu_uuids"] = ["GPU-1"]
        self.assertEqual(select_device(manual, self.snapshot, [])["gpu_uuid"], "GPU-1")
        self.snapshot["gpus"][1]["free_mb"] = 4999
        self.assertIsNone(select_device(manual, self.snapshot, []))

    def test_cpu_budget_sums_all_active_tasks_across_gpus(self):
        task = gpu_task(1000)
        task["resources"]["cpu"] = 2
        active = [self.active(gpu="GPU-2", cpu=3)]
        self.snapshot["policy"]["cpu_budget"] = 5
        self.assertIsNotNone(select_device(task, self.snapshot, active))
        self.snapshot["policy"]["cpu_budget"] = 4
        self.assertIsNone(select_device(task, self.snapshot, active))

    def test_ram_budget_sums_all_active_tasks_across_gpus(self):
        task = gpu_task(1000)
        task["resources"]["ram_mb"] = 3000
        active = [self.active(gpu="GPU-2", ram=5000)]
        self.snapshot["policy"]["ram_budget_mb"] = 8000
        self.assertIsNotNone(select_device(task, self.snapshot, active))
        self.snapshot["policy"]["ram_budget_mb"] = 7999
        self.assertIsNone(select_device(task, self.snapshot, active))

    def test_free_ram_keeps_growth_headroom_even_when_budget_allows_more(self):
        task = gpu_task(1000)
        task["resources"]["ram_mb"] = 3000
        active = [self.active(gpu="GPU-2", ram=5000)]
        self.snapshot["free_ram_mb"] = 8000
        self.assertIsNotNone(select_device(task, self.snapshot, active))
        self.snapshot["free_ram_mb"] = 7999
        self.assertIsNone(select_device(task, self.snapshot, active))

    def test_unknown_memory_and_insufficient_disk_never_admit_a_gpu_task(self):
        for target, field in ((self.snapshot, "free_ram_mb"),
                              (self.snapshot["gpus"][0], "free_mb")):
            original = target[field]
            for missing in (None, float("nan"), float("inf")):
                with self.subTest(field=field, value=missing):
                    target[field] = missing
                    self.assertIsNone(select_device(gpu_task(1000), self.snapshot, []))
            target[field] = original
        self.snapshot["disk_free_mb"] = 1023
        self.assertIsNone(select_device(gpu_task(1000), self.snapshot, []))


if __name__ == "__main__":
    unittest.main()
