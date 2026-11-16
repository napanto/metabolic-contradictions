"""Signed provider-concurrency extension; no frozen request/ledger changes."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import time
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from pathlib import Path

import httpx

from .io import digest, file_sha, write_json
from .llm import RequestConfigurationError
from .model_client import V2Client
from .frozen_compat import _signed
from .sqlite_ledger import AUTHORITY, authority

POLICY={'maximum':64,'minimum':1,'window_seconds':60,'unknown_window_posts':20,
        'unknown_minimum_posts':10,'unknown_fraction':.05,'server_error_burst':3,
        'halve_cooldown_seconds':15,'clean_seconds':60,'clean_successes':5,
        'restore_step':8,'persistent_throttle_seconds':120}
EXTENSION='configs/provider_concurrency.json'
RUNTIME=('src/chl_rag/adaptive_transport.py','scripts/run_adaptive_production.py')



def validate_hold_shape(holds):
    if holds is None:return []
    if not isinstance(holds,(list,tuple)):raise RequestConfigurationError('Route health holds must be a list')
    seen=set();result=[]
    required={'model','phase','provider_tag','reason','evidence_path','evidence_sha256','authority'}
    for hold in holds:
        if (not isinstance(hold,dict) or set(hold)!=required
                or hold['model']!='z-ai/glm-5.3-flash' or hold['phase']!='production_classification'
                or hold['provider_tag']!='tenstorrent/fp8' or hold['reason']!='persistent_known_free_http_429'
                or not isinstance(hold['authority'],str) or not hold['authority'].strip()
                or not isinstance(hold['evidence_sha256'],str) or len(hold['evidence_sha256'])!=64):
            raise RequestConfigurationError('Unsupported or incomplete signed route health hold')
        relative=Path(hold['evidence_path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise RequestConfigurationError('Health evidence must stay inside the project')
        key=(hold['model'],hold['phase'],hold['provider_tag'])
        if key in seen:raise RequestConfigurationError('Duplicate route health hold')
        seen.add(key);result.append(deepcopy(hold))
    return result


def filtered_client_models(models,phase,holds):
    """Client-owned copy only; never rewrite a registry, artifact or checkpoint."""
    result=deepcopy(models);applied=[]
    for hold in validate_hold_shape(holds):
        if hold['phase']!=phase:continue
        model=hold['model'];tag=hold['provider_tag']
        if model not in result or sum(route['tag']==tag for route in result[model]['routes'])!=1:
            raise RequestConfigurationError('Held route is absent or ambiguous in the original registered model')
        remaining=[route for route in result[model]['routes'] if route['tag']!=tag]
        if not remaining:raise RequestConfigurationError('Health hold would remove every registered route')
        result[model]['routes']=remaining;applied.append(hold)
    return result,applied


def extension(root=None,path=EXTENSION):
    root=Path(root or Path.cwd()).resolve();path=root/path
    value=json.loads(path.read_text());authority(root)
    if (value.get('schema_version')!=1 or value.get('status')!='registered' or value.get('policy')!=POLICY
            or value.get('base_sqlite_authority_sha256')!=file_sha(root/AUTHORITY)
            or value.get('base_sqlite_runner_sha256')!=file_sha(root/'scripts/run_v2_sqlite_production.py')
            or value.get('extraction_concurrency')!=256 or value.get('classification_concurrency')!=320):
        raise RequestConfigurationError('Adaptive extension differs from approved64-provider policy or immutable SQLite base')
    _signed(path,root)
    protocol=root/value['protocol_path']
    if file_sha(protocol)!=value.get('protocol_sha256'):raise RequestConfigurationError('Adaptive protocol changed')
    _signed(protocol,root)
    for relative in RUNTIME:
        if file_sha(root/relative)!=value.get('runtime_sha256',{}).get(relative):
            raise RequestConfigurationError('Adaptive extension code changed')
        _signed(root/relative,root)
    for hold in validate_hold_shape(value.get('route_health_holds',[])):
        evidence=(root/hold['evidence_path']).resolve()
        if not evidence.is_relative_to(root) or file_sha(evidence)!=hold['evidence_sha256']:
            raise RequestConfigurationError('Signed route health evidence changed or leaves the project')
        _signed(evidence,root)
    return value


class ProviderControl:
    def __init__(self,slot_root,state_root='data/raw/v2/adaptive_provider_state',policy=None,clock=time.time):
        self.slots=Path(slot_root);self.root=Path(state_root);self.policy=policy or POLICY;self.clock=clock
        self.fault=None
        self.blocked_until={}
        self.root.mkdir(parents=True,exist_ok=True);self.slots.mkdir(parents=True,exist_ok=True)

    @contextmanager
    def _locked(self,name):
        if self.fault:raise RequestConfigurationError(self.fault)
        key=digest(name.casefold().replace(' ',''))[:20]
        try:
            with (self.root/(key+'.lock')).open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                path=self.root/(key+'.json');now=self.clock()
                state=json.loads(path.read_text()) if path.exists() else {
                    'provider':name,'policy_sha256':digest(self.policy),'cap':self.policy['maximum'],
                    'events':[],'server_errors_60s':[],'total_posts':0,'unknown_posts':0,'http_429':0,'http_5xx':0,
                    'last_bad_at':None,'last_change_at':now,'throttled_since':None,'clean_successes':0,'transitions':[]}
                if state.get('policy_sha256')!=digest(self.policy):raise ValueError('different adaptive policy')
                yield state,path,key,now
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.fault='Adaptive provider state unavailable; stop new admission ('+type(exc).__name__+')'
            raise RequestConfigurationError(self.fault) from None

    def _restore(self,state,now):
        p=self.policy
        if (state['cap']<p['maximum'] and now-state['last_change_at']>=p['clean_seconds']
                and (state['last_bad_at'] is None or now-state['last_bad_at']>=p['clean_seconds'])
                and state['clean_successes']>=p['clean_successes']):
            before=state['cap'];state['cap']=min(p['maximum'],before+p['restore_step'])
            state['last_change_at']=now;state['clean_successes']=0
            state['transitions'].append({'at':now,'from':before,'to':state['cap'],'reason':'clean_interval'})
            if state['cap']==p['maximum']:state['throttled_since']=None

    def _save(self,state,path,now):
        state['updated_at_epoch']=now
        state['persistent_throttle']=state['throttled_since'] is not None and now-state['throttled_since']>=self.policy['persistent_throttle_seconds']
        write_json(path,state)

    def try_slot(self,name):
        # Shared by this controller's waiting workers; avoid64 probes per poller.
        if self.fault:raise RequestConfigurationError(self.fault)
        if self.clock()<self.blocked_until.get(name,0):return None
        with self._locked(name) as (state,path,key,now):
            previous_cap=state['cap'];self._restore(state,now);free=None;occupied=0
            try:
                for number in range(self.policy['maximum']):
                    fd=os.open(self.slots/f'{key}-{number}.lock',os.O_CREAT|os.O_RDWR,0o600)
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:os.close(fd);occupied+=1
                    else:
                        if free is None:free=fd
                        else:os.close(fd)
                if occupied>=state['cap'] and free is not None:os.close(free);free=None
                state['leased_slots_at_observation']=occupied+int(free is not None)
                if free is None:
                    self.blocked_until[name]=now+.2
                    if state['cap']!=previous_cap:self._save(state,path,now)
                else:self._save(state,path,now)
                result=free;free=None
                return result
            finally:
                if free is not None:os.close(free)

    def observe(self,name,status,unknown):
        with self._locked(name) as (state,path,_key,now):
            p=self.policy
            prior=state['events'][-p['unknown_window_posts']:]
            previous=state['unknown_posts']/state['total_posts'] if state['total_posts']>=p['unknown_minimum_posts'] else 0
            event={'at':now,'status':status,'unknown':bool(unknown)}
            events=(prior+[event])[-p['unknown_window_posts']:];state['events']=events
            state['total_posts']+=1;state['unknown_posts']+=bool(unknown)
            state['http_429']+=status==429;state['http_5xx']+=status is not None and 500<=status<600
            fraction=sum(e['unknown'] for e in events)/len(events)
            cumulative=state['unknown_posts']/state['total_posts']
            is_server_error=status is not None and 500<=status<600
            errors=[at for at in state.get('server_errors_60s',[]) if now-at<=p['window_seconds']]
            if is_server_error:errors.append(now)
            state['server_errors_60s']=errors
            reason=('HTTP429' if status==429 else 'HTTP5xx_burst' if is_server_error and len(errors)>=p['server_error_burst']
                    else 'rising_unknown_billing' if unknown and state['total_posts']>=p['unknown_minimum_posts'] and cumulative>p['unknown_fraction'] and cumulative>previous else None)
            bad=unknown or status==429 or (status is not None and 500<=status<600)
            if bad:state['last_bad_at']=now;state['clean_successes']=0
            elif status is not None and 200<=status<300:state['clean_successes']+=1
            if reason and (not state['transitions'] or now-state['last_change_at']>=p['halve_cooldown_seconds']):
                before=state['cap'];state['cap']=max(p['minimum'],before//2)
                if before!=state['cap']:
                    state['last_change_at']=now
                    if state['throttled_since'] is None:state['throttled_since']=now
                    state['clean_successes']=0
                    state['transitions'].append({'at':now,'from':before,'to':state['cap'],'reason':reason})
            self._restore(state,now)
            state['recent_posts']=len(events);state['recent_unknown_fraction']=fraction
            state['cumulative_unknown_fraction']=cumulative
            self._save(state,path,now)

    def report(self):
        return [json.loads(p.read_text()) for p in sorted(self.root.glob('*.json'))]


class ObservedHTTP:
    def __init__(self,client,control,provider_names):
        self.client,self.control,self.names=client,control,provider_names

    def _observe(self,name,status,unknown):
        try:self.control.observe(name,status,unknown)
        except RequestConfigurationError:
            # The POST already happened. Preserve its response/exception for normal
            # accounting; the control fault stops all NEW route admissions.
            pass

    async def post(self,*args,**kwargs):
        tag=kwargs['json']['provider']['order'][0];name=self.names[tag]
        try:response=await self.client.post(*args,**kwargs)
        except (httpx.HTTPError,asyncio.CancelledError):
            self._observe(name,None,True)
            raise
        try:value=response.json()
        except (ValueError,TypeError):value=None
        usage=value.get('usage') if isinstance(value,dict) else None
        cost=usage.get('cost') if isinstance(usage,dict) else None
        known_free=(response.status_code in {400,401,402,403,404,422,429}
                    and isinstance(value,dict) and not value.get('id') and not value.get('usage'))
        unknown=not known_free and (type(cost) not in {int,float} or not math.isfinite(cost) or cost<0)
        self._observe(name,response.status_code,unknown)
        return response

    def __getattr__(self,name):return getattr(self.client,name)


class AdaptiveV2Client(V2Client):
    def __init__(self,*args,adaptive_policy=None,route_health_holds=None,**kwargs):
        holds=validate_hold_shape(route_health_holds)
        super().__init__(*args,**kwargs)
        self.models,self.applied_route_health_holds=filtered_client_models(self.models,self.ledger.phase,holds)
        self.held_routes={(hold['model'],hold['provider_tag']) for hold in self.applied_route_health_holds}
        self.control=ProviderControl(self.provider_slot_root,policy=adaptive_policy)
        self.http=ObservedHTTP(self.http,self.control,{route['tag']:route['provider_name'] for model in self.models.values() for route in model['routes']})

    def _try_provider_slot(self,name):return self.control.try_slot(name)

    @asynccontextmanager
    async def route(self,model,max_tokens,forced_provider=None):
        if forced_provider is not None and (model,forced_provider) in getattr(self,'held_routes',set()):
            raise RequestConfigurationError('Forced retry targets an explicit signed route health hold; no rerouting allowed')
        if self.models[model].get('execution_hold'):raise RequestConfigurationError('Model remains on the explicit user execution hold')
        options=[r for r in self.models[model]['routes'] if r['max_completion_tokens']>=max_tokens
                 and (forced_provider is None or r['tag']==forced_provider)]
        if not options:raise RequestConfigurationError('No registered fp8 endpoint supports the frozen output cap')
        while True:
            for option in options:
                fd=self._try_provider_slot(option['provider_name'])
                if fd is None:continue
                try:yield option
                finally:os.close(fd)
                return
            await asyncio.sleep(.1)
