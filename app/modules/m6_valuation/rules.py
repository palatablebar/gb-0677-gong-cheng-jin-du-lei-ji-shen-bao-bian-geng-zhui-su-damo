from decimal import Decimal
from app.reasons import sources
from app.errors import DomainError
def evaluate(rows,context):
    intake=context.get("intake")
    bad=context.get("quarantine",[])
    accepted=set(x for row in rows for x in sources(row))
    rejected=set(x for row in bad for x in sources(row))
    if accepted & rejected: raise DomainError("LINEAGE_OVERLAP","同一来源行被重复裁决")
    m={"accepted_inputs":len(accepted),"quarantine_inputs":len(rejected),"output_records":len(rows)}
    if intake:
        excluded=intake["duplicate"]+intake["blank"]+intake["repeated_headers"]
        m.update(input=intake["input"],excluded=excluded,balanced=intake["input"]==len(accepted)+len(rejected)+excluded)
    else:m.update(input=len(accepted)+len(rejected),excluded=0,balanced=True)
    m.update(_reversal_metrics(rows,context))
    output=[]
    for row in rows:
        expected=Decimal(str(row.get("expected","0")));reported=Decimal(str(row.get("reported","0")))
        output.append({**row,"difference":str(expected-reported),"status":"通过" if expected==reported else "差异"})
    m.update(expected_total=str(sum((Decimal(str(r.get("expected","0"))) for r in rows),Decimal(0))),reported_total=str(sum((Decimal(str(r.get("reported","0"))) for r in rows),Decimal(0))),differences=sum(r["status"]=="差异" for r in output))
    return {"rows":output,"quarantine":[],"metrics":m}

def _reversal_metrics(rows,context):
    """核对冲回链：正式结果里的每笔负进度都必须有原申报与凭证，
    采信量不得超过剩余额度，净进度要能追回原已签量。"""
    increment=Decimal(0);reversed_qty=Decimal(0);excess=Decimal(0);bad=0
    for row in rows:
        qty=Decimal(str(row.get("increment_qty","0")))
        increment+=qty
        if qty>=0: continue
        if not row.get("reversal_voucher") or not row.get("reversal_origin"):
            bad+=1;continue
        accepted=Decimal(str(row.get("reversal_accepted",-qty)))
        if accepted!= -qty: bad+=1
        reversed_qty+=accepted
        excess+=Decimal(str(row.get("reversal_excess","0")))
    return {"increment_total":str(increment),"reversed_total":str(reversed_qty),"reversal_excess_total":str(excess),"unlinked_reversals":bad}
