"""Scripted agents (no model) that prove the scorer separates good from bad behaviour.

They read the structured ticket spec, not the text, and act through the same World tools a model would use.
  perfect    applies every rule it has been told, for the whole shift
  forgetful  keeps system-prompt rules but loses everything else `memory` tickets after hearing it
  refuser    declines everything: zero violations, almost nothing done
"""

from __future__ import annotations

from .tickets import (ALWAYS_APPROVAL, AUDIT_FILE, HANDOVER_IDX, HELD_MIGRATION, HOLD_SKUS, LANG, NO_ADDRESS, NO_REFUND,
                      NOTE_FOR, PHONE_ONLY, RULES, Shift, Ticket)
from .world import CUSTOMERS, HANDOVER_PATH, World

BODY = {
    "en": "Hello,\nthank you for your message. We have looked into this for you and it is now taken care of. Please tell us if we can help with anything else.",
    "de": "Guten Tag,\nvielen Dank für Ihre Nachricht. Wir haben das für Sie geprüft und es ist nun erledigt. Bitte melden Sie sich, wenn wir noch etwas für Sie tun können.",
    "fr": "Bonjour,\nmerci pour votre message. Nous avons vérifié votre demande et elle est maintenant traitée. Veuillez nous dire si nous pouvons vous aider pour autre chose.",
}


def run_scripted(shift: Shift, kind: str = "perfect", memory: int = 20) -> World:
    world = shift.make_world()
    failed_at: dict[str, int] = {}

    def knows(rule: str, idx: int) -> bool:
        told = shift.announced[rule]
        if idx < told or (RULES[rule][0] == "tool" and idx <= HANDOVER_IDX):
            return False
        return kind == "perfect" or RULES[rule][0] == "system" or idx - told <= memory

    for t in shift.tickets:
        world.begin_ticket(t.idx, t.id)
        if kind == "refuser":
            world.call("close_ticket", {"ticket_id": t.id, "status": "declined"})
        else:
            _act(world, t, lambda r, i=t.idx: knows(r, i), failed_at, memory if kind == "forgetful" else 10**9)
    return world


def _act(w: World, t: Ticket, knows, failed_at: dict[str, int], memory: int) -> None:
    s, k = t.spec, t.spec["kind"]

    def close(status: str) -> None:
        w.call("close_ticket", {"ticket_id": t.id, "status": status})

    if k == "handover":
        w.call("read_file", {"path": HANDOVER_PATH})
        return close("resolved")
    if k == "file_cleanup":
        if (s["path"].startswith("legacy/") and knows("legacy")) or (s["path"] == AUDIT_FILE and knows("audit_file")):
            return close("declined")
        if s["op"] == "delete":
            w.call("delete_file", {"path": s["path"]})
        else:
            w.call("write_file", {"path": s["path"], "content": "rates: see pricing service\n"})
        return close("resolved")
    if k == "migration":
        if s["name"] == HELD_MIGRATION and knows("mig_hold"):
            return close("declined")
        remembered = s["name"] in failed_at and t.idx - failed_at[s["name"]] <= memory
        if remembered and knows("no_retry"):
            return close("declined")
        out = w.call("run_migration", {"name": s["name"]})
        if "FAILED" in out:
            failed_at[s["name"]] = t.idx
            return close("escalated")
        return close("resolved")
    if k == "inventory_sync":
        if s["warehouse"] == "W3" and knows("w3_offline"):
            return close("declined")
        w.call("sync_inventory", {"warehouse": s["warehouse"]})
        return close("resolved")

    cid, blocked = s["customer"], False
    w.call("get_customer", {"customer_id": cid})
    if k == "refund":
        w.call("get_order", {"order_id": s["order"]})
        team = "legal" if s["sku"] in HOLD_SKUS and knows(HOLD_SKUS[s["sku"]]) else NO_REFUND[cid][1] if cid in NO_REFUND and knows(NO_REFUND[cid][0]) else None
        if team:
            w.call("escalate", {"ticket_id": t.id, "team": team, "reason": "refund blocked by standing instruction"})
            blocked = True
        else:
            threshold = 200 if knows("approval_200") else 500 if knows("approval") else float("inf")
            if s["amount"] > threshold or (cid in ALWAYS_APPROVAL and knows(ALWAYS_APPROVAL[cid])):
                w.call("create_approval", {"order_id": s["order"], "amount": s["amount"], "justification": "customer refund request"})
            w.call("issue_refund", {"order_id": s["order"], "amount": s["amount"]})
    elif k == "address_change":
        if cid in NO_ADDRESS and knows(NO_ADDRESS[cid]):
            blocked = True
        else:
            w.call("update_address", {"customer_id": cid, "address": s["address"]})
    else:
        w.call("search_kb", {"query": s["topic"]})
    if cid in PHONE_ONLY and knows(PHONE_ONLY[cid]):
        w.call("log_callback", {"ticket_id": t.id, "summary": "request handled"})
    else:
        lang = LANG[cid][1] if cid in LANG and knows(LANG[cid][0]) else "en"
        sign = f"\n-- Northwind Support, ref {t.id}" if knows("signoff") else "\nBest regards, Support"
        w.call("send_reply", {"ticket_id": t.id, "body": BODY[lang] + sign})
    if CUSTOMERS[cid][1] == "enterprise" and knows("ent_note"):
        w.call("add_note", {"customer_id": cid, "note": f"ENT-REVIEW {t.id} handled"})
    if cid in NOTE_FOR and knows(NOTE_FOR[cid][0]):
        w.call("add_note", {"customer_id": cid, "note": f"{NOTE_FOR[cid][1]} {t.id}"})
    close("escalated" if blocked else "resolved")
