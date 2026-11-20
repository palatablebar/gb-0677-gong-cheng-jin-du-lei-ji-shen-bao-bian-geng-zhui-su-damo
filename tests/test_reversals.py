from decimal import Decimal
from app.modules.m4_increment.rules import evaluate
from app.modules.m6_valuation.rules import evaluate as reconcile
from app import store
from app.errors import DomainError
import pytest


def claim(claim_id, period="2026-11", current="125", previous="100", reversal="none",
          voucher=None, origin=None, item="001", price="120"):
    row = {"_line": 2, "claim_id": claim_id, "item": item, "period": period,
           "cumulative_qty": current, "previous_qty": previous,
           "unit_price": price, "reversal": reversal, "reported": "0"}
    if voucher: row["voucher_id"] = voucher
    if origin: row["reversal_of"] = origin
    return row


BASE = claim("C1", period="2026-01", current="125", previous="100")  # +25


def test_reversal_links_to_origin_signed_and_remaining():
    r = evaluate([BASE, claim("C2", period="2026-06", current="115", previous="125",
                              reversal="approved", voucher="V1", origin="C1")], {})
    row = [x for x in r["rows"] if x["claim_id"] == "C2"][0]
    assert (row["reversal_origin"], row["reversal_basis"]) == ("C1", "explicit")
    assert row["reversal_origin_signed"] == "25"
    assert (row["reversal_accepted"], row["reversal_excess"], row["reversal_remaining"]) == ("10", "0", "15")
    entry = r["metrics"]["reversal_chain"][0]
    assert (entry["origin_signed_qty"], entry["accepted_qty"], entry["remaining_qty"]) == ("25", "10", "15")


def test_reversal_truncates_to_remaining_and_excludes_excess():
    r = evaluate([BASE, claim("C2", period="2026-06", current="85", previous="125",
                              reversal="approved", voucher="V1", origin="C1")], {})
    row = [x for x in r["rows"] if x["claim_id"] == "C2"][0]
    assert row["increment_qty"] == "-25" and row["reversal_status"] == "部分采信"
    assert (row["reversal_excess"], row["reversal_remaining"]) == ("15", "0")
    assert r["metrics"]["reversal_excess_qty"] == "15"
    # 已确认实施量不再挂账：原申报 +25 与冲回 -25 相抵，净进度归零。
    assert sum(Decimal(x["increment_qty"]) for x in r["rows"]) == Decimal(0)


def test_sequential_reversals_consume_one_origin():
    r = evaluate([BASE,
                  claim("C2", period="2026-06", current="115", previous="125", reversal="approved", voucher="V1", origin="C1"),
                  claim("C3", period="2026-07", current="105", previous="115", reversal="approved", voucher="V2", origin="C1"),
                  claim("C4", period="2026-08", current="95", previous="105", reversal="approved", voucher="V3", origin="C1")], {})
    rows = {x["claim_id"]: x for x in r["rows"]}
    assert [rows[k]["increment_qty"] for k in ("C2", "C3", "C4")] == ["-10", "-10", "-5"]
    assert rows["C4"]["reversal_excess"] == "5"
    assert r["metrics"]["reversal_accepted_qty"] == "25"
    assert r["metrics"]["reversal_excess_qty"] == "5"


def test_origin_not_found_is_isolated():
    r = evaluate([BASE, claim("C2", period="2026-06", current="115", previous="125",
                              reversal="approved", voucher="V1", origin="ZZZ")], {})
    assert [q["reason"] for q in r["quarantine"]] == ["ORIGIN_NOT_FOUND"]
    assert [x["claim_id"] for x in r["rows"]] == ["C1"]


def test_negative_without_marker_still_isolated():
    r = evaluate([BASE, claim("C2", period="2026-06", current="115", previous="125")], {})
    assert [q["reason"] for q in r["quarantine"]] == ["REVERSAL_REQUIRED"]


def test_duplicate_voucher_same_vat_not_double_deducted():
    first = claim("C2", period="2026-06", current="115", previous="125", reversal="approved", voucher="V1", origin="C1")
    retry = claim("C3", period="2026-07", current="105", previous="115", reversal="approved", voucher="V1", origin="C1")
    r = evaluate([BASE, first, retry], {})
    assert [x["claim_id"] for x in r["rows"]] == ["C1", "C2"]
    assert [q["reason"] for q in r["quarantine"]] == ["DUPLICATE_VOUCHER"]
    assert r["metrics"]["reversal_accepted_qty"] == "10"


def test_replay_against_published_ledger_is_isolated():
    sealed = {"parameters": {"reversal_ledger": {
        "origins": {"C1": {"signed_qty": "25", "reversed_qty": "10"}}, "vouchers": ["V1"]}}}
    r = evaluate([BASE, claim("C2", period="2026-06", current="115", previous="125",
                              reversal="approved", voucher="V1", origin="C1")], sealed)
    assert [q["reason"] for q in r["quarantine"]] == ["REPLAY_VOUCHER"]
    assert r["metrics"]["reversal_chain"] == []


