#!/usr/bin/env python3
"""ffbe_pipeline.py — the ORDER: the `PIPELINE` table, its declared edges, and the loop that runs them.

This is the module the whole refactor was for. Before it, the reply chain was 30 positional calls in an
HTTP handler whose order was load-bearing and recorded only in comments; now the order is data, the seven
(then eight) real write->read dependencies are declared as `Edge` records, and `validate_pipeline()` runs
**fatal at import** so a registry that contradicts itself is loud before the first request.

It is the ONLY module that knows all three transform modules, which is what makes the layering acyclic:
config -> state -> {battle, gacha, player} -> pipeline -> capture_server.

Extracted 2026-09-26 from `capture_server.py` (docs/SERVER_REFACTOR_PLAN.md, Phase 3, last seam).
"""
from __future__ import annotations


from ffbe_config import *          # noqa: F401,F403 -- constants; the table has no behaviour
import ffbe_state as st           # the REBOUND runtime state; see ffbe_state's docstring
from ffbe_state import State, _note, save_state
# `save_state` is needed by a LAMBDA inside the module-level `PIPELINE` tuple, not by any function body,
# which is why a free-name sweep that only walks `def`/`class` blocks reports this module as complete
# and the module still fails at call time. The sweep was fixed; the note is here so it is not re-broken.

# The only module that knows all three transform modules. That is what makes the layering
# acyclic: config -> state -> {battle, gacha, player} -> pipeline -> capture_server.
from ffbe_battle import (apply_battle_bgm, apply_result_drops, apply_result_item_columns,
                          credit_mission_lapis, credit_mission_rewards, grant_cleared_mission,
                          mission_end_echo, record_pushed_switches, roll_drops,
                          serve_challenge_clear, serve_challenge_clear_all, serve_version_table)
from ffbe_banner import MARQUEE_GROUP, serve_home_marquee
from ffbe_friends import serve_friends
from ffbe_gacha import serve_gacha, serve_gacha_info
from ffbe_player import (apply_carry_edit, apply_emblem_setting, apply_party_edit, apply_progress, claim_daily_quest,
                          claim_panel_quest,
                          claim_sp_challenge,
                          credit_emblem_objectives,
                          deliver_anniversary_ticket,
                          deliver_catchup_rewards,
                          deliver_login_mail,
                          echo_client_group,
                          serve_daily_quest,
                          serve_beast_board, serve_beast_mix,
                          serve_diamonds, serve_emblem_info, serve_emblem_event, serve_panel_quest,
                          serve_event_gate, serve_favourite_info,
                          serve_home_badges, serve_login_bonus, serve_mail,
                          serve_sp_challenge,
                          serve_survival,
                          serve_switches, serve_unit_favourite, serve_unit_mix, serve_unit_sell,
                          serve_unit_storage)


__all__ = [
    "Edge",
    "PIPELINE",
    "PIPELINE_EDGES",
    "Step",
    "_Ctx",
    "reply_pipeline",
    "validate_pipeline",
]

class _Ctx:
    """Everything a pipeline step is allowed to see besides the spec it is handed.

    The steps' signatures were never uniform -- some take the spec, some take the request, one takes the
    pre-battle snapshot, one is pure I/O -- and normalising them onto `(spec, ctx)` is what lets the order
    live in a table instead of a call list. `prior` is a FIELD rather than a local because it is D3: it is
    the pre-battle cleared set, and it has to be read before the step that spends it, which is exactly the
    kind of thing that used to be an inline assignment with a comment.

    `spec` is here so a step that does not touch the spec (`record_pushed_switches`, `apply_party_edit`,
    `save_state`) can return it unchanged, and every step has the same shape: take a ctx, return a spec.
    """

    __slots__ = ("spec", "reply_id", "plaintext", "mid", "spec_key", "is_battle_bundle", "prior")

    def __init__(self, spec, reply_id, plaintext, mid, spec_key):
        self.spec = spec
        self.reply_id = reply_id
        self.plaintext = plaintext
        self.mid = mid
        self.spec_key = spec_key
        # `is_battle_bundle` is the `<reply_id>@<mission>` selection, i.e. THIS reply is that mission's own
        # content -- the only thing that may claim the mission's slot in LAST_DROPS. Keying the roll by
        # mission is what stops a mission-end from mirroring a different battle's drops.
        self.is_battle_bundle = mid is not None and spec_key == f"{reply_id}@{mid}"
        self.prior: set = set()
