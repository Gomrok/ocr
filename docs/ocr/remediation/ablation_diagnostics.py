"""Synthetic-only quality ablation; no production imports or model installation."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))

def worker(variant):
    from ocr_pipeline.process_guard import enter_worker_job
    enter_worker_job()
    from docs.ocr.audit_diagnostics import setup, benchmark_case
    setup()
    from ocr_pipeline import engines as E, render as R
    if variant=='single_render':
        R.RENDER_TILE_EDGE=100000
    elif variant=='alias_consensus':
        from ocr_pipeline.observations import carry_observations
        def select(results):
            groups={}
            for reading in results:
                key=' '.join(E.normalize_text(reading.get('text',''),normalize_digits=True).split())
                if key: groups.setdefault(key,[]).append(reading)
            if not groups: return dict(results[0]) if results else {'text':'','words':[],'confidence':None}
            trusted=[g for g in groups.values() if max(map(E.result_confidence,g))>=.85]
            group=max(trusted or groups.values(),key=lambda g:(len(g),max(map(E.result_confidence,g))))
            chosen=carry_observations(max(group,key=E.result_confidence),*results,stage='ablation_consensus')
            chosen['votes']=len(group)
            for word in chosen.get('words',[]):
                if chosen['needs_review']:
                    word['needs_review']=True
                    word['uncertainty_reasons']=chosen['uncertainty_reasons']
                if len(chosen.get('words',[]))==1: word['candidates']=chosen['candidates']
            return chosen
        E.select_consensus=select
    result=benchmark_case('bench_mixed')
    result['ablation_variant']=variant
    result['limitation']='quality_ablation_only_not_latency_comparison'
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('variant',choices=('single_render','alias_consensus'));parser.add_argument('--worker',action='store_true');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.worker: worker(args.variant)
    else:
        from ocr_pipeline.process_guard import run_worker
        code, output=run_worker([sys.executable,'-B','-X','utf8',__file__,args.variant,'--worker'],memory_bytes=8*1024**3,seconds=600,output_limit=1024**2)
        if code: raise RuntimeError('Synthetic ablation worker failed')
        result=json.loads(output)
        if not args.output or args.output.exists(): raise ValueError('New evidence path required')
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'variant':args.variant,'text':result['evaluation_v2']['text'],'numeric':result['evaluation_v2']['numeric']}))
