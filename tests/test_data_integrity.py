import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from data_integrity import session_check, validate_quote, structural_rr, parse_timestamp
IST=ZoneInfo("Asia/Kolkata")

class DataIntegrityTests(unittest.TestCase):
    def test_weekend_closed(self):
        self.assertEqual(session_check(datetime(2026,10,17,12,tzinfo=IST),set(),True)[1],"WEEKEND")
    def test_calendar_unverified_fails(self):
        self.assertFalse(session_check(datetime(2026,10,15,12,tzinfo=IST))[0])
    def test_holiday_closed(self):
        t=datetime(2026,10,15,12,tzinfo=IST)
        self.assertFalse(session_check(t,{t.date()},True)[0])
    def test_naive_timestamp_rejected(self):
        self.assertIsNone(parse_timestamp("2026-10-15T12:00:00"))
    def test_stale_quote_rejected(self):
        t=datetime(2026,10,15,12,tzinfo=IST)
        q={"last_price":100,"timestamp":(t-timedelta(minutes=10)).isoformat()}
        self.assertFalse(validate_quote(q,t)[0])
    def test_fresh_quote_accepted(self):
        t=datetime(2026,10,15,12,tzinfo=IST)
        self.assertTrue(validate_quote({"last_price":100,"timestamp":t.isoformat()},t)[0])
    def test_structural_rr(self):
        self.assertEqual(structural_rr("LONG",100,98,106),3)
        self.assertIsNone(structural_rr("LONG",100,101,106))
        self.assertIsNone(structural_rr("LONG",100,98,None))

if __name__=="__main__":
    unittest.main()