def _snapshot_cleared(ctx: _Ctx):
    """D3, as a named step: the PRE-battle cleared set, snapshotted before anything spends it.

    `serve_challenge_clear` reports the ids credited *by this request* (now minus prior), because the
    objectives page needs the state the player had when the battle started. It was an inline assignment
    wedged between two calls; as a step it is in the table, and the edge that requires it to precede
    `credit_mission_lapis` is checkable rather than commented.
    """
    ctx.prior = set(State.player.cleared_challenges.get(str(ctx.mid), set())) if ctx.mid else set()
    return ctx.spec
class Step:
    """One transform in the reply chain: what it is, what it writes, and how to call it.

    `call` is what makes the chain a TABLE rather than a call list: it takes the context and returns the
    spec, so `reply_pipeline` is a loop with no knowledge of any individual transform. Every step has that
    shape, including the ones that do not touch the spec and the ones whose real signature is nothing like
    it (`credit_mission_rewards(plaintext, spec)` has its arguments the other way round).

    `writes` is the channel the step's output travels on, and it is the distinction that matters: a step
    that writes `spec` is visible to everything after it in the chain, while a step that writes `state` is
    visible only because a LATER step re-projects `State.player` into its reply. Four of the seven real
    dependencies are the second kind, which is why "run the transforms in any order" is wrong and a
    dispatcher that iterated an unordered collection would be wrong too.
    """

    __slots__ = ("key", "call", "writes", "channel", "note")

    def __init__(self, key, call, writes, channel="", note=""):
        self.key = key
        self.call = call
        self.writes = writes          # "spec" | "state" | "global" | "io"
        self.channel = channel        # the named thing written, e.g. "LAST_DROPS", "State.player"
        self.note = note

    def __repr__(self) -> str:
        return f"Step({self.key!r}, writes={self.writes}{':' + self.channel if self.channel else ''})"
class Edge:
    """A declared write->read dependency: `before` writes something `after` reads.

    This is the knowledge that used to exist only as prose in the chain's comments ("runs LAST", "Snapshot
    the paid set FIRST"). Declared as data it can be CHECKED, which is the whole point: the chain's order
    is load-bearing, and a reordering that breaks it produces a different served reply and nothing
    complains until a driven run does.
    """

    __slots__ = ("before", "after", "channel", "why")

    def __init__(self, before, after, channel, why):
        self.before = before
        self.after = after
        self.channel = channel
        self.why = why

    def __repr__(self) -> str:
        return f"Edge({self.before!r} -> {self.after!r} via {self.channel})"
