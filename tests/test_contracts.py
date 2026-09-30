import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from scholarship_pool.contracts import FundingRuleVersion, ApplicationRecord


class ContractTests(unittest.TestCase):
    def test_record_keeps_version_reference(self):
        entity = FundingRuleVersion("E-1", "示例实体", 1)
        record = ApplicationRecord("R-1", entity.entity_id, "已登记")
        self.assertEqual(record.entity_id, "E-1")

    def test_invalid_revision_is_rejected(self):
        with self.assertRaises(ValueError):
            FundingRuleVersion("E-2", "无效版本", 0)


if __name__ == "__main__":
    unittest.main()
