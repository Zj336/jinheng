"""Offline reproducible evidence; every run owns a temporary database."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', help='Attack turn; repeat for a multi-turn attempt without valid authorization')
    parser.add_argument('--json', default='validation/extended.json', help='JSON output file')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='statera-eval-') as tmp:
        os.environ['JH_DB'] = str(Path(tmp)/'bank.db')
        os.environ['JH_LLM_MODE'] = 'mock'
        from app._console import init_utf8
        init_utf8()
        from app.eval.extended import attack_case, run_extended
        if args.case:
            report = attack_case({'id':'CUSTOM','cat':'自定义','turns':args.case})
            ok = report['defended']
            display = report
        else:
            report = run_extended(Path(__file__).parent/'eval'/'normal.yaml')
            display = {k:v for k,v in report.items() if k not in ('attacks','normal')}
            n = report['normal_summary']
            ok = report['summary']['full']['defended'] == report['summary']['full']['total'] and n['correct_gate'] == n['completed'] == n['total']
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(display, ensure_ascii=False, indent=2))
        return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