PIPELINE: tuple = (
    # Drops are server-authoritative and per-battle: roll the candidate list now. (A no-op for every reply
    # that carries no drop rows.) See roll_drops for why this cannot live in the payload.
    Step("roll_drops", lambda c: roll_drops(c.spec, mid=c.mid, is_battle_bundle=c.is_battle_bundle),
         "global", "LAST_DROPS", "rolls each mission's candidate drop list; claims the mission's own slot"),
    # The result screen reads its OWN channels, not the wave drop list, so mirror this battle's roll into
    # the drops channel of 09HRWXDf. (No-op for any reply without that tag.)
    Step("apply_result_drops", lambda c: apply_result_drops(c.spec, mid=c.mid),
         "spec", "09HRWXDf", "mirrors THIS mission's roll into the result screen's drops channel"),
    # The summon's award is SERVER-AUTHORITATIVE: grant it into the roster (once) and serve the effect
    # pattern that matches the granted rarity. (No-op for any reply without `5yLD4dYI`.)
    Step("serve_gacha", lambda c: serve_gacha(c.spec, c.plaintext),
         "state", "State.player", "the summon's award is server-authoritative: grants into the roster"),
    # The authored gacha BANNERS are reply-delivered, not CDN MST (gacha_reconstruction.md SS1), so they
    # ride the reply of the request that opens the gacha screen (GachaEntryRequest / GachaInfoRequest).
    Step("serve_gacha_info", lambda c: serve_gacha_info(c.spec, c.reply_id),
         "spec", "gacha banner groups", "the banners ride the gacha screen's opener"),
    # ...then let the client's OWN reported numbers win: the MissionEnd request carries the unit EXP, gil
    # and drops it computed, and the reply must carry them back (the ctor clears the client's list).
    Step("mission_end_echo", lambda c: mission_end_echo(c.spec, c.plaintext),
         "spec", "xF9Sr1a6", "the client's OWN reported exp/gil/drops win and are carried back"),
    # ...and make the same reported gil/drops REAL in the state (they were display-only before). It
    # returns the spec: it writes Qic38SuC/GfQmi4h5 into the result rows, so it must copy before writing.
    Step("credit_mission_rewards", lambda c: credit_mission_rewards(c.plaintext, c.spec),
         "spec", "Qic38SuC/GfQmi4h5", "makes the reported gil/loot/rank real, and writes the result rows"),
    # The EMBLEM EVENT's weekly objectives ride the SAME mission end: a cleared mission that a campaign
    # objective names earns its crest, and a crest can cross a tier, so the tier mail is composed here
    # rather than waiting for the screen (the mailbox is state; the reply only carries the counters).
    Step("credit_emblem_objectives", lambda c: credit_emblem_objectives(c.plaintext, c.spec),
         "state", "State.player.emblem_event / mailbox",
         "credits the live campaign's mission-shaped objectives and mails any tier they reach"),
    # A cleared mission's rewards become real here: credit only the challenges this mission has NOT been
    # paid for (first-clear only -- a repeat clear must not pay again).
    Step("snapshot_cleared", _snapshot_cleared, "io", "ctx.prior",
         "D3: the PRE-battle cleared set, which the objectives page is computed against"),
    Step("credit_mission_lapis", lambda c: credit_mission_lapis(c.spec, c.mid, c.plaintext),
         "state", "State.player.cleared_challenges", "first-clear-only challenge payment"),
    # The ending cutscene's gate reads the switch container, so the completed mission must grant its OWN
    # flag here -- a static container never could, which is why the shrine's map event never fired.
    Step("grant_cleared_mission", lambda c: grant_cleared_mission(c.spec, c.mid),
         "state", "State.player.switches", "the completed mission grants its own open switch"),
    # The client grants switches to itself and REPORTS them here; the reply container replaces the whole
    # type, so dropping this does not just fail to grant -- it erases on the next sync.
    Step("record_pushed_switches", lambda c: (record_pushed_switches(c.plaintext), c.spec)[1],
         "state", "State.player.switches", "the client grants switches to itself and reports them here"),
    # The DECK the player just edited, before the state is projected into this reply -- otherwise the
    # reply (and the next battle) serves the deck they did not choose. See apply_party_edit.
    Step("apply_party_edit", lambda c: (apply_party_edit(c.plaintext), c.spec)[1],
         "state", "State.player.party", "the deck just edited, before it is projected into this reply"),
    # Unit management (SW-106): favourite / storage / sell / mix. Each reads its own request group,
    # mutates the ONE player state, and (for favourite) builds the reply the client's only applier needs.
    Step("serve_unit_favourite", lambda c: serve_unit_favourite(c.spec, c.plaintext),
         "spec", "R8U3uD1i", "the reply is the client's ONLY applier, so it echoes the request's lists"),
    Step("serve_unit_storage", lambda c: serve_unit_storage(c.spec, c.plaintext),
         "state", "State.player.roster"),
    Step("serve_unit_sell", lambda c: serve_unit_sell(c.spec, c.plaintext),
         "state", "State.player.roster"),
    Step("serve_unit_mix", lambda c: serve_unit_mix(c.spec, c.plaintext),
         "state", "State.player.roster"),
    # The BEAST (esper) progression: the board's two piece lists and the magicite mix. Neither request has
    # a factory response class, so the reply is an ack ({} when there was no payload) and the STATE is the
    # whole job -- the next user-state refresh's `gP9TW2Bf`/`1S8P2u9f` must show what the client already
    # drew. Rules read in libgame.so; one home: docs/notes/beast_esper_surface.md §8.
    Step("serve_beast_board", lambda c: serve_beast_board(c.spec, c.plaintext),
         "state", "State.player.beasts[].board / .cp",
         "learning SPENDS CP (`BeastUtil::learningPiece`), the lock pieces spend item 107037768"),
    Step("serve_beast_mix", lambda c: serve_beast_mix(c.spec, c.plaintext),
         "state", "State.player.beasts[].level/.exp/.cp + the kind-20 magicite",
         "giveBeastBait + the beastLvUp loop, item order preserved because the level-up runs between taps"),
    # The client's own reinforcement/friend state, echoed rather than hardcoded (SW-36).
    Step("echo_client_group", lambda c: echo_client_group(c.spec, c.plaintext, "I53AVzSo"),
         "spec", "I53AVzSo", "the client's own reinforcement state, echoed"),
    Step("serve_switches", lambda c: serve_switches(c.spec),
         "spec", "8J1R5PXG", "the switch container, projected from state"),
    Step("serve_home_badges", lambda c: serve_home_badges(c.spec), "spec", "merqbE18"),
    # The HOME marquee is 100% server-authoritative -- `getBannerMstList()` builds its list from the
    # REPLY's own records, never a table lookup -- so the banners ride whatever reply carries the group,
    # and the same step turns `P_GS_FEATURE_HOME_MARQUEE` on where the boot packet carries it. Both halves
    # or neither: rows with the flag off, and the flag with no rows, each render nothing. The SELECTION
    # is ours (`BANNER_CAMPAIGN`), like the gacha campaign. docs/notes/home_screen_payloads.md S11.
    Step("serve_home_marquee", lambda c: serve_home_marquee(c.spec, c.reply_id),
         "spec", MARQUEE_GROUP + " + P_GS_FEATURE_HOME_MARQUEE",
         "the banner rows + the marquee enable flag, from the era table"),
    # The calendar's day rows and its banner come FIRST: deliver_login_mail then reads the row whose
    # `state` marks the player's current day, so the inbox and the calendar tell one story (SW-86).
    # The 8th-anniversary ticket arrives by POST, as it did in the original game: one ordinary mail per
    # calendar day, nothing granted until it is received (so a full bag is harmless). It rides the Initialize
    # reply -- boot step 3, every boot -- so the ticket is in the inbox before HOME. Deliberately NOT the
    # CreateUser reply, whose result gate is INVERTED. docs/notes/home_screen_payloads.md S11.14.
    Step("deliver_anniversary_ticket", lambda c: deliver_anniversary_ticket(c.spec, c.reply_id),
         "state", "State.player.mailbox", "one 8th-anniversary ticket per day, by mail"),
    Step("serve_login_bonus", lambda c: serve_login_bonus(c.spec, c.reply_id),
         "spec", "f5bA4wFx", "the calendar's day rows, marked with the player's reached day"),
    Step("deliver_login_mail", lambda c: deliver_login_mail(c.spec, c.reply_id),
         "state", "State.player.mailbox", "reads the row serve_login_bonus marked"),
    # The CATCH-UP schedules (the shipped cumulative ladder + the season's chosen special-login campaign +
    # the authored welcome grant). It advances its own counters and pays what fell due, so it must run
    # AFTER serve_login_bonus (same request, same day) and its progress groups must be built from the state
    # it just wrote. Home: docs/notes/catchup_bonus_family.md.
    Step("deliver_catchup_rewards", lambda c: deliver_catchup_rewards(c.spec, c.reply_id),
         "state", "State.player.catchup", "advances the catch-up counters and pays what became due"),
    # PANEL QUEST (SW-121): the screen's only request, answered with the schedule + status + progress groups.
    # Read-only -- it writes no state, so it is declared `spec` and needs no edge; it must sit before
    # `apply_to_reply` so the player projection cannot overwrite it.
    Step("serve_panel_quest", lambda c: serve_panel_quest(c.spec, c.reply_id),
         "spec", "nnR59m0N/UvwpPAtp/ELPwhZj0", "the PANEL QUEST screen's schedule, status and progress rows"),
    # DAILY QUEST (2026-09-26): today's rows (Hqdgx83H) + the screen's own state (sxe5C48P), on every carrier
    # in DAILY_QUEST_CARRIERS -- all five of which land in `SpChallengeListScene::checkConnectResult`
    # 0x2544128. Writes state ONLY for the day roll, and must sit BEFORE apply_progress, which is what
    # writes DQ_CURRENT; a projection that ran after would be the last writer and the counts would never
    # move. It must also sit before the player projection for the same reason serve_panel_quest does.
    Step("serve_daily_quest", lambda c: serve_daily_quest(c.spec, c.reply_id),
         "state", "Hqdgx83H/sxe5C48P", "today's daily quest rows and the screen's current-quest state"),
    # SP CHALLENGE (2026-09-27): the Rookie / ADVANCED / Expert quest schedules. The schedule MASTER is not
    # shipped, so the three groups are built here at serve time from the shipped `F_SP_CHALLENGE_MST` and the
    # live progress store -- one writer for every quest column, replacing the rows `harness/derive_sp_challenge.py`
    # used to bake into responses.json (a second copy of the same selection, which is the thing rule 6 forbids).
    # It must sit BEFORE apply_progress, which is what writes SP_PROGRESS/SP_CLEAR on the rows it builds, and
    # before apply_to_reply for the same reason serve_panel_quest does.
    Step("serve_sp_challenge", lambda c: serve_sp_challenge(c.spec, c.reply_id),
         "spec", "NI12Hvxy/WS1g0ECn/3KLU4mSD", "the quest schedules, their status and the player's rows"),
    # SURVIVAL (2026-09-29): the 2020 trial gauntlet's event row. Its tag `NsC8t4Qr` is the ONLY writer of
    # `SurvivalEventInfoList`, and `SurvivalUtil::isPlayingSurvivalMission(areaId)` is a lookup in that list,
    # so this step is what makes the mode's tiles exist at all. DEFAULT OFF (`SURVIVAL=1` enables); it writes
    # the spec only. Its gate row is APPENDED to the `0xkoNP8U` rows the carrier already serves -- the list
    # is replace-on-read, so a partial list is a destructive list.
    Step("serve_survival", lambda c: serve_survival(c.spec, c.reply_id),
         "spec", "NsC8t4Qr/65cMLkuQ/0xkoNP8U", "the Survival event row + its gate tile (authored)"),
    # The quest PROGRESS store (2026-09-26): score the action this request reports, then project every
    # quest's progress into this reply. It must run AFTER the builders that create the rows (so panel quest
    # and sp challenge exist) and BEFORE apply_to_reply (which projects the player groups). `plaintext` is
    # REQUIRED for the parameter-aware codes: `mission_clear` scores `15:<missionId>` against the id in the
    # body, and without the body it can only score the wildcard rows.
    Step("apply_progress", lambda c: apply_progress(c.spec, c.reply_id, c.plaintext),
         "state", "State.player.progress", "scores the reported action and projects oz5Vt6AR/Ag8bisM0"),
    # The CLAIM (tapping a panel). It pays the quest's own shipped reward and flips QCLs6SV8, which is the
    # state that draws the crossed swords -- distinct from being cleared, and refused unless cleared.
    Step("claim_panel_quest", lambda c: claim_panel_quest(c.spec, c.reply_id, c.plaintext),
         "state", "State.player.progress", "pays a CLEARED quest's shipped reward and marks it received"),
    # ...and the DAILY QUEST's claim, for the same reason and with the same three rules (verify, then pay;
    # a claim is neither a clear nor progress; report what was CREDITED). The claim itself is recorded, not
    # projected: `serve_daily_quest` has already built the rows this reply carries, and the claim only flips
    # the recorded state, so the NEXT carrier shows the claimed row.
    Step("claim_daily_quest", lambda c: claim_daily_quest(c.spec, c.reply_id, c.plaintext),
         "state", "State.player.progress", "pays a COMPLETE daily quest and records it as received"),
    # ...and the SP CHALLENGE's claim, the same three rules (verify, then pay; a claim is neither a clear nor
    # progress; report what was CREDITED). It re-sends the claimed rows with SP_REWARD_FLAG = 1, because
    # `SpChallengeListScene::checkConnectResult` 0x2544128 only clears its in-flight flag and plays
    # `playClearEffect` -- the list itself is redrawn from the rows the reply carries.
    Step("claim_sp_challenge", lambda c: claim_sp_challenge(c.spec, c.reply_id, c.plaintext),
         "state", "State.player.progress", "pays a CLEARED SP-challenge quest and marks it received"),
    Step("serve_mail", lambda c: serve_mail(c.spec, c.reply_id, c.plaintext), "spec", "mailbox groups"),
    Step("serve_challenge_clear", lambda c: serve_challenge_clear(c.spec, c.mid, c.prior),
         "spec", "qdhWF7z9", "this battle's award, computed against the PRE-battle state"),
    # ...and the whole cumulative set on the user-state replies, which is the only way a client that
    # restarted can relearn it (the mission-end reply above is the only other carrier; see the fn).
    Step("serve_challenge_clear_all", lambda c: serve_challenge_clear_all(c.spec, c.reply_id),
         "spec", "qdhWF7z9", "the cumulative set, so a restarted client can relearn it"),
    Step("serve_favourite_info", lambda c: serve_favourite_info(c.spec, c.reply_id), "spec", "F2pYJGt5"),
    Step("serve_diamonds", lambda c: serve_diamonds(c.spec), "spec", "J3pAG0I5",
         "NOTE: apply_to_reply overwrites this group with the same value (dead weight, see the audit)"),
    # A cleared client has no persisted version table, so `c1qYg84Q` is the only source of the tables it
    # will fetch -- four rows there is `MST_LOAD_FAILED` before character creation (2026-09-20).
    Step("serve_version_table", lambda c: serve_version_table(c.spec), "spec", "c1qYg84Q",
         "the only source of the table versions a cleared client will fetch"),
    # The EMBLEM family. `serve_emblem_info` answers the select screen's entry request (an EMPTY body there
    # is a connection error, measured live 2026-09-30); `apply_emblem_setting` records the *Set* press.
    # Both sit immediately before the projection: the setting writes `player_emblem_id`, and the reply it
    # builds carries `3oU9Ktb7` so the projection is what actually fills it in.
    Step("serve_emblem_info", lambda c: serve_emblem_info(c.spec, c.reply_id), "spec", EMBLEM_GROUP),
    Step("apply_emblem_setting", lambda c: apply_emblem_setting(c.plaintext, c.spec),
         "spec", f"State.player.profile / {EMBLEM_GROUP}"),
    # The EVENT GATE (APP-35): a story-event area's tile, appended to the carriers that already serve the
    # gate list. Same mechanism as the survival family's gate row, and it must run where the list already
    # exists -- see the Step's own note and EVENT_GATE_CARRIERS.
    Step("serve_event_gate", lambda c: serve_event_gate(c.spec, c.reply_id),
         "spec", EVENT_GATE_TAG,
         "one gate row per EVENT_GATE_AREAS area: area/dungeon/mission derived, window/tab/art authored"),
    # The EMBLEM EVENT screen (the crest campaign). ONE request, THREE class-typed groups -- the client's
    # response factory dispatches by tag -- plus one state write: opening the screen delivers the crest-0
    # opening tier. Gated on the request id because the groups are the campaign's own whole answer.
    Step("serve_emblem_event", lambda c: serve_emblem_event(c.spec, c.reply_id),
         "spec", f"{EMBLEM_MONTHLY_GROUP}/{EMBLEM_WEEKLY_GROUP}/{EMBLEM_MILESTONE_GROUP}",
         "the live campaign: the monthly row, the per-milestone progress, the milestone windows"),
    # The ITEM SET (the battle carry list). The client's own serialisation arrives in the SAME group the reply
    # uses, and the reply must echo it COMPLETE: `UserCarryItemInfoResponse::onReadStarted` clears the client's
    # `UserCarryItemInfoList` before the parse rebuilds it, so a partial echo deletes the rest. Deliberately not
    # a `LhVz6aD2` user-state reply -- see the function's docstring and `apply_to_reply`'s `UnitEquipRequest`
    # lesson about unexpected clear-and-rebuild groups.
    Step("apply_carry_edit", lambda c: apply_carry_edit(c.plaintext, c.spec, c.reply_id),
         "state", "State.player.inventory.carry_items / nE7pV2wr",
         "records the client's item set and echoes it complete (the reply clears before it rebuilds); GATED "
         "on the carry-edit requestID because the group is clear-and-rebuild on the client"),
    # ORB RECHARGE (APP-34, 2026-10-11): the Arena's "recover orbs" confirm sends `ShopUseRequest` with
    # shopId 21 (libgame 0x208b8f4 constructs it and calls setShopId with 0x15, the client's own analytics
    # call naming the action "Recover Colosseum Orb"); until now the reply was EMPTY, which the client
    # shows as the connection error. The step spends the row's shipped lapis price and re-anchors the
    # family at its cap. It must sit BEFORE `apply_to_reply` (which projects the new orb point into
    # `3oU9Ktb7`) and before `save_state`; gated on the request id like `apply_carry_edit`.
    Step("apply_orb_recharge", lambda c: State.player.apply_shop_use(c.spec, c.reply_id, c.plaintext)
         if State.player is not None else c.spec,
         "state", "State.player.orbs / State.player.currencies",
         "ShopUseRequest shopId 21 -> fill the family to cap and spend its shipped useDia (100 lapis)"),
    # THE FRIEND ROSTER (APP-13): the hand-curated `friends.yaml` fills the two friend replies -- the
    # support/companion list (ReinforcementResponse, the NON-clearing tag) and the friends area
    # (FriendUnitInfoResponse). The file is re-read on every request, so an edit takes effect on the
    # next request with no restart; a missing file leaves both replies exactly as they were (empty),
    # which is what makes this step unable to regress the empty case.
    Step("serve_friends", lambda c: serve_friends(c.spec, c.reply_id),
         "spec", "9Tp2fiyB / I53AVzSo / 56gJBnVh",
         "friends.yaml -> the support list + the friends area (hot: re-read per request)"),
    # The player groups (roster, equipment, decks, rank/NRG, lapis) are a PROJECTION of the one player
    # state: replace whatever the payload carried. A reply with no user-state group is left untouched.
    Step("apply_to_reply", lambda c: State.player.apply_to_reply(c.spec, reply_id=c.reply_id),
         "spec", "12 player groups", "the PROJECTION: rebuilt from the one player state"),
    # Persist what this request changed. One small file under out/; see the STATE_FILE block for why it
    # stays a single, wipeable file. The player's OWN dirty flag counts too.
    Step("save_state", lambda c: (save_state(), c.spec)[1], "io", st.STATE_FILE,
         "position is load-bearing: the strips below rewrite the reply AFTER this, so persisting earlier "
         "would write a different spec than the one served"),
    # The result-item A/B strips LAST, after every transform: mission_end_echo and credit_mission_rewards
    # both rewrite xF9Sr1a6, so an earlier strip would be undone.
    Step("apply_result_item_columns", lambda c: apply_result_item_columns(c.spec),
         "spec", "09HRWXDf", "A/B strip; LAST because the two steps above rewrite xF9Sr1a6"),
    # ...and the battle-BGM A/B beside it, for the same reason: nothing else rewrites the phase rows, but
    # keeping the strips together makes the "this runs last" rule one place.
    Step("apply_battle_bgm", lambda c: apply_battle_bgm(c.spec),
         "spec", "an6TB4WS", "A/B strip; last, beside the other one, so the rule is in one place"),
)
PIPELINE_EDGES: tuple = (
    Edge("roll_drops", "apply_result_drops", "LAST_DROPS",
         "the roll is written at mission LOAD and read at mission END -- two different requests, so it has "
         "to survive in between"),
    Edge("serve_login_bonus", "deliver_login_mail", "spec f5bA4wFx",
         "the mail delivered reads the day row the calendar marked as reached"),
    Edge("serve_login_bonus", "deliver_catchup_rewards", "State.player.login_bonus vs State.player.catchup",
         "BOTH advance once per calendar day off the same request, so they must share the day or the "
         "account's daily calendar and its catch-up ladders disagree about what day it is"),
    Edge("deliver_catchup_rewards", "apply_to_reply", "State.player.catchup",
         "the progress groups are a projection of the counters this step advanced"),
    Edge("snapshot_cleared", "credit_mission_lapis", "ctx.prior",
         "the snapshot must be taken before the step that spends it, or 'this battle's award' is computed "
         "against the state the award already changed (D3)"),
    Edge("credit_mission_lapis", "serve_challenge_clear", "ctx.prior",
         "and the reader needs both: the pre-battle set AND the ids this request credited"),
    Edge("grant_cleared_mission", "serve_switches", "State.player.switches",
         "a mission's own open switch is granted here and the container is rebuilt from state after"),
    Edge("credit_mission_rewards", "serve_diamonds", "State.player",
         "the credited balance is what the diamond group reports"),
    Edge("credit_mission_rewards", "apply_result_item_columns", "spec xF9Sr1a6",
         "MEASURED: strip after the writers -> the field is gone; strip before -> the writers put it back, "
         "so an earlier strip is silently undone. This is the dependency the chain's comments were really "
         "about"),
    Edge("apply_progress", "claim_panel_quest", "State.player.progress",
         "the claim reads the progress the previous step wrote -- a claim of an uncleared quest is REFUSED, "
         "so the two must not be reordered"),
    Edge("serve_daily_quest", "apply_progress", "Hqdgx83H / State.player.daily_quest",
         "the rows are BUILT here and their progress columns are WRITTEN by the next step, so a projection "
         "that ran first would be overwritten and a builder that ran second would reset the counts"),
    Edge("serve_sp_challenge", "apply_progress", "State.player.progress",
         "the scoring step must run before the CLAIM, not before this builder: the builder reads the store to "
         "write the rows' own progress columns, and `claim_sp_challenge` refuses a quest that is not cleared, "
         "so it must read the counters AFTER the scoring. (`apply_progress` no longer projects the SP rows at "
         "all -- that arm was a second, byte-identical writer and was removed 2026-09-27.)"),
    Edge("apply_progress", "claim_sp_challenge", "State.player.progress",
         "the SP-challenge claim refuses an uncleared quest, so it must read the counters AFTER the scoring "
         "step -- and the claim's own reply rows are written by the claim, not the projection"),
    Edge("apply_progress", "claim_daily_quest", "State.player.progress",
         "the daily claim refuses an incomplete quest, so it must read the counters AFTER the scoring step"),
    Edge("apply_progress", "apply_to_reply", "State.player.progress",
         "the progress step WRITES the store and PROJECTs the reply, so it must precede the player-group "
         "projection -- otherwise the projection is the last writer and the quest rows keep their old values"),
    Edge("serve_survival", "serve_event_gate", "spec / 0xkoNP8U",
         "BOTH append to the same replace-on-read gate list, so the second must see the first's rows -- "
         "either order works only if each builds on what the other left"),
    Edge("credit_emblem_objectives", "apply_to_reply", "State.player / mailbox",
         "the crest count and the tier mail are state written on a MissionEnd; the same reply's projections "
         "(and the mailbox screen's own request) read them after, so a later reorder would serve a reply "
         "that disagrees with the state it just wrote"),
    Edge("credit_mission_rewards", "apply_to_reply", "State.player",
         "the player groups are a projection, so they must be rebuilt AFTER the state writers have run"),
    Edge("apply_emblem_setting", "apply_to_reply", "State.player.profile.player_emblem_id",
         "the setting WRITES the displayed emblem and the projection is what carries it to the header "
         "(`n69quiCJ`); project first and the header shows the PREVIOUS crest"),
    Edge("apply_emblem_setting", "save_state", "st.STATE_FILE",
         "the chosen emblem has to survive the restart that ends the run, or it silently reverts"),
    Edge("apply_carry_edit", "save_state", "State.player.inventory.carry_items",
         "the item set is written by the same request that sets it, so the state writer must persist it"),
    Edge("apply_orb_recharge", "apply_to_reply", "State.player.orbs",
         "the team row's orb tokens are a projection of the point this step just wrote, so the write must "
         "precede the projection"),
    Edge("apply_orb_recharge", "save_state", "State.player.orbs",
         "the filled bar and the spent lapis have to survive the restart that ends the run"),
    Edge("serve_beast_board", "save_state", "State.player.beasts",
         "the learned bits and the spent CP have to survive the restart that ends the run, or the board "
         "silently reverts to what we served before the tap"),
    Edge("serve_beast_mix", "save_state", "State.player.beasts",
         "the exp/level and the consumed magicite are the same state the next refresh projects"),
)
def validate_pipeline() -> list:
    """Check the registry against itself. Returns the problems; empty is good.

    Two things are checked, and each closes a different way the data could lie:

    1. **The edges are satisfiable by the declared order.** An edge whose `before` runs after its `after` is
       a violated dependency, whether or not the current data happens not to trip it.
    2. **Every edge endpoint is a real step.** A typo in a key would otherwise make the edge permanently
       unsatisfied and permanently invisible.

    Also structural: unique keys, callable steps, and a known channel kind for each.

    **What is no longer checked here, and why.** Until 2026-09-26 this also compared the registry against
    `reply_pipeline`'s own source, so a transform added to the body without being registered would fail the
    import. That check is GONE because the body is now a loop over the registry: there is no second copy of
    the order left to drift from. The thing it was protecting -- the order being what the code actually does
    -- is now true by construction, and `harness/server_regression.py` is what protects the order's
    BEHAVIOUR, by diffing 78 served replies against a committed golden.

    Called once at import, so a bad registry is loud immediately. `harness/server_regression.py` calls it
    too and reports the counts, so the gate has something to assert about the order.
    """
    problems: list = []
    order = {s.key: i for i, s in enumerate(PIPELINE)}
    if len(order) != len(PIPELINE):
        problems.append(f"duplicate step keys in PIPELINE ({len(PIPELINE)} steps, {len(order)} unique)")

    for edge in PIPELINE_EDGES:
        for end in (edge.before, edge.after):
            if end not in order:
                problems.append(f"{edge!r}: endpoint {end!r} is not a registered step")
        if edge.before in order and edge.after in order and order[edge.before] >= order[edge.after]:
            problems.append(
                f"{edge!r}: VIOLATED -- {edge.before!r} is at {order[edge.before]} and "
                f"{edge.after!r} at {order[edge.after]}, so the reader runs BEFORE the writer")

    for step in PIPELINE:
        if not callable(step.call):
            problems.append(f"Step({step.key!r}): call is not callable")
        if step.writes not in ("spec", "state", "global", "io"):
            problems.append(f"Step({step.key!r}): unknown channel kind {step.writes!r}")
    return problems
