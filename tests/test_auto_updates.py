"""Automatic security updates activate only after the manual update clears eligible security packages."""

import unittest

from nanohpc.auto_updates import waiting_security


class SecurityBacklogTest(unittest.TestCase):
    """The backlog gate excludes only packages that automatic updates must never install."""

    def test_waiting_security(self) -> None:
        packages = ["bash", "nvidia-driver-595-open", "cuda-toolkit-13-0"]
        self.assertEqual(waiting_security(packages), ["bash"])
        self.assertEqual(waiting_security(packages[1:]), [])


if __name__ == "__main__":
    unittest.main()
