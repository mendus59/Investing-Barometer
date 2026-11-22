"""Checks for the ZIP data builder. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_zip as z  # noqa: E402

ZHVI = (
    "RegionID,SizeRank,RegionName,RegionType,StateName,State,City,Metro,CountyName,2025-09-30,2025-10-31,2026-10-31\n"
    "1,1,2134,zip,MA,MA,Boston,\"Boston, MA\",Suffolk County,100,100,110\n"
    "2,2,98101,zip,WA,WA,Seattle,\"Seattle, WA\",King County,200,,190\n"
)
GAZ = "GEOID\tALAND\tINTPTLAT\tINTPTLONG   \n02134\t1\t42.358162\t-71.112624\n"


class ZipBuild(unittest.TestCase):
    def test_gazetteer_parses_padded_header(self):
        self.assertEqual(z.parse_gazetteer(GAZ), {"02134": (42.358162, -71.112624)})

    def test_gazetteer_parses_pipe_delimited(self):
        text = "GEOID|GEOIDFQ|ALAND|INTPTLAT|INTPTLONG\n00601|860Z200US00601|1|18.18|-66.75\n"
        self.assertEqual(z.parse_gazetteer(text), {"00601": (18.18, -66.75)})

    def test_build_pads_zips_and_indexes_located_ones(self):
        shards, meta, near = z.build(ZHVI, None, None, {}, z.parse_gazetteer(GAZ), 2025)
        self.assertIn("02134", shards["021"])
        self.assertEqual(shards["021"]["02134"]["v"], 110)
        self.assertEqual(meta["zip_count"], 2)
        self.assertEqual(near["z"], ["02134"])  # 98101 has no coordinates
        self.assertEqual(meta["located_count"], 1)


if __name__ == "__main__":
    unittest.main()
