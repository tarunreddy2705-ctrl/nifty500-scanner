"""Offline behavior checks: timing, fail-closed decisions and report explanations."""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('scanner_under_test',ROOT/'scanner.py')
scanner=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=scanner
spec.loader.exec_module(scanner)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((ROOT/'config.json').read_text())

    def test_early_run_waits_even_with_old_config_without_network(self):
        self.config['session_windows']['morning']['start']='09:20'
        clock=datetime(2026,10,22,9,25,tzinfo=scanner.IST)
        with patch.object(scanner,'now_ist',return_value=clock),patch.object(scanner,'req') as req:
            allowed,reason=scanner.session_gate('morning',self.config)
        self.assertFalse(allowed)
        self.assertIn('WAITING FOR OPENING RANGE until 09:30',reason)
        req.assert_not_called()

    def test_935_requires_verified_exchange_status(self):
        clock=datetime(2026,10,22,9,35,tzinfo=scanner.IST)
        payload={'status':'success','data':{'exchange':'NSE','status':'NORMAL_OPEN','last_updated':clock.isoformat()}}
        with patch.object(scanner,'now_ist',return_value=clock),patch.object(scanner,'req',return_value=Mock(json=lambda:payload)):
            self.assertTrue(scanner.session_gate('morning',self.config)[0])
            payload['data']['status']='CLOSED'
            self.assertFalse(scanner.session_gate('morning',self.config)[0])

    def test_waiting_and_diagnostic_reports_never_become_no_trade(self):
        for outcome in ['NO SCAN','SCAN FAILED','DIAGNOSTIC ONLY']:
            report={'mode':'morning','timestamp':'fixture','outcome':outcome,'reason':'original reason',
                    'rejected':[],'watches':[],'trades':[]}
            with tempfile.TemporaryDirectory() as directory,patch.object(scanner,'REPORTS',Path(directory)),patch('builtins.print'):
                saved=scanner.save_report(report)
                self.assertEqual(saved['outcome'],outcome)
                self.assertEqual(saved['reason'],'original reason')

    def test_blockers_are_counted_by_candidate_not_semicolon(self):
        reason='Extended from VWAP; do not chase'
        rows=[{'blockers':[reason,reason,'Invalid structural entry/stop']},
              {'blockers':[reason]}]
        counts={x['reason']:x['count'] for x in scanner.rejection_summary(rows)}
        self.assertEqual(counts,{reason:2,'Invalid structural entry/stop':1})

    def test_saved_report_and_cloud_summary_include_reasons(self):
        report={'mode':'midday','timestamp':'fixture','outcome':'NO TRADE','reason':'old',
                'rejected':[{'blockers':['No identified target']}]*3,'watches':[],'trades':[],
                'test_data':False,'setup_type':'OPENING_RANGE_ONLY'}
        with tempfile.TemporaryDirectory() as directory,patch.object(scanner,'REPORTS',Path(directory)),patch('builtins.print') as output:
            scanner.save_report(report)
            saved=json.loads(next(Path(directory).glob('*.json')).read_text())
            summary=json.loads(output.call_args_list[-1].args[0].split('=',1)[1])
        self.assertEqual(saved['rejection_summary'],[{'reason':'No identified target','count':3}])
        self.assertIn('3: No identified target',saved['reason'])
        self.assertFalse(summary['test_data'])
        self.assertEqual(summary['rejection_summary'],saved['rejection_summary'])

    def test_scoring_retains_risk_gates_and_never_emits_trade(self):
        row={'change_pct':1,'turnover_cr':30}
        t={'close':100,'vwap':100.2,'ema9':99,'ema20':101,'ema50':102,'rsi':40,
           'atr':1,'macd':-1,'macd_signal':0,'rvol_same_time':2,'opening_complete':True,
           'opening_high':104,'opening_low':101,'recent_low':99,'recent_high':100,
           'support_levels':[98]}
        result=scanner.score_candidate(row,t,self.config['technical'])
        self.assertEqual(result['status'],'REJECT')
        self.assertIn('Invalid structural entry/stop',result['blockers'])
        self.assertIn('Opening trigger already passed; require a new retest setup',result['blockers'])
        self.assertEqual(result['reason'],'; '.join(result['blockers']))
        self.assertFalse(self.config['policy']['trade_enabled'])


if __name__=='__main__':
    unittest.main()
