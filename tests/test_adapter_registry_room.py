import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
from adapters import get_adapter_class, list_adapter_types


class RegistrySmokeTests(unittest.TestCase):
    def test_entries_resolve(self):
        names = list_adapter_types()
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            self.assertEqual(get_adapter_class(name).type, name)


if __name__ == '__main__':
    unittest.main()
