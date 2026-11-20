import sqlite3,json
from decimal import Decimal
from pathlib import Path
from app.config import DATA_DIR
from app.errors import DomainError
from app.values import number

SCHEMA=(
 "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核')",
 "CREATE TABLE IF NOT EXISTS voucher_ledger (voucher TEXT PRIMARY KEY,run_id TEXT NOT NULL,origin_claim_id TEXT NOT NULL,accepted_qty TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS origin_ledger (claim_id TEXT PRIMARY KEY,signed_qty TEXT NOT NULL,reversed_qty TEXT NOT NULL,run_id TEXT NOT NULL)",
)

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"records.db",timeout=20,isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA: db.execute(statement)
    return db

def save(run_id,payload):
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)",(run_id,json.dumps(payload,ensure_ascii=False)))
    return get(run_id)

def get(run_id):
    with connect() as db: row=db.execute("SELECT payload,version,decision FROM runs WHERE id=?",(run_id,)).fetchone()
    if not row: raise DomainError("NOT_FOUND","批次不存在",404)
    return {**json.loads(row[0]),"version":row[1],"decision":row[2]}

def latest():
    with connect() as db: row=db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None

def history():
    with connect() as db: return [{"run_id":i,"version":v,"decision":d} for i,v,d in db.execute("SELECT id,version,decision FROM runs ORDER BY rowid DESC")]

def published_ledger():
    """已签批次固化的冲回台账，供下一批重放识别。"""
    with connect() as db:
        origins={claim:{"signed_qty":signed,"reversed_qty":reversed_qty} for claim,signed,reversed_qty in db.execute("SELECT claim_id,signed_qty,reversed_qty FROM origin_ledger")}
        vouchers=[v for (v,) in db.execute("SELECT voucher FROM voucher_ledger ORDER BY rowid")]
    return {"origins":origins,"vouchers":vouchers}

def publish(run_id,version,decision):
    """签认并固化冲回台账，或仅变更复核状态。

    版本条件更新、凭证唯一登记、原申报冲回余量累加在同一事务完成；
    任一步失败整体回滚，旧版本与旧台账不受影响。同一凭证即使重试也只会入册一次。
    """
    if decision not in {"已复核","退回复核"}: raise DomainError("BAD_DECISION","复核状态无效")
    db=connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT payload,version,decision FROM runs WHERE id=?",(run_id,)).fetchone()
        if not row: raise DomainError("NOT_FOUND","批次不存在",404)
        if row[1]!=version: raise DomainError("STALE_VERSION","版本已更新，请刷新",409)
        if decision=="已复核": _publish_ledger(db,run_id,json.loads(row[0]))
        cursor=db.execute("UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?",(decision,run_id,version))
        if cursor.rowcount!=1: raise DomainError("STALE_VERSION","版本已更新，请刷新",409)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return get(run_id)

def _publish_ledger(db,run_id,payload):
    stages=payload.get("stages") or []
    if len(stages)<4: return
    accepted=stages[3]["rows"]
    chain=stages[3]["metrics"].get("reversal_chain") or []
    for entry in chain:
        accepted_qty=number(entry.get("accepted_qty","0"))
        if accepted_qty<=0: continue
        voucher=str(entry.get("voucher_id") or "")
        if not voucher: continue
        existing=db.execute("SELECT run_id FROM voucher_ledger WHERE voucher=?",(voucher,)).fetchone()
        if existing:
            if existing[0]==run_id: continue
            raise DomainError("REPLAY_VOUCHER",f"冲回凭证 {voucher} 已在批次 {existing[0]} 签认，不得重复扣减",409)
        db.execute("INSERT INTO voucher_ledger(voucher,run_id,origin_claim_id,accepted_qty) VALUES (?,?,?,?)",(voucher,run_id,str(entry.get("origin_claim_id","")),str(accepted_qty)))
        origin=str(entry.get("origin_claim_id",""))
        signed=number(entry.get("origin_signed_qty","0"))
        current=db.execute("SELECT reversed_qty FROM origin_ledger WHERE claim_id=?",(origin,)).fetchone()
        reversed_total=(number(current[0]) if current else Decimal(0))+accepted_qty
        db.execute("INSERT INTO origin_ledger(claim_id,signed_qty,reversed_qty,run_id) VALUES (?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET signed_qty=excluded.signed_qty,reversed_qty=excluded.reversed_qty,run_id=excluded.run_id",(origin,str(signed),str(reversed_total),run_id))
        if reversed_total>signed:
            raise DomainError("EXCESS_REVERSAL",f"原申报 {origin} 冲回超过原已签量，拒绝发布",409)
    for row in accepted:
        claim=str(row.get("claim_id",""))
        if not claim or row.get("reversal_voucher"): continue
        signed=number(row.get("increment_qty","0"))
        current=db.execute("SELECT reversed_qty FROM origin_ledger WHERE claim_id=?",(claim,)).fetchone()
        reversed_total=number(current[0]) if current else Decimal(0)
        if reversed_total>signed:
            raise DomainError("EXCESS_REVERSAL",f"原申报 {claim} 已冲回超过本次签认量，拒绝发布",409)
        db.execute("INSERT INTO origin_ledger(claim_id,signed_qty,reversed_qty,run_id) VALUES (?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET signed_qty=excluded.signed_qty,run_id=excluded.run_id",(claim,str(signed),str(reversed_total),run_id))

def review(run_id,version,decision): return publish(run_id,version,decision)
