"""Every VM test in tests/test_sim.py is skipped unless NANOHPC_SIM=1, so a plain test run never starts VMs."""

import inspect
import os
import unittest

from tests import test_sim

# Classes in test_sim that run without VMs.
NO_VM_CLASSES = {"SimPlanTest", "MonitorSimPlanTest", "SimOutputTest", "SimCommandTest", "SimUsersBase"}


class SimGuardTest(unittest.TestCase):
    """A test class in test_sim that is not in NO_VM_CLASSES carries the NANOHPC_SIM skip."""

    @unittest.skipIf(os.environ.get("NANOHPC_SIM") == "1", "the guard is off when VM tests are wanted")
    def test_every_vm_class_is_skipped_without_nanohpc_sim(self) -> None:
        unguarded = [
            name
            for name, item in inspect.getmembers(test_sim, inspect.isclass)
            if issubclass(item, unittest.TestCase)
            and item.__module__ == test_sim.__name__
            and name not in NO_VM_CLASSES
            and not getattr(item, "__unittest_skip__", False)
        ]
        self.assertEqual(unguarded, [])


if __name__ == "__main__":
    unittest.main()
