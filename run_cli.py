import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from scholarship_pool.contracts import FundingRuleVersion, ApplicationRecord

entity = FundingRuleVersion("E-DEMO", "区域奖学金名额治理", 1)
record = ApplicationRecord("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
