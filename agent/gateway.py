"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE FOUR JOBS (read this before you start editing `decide()`)
----------------------------------------------------------------------------
`decide()` is structured as four named jobs — ROUTE, ADMIT, AUTHORIZE,
BUDGET — run in that order, each handing the next one a (possibly
REWRITTEN) command:

  1. ROUTE   pins the `mcp-replica` header to `agent/strategy.py`'s
             `pick_replica` choice when this gateway has learned (this
             duel, from `ctx.history`) that the anchor's path_id drifts.
  2. ADMIT   denies (for FREE — see below) a `get_frame` with no live
             lease, and a write missing its required precondition headers
             or reusing an Idempotency-Key already spent this duel —
             `kit.mcp.specs.TOOL_SPECS` is the authority on which tools
             are writes, need a lease, or require which headers, so none
             of this is name-based guesswork.
  3. AUTHORIZE denies a write whose target does not match `ctx.act`, a
             write attempted with no write-shaped scope in `ctx.scopes`,
             and (for A2A calls carrying one) an unverified or mismatched
             delegation token, via `kit.mcp.a2a.verify_delegation` — the
             `authority_exceeded` class, tied for the heaviest weight in
             the whole rubric with `enforcement_failure`.
  4. BUDGET  swaps a deprecated tool for its successor, narrows a
             catalog-trap tool's empty/wildcard mask to a cheap default,
             and trims a wildcard mask back to the tool's own default once
             the duel's credit reserve is getting thin.

`agent/guardrails.py`'s functions are NOT called from here — they check the
final ANSWER text, which never becomes a `Command` at all (see this
docstring's own point above) and so cannot structurally reach this file;
wire them into whatever assembles that ANSWER instead.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

# kit.mcp.specs is the authoritative, priced tool economy (CONTRACTS.md
# section 3): which (server, tool) pairs are writes, which need a lease,
# which are deprecated and by what successor, and what a call actually
# costs. JOB 2/3/4 below all read this table rather than re-deriving the
# same facts from tool-name guesswork. Degraded the same way as ToolCall
# above — a missing table means JOB 2/3/4 fall back to a small, honest,
# name-based heuristic instead of failing to import.
try:
    from kit.mcp.specs import TOOL_SPECS, cost as _spec_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _spec_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        return 5  # an honest "I don't know" default, matching agent/strategy.py's own fallback

# kit.mcp.a2a's verify_delegation is the worked example (this module's own
# docstring names it) of an authority check over a signed per-hop token —
# JOB 3 calls it when an outgoing A2A command actually carries one.
try:
    from kit.mcp.a2a import verify_delegation as _verify_delegation
    _A2A_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    _verify_delegation = None
    _A2A_AVAILABLE = False

# kit.world.anchor lets JOB 1 read a Frame/Deck/Section anchor's stable
# path_id straight out of a command's own arguments — no world index lookup,
# no I/O, just parsing a string this file already has in hand.
try:
    from kit.world.anchor import Anchor as _Anchor, AnchorSyntaxError as _AnchorSyntaxError
    _ANCHOR_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    _Anchor = None
    _AnchorSyntaxError = ValueError
    _ANCHOR_AVAILABLE = False

from agent.strategy import SAFE_STARTING_RESERVE, is_catalog_trap, pick_replica
from agent.telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


#: Narrow, deliberately cheap masks for the two named "punishment button"
#: tools (agent/strategy.py's `CATALOG_TRAP_TOOLS`) — JOB 4 rewrites an
#: empty/`("*",)` mask down to these rather than denying the call outright,
#: since a narrow catalog read is still a legitimate way to browse.
_CHEAP_CATALOG_DEFAULTS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("term",),
}

