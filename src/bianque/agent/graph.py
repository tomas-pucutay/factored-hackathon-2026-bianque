"""The agent: a LangGraph state machine for the unrecognized-charge workflow.

  Understand -> Decide -> Act -> Verify -> Escalate

Each customer message is one run of the graph; the conversation's state persists between
turns in a LangGraph checkpointer (thread = conversation_id). Nodes are deterministic code:

  alert            proactive start: the contact policy decides; if it contacts, ask
  understand       Gemini extracts intent / confirmation / language / slots (menu fallback)
  identify         reactive start: find the charge the customer describes (their own only)
  dispute          "not mine": open a dispute, verify it, then ask about a provisional block,
                   or hand off when the policy escalates (high amount, repeat complainer)
  block            explicit "yes": block the product, verify the block, confirm
  finish_dispute   explicit "no": confirm the verified dispute without a block
  close_legit      "it's mine": store the label, verify it, close
  clarify          unclear or injected message: ask again; hand off after N unclear turns
  unsupported      out-of-scope request: say so and do nothing (abstain)
  handoff          structured package for a human, routed by language and specialty

Safety properties, enforced here and in the tools:
  - Identity comes only from the session token of each turn (never from what is written).
  - Every action goes through a tool that checks ownership and the policy's requirements.
  - A reply only states actions read back from the store (verified), never intentions.
  - Tool failures are retried MAX_TOOL_ATTEMPTS times, then the case goes to a human.
  - Every step is written to the audit log (redacted message, extraction, rules, tool calls).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from bianque.agent.llm import Extraction, GeminiExtractor, LLMUnavailable, MenuParser, redact
from bianque.agent.replies import charge_facts, render
from bianque.agent.session import Session, SessionError, verify
from bianque.agent.tools import (
    ChargeView,
    PermissionDenied,
    RequirementsNotMet,
    ToolFailure,
    Tools,
)

MAX_TOOL_ATTEMPTS = 2
MAX_CANDIDATES = 3


class State(TypedDict, total=False):
    # input of a turn
    event: str  # "start_proactive" | "start_reactive" | "message"
    message: str
    session_token: str | None
    transaction_id: str | None
    # conversation
    conversation_id: str
    mode: str  # "proactive" | "reactive"
    stage: str
    language: str
    facts: list[str]
    unclear_turns: int
    candidates: list[str]
    decision: dict | None
    case_id: str | None
    actions: list[dict]
    handoff: dict | None
    transcript: list[dict]
    llm_mode: str
    extraction: dict | None
    pending_intent: str | None  # intent stated before the charge was identified
    handoff_reason: str | None
    # output of a turn
    reply: str
    route: str


class Agent:
    def __init__(
        self,
        tools: Tools,
        extractor: GeminiExtractor | MenuParser | None = None,
        today: str = "2026-06-17",
    ) -> None:
        self.tools = tools
        self.extractor = extractor
        self.menu = MenuParser()
        self.today = today
        self.graph = self._build().compile(checkpointer=InMemorySaver())

    # --- public API (one call per turn) ----------------------------------------------------

    def start_proactive(
        self, conversation_id: str, token: str | None, transaction_id: str, language: str = "es"
    ) -> State:
        return self._run(
            conversation_id,
            {
                "event": "start_proactive",
                "session_token": token,
                "transaction_id": transaction_id,
                "language": language,
                "mode": "proactive",
            },
        )

    def start_reactive(self, conversation_id: str, token: str | None, message: str) -> State:
        return self._run(
            conversation_id,
            {
                "event": "start_reactive",
                "session_token": token,
                "message": message,
                "mode": "reactive",
                "stage": "identify",
            },
        )

    def reply(self, conversation_id: str, token: str | None, message: str) -> State:
        return self._run(
            conversation_id, {"event": "message", "session_token": token, "message": message}
        )

    def state(self, conversation_id: str) -> State:
        return self.graph.get_state(self._config(conversation_id)).values

    def _config(self, conversation_id: str) -> dict:
        return {"configurable": {"thread_id": conversation_id}}

    def _run(self, conversation_id: str, turn: dict) -> State:
        current = self.state(conversation_id)
        if turn["event"] != "message" and current:
            raise ValueError(f"conversation {conversation_id} already exists")
        if turn["event"] == "message" and not current:
            raise KeyError(f"unknown conversation {conversation_id}")
        base = {
            "conversation_id": conversation_id,
            "facts": [],
            "unclear_turns": 0,
            "candidates": [],
            "actions": [],
            "transcript": [],
            "decision": None,
            "case_id": None,
            "handoff": None,
            "language": "es",
            "llm_mode": "none",
        }
        if current:
            base = {}
        message = turn.get("message")
        transcript = list((current or {}).get("transcript", []))
        if message:
            transcript.append({"role": "customer", "text": message})
        turn_input = {**base, **turn, "transcript": transcript, "reply": "", "handoff_reason": None}
        self.graph.invoke(turn_input, self._config(conversation_id))
        out = self.state(conversation_id)
        transcript = [*out["transcript"], {"role": "bianque", "text": out["reply"]}]
        self.graph.update_state(self._config(conversation_id), {"transcript": transcript})
        return self.state(conversation_id)

    # --- helpers ---------------------------------------------------------------------------

    def _audit(self, s: State, step: str, **detail: Any) -> None:
        self.tools.store.audit(s["conversation_id"], step, detail)

    def _session(self, s: State) -> Session:
        return verify(s.get("session_token"))

    def _call(self, s: State, name: str, fn: Callable, *args: Any) -> Any:
        """A tool call with bounded retries; every attempt is audited."""
        for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
            try:
                result = fn(*args)
                self._audit(s, "tool", tool=name, attempt=attempt, ok=True)
                return result
            except ToolFailure as e:
                self._audit(s, "tool", tool=name, attempt=attempt, ok=False, error=str(e))
        raise ToolFailure(f"{name} failed after {MAX_TOOL_ATTEMPTS} attempts")

    def _charge(self, s: State, session: Session) -> ChargeView:
        return self.tools.get_charge(session, s["transaction_id"])

    def _say(self, s: State, key: str, **facts: Any) -> dict:
        self._audit(s, "reply", template=key)
        return {"reply": render(key, s.get("language", "es"), **facts)}

    def _context(self, s: State) -> str:
        return {
            "await_recognition": "asked whether the customer recognizes a specific charge",
            "await_block_confirmation": "asked yes/no: provisionally block the product?",
            "await_charge_choice": "asked which of several listed charges, by number",
            "identify": "conversation start: the customer writes about a charge",
        }.get(s.get("stage", ""), "conversation in progress")

    def _login_required(self, s: State, error: SessionError) -> dict:
        self._audit(s, "session_rejected", reason=str(error))
        return {**self._say(s, "login_required"), "route": "end"}

    # --- nodes -----------------------------------------------------------------------------

    def _entry(self, s: State) -> str:
        if s["event"] == "start_proactive":
            return "alert"
        if s.get("stage") in ("closed", "handed_off"):
            return "finished"
        return "understand"

    def alert(self, s: State) -> dict:
        try:
            session = self._session(s)
            decision = self.tools.policy_decision(session, s["transaction_id"])
            charge = self._charge(s, session)
        except SessionError as e:
            return {**self._login_required(s, e), "stage": "await_recognition"}
        except PermissionDenied:
            self._audit(s, "permission_denied", tool="policy_decision")
            return {**self._say(s, "not_found"), "stage": "closed", "route": "end"}
        decision_d = {
            "action": decision.action,
            "channel": decision.channel,
            "handled_by": decision.handled_by,
            "reasons": [asdict(r) for r in decision.reasons],
            "policy_version": decision.policy_version,
        }
        self._audit(s, "policy_decision", **decision_d)
        if decision.action == "human_review":
            return {"decision": decision_d, "route": "handoff", "stage": "await_recognition"}
        if decision.action != "contact":
            return {
                "decision": decision_d,
                "stage": "closed",
                "reply": "",
                "route": "end",
            }
        return {
            **self._say(s, "alert", **charge_facts(charge, s.get("language", "es"))),
            "decision": decision_d,
            "stage": "await_recognition",
            "route": "end",
        }

    def understand(self, s: State) -> dict:
        message = s.get("message") or ""
        mode = "gemini"
        try:
            if self.extractor is None:
                raise LLMUnavailable("no model configured")
            extraction = self.extractor.extract(message, self._context(s), self.today)
        except LLMUnavailable as e:
            self._audit(s, "llm_fallback", reason=str(e)[:200])
            extraction, mode = self.menu.extract(message, self._context(s), self.today), "menu"
        self._audit(
            s,
            "understood",
            message=redact(message)[:500],
            by=mode,
            **extraction.model_dump(),
        )
        language = extraction.language if extraction.language in ("es", "pt") else s["language"]
        return {
            "language": language,
            "llm_mode": mode,
            "extraction": extraction.model_dump(),
            "route": self._next(s, extraction),
        }

    def _next(self, s: State, x: Extraction) -> str:
        stage = s.get("stage")
        if x.injection_suspected:
            self._audit(s, "injection_suspected")
            return "clarify"
        if stage in ("identify", "await_charge_choice"):
            return "identify"
        if x.intent == "out_of_scope":
            return "unsupported"
        if stage == "await_block_confirmation":
            return {"yes": "block", "no": "finish_dispute"}.get(x.confirmation, "clarify")
        if stage == "await_recognition":
            return {"not_mine": "dispute", "its_mine": "close_legit"}.get(x.intent, "clarify")
        return "clarify"

    def identify(self, s: State) -> dict:
        x = Extraction(**s["extraction"])
        if x.intent == "out_of_scope" and s.get("stage") == "identify":
            return {"route": "unsupported"}
        try:
            session = self._session(s)
        except SessionError as e:
            return self._login_required(s, e)
        if s.get("stage") == "await_charge_choice":
            choice = re.fullmatch(r"\s*(\d)\s*", s.get("message") or "")
            if choice and 1 <= int(choice.group(1)) <= len(s["candidates"]):
                chosen = s["candidates"][int(choice.group(1)) - 1]
                stated = s.get("pending_intent") or x.intent
                return self._identified(s, session, chosen, x.model_copy(update={"intent": stated}))
        from datetime import date as _date

        on_date = None
        if x.date:
            try:
                on_date = _date.fromisoformat(x.date)
            except ValueError:
                on_date = None
        found = self.tools.find_charges(session, x.amount_usd, on_date, x.merchant)
        self._audit(s, "charges_found", n=len(found))
        if len(found) == 1:
            return self._identified(s, session, found[0].transaction_id, x)
        if not found:
            return {"route": "clarify", "stage": "identify"}
        top = found[:MAX_CANDIDATES]
        options = "\n".join(
            f"{i}. USD {c.amount_usd:,.2f} - {c.merchant or '-'} - {c.transaction_date[:10]}"
            for i, c in enumerate(top, 1)
        )
        return {
            **self._say(s, "choose_charge", options=options),
            "candidates": [c.transaction_id for c in top],
            "pending_intent": x.intent if x.intent in ("not_mine", "its_mine") else None,
            "stage": "await_charge_choice",
            "route": "end",
        }

    def _identified(self, s: State, session: Session, transaction_id: str, x: Extraction) -> dict:
        update: dict = {"transaction_id": transaction_id, "candidates": []}
        charge = self.tools.get_charge(session, transaction_id)
        self._audit(s, "charge_identified", transaction_id=transaction_id)
        if x.intent == "not_mine":
            return {**update, "stage": "await_recognition", "route": "dispute"}
        if x.intent == "its_mine":
            return {**update, "stage": "await_recognition", "route": "close_legit"}
        return {
            **update,
            **self._say(s, "ask_recognition", **charge_facts(charge, s["language"])),
            "stage": "await_recognition",
            "route": "end",
        }

    def dispute(self, s: State) -> dict:
        try:
            session = self._session(s)
            facts = {*s["facts"], "authenticated_session", "intent_not_mine"}
            charge = self._charge(s, session)
            decision = s.get("decision")
            if decision is None:  # reactive: apply the policy to the identified charge
                d = self.tools.policy_decision(session, s["transaction_id"])
                decision = {
                    "action": d.action,
                    "handled_by": d.handled_by,
                    "reasons": [asdict(r) for r in d.reasons],
                    "policy_version": d.policy_version,
                }
                self._audit(s, "policy_decision", **decision)
            case = self._call(
                s, "open_dispute", self.tools.open_dispute, session, s["transaction_id"], facts
            )
            verified = self._call(
                s, "verify_case", self.tools.verify_case, session, case["case_id"]
            )
        except SessionError as e:
            return self._login_required(s, e)
        except PermissionDenied:
            self._audit(s, "permission_denied", tool="open_dispute")
            return {**self._say(s, "not_found"), "route": "end"}
        except (ToolFailure, RequirementsNotMet) as e:
            return {
                "route": "handoff",
                "handoff_reason": f"tool_failure: {e}",
                "facts": sorted(facts),
            }
        actions = [
            *s["actions"],
            {"action": "open_dispute", "case_id": verified["case_id"], "verified": True},
        ]
        escalate = decision.get("handled_by") == "human" or decision.get("action") == "human_review"
        update = {
            "facts": sorted(facts),
            "case_id": verified["case_id"],
            "actions": actions,
            "decision": decision,
        }
        if escalate:
            rules = [
                r["rule"]
                for r in decision["reasons"]
                if r["rule"] in ("high_amount", "repeat_complainer", "uncertain")
            ]
            return {**update, "route": "handoff", "handoff_reason": "policy: " + ", ".join(rules)}
        return {
            **update,
            **self._say(
                s,
                "ask_block",
                case_id=verified["case_id"],
                **charge_facts(charge, s["language"]),
            ),
            "stage": "await_block_confirmation",
            "route": "end",
        }

    def block(self, s: State) -> dict:
        try:
            session = self._session(s)
            facts = {*s["facts"], "explicit_confirmation"}
            charge = self._charge(s, session)
            blk = self._call(
                s,
                "block_product",
                self.tools.block_product,
                session,
                s["transaction_id"],
                s["case_id"],
                facts,
            )
            verified = self._call(
                s, "verify_block", self.tools.verify_block, session, blk["block_id"]
            )
        except SessionError as e:
            return self._login_required(s, e)
        except (ToolFailure, RequirementsNotMet, PermissionDenied) as e:
            return {"route": "handoff", "handoff_reason": f"tool_failure: {e}"}
        actions = [
            *s["actions"],
            {"action": "provisional_block", "block_id": verified["block_id"], "verified": True},
        ]
        return {
            **self._say(
                s,
                "done_blocked",
                case_id=s["case_id"],
                block_id=verified["block_id"],
                **charge_facts(charge, s["language"]),
            ),
            "facts": sorted(facts),
            "actions": actions,
            "stage": "closed",
            "route": "end",
        }

    def finish_dispute(self, s: State) -> dict:
        try:
            session = self._session(s)
            charge = self._charge(s, session)
            self._call(s, "verify_case", self.tools.verify_case, session, s["case_id"])
        except SessionError as e:
            return self._login_required(s, e)
        except (ToolFailure, PermissionDenied) as e:
            return {"route": "handoff", "handoff_reason": f"tool_failure: {e}"}
        return {
            **self._say(
                s, "done_not_blocked", case_id=s["case_id"], **charge_facts(charge, s["language"])
            ),
            "stage": "closed",
            "route": "end",
        }

    def close_legit(self, s: State) -> dict:
        try:
            session = self._session(s)
            facts = {*s["facts"], "authenticated_session", "intent_its_mine"}
            charge = self._charge(s, session)
            self._call(
                s,
                "close_as_legitimate",
                self.tools.close_as_legitimate,
                session,
                s["transaction_id"],
                facts,
            )
            label = self.tools.store.get_label(s["transaction_id"])
            if not label or label["customer_id"] != session.customer_id:
                raise ToolFailure("label not stored")
        except SessionError as e:
            return self._login_required(s, e)
        except (ToolFailure, RequirementsNotMet, PermissionDenied) as e:
            return {"route": "handoff", "handoff_reason": f"tool_failure: {e}"}
        actions = [
            *s["actions"],
            {"action": "close_as_legitimate", "label": "legitimate", "verified": True},
        ]
        return {
            **self._say(s, "legit_closed", **charge_facts(charge, s["language"])),
            "facts": sorted(facts),
            "actions": actions,
            "stage": "closed",
            "route": "end",
        }

    def clarify(self, s: State) -> dict:
        turns = s.get("unclear_turns", 0) + 1
        if turns >= self.tools.policy.on_unclear_intent_turns:
            return {
                "unclear_turns": turns,
                "route": "handoff",
                "handoff_reason": f"unclear intent after {turns} turns",
            }
        stage = s.get("stage")
        if stage in ("identify", "await_charge_choice") or not s.get("transaction_id"):
            return {**self._say(s, "clarify_charge"), "unclear_turns": turns, "route": "end"}
        try:
            charge = self._charge(s, self._session(s))
        except SessionError as e:
            return self._login_required(s, e)
        key = "clarify_block" if stage == "await_block_confirmation" else "clarify_recognition"
        return {
            **self._say(s, key, **charge_facts(charge, s["language"])),
            "unclear_turns": turns,
            "route": "end",
        }

    def unsupported(self, s: State) -> dict:
        self._audit(s, "abstained", reason="out_of_scope")
        return {**self._say(s, "unsupported"), "route": "end"}

    def handoff(self, s: State) -> dict:
        reason = s.get("handoff_reason") or "policy: human review"
        agent = self.tools.route_agent(s.get("language", "es"), fraud=True)
        charge = None
        try:
            charge = self._charge(s, self._session(s)) if s.get("transaction_id") else None
        except (SessionError, PermissionDenied):
            charge = None
        open_questions = []
        if not s.get("case_id"):
            open_questions.append("Does the customer recognize the charge?")
        elif not any(a["action"] == "provisional_block" for a in s["actions"]):
            open_questions.append("Should the product be provisionally blocked?")
        if "unclear" in reason:
            open_questions.append("The customer's intent was unclear to the assistant.")
        if "tool_failure" in reason:
            open_questions.append("A system action failed; check and complete it manually.")
        package = {
            "request": "Unrecognized charge"
            + (" (customer disputes it)" if "intent_not_mine" in s["facts"] else ""),
            "reason": reason,
            "language": s.get("language", "es"),
            "verified_facts": {
                "session_verified": "authenticated_session" in s["facts"],
                "charge": None if charge is None else asdict(charge),
                "policy_decision": s.get("decision"),
            },
            "actions_taken": s["actions"],
            "evidence": {
                "conversation_id": s["conversation_id"],
                "audit_events": len(self.tools.store.audit_log(s["conversation_id"])),
                "customer_messages": sum(1 for t in s["transcript"] if t["role"] == "customer"),
            },
            "open_questions": open_questions,
            "routed_to": agent,
        }
        handoff_id = self.tools.store.add_handoff(
            s["conversation_id"], agent and agent["agent_id"], package
        )
        self._audit(
            s, "handoff", handoff_id=handoff_id, reason=reason, agent=agent and agent["agent_id"]
        )
        note = f" ({s['case_id']})" if s.get("case_id") else ""
        return {
            **self._say(s, "handoff", case_note=note),
            "handoff": {"handoff_id": handoff_id, **package},
            "stage": "handed_off",
            "route": "end",
        }

    def finished(self, s: State) -> dict:
        return self._say(s, "closed" if s["stage"] == "closed" else "handed_off")

    # --- graph -----------------------------------------------------------------------------

    def _build(self) -> StateGraph:
        g = StateGraph(State)
        nodes = {
            "alert": self.alert,
            "understand": self.understand,
            "identify": self.identify,
            "dispute": self.dispute,
            "block": self.block,
            "finish_dispute": self.finish_dispute,
            "close_legit": self.close_legit,
            "clarify": self.clarify,
            "unsupported": self.unsupported,
            "handoff": self.handoff,
            "finished": self.finished,
        }
        for name, fn in nodes.items():
            g.add_node(name, fn)
        g.add_conditional_edges(
            START,
            self._entry,
            {"alert": "alert", "understand": "understand", "finished": "finished"},
        )
        targets = {name: name for name in nodes if name not in ("alert", "finished")} | {"end": END}
        for name in nodes:
            if name in ("handoff", "finished", "unsupported"):
                g.add_edge(name, END)
            else:
                g.add_conditional_edges(name, lambda s: s.get("route", "end"), targets)
        return g
