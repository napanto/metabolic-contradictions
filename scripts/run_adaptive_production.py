#!/usr/bin/env python3
"""Signed provider64 extension around the immutable SQLite production runner."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path

from chl_rag.io import write_json
from chl_rag.adaptive_transport import AdaptiveV2Client, extension

ROOT=Path(__file__).resolve().parents[1]


def load_base_runner():
    spec=importlib.util.spec_from_file_location('approved_sqlite_runner',ROOT/'scripts/run_v2_sqlite_production.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


async def run(stage,*,scope='full',concurrency=None):
    approved=extension(ROOT)
    ceiling=approved['extraction_concurrency'] if stage=='extract' else approved['classification_concurrency']
    concurrency=ceiling if concurrency is None else concurrency
    if type(concurrency) is not int or not 1<=concurrency<=ceiling:raise ValueError('Controller concurrency exceeds signed extension')
    base=load_base_runner();clients=[]
    def client(*args,**kwargs):
        result=AdaptiveV2Client(*args,adaptive_policy=approved['policy'],route_health_holds=approved.get('route_health_holds',[]),**kwargs);clients.append(result);return result
    base.V2Client=client
    try:return await base.run(stage,scope=scope,concurrency=concurrency)
    finally:
        if clients:
            write_json('results/provider_adaptive_report.json',{
                'policy':approved['policy'],'providers':clients[-1].control.report(),
                'applied_route_health_holds':clients[-1].applied_route_health_holds,
                'request_parameters_changed':False,'base_sqlite_authority_changed':False})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['extract','classify'])
    parser.add_argument('--scope',choices=['full','reserve'],default='full')
    parser.add_argument('--concurrency',type=int)
    args=parser.parse_args()
    print(json.dumps(asyncio.run(run(args.stage,scope=args.scope,concurrency=args.concurrency))),flush=True)