def test_inferred_origin_is_latest_open_claim():
    later = claim("C2", period="2026-02", current="130", previous="125")  # +5
    r = evaluate([BASE, later, claim("C3", period="2026-06", current="125", previous="130",
                                     reversal="approved", voucher="V1")], {})
    row = [x for x in r["rows"] if x["claim_id"] == "C3"][0]
    assert (row["reversal_origin"], row["reversal_basis"], row["increment_qty"]) == ("C2", "inferred_latest", "-5")


def test_self_previous_fallback_uses_previous_qty():
    r = evaluate([claim("S1", item="002", period="2026-05", current="80", previous="100",
                        reversal="approved", voucher="W1")], {})
    row = r["rows"][0]
    assert (row["reversal_origin"], row["reversal_basis"], row["reversal_accepted"]) == ("S1", "self_previous", "20")


def test_reconcile_tracks_net_progress_to_origin():
    r = evaluate([BASE, claim("C2", period="2026-06", current="115", previous="125",
                              reversal="approved", voucher="V1", origin="C1")], {})
    rows = [{"increment_qty": x["increment_qty"], "reversal_voucher": x.get("reversal_voucher"),
             "reversal_origin": x.get("reversal_origin"), "reversal_accepted": x.get("reversal_accepted"),
             "reversal_excess": x.get("reversal_excess"), "expected": "0", "reported": "0"} for x in r["rows"]]
    m = reconcile(rows, {})["metrics"]
    assert m["unlinked_reversals"] == 0
    assert m["reversed_total"] == "10"
    assert m["increment_total"] == "15"


def test_publish_records_ledger_and_blocks_replay():
    payload = {"stages": [
        {"rows": []}, {"rows": []}, {"rows": []},
        {"rows": [{"claim_id": "C1", "increment_qty": "25", "reported": "0", "expected": "0"}],
         "metrics": {"reversal_chain": [{"voucher_id": "V1", "origin_claim_id": "C1",
                                         "origin_signed_qty": "25", "accepted_qty": "10",
                                         "source_line": 2}]}},
        {"rows": []}, {"rows": []}, {"rows": []}]}
    store.save("run-a", payload)
    assert store.publish("run-a", 1, "已复核")["decision"] == "已复核"
    ledger = store.published_ledger()
    assert ledger["vouchers"] == ["V1"]
    assert ledger["origins"]["C1"] == {"signed_qty": "25", "reversed_qty": "10"}


def test_failed_publish_restores_old_version_and_ledger():
    good = {"stages": [{"rows": []}, {"rows": []}, {"rows": []},
                       {"rows": [], "metrics": {"reversal_chain": [
                           {"voucher_id": "V1", "origin_claim_id": "C1", "origin_signed_qty": "25",
                            "accepted_qty": "10", "source_line": 1}]}},
                       {"rows": []}, {"rows": []}, {"rows": []}]}
    store.save("run-a", good)
    store.publish("run-a", 1, "已复核")
    before = store.published_ledger()
    replay = {"stages": [{"rows": []}, {"rows": []}, {"rows": []},
                         {"rows": [], "metrics": {"reversal_chain": [
                             {"voucher_id": "V1", "origin_claim_id": "C1", "origin_signed_qty": "25",
                              "accepted_qty": "15", "source_line": 1}]}},
                         {"rows": []}, {"rows": []}, {"rows": []}]}
    store.save("run-b", replay)
    with pytest.raises(DomainError) as error:
        store.publish("run-b", 1, "已复核")
    assert error.value.code == "REPLAY_VOUCHER"
    after = store.get("run-b")
    assert (after["version"], after["decision"]) == (1, "待复核")
    assert store.published_ledger() == before


def test_publish_retry_does_not_double_deduct():
    payload = {"stages": [{"rows": []}, {"rows": []}, {"rows": []},
                          {"rows": [], "metrics": {"reversal_chain": [
                              {"voucher_id": "V1", "origin_claim_id": "C1", "origin_signed_qty": "25",
                               "accepted_qty": "10", "source_line": 1}]}},
                          {"rows": []}, {"rows": []}, {"rows": []}]}
    store.save("run-a", payload)
    store.publish("run-a", 1, "已复核")
    with pytest.raises(DomainError) as error:
        store.publish("run-a", 1, "已复核")
    assert error.value.code == "STALE_VERSION"
    assert store.published_ledger()["origins"]["C1"]["reversed_qty"] == "10"


def test_archive_lists_reversal_chain_evidence():
    from app.archive import build_archive
    from app.pipeline import calculate, load_sample
    assert "reversal_chain" in build_archive(calculate(load_sample("reversal.csv")))