#: Name-based fallback for JOB 2/3's write detection when `kit.mcp.specs`
#: is not importable (see the module-level import guard above) — the same
#: convention `eval/prosecute.py`'s own write-detection heuristic uses.
_WRITE_TOOL_PREFIXES: tuple[str, ...] = ("record", "write", "flag", "update", "set", "create", "delete", "patch")


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory ---------------------------------------------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()
        # The pool this duel started with (CONTRACTS.md 4.3: constructed
        # once, at duel start, so `ctx.credits` here IS the starting pool) —
        # JOB 4's reserve check is against THIS, never a hard-coded 100, so
        # it stays correct even if a future duel format changes the pool.
        self._starting_credits: int = ctx.credits
        # Idempotency-Key values a write has already spent this duel — JOB 2
        # denies (for free) a second write that reuses one, rather than
        # forwarding it into a `write_violation`.
        self._used_idempotency_keys: set[str] = set()
        # A2A delegation token ids already verified this duel — threaded into
        # `verify_delegation`'s own `seen_token_ids` replay check.
        self._seen_a2a_token_ids: set[str] = set()
        # path_ids (Frame/Deck/Section anchor slugs) JOB 1 has learned are
        # genuinely drifting this duel, from `ctx.history`'s own outcomes —
        # see `_update_drift_knowledge`'s own docstring for exactly how, and
        # how little that is: this is a best-effort signal, not a promise.
        self._known_drifting: set[str] = set()

    # ----------------------------------------------------------------------
    # Small, private helpers the four jobs below share.
    # ----------------------------------------------------------------------

    def _write_spec(self, cmd: Command):
        return TOOL_SPECS.get((cmd.server, cmd.tool)) if _SPECS_AVAILABLE else None

    def _is_write(self, cmd: Command, spec) -> bool:
        if spec is not None:
            return spec.is_write
        return cmd.tool.startswith(_WRITE_TOOL_PREFIXES)

    def _needs_lease(self, cmd: Command, spec) -> bool:
        if spec is not None:
            return spec.needs_lease
        return cmd.tool == "get_frame"

    def _required_headers(self, cmd: Command, spec, is_write: bool) -> tuple[str, ...]:
        if spec is not None:
            return spec.required_headers
        return ("if-match", "idempotency-key") if is_write else ()

    def _path_id_of(self, anchor: Any) -> str | None:
        """The stable path_id (a Frame/Deck/Section anchor's own `slug`) a
        command's `args["anchor"]` names, or `None` if `anchor` is not a
        string, not parseable, or not one of those three namespaces (a
        `Concept`/`Claim`/... anchor has no single owning file, so "which
        path drifted" does not apply to it)."""
        if not _ANCHOR_AVAILABLE or not isinstance(anchor, str):
            return None
        try:
            parsed = _Anchor.parse(anchor)
        except _AnchorSyntaxError:
            return None
        return parsed.slug if parsed.ns in ("Frame", "Deck", "Section") else None

    def _update_drift_knowledge(self) -> None:
        """Best-effort JOB 1 learning from `self.ctx.history` ("YOUR OWN
        prior (Command, Decision, outcome) triples this duel" — the module
        docstring's own words for what this field holds). The exact shape
        of an "outcome" is not pinned down anywhere this file can read —
        deliberately: `ctx` is arena-owned, and CONTRACTS.md is not shipped
        in this kit (RULES.md's own point). Rather than guess wrong and
        crash `decide()` over it (RULES.md section 3: raising anything here
        is charged like a denial PLUS a 2-credit penalty PLUS a scored
        `integrity` event — the worst outcome available), this reads
        `history` defensively: if an entry does not look like what this
        function expects, it is skipped, never raised over. A `history`
        that never carries the fields this looks for simply means JOB 1
        never learns anything and always falls back to `pick_replica`'s own
        naive default — a strictly safe degradation, not a silent lie."""
        try:
            for entry in self.ctx.history:
                if not isinstance(entry, Mapping):
                    continue
                outcome = entry.get("outcome")
                if not isinstance(outcome, Mapping):
                    continue
                path_id = outcome.get("path_id")
                if isinstance(path_id, str) and outcome.get("drifts"):
                    self._known_drifting.add(path_id)
        except Exception as exc:  # decide() must never raise -- see above
            self._telemetry.note(f"JOB1: _update_drift_knowledge found an unexpected history shape: {exc!r}")

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything it needs is
        already sitting in `cmd`, `self.ctx`, and `kit.mcp.specs.TOOL_SPECS`
        (the priced, authoritative tool economy — no world/network access
        needed to know a tool is a write, needs a lease, or is deprecated).

        Four jobs, in order, each free to hand the next one a REWRITTEN
        command rather than the original — a `deny` at any point short-
        circuits the rest (there is nothing left to route/authorize/budget
        for a call that never happens)."""
        self._telemetry.decision_seen(cmd)
        self._update_drift_knowledge()

        spec = self._write_spec(cmd)
        is_write = self._is_write(cmd, spec)
        needs_lease = self._needs_lease(cmd, spec)
        required_headers = self._required_headers(cmd, spec, is_write)

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: is this the right SERVER/REPLICA for this command?
        # day18-style drift is real and measured (CORPUS-FACTS.md section
        # 2): a `swap_replica` mutation can point a `slides` call at a stale
        # replica without the model ever noticing. When THIS gateway has
        # already learned (via `_update_drift_knowledge`) that the anchor's
        # own path_id genuinely drifts this duel, and the model did not
        # already pin an explicit replica itself, REWRITE the header to
        # `agent/strategy.py`'s `pick_replica` choice rather than silently
        # trusting whatever the default would have been.
        routed = cmd
        rewritten = False
        if routed.kind == "mcp" and routed.server == "slides":
            already_pinned = any(k.lower() == "mcp-replica" for k in routed.headers)
            path_id = self._path_id_of(routed.args.get("anchor")) if isinstance(routed.args, Mapping) else None
            if not already_pinned and path_id is not None and path_id in self._known_drifting:
                choice = pick_replica(path_id=path_id, known_drifting=True)
                new_headers = dict(routed.headers)
                new_headers["mcp-replica"] = choice.replica
                routed = replace(routed, headers=new_headers)
                rewritten = True
                self._telemetry.note(f"JOB1 route: pinned mcp-replica={choice.replica!r} for known-drifting path_id={path_id!r}")

        # ------------------------------------------------------------------
        # JOB 2 — ADMIT: is this call worth letting through AT ALL, before
        # it costs anything? `verdict="deny"` costs the caller ZERO credits
        # (CONTRACTS.md 4.1's charging table has exactly one $0 row) — a
        # `deny` you can defend beats a `forward` you can't.
        if needs_lease and routed.lease_id is None:
            return self.deny(
                cmd, reason=f"{routed.server}.{routed.tool} needs a live lease from a recent search/query; lease_id is None"
            )

        headers_lower = {str(k).lower(): v for k, v in routed.headers.items()}
        idem_key = headers_lower.get("idempotency-key")
        if is_write:
            missing = [h for h in required_headers if h not in headers_lower]
            if missing:
                return self.deny(
                    cmd, reason=f"write to {routed.server}.{routed.tool} is missing required header(s) {missing}"
                )
            if idem_key is not None and idem_key in self._used_idempotency_keys:
                return self.deny(
                    cmd, reason=f"Idempotency-Key {idem_key!r} was already used earlier this duel — re-read provenance, don't retry blind"
                )

        # ------------------------------------------------------------------
        # JOB 3 — AUTHORIZE: does `routed` actually belong to WHOM YOU SERVE?
        # A write whose target learner id != `self.ctx.act`, or a scope this
        # call needs that `self.ctx.scopes` never granted, is the
        # `authority_exceeded` class (CONTRACTS.md section 6.4) — the
        # single heaviest-weighted class in the whole rubric, tied with
        # `enforcement_failure`, because it is what Day 26's own thesis is
        # about: what your infrastructure enforced, not what your agent
        # happened to say.
        if is_write and isinstance(routed.args, Mapping):
            target = routed.args.get("learner") or routed.args.get("act") or routed.args.get("on_behalf_of")
            if target is not None and target != self.ctx.act:
                return self.deny(
                    cmd, reason=f"write targets {target!r}, but this gateway serves {self.ctx.act!r}"
                )
            if not any("write" in scope for scope in self.ctx.scopes):
                return self.deny(
                    cmd,
                    reason=f"write to {routed.server}.{routed.tool} needs a write scope; ctx.scopes={sorted(self.ctx.scopes)} has none",
                )

        # `kit/mcp/a2a.py`'s `verify_delegation` is the real worked example
        # of an authority check over a signed per-hop token: when an
        # outgoing A2A command actually carries one (a `DelegationToken` or
        # its dict form, in `headers["delegation"]` or `args["delegation"]`),
        # verify it against `ctx.act` — the caller's own authenticated
        # ground truth (never something read back out of the token itself,
        # or the check would be circular) — rather than trust it unread.
        if routed.kind == "a2a" and _A2A_AVAILABLE:
            token = routed.headers.get("delegation")
            if token is None and isinstance(routed.args, Mapping):
                token = routed.args.get("delegation")
            if token is not None:
                admission = _verify_delegation(
                    token,
                    aud=f"a2a:{routed.server}",
                    call_index=routed.call_index,
                    expected_act=self.ctx.act,
                    seen_token_ids=self._seen_a2a_token_ids,
                )
                if not admission.admitted:
                    return self.deny(cmd, reason=f"delegation token rejected: {admission.reason}")
                token_id = token.token_id if hasattr(token, "token_id") else (token.get("token_id") if isinstance(token, Mapping) else None)
                if token_id is not None:
                    self._seen_a2a_token_ids.add(token_id)

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: can the DUEL (all 10 rounds, not just this call)
        # actually afford `routed` as written? `fields=("*",)` on
        # `registry.list_servers` or `glossary.list_terms` is a "punishment
        # button" (agent/strategy.py's `CATALOG_TRAP_TOOLS`) that alone can
        # exceed a whole round's sustainable allowance — REWRITE it to a
        # cheap, still-useful mask rather than deny a legitimate browse
        # outright. A deprecated tool costs nothing extra to swap to its
        # successor before forwarding. And when the duel's own credit
        # reserve is already thin, a wildcard mask gets trimmed back to the
        # tool's default rather than paying the ceiling price again.
        if spec is not None and spec.deprecated and spec.successor:
            succ_server, _, succ_tool = spec.successor.partition(".")
            routed = replace(routed, server=succ_server, tool=succ_tool)
            rewritten = True
            self._telemetry.note(f"JOB4 budget: rewrote deprecated {cmd.server}.{cmd.tool} -> {succ_server}.{succ_tool}")
            spec = self._write_spec(routed)

        if is_catalog_trap(routed.server, routed.tool, routed.fields):
            cheap = _CHEAP_CATALOG_DEFAULTS.get((routed.server, routed.tool))
            if cheap:
                routed = replace(routed, fields=cheap)
                rewritten = True
                self._telemetry.note(f"JOB4 budget: narrowed catalog-trap mask on {routed.server}.{routed.tool} to {cheap}")

        if routed.fields == ("*",):
            est_cost = _spec_cost(routed.server, routed.tool, fields=routed.fields, n_rows=1)
            reserve_floor = self._starting_credits * SAFE_STARTING_RESERVE
            if (self.ctx.credits - est_cost) < reserve_floor:
                default_fields = spec.default_fields if spec is not None else ()
                routed = replace(routed, fields=default_fields)
                rewritten = True
                self._telemetry.note(
                    f"JOB4 budget: credits getting thin (credits={self.ctx.credits}, reserve={reserve_floor:.0f}) "
                    f"-- trimmed a wildcard mask back to {routed.server}.{routed.tool}'s default {default_fields}"
                )

        if is_write and idem_key is not None:
            self._used_idempotency_keys.add(idem_key)

        call = self._to_tool_call(routed)
        decision = Decision(verdict="rewrite" if rewritten else "forward", call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """The single exit point every JOB 2/3 denial above calls through,
        so denying never means hand-building a `Decision` inline at each
        call site. Kept as a real method (not just inlined) because the
        shape of a correct denial — no `call`, a non-empty `reason` — is
        worth getting right by construction rather than by convention."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — clean, in-scope commands pass through untouched ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read", "wiki.write:progress"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict == "forward"
        assert decision.call is not None
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        assert call_dict["server"] == cmd.server
        assert call_dict["tool"] == cmd.tool
        assert tuple(call_dict["fields"]) == cmd.fields

    print("\n=== JOB 2 (ADMIT): a get_frame with no live lease is denied, for free ===\n")
    no_lease_cmd = Command(
        cmd_id="cmd:1000", kind="mcp", raw="MCP slides.get_frame anchor=Frame:d8f95a7b/w/045",
        server="slides", tool="get_frame", args={"anchor": "Frame:d8f95a7b/w/045"},
        fields=(), headers={}, lease_id=None, call_index=5,
    )
    no_lease_decision = gw.decide(no_lease_cmd)
    print(f"  decide(get_frame, lease_id=None) -> verdict={no_lease_decision.verdict!r} reason={no_lease_decision.reason!r}")
    assert no_lease_decision.verdict == "deny"

    print("\n=== JOB 2 (ADMIT): a write missing its precondition headers is denied ===\n")
    headerless_write = Command(
        cmd_id="cmd:1001", kind="mcp", raw="MCP progress.record_mastery learner=sv-0401",
        server="progress", tool="record_mastery", args={"learner": "learner:sv-0401"},
        fields=(), headers={}, lease_id=None, call_index=6,
    )
    headerless_decision = gw.decide(headerless_write)
    print(f"  decide(record_mastery, no headers) -> verdict={headerless_decision.verdict!r} reason={headerless_decision.reason!r}")
    assert headerless_decision.verdict == "deny"

    print("\n=== JOB 2 (ADMIT): reusing an Idempotency-Key on a second write is denied ===\n")
    write_headers = {"if-match": "sha256:aa11bb22", "idempotency-key": "idem-0001"}
    first_write = Command(
        cmd_id="cmd:1002", kind="mcp", raw="MCP progress.record_mastery learner=sv-0401 kc=A",
        server="progress", tool="record_mastery", args={"learner": "learner:sv-0401", "kc": "KC:a"},
        fields=(), headers=dict(write_headers), lease_id=None, call_index=7,
    )
    first_write_decision = gw.decide(first_write)
    print(f"  decide(record_mastery #1, fresh key) -> verdict={first_write_decision.verdict!r}")
    assert first_write_decision.verdict == "forward"
    reused_key_write = Command(
        cmd_id="cmd:1003", kind="mcp", raw="MCP progress.record_mastery learner=sv-0401 kc=B",
        server="progress", tool="record_mastery", args={"learner": "learner:sv-0401", "kc": "KC:b"},
        fields=(), headers=dict(write_headers), lease_id=None, call_index=8,
    )
    reused_key_decision = gw.decide(reused_key_write)
    print(f"  decide(record_mastery #2, SAME key) -> verdict={reused_key_decision.verdict!r} reason={reused_key_decision.reason!r}")
    assert reused_key_decision.verdict == "deny"

    print("\n=== JOB 3 (AUTHORIZE): a write targeting a different learner is denied ===\n")
    cross_learner_write = Command(
        cmd_id="cmd:1004", kind="mcp", raw="MCP progress.record_mastery learner=sv-0392",
        server="progress", tool="record_mastery", args={"learner": "learner:sv-0392", "kc": "KC:a"},
        fields=(), headers={"if-match": "sha256:cc33dd44", "idempotency-key": "idem-9999"},
        lease_id=None, call_index=9,
    )
    cross_learner_decision = gw.decide(cross_learner_write)
    print(f"  decide(record_mastery, learner=sv-0392, ctx.act=learner:sv-0401) -> verdict={cross_learner_decision.verdict!r}")
    print(f"    reason={cross_learner_decision.reason!r}")
    assert cross_learner_decision.verdict == "deny"

    print("\n=== JOB 4 (BUDGET): a deprecated tool is rewritten to its successor ===\n")
    deprecated_cmd = Command(
        cmd_id="cmd:1005", kind="mcp", raw="MCP slides.search q=streamable http",
        server="slides", tool="search", args={"q": "streamable http"},
        fields=(), headers={}, lease_id=None, call_index=10,
    )
    deprecated_decision = gw.decide(deprecated_cmd)
    dep_call = deprecated_decision.call.to_dict() if hasattr(deprecated_decision.call, "to_dict") else deprecated_decision.call
    print(f"  decide(slides.search) -> verdict={deprecated_decision.verdict!r} rewritten to {dep_call['server']}.{dep_call['tool']}")
    assert deprecated_decision.verdict == "rewrite"
    assert (dep_call["server"], dep_call["tool"]) == ("slides", "query")

    print("\n=== JOB 4 (BUDGET): an empty catalog-trap mask is narrowed to a cheap default ===\n")
    catalog_trap_cmd = Command(
        cmd_id="cmd:1006", kind="mcp", raw="MCP registry.list_servers",
        server="registry", tool="list_servers", args={},
        fields=(), headers={}, lease_id=None, call_index=11,
    )
    catalog_decision = gw.decide(catalog_trap_cmd)
    cat_call = catalog_decision.call.to_dict() if hasattr(catalog_decision.call, "to_dict") else catalog_decision.call
    print(f"  decide(registry.list_servers, fields=()) -> verdict={catalog_decision.verdict!r} fields={cat_call['fields']}")
    assert catalog_decision.verdict == "rewrite"
    assert tuple(cat_call["fields"]) == ("name",)

    print(f"\n=== Gateway.deny — the free-abstention path, exercised for real above ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids
    assert no_lease_cmd.cmd_id in gw._denied_cmd_ids
    assert headerless_write.cmd_id in gw._denied_cmd_ids
    assert reused_key_write.cmd_id in gw._denied_cmd_ids
    assert cross_learner_write.cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run.")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
