import time
from app.config import ROOT,MODULES
from app.errors import DomainError
from app.values import scalar
from app.manifest import digest,manifest
from app import store
from app.modules.m1_claims.rules import evaluate as stage1
from app.modules.m2_billitems.rules import evaluate as stage2
from app.modules.m3_changes.rules import evaluate as stage3
from app.modules.m4_increment.rules import evaluate as stage4
from app.modules.m5_deductions.rules import evaluate as stage5
from app.modules.m6_valuation.rules import evaluate as stage6
from app.modules.m7_certificates.rules import evaluate as stage7
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]
def load_sample(name):
    if name not in {"dirty.csv","clean.csv","legacy.csv","reversal.csv"}: raise DomainError("BAD_SAMPLE","样例不存在",404)
    return (ROOT/"sample_data"/name).read_bytes()
def calculate(raw,params=None,stop=6,sealed=None):
    params=params or {};started=time.monotonic();context={"parameters":params,"raw":raw,"quarantine":[]};stages=[];rows=[]
    if sealed is not None: context["_reversal_ledger"]=sealed
    for index,stage in enumerate(STAGES[:stop+1]):
        result=stage(raw if index==0 else rows,context)
        result=scalar(result);rows=result["rows"]
        context["quarantine"].extend(result.get("quarantine",[]))
        if index==0: context["intake"]=result["metrics"]
        result["cumulative_quarantine"]=list(context["quarantine"])
        context[MODULES[index][0]]=result
        stages.append(result)
    return {"run_id":digest(raw,params)[:20],"stages":stages,"manifest":manifest(raw,params,started),"parameters":params}
def execute(raw,params=None):
    sealed=store.published_ledger()
    result=calculate(raw,params,sealed=sealed if (sealed["origins"] or sealed["vouchers"]) else None)
    return store.save(result["run_id"],result)
def stage_preview(index,payload):
    if "rows" in payload and index>0:
        if not isinstance(payload["rows"],list) or any(not isinstance(row,dict) for row in payload["rows"]): raise DomainError("BAD_ROWS","记录需要对象列表")
        context=payload.get("context")
        if context is None: context={"parameters":{},"quarantine":[]}
        if not isinstance(context,dict): raise DomainError("BAD_CONTEXT","上下文需要对象")
        parameters=context.get("parameters")
        if parameters is not None and not isinstance(parameters,dict): raise DomainError("BAD_CONTEXT","参数需要对象")
        if "reversal_ledger" not in (parameters or {}):
            sealed=store.published_ledger()
            if sealed["origins"] or sealed["vouchers"]: context={**context,"_reversal_ledger":sealed}
        return scalar(STAGES[index](payload["rows"],context))
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    return calculate(raw,payload.get("parameters",{}),index,sealed=store.published_ledger())["stages"][-1]
