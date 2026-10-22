import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import paper_tracking as paper
spec=importlib.util.spec_from_file_location('midday_scanner',ROOT/'scanner.py')
scanner=importlib.util.module_from_spec(spec);sys.modules[spec.name]=scanner;spec.loader.exec_module(scanner)


class MiddayTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((ROOT/'config.json').read_text())['technical']
        self.row={'change_pct':1,'turnover_cr':30}
        self.t={'close':100,'vwap':99.8,'ema9':100,'ema20':99.9,'ema50':99.5,'rsi':60,
                'atr':1,'macd':1,'macd_signal':0,'rvol_same_time':2,'opening_complete':True,
                'opening_high':98,'opening_low':95,'recent_low':99.8,'recent_high':100.2,
                'resistance_levels':[103.5],'midday_range_high':100.1,'midday_range_low':99.9,'midday_range_bars':6}

    def test_midday_uses_recent_consolidation_after_opening_move(self):
        old=scanner.score_candidate(self.row,self.t,self.config,'morning')
        new=scanner.score_candidate(self.row,self.t,self.config,'midday')
        self.assertEqual(old['status'],'REJECT')
        self.assertIn('Opening trigger already passed',old['reason'])
        self.assertEqual(new['status'],'WATCH')
        self.assertEqual(new['setup_type'],'MIDDAY_CONSOLIDATION_BREAKOUT')
        self.assertEqual(new['trigger'],100.15)
        self.assertEqual(new['stop'],99.85)
        self.assertLessEqual(new['expected_move_pct'],2)
        self.assertGreaterEqual(new['rr'],2)

    def test_missing_recent_bars_fail_closed(self):
        self.t.pop('midday_range_low')
        self.assertEqual(scanner.score_candidate(self.row,self.t,self.config,'midday')['status'],'DATA_UNAVAILABLE')

    def test_wide_consolidation_rejected(self):
        self.t['midday_range_low']=97
        result=scanner.score_candidate(self.row,self.t,self.config,'midday')
        self.assertEqual(result['status'],'REJECT')
        self.assertIn('Midday range too wide',result['reason'])

    def test_nearby_barrier_not_skipped_to_force_two_percent(self):
        self.t['resistance_levels']=[100.3,104]
        result=scanner.score_candidate(self.row,self.t,self.config,'midday')
        self.assertEqual(result['target'],100.3)
        self.assertEqual(result['status'],'REJECT')

    def test_midday_no_chasing_or_weaker_momentum(self):
        self.t.update(close=104,rsi=80)
        result=scanner.score_candidate(self.row,self.t,self.config,'midday')
        self.assertEqual(result['status'],'REJECT')
        self.assertIn('RSI confirmation absent',result['reason'])
        self.assertIn('Extended from VWAP',result['reason'])

    def test_live_quote_cannot_chase_beyond_completed_range(self):
        self.row['ltp']=100.3
        result=scanner.score_candidate(self.row,self.t,self.config,'midday')
        self.assertEqual(result['status'],'REJECT')
        self.assertIn('Midday trigger already passed in current quote',result['reason'])


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,22,10,0,tzinfo=paper.IST)
        self.signal={'created_at':self.start.isoformat(),'symbol':'FIXTURE','instrument_key':'NSE_EQ|FIXTURE',
                     'direction':'LONG','trigger':100,'stop':99,'target':102}

    def bar(self,minute,o=99.8,h=100.5,l=99.5,c=100.2):
        return {'timestamp':(self.start+timedelta(minutes=minute)).isoformat(),'open':o,'high':h,'low':l,'close':c}

    def test_future_candle_cannot_create_historical_fill(self):
        result=paper.evaluate(self.signal,[self.bar(0,h=103)],self.start+timedelta(minutes=4))
        self.assertEqual(result['status'],'PENDING')

    def test_partial_formation_candle_is_ambiguous(self):
        self.signal['created_at']=(self.start+timedelta(seconds=20)).isoformat()
        self.assertEqual(paper.evaluate(self.signal,[self.bar(0)],self.start+timedelta(minutes=5))['status'],'AMBIGUOUS_START')

    def test_target_after_entry_with_cost_assumptions(self):
        bars=[self.bar(0),self.bar(5,o=100.2,h=102.5,l=100,c=102)]
        result=paper.evaluate(self.signal,bars,self.start+timedelta(minutes=10))
        self.assertEqual(result['status'],'TARGET')
        self.assertAlmostEqual(result['gross_pct'],2)
        self.assertAlmostEqual(result['illustrative_net_pct'],1.8)

    def test_target_and_stop_same_bar_never_counted_as_win(self):
        bars=[self.bar(0),self.bar(5,o=100.2,h=103,l=98,c=100)]
        self.assertEqual(paper.evaluate(self.signal,bars,self.start+timedelta(minutes=10))['status'],'AMBIGUOUS')

    def test_stop_on_entry_bar_has_unknown_order(self):
        self.assertEqual(paper.evaluate(self.signal,[self.bar(0,l=98)],self.start+timedelta(minutes=5))['status'],'AMBIGUOUS_ENTRY_BAR')

    def test_gap_entry_cancelled(self):
        self.assertEqual(paper.evaluate(self.signal,[self.bar(0,o=100.1)],self.start+timedelta(minutes=5))['status'],'GAP_CANCELLED')

    def test_missing_bars_do_not_become_pending_or_wins(self):
        result=paper.evaluate(self.signal,[self.bar(5,h=103)],self.start+timedelta(minutes=10))
        self.assertEqual(result['status'],'DATA_INCOMPLETE')

    def test_gap_stop_uses_worse_open(self):
        bars=[self.bar(0),self.bar(5,o=98,h=98.5,l=97,c=98)]
        result=paper.evaluate(self.signal,bars,self.start+timedelta(minutes=10))
        self.assertEqual(result['status'],'STOP')
        self.assertEqual(result['exit_price'],98)

    def test_short_direction(self):
        self.signal.update(direction='SHORT',stop=101,target=98)
        bars=[self.bar(0,o=100.2,h=100.5,l=99.5,c=99.8),self.bar(5,o=99.8,h=100,l=97.5,c=98)]
        result=paper.evaluate(self.signal,bars,self.start+timedelta(minutes=10))
        self.assertEqual(result['status'],'TARGET')
        self.assertEqual(result['gross_pct'],2)

    def test_no_trigger_expires(self):
        bars=[self.bar(i,o=99.5,h=99.8,l=99.2,c=99.5) for i in range(0,60,5)]
        self.assertEqual(paper.evaluate(self.signal,bars,self.start+timedelta(minutes=60))['status'],'EXPIRED')

    def test_invalid_prices_rejected(self):
        with self.assertRaises(ValueError):
            paper.evaluate(self.signal,[self.bar(0,o=float('nan'))],self.start+timedelta(minutes=5))

    def test_persistence_idempotence_and_diagnostic_exclusion(self):
        report={'mode':'morning','test_data':False,'timestamp':self.start.isoformat(),
                'watches':[{**self.signal,'status':'WATCH'}]}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.sqlite3'
            fetch=lambda key,day:[self.bar(0,h=103)]
            first=paper.update_paper_tracking(path,report,fetch,self.start)
            again=paper.update_paper_tracking(path,report,fetch,self.start+timedelta(minutes=5))
            self.assertEqual(first['registered'],1)
            self.assertEqual(again['registered'],0)
            self.assertEqual(again['counts'],{'TARGET':1})
            report['test_data']=True
            self.assertEqual(paper.update_paper_tracking(path,report,fetch,self.start)['status'],'SKIPPED')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM paper_signals').fetchone()[0],1)


if __name__=='__main__': unittest.main()