def reply_pipeline(spec, reply_id: str, plaintext: str | None, mid: str | None,
                   spec_key: str | None):
    """The whole reply transform chain, in its one load-bearing order, and nothing else.

    This is a LOOP over `PIPELINE`. It was originally 29 hand-written calls inside `Handler._handle`, then a
    transcription of them in the regression harness, then a function holding the 29 calls. Each step moved
    closer to being DATA, and this is the end state: the order exists in exactly one place, the table above,
    and this function knows nothing about any individual transform.

    `docs/SERVER_REFACTOR_PLAN.md` Phase 2, and the shape is deliberate: dispatch (choosing the payload,
    including the `<reply_id>@<mission>` lookup), the two hot-reload checks, the evidence record, encryption
    and the envelope all stay in `_handle` — they are the request's I/O, not the reply's construction.

    THE ORDER IS NOT FREE, and the table is what now says so. Eight real write->read dependencies run
    through here, four of them through `State.player` rather than the payload; see `PIPELINE_EDGES`, which
    `validate_pipeline()` enforces at import. The audit that established them is
    `docs/notes/server_code_review_2nd_opinion.md` §C1.
    """
    ctx = _Ctx(spec, reply_id, plaintext, mid, spec_key)
    for step in PIPELINE:
        ctx.spec = step.call(ctx)
    return ctx.spec
_PIPELINE_PROBLEMS = validate_pipeline()
