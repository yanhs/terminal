"""Unit tests for the Telegram<->tmux bridge pure helpers (no network/tmux).

The reply comes from the session transcript (.jsonl); the visible pane is used
only for the 'working' boolean and menu detection. Tests focus on those paths.
One test deliberately exercises Cyrillic so non-ASCII handling stays covered."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_BRIDGE_TOKEN", "test:token")
os.environ.setdefault("TG_BRIDGE_OWNER", "1")
os.environ["TG_CONVO_LOG"] = "/tmp/test_tg_convo.log"  # don't pollute the real log

import tg_bridge as tb  # noqa: E402


# --- session_for ----------------------------------------------------------

def test_session_for_known():
    assert tb.session_for("1") == "claude-terminal"
    assert tb.session_for("2") == "claude-terminal-2"


def test_session_for_unknown():
    assert tb.session_for("99") is None
    assert tb.session_for("9") is None      # orchestra agents excluded


# --- send_text: literal text, PAUSE, then Enter (the submission fix) -------

def test_send_text_pauses_before_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))

    class FakeTime:
        def sleep(self, n): calls.append(("SLEEP", n))
    monkeypatch.setattr(tb, "time", FakeTime())

    tb.send_text("sess", "hello")
    # order: clear the input line (C-u) -> literal text -> sleep (>0) -> Enter
    assert calls[0] == ("send-keys", "-t", "sess", "C-u")
    assert calls[2] == ("send-keys", "-t", "sess", "-l", "hello")
    assert calls[3][0] == "SLEEP" and calls[3][1] > 0
    assert calls[4] == ("send-keys", "-t", "sess", "Enter")


def test_send_text_clears_input_before_typing(monkeypatch):
    # after an Esc-cancel the previous command can linger in the input box; the
    # new text must NOT stick to it — so we wipe the line (C-u) before typing
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))
    monkeypatch.setattr(tb, "time", type("T", (), {"sleep": lambda self, n: None})())
    tb.send_text("sess", "next message")
    keyseqs = [c for c in calls]
    assert keyseqs[0] == ("send-keys", "-t", "sess", "C-u")            # clear first
    assert keyseqs.index(("send-keys", "-t", "sess", "C-u")) < \
        keyseqs.index(("send-keys", "-t", "sess", "-l", "next message"))


def test_send_text_handles_cyrillic(monkeypatch):
    # the owner writes in Russian — literal Cyrillic must pass through unchanged
    calls = []
    monkeypatch.setattr(tb, "_tmux", lambda *a: calls.append(a))
    monkeypatch.setattr(tb, "time", type("T", (), {"sleep": lambda self, n: None})())
    tb.send_text("sess", "hello, world")
    assert ("send-keys", "-t", "sess", "-l", "hello, world") in calls


# --- is_working -----------------------------------------------------------

def test_is_working_true_while_generating():
    assert tb.is_working("  ⏵⏵ ... · esc to interrupt") is True


def test_is_working_false_when_idle():
    assert tb.is_working("❯\n  ⏵⏵ bypass permissions on (shift+tab to cycle)") is False


# --- clean_pane (used only by /read) --------------------------------------

def test_clean_pane_strips_ansi_and_chrome():
    pane = ("\x1b[1m● Hello\x1b[22m\n"
            "─────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            "Some answer line")
    out = tb.clean_pane(pane)
    assert "Hello" in out and "Some answer line" in out
    assert "bypass permissions" not in out and "─" not in out


def test_clean_pane_collapses_blank_runs():
    assert tb.clean_pane("a\n\n\n\nb") == "a\n\nb"


# --- chunk / cap_reply ----------------------------------------------------

def test_chunk_short_single():
    assert tb.chunk("hello") == ["hello"]


def test_chunk_empty():
    assert tb.chunk("") == []


def test_chunk_splits_long_on_line_boundaries():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = tb.chunk(text, limit=500)
    assert len(parts) > 1 and all(len(p) <= 500 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_chunk_breaks_a_single_overlong_line():
    parts = tb.chunk("x" * 1200, limit=500)
    assert all(len(p) <= 500 for p in parts) and "".join(parts) == "x" * 1200


def test_cap_reply_keeps_tail_when_too_long():
    out = tb.cap_reply("A" + "B" * 20000, max_chars=100)
    assert out.startswith("…\n") and len(out) <= 103 and out.endswith("B")


def test_cap_reply_short_untouched():
    assert tb.cap_reply("short") == "short"


# --- tool summaries / record rendering ------------------------------------

def test_summarize_tool_bash_first_line():
    assert tb._summarize_tool({"name": "Bash", "input": {"command": "ls -la\nrm x"}}) == "🔧 Bash: ls -la"


def test_summarize_tool_edit_file():
    assert tb._summarize_tool({"name": "Edit", "input": {"file_path": "/a/b.py"}}) == "🔧 Edit: /a/b.py"


def test_render_record_text_and_tool():
    msg = {"content": [{"type": "thinking", "thinking": "hmm"},
                       {"type": "text", "text": "done"},
                       {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}
    out = tb._render_record(msg)
    assert "done" in out and "🔧 Bash: echo hi" in out and "hmm" not in out


def test_render_record_skips_askuserquestion_tool():
    # the question is surfaced as buttons; it must not also appear as text in the reply
    msg = {"content": [{"type": "tool_use", "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "Which color?"}]}},
                       {"type": "text", "text": "after the pick"}]}
    out = tb._render_record(msg)
    assert out == "after the pick" and "AskUserQuestion" not in out


# --- assistant_records (transcript = source of truth) ---------------------

def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_assistant_records_skips_synthetic_and_sidechain(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "a1",
         "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "uuid": "a2",
         "message": {"model": "claude-opus-4-8", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "assistant", "uuid": "syn",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "limit reached"}]}},
        {"type": "assistant", "uuid": "side", "isSidechain": True,
         "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "subagent"}]}},
        {"type": "user", "message": {"content": "hi"}},
    ])
    recs = dict(tb.assistant_records(str(f)))
    assert recs.get("a1") == "hello"
    assert recs.get("a2") == "🔧 Bash: ls"
    assert "syn" not in recs and "side" not in recs


def test_assistant_records_missing_file(tmp_path):
    assert tb.assistant_records(str(tmp_path / "nope.jsonl")) == []


def test_render_new_only_after_baseline(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "old", "message": {"model": "m", "content": [{"type": "text", "text": "old one"}]}},
        {"type": "assistant", "uuid": "new1", "message": {"model": "m", "content": [{"type": "text", "text": "new A"}]}},
        {"type": "assistant", "uuid": "new2", "message": {"model": "m", "content": [{"type": "text", "text": "new B"}]}},
    ])
    out = tb.render_new(str(f), {"old"})
    assert out == "new A\nnew B" and "old one" not in out


def test_render_new_no_path():
    assert tb.render_new(None, set()) == ""


# --- AskUserQuestion from transcript + enrichment -------------------------

def test_last_askuserquestion_extracts_question_and_labels(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Which color?",
                                      "options": [{"label": "Red"}, {"label": "Blue"}]}]}}]}},
    ])
    aq = tb.last_askuserquestion(str(f), set())
    assert aq == {"question": "Which color?", "labels": ["Red", "Blue"]}


def test_enrich_menu_uses_transcript_labels(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Full question?",
                                      "options": [{"label": "Option one"}, {"label": "Option two"}]}]}}]}},
    ])
    menu = {"question": "trunc", "options": [(1, "Opt"), (2, "Op")], "current": 1}
    out = tb.enrich_menu(menu, str(f), set())
    assert out["question"] == "Full question?"
    assert out["options"] == [(1, "Option one"), (2, "Option two")]


# --- parse_menu (visible pane only) ---------------------------------------

MENU_PANE = """ ☐ Color
Which color do you like?
❯ 1. Red
     the red one
  2. Green
     the green one
  3. Blue
  4. Type something.
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""


def test_parse_menu_basic():
    m = tb.parse_menu(MENU_PANE)
    assert m is not None
    assert m["question"] == "Which color do you like?"
    assert m["options"][:3] == [(1, "Red"), (2, "Green"), (3, "Blue")]
    assert (4, "Type something.") in m["options"] and m["current"] == 1


def test_parse_menu_current_follows_pointer():
    pane = MENU_PANE.replace("❯ 1. Red", "  1. Red").replace("  3. Blue", "❯ 3. Blue")
    assert tb.parse_menu(pane)["current"] == 3


def test_parse_menu_unicode_options():
    # the agent may ask in any language — non-ASCII titles must parse fine
    pane = ("¿Qué color?\n❯ 1. Rojo\n  2. Verde\n  3. Type something.\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m["question"] == "¿Qué color?"
    assert m["options"][:2] == [(1, "Rojo"), (2, "Verde")]


def test_parse_menu_real_layout_with_divider():
    pane = (" ☐ Color\nRed, green or blue?\n"
            "❯ 1. Red\n     the red one\n  2. Green\n     the green one\n"
            "  3. Blue\n     the blue one\n  4. Type something.\n"
            "──────────────────────────────\n  5. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m is not None and [n for n, _ in m["options"]] == [1, 2, 3, 4, 5]
    assert m["question"] == "Red, green or blue?"


def test_parse_menu_captures_option_descriptions():
    pane = ("Pick an item:\n"
            "❯ 1. One\n     first item\n"
            "  2. Two\n     second item\n"
            "  3. Type something.\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    m = tb.parse_menu(pane)
    assert m["descs"].get(1) == "first item"
    assert m["descs"].get(2) == "second item"
    assert 3 not in m["descs"]                     # 'Type something.' has none
    txt = tb._menu_text(m)
    assert "1. One" in txt and "first item" in txt and "second item" in txt


def test_parse_menu_none_without_footer():
    assert tb.parse_menu("text\n1. a\n2. b") is None


def test_parse_menu_numbered_prose_is_not_a_menu():
    # a plain numbered-list reply must NOT be taken for a menu (the old false +)
    assert tb.parse_menu("Here's a list:\n1. one\n2. two\n3. three\n4. four") is None


def test_parse_menu_rejects_non_contiguous():
    pane = "question?\n1. a\n2. b\n4. d\nEnter to select · Esc to cancel"
    assert tb.parse_menu(pane) is None


def test_parse_menu_rejects_single_option():
    assert tb.parse_menu("question?\n1. only\nEnter to select · Esc to cancel") is None


# --- _menu_text -----------------------------------------------------------

def test_menu_text_marks_choice():
    menu = {"question": "Color?", "options": [(1, "Red"), (2, "Blue")], "current": 1}
    pre = tb._menu_text(menu)
    assert "Color?" in pre and "1. Red" in pre and "✅" not in pre
    post = tb._menu_text(menu, chosen=2)
    assert post.count("✅") == 1 and "✅ 2. Blue" in post


# --- built-in option filtering (Claude Code's 'Type something' / 'Chat about
#     this instead' rows are NOT real options — never surface them as buttons) ---

def _builtin_menu():
    # mirrors a real pane: 4 agent options, then Claude Code's two built-ins
    return {"question": "Color?",
            "options": [(1, "🔴 Red"), (2, "🟢 Green"), (3, "🔵 Blue"),
                        (4, "🟡 Yellow"), (5, "Type something."),
                        (6, "Chat about this instead")],
            "current": 1, "descs": {}}


def test_real_options_drops_builtins():
    real = tb._real_options(_builtin_menu())
    assert real == [(1, "🔴 Red"), (2, "🟢 Green"), (3, "🔵 Blue"), (4, "🟡 Yellow")]


def test_real_options_keeps_all_when_no_builtins():
    menu = {"options": [(1, "A"), (2, "B")]}
    assert tb._real_options(menu) == [(1, "A"), (2, "B")]


def test_real_options_never_empty():
    # defensive: a menu that is ONLY built-ins must not collapse to no buttons
    menu = {"options": [(1, "Type something."), (2, "Chat about this instead")]}
    assert tb._real_options(menu) == [(1, "Type something."), (2, "Chat about this instead")]


def _kbd_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_menu_keyboard_excludes_builtins():
    buttons = _kbd_buttons(tb._menu_keyboard("4", _builtin_menu()))
    msel = [b.callback_data for b in buttons if b.callback_data.startswith("msel:")]
    assert msel == ["msel:4:1", "msel:4:2", "msel:4:3", "msel:4:4"]
    joined = " ".join(b.text for b in buttons)
    assert "Type something" not in joined and "Chat about this" not in joined


# --- '💬 Поговорить' button: decline the question, return to free chat --------

def test_chat_option_finds_builtin():
    assert tb._chat_option(_builtin_menu()) == 6


def test_chat_option_none_when_absent():
    assert tb._chat_option({"options": [(1, "A"), (2, "B")]}) is None


def test_menu_keyboard_includes_chat_button():
    buttons = _kbd_buttons(tb._menu_keyboard("4", _builtin_menu()))
    chat = [b for b in buttons if b.callback_data == "mchat:4"]
    assert len(chat) == 1 and "💬" in chat[0].text
    assert buttons[-1].callback_data == "mchat:4"        # placed after the real options


def test_menu_keyboard_no_chat_button_when_no_builtin():
    menu = {"question": "Q", "options": [(1, "A"), (2, "B")], "current": 1, "descs": {}}
    buttons = _kbd_buttons(tb._menu_keyboard("4", menu))
    assert all(not b.callback_data.startswith("mchat:") for b in buttons)


# --- free-text answer: 'Type something' just declines (Claude Code 2.1.183), so
#     close the picker with Escape (robust) — NOT fragile arrow-nav -------------

def test_answer_freeform_declines_with_escape_not_arrows(monkeypatch):
    import asyncio
    keys, texts = [], []
    monkeypatch.setattr(tb, "send_key", lambda s, k: keys.append(k))
    monkeypatch.setattr(tb, "send_text", lambda s, t: texts.append(t))
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    menu = {"options": [(1, "Red"), (2, "Green"), (3, "Type something.")], "current": 1}
    ok = asyncio.run(tb._answer_freeform("sess", menu, "Purple"))
    assert ok is True
    assert keys == ["Escape"]                         # decline via Escape only
    assert "Down" not in keys and "Up" not in keys    # no fragile arrow navigation
    assert texts == ["Purple"]                        # then the text is typed + submitted


def test_answer_freeform_false_without_typesomething(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "send_key", lambda s, k: None)
    monkeypatch.setattr(tb, "send_text", lambda s, t: None)
    menu = {"options": [(1, "Red"), (2, "Blue")], "current": 1}   # no free-text row
    assert asyncio.run(tb._answer_freeform("sess", menu, "Purple")) is False


def test_menu_text_excludes_builtins_and_hints_freetext():
    txt = tb._menu_text(_builtin_menu())
    assert "Type something" not in txt and "Chat about this" not in txt
    assert "🔴 Red" in txt and "🟡 Yellow" in txt
    assert "✍️" in txt                                   # free-text affordance kept as a hint


def test_menu_text_no_freetext_hint_when_no_typesomething():
    menu = {"question": "Q?", "options": [(1, "A"), (2, "B")], "current": 1, "descs": {}}
    assert "✍️" not in tb._menu_text(menu)


def test_enrich_menu_relabels_real_when_pane_has_builtins(tmp_path):
    # pane parsed 5 options (3 real + 2 built-ins); transcript has the 3 real labels.
    # the real options get clean labels; the built-ins stay (so free-text still works).
    f = tmp_path / "s.jsonl"
    _write(f, [
        {"type": "assistant", "uuid": "q1",
         "message": {"model": "m", "content": [{"type": "tool_use", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Pick?",
                                      "options": [{"label": "Alpha"}, {"label": "Beta"},
                                                  {"label": "Gamma"}]}]}}]}},
    ])
    menu = {"question": "trunc", "current": 1, "descs": {},
            "options": [(1, "Alp"), (2, "Bet"), (3, "Gam"),
                        (4, "Type something."), (5, "Chat about this instead")]}
    out = tb.enrich_menu(menu, str(f), set())
    assert out["question"] == "Pick?"
    assert out["options"] == [(1, "Alpha"), (2, "Beta"), (3, "Gamma"),
                              (4, "Type something."), (5, "Chat about this instead")]


# --- safety: network errors swallowed; per-terminal lock ------------------

def test_safe_edit_swallows_telegram_error():
    import asyncio
    from telegram.error import TimedOut

    class M:
        async def edit_text(self, *a, **k):
            raise TimedOut("boom")
    asyncio.run(tb._safe_edit(M(), "hi"))   # must NOT raise


def test_safe_send_swallows_and_returns_none():
    import asyncio
    from telegram.error import NetworkError

    class Bot:
        async def send_message(self, *a, **k):
            raise NetworkError("boom")
    assert asyncio.run(tb._safe_send(Bot(), 1, "hi")) is None


# --- flood control: the FINAL reply must survive RetryAfter (waits + retries) --

def test_safe_edit_retries_on_flood_then_succeeds(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class M:
        async def edit_text(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryAfter(5)                  # flood once, then succeed
    ok = asyncio.run(tb._safe_edit(M(), "hi", retries=2))
    assert ok is True and calls["n"] == 2 and slept   # waited, then delivered


def test_safe_edit_gives_up_after_retries(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)

    class M:
        async def edit_text(self, *a, **k):
            raise RetryAfter(3)                      # always flooded
    assert asyncio.run(tb._safe_edit(M(), "hi", retries=2)) is False


def test_safe_send_retries_on_flood(monkeypatch):
    import asyncio
    from telegram.error import RetryAfter
    async def fake_sleep(s): pass
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class Bot:
        async def send_message(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryAfter(2)
            return "msg"
    assert asyncio.run(tb._safe_send(Bot(), 1, "hi", retries=1)) == "msg" and calls["n"] == 2


def test_live_edit_interval_grows_with_elapsed():
    # spinner edits must space out as a reply runs long, so we don't rack up
    # enough edits to a single message to trip Telegram flood control
    early, mid, late = (tb._live_edit_interval(5), tb._live_edit_interval(90),
                        tb._live_edit_interval(300))
    assert early >= 3 and early < mid < late


def test_lock_for_is_per_session():
    a, b, c = tb.lock_for("s1"), tb.lock_for("s1"), tb.lock_for("s2")
    assert a is b and a is not c


# --- _screen_mirror -------------------------------------------------------

def test_screen_mirror_answer_only_drops_question_and_chrome():
    pane = ("● old answer from before\n"
            "❯ count the files in the dir\n"
            "✶ Architecting… (9s · ↓ 96 tokens)\n"
            "● Bash(ls | wc -l)\n"
            "  ⎿  42\n"
            "✻ Crunched for 19s\n"
            "Files: 42\n"
            "How is Claude doing this session?\n"
            "(optional)\n"
            "1: Bad  2: Fine  3: Good  0: Dismiss\n"
            "❯ \n"
            "  ⏵⏵ bypass permissions on · esc to interrupt")
    out = tb._screen_mirror(pane, "count the files in the dir")
    assert "count the files" not in out            # QUESTION dropped (anchor only cuts scrollback)
    assert "Architecting… (9s" in out              # spinner kept
    assert "Crunched for 19s" in out               # timing kept (user wants it)
    assert "Bash(ls | wc -l)" in out and "42" in out
    assert "Files: 42" in out                      # answer kept
    assert "old answer" not in out                 # nothing before the question (anchor)
    assert "How is Claude" not in out and "Dismiss" not in out and "(optional)" not in out
    assert "bypass permissions" not in out         # bottom status bar dropped


def test_screen_mirror_keeps_blank_lines():
    pane = ("❯ my question\n"
            "First paragraph.\n"
            "\n"
            "Second paragraph.\n"
            "\n"
            "\n"
            "Third paragraph.\n"
            "✻ Cooked for 3s")
    out = tb._screen_mirror(pane, "my question")
    assert "First paragraph.\n\nSecond paragraph." in out   # blank line preserved
    assert "\n\n\n" not in out                              # runs collapsed to one
    assert "Third paragraph." in out and "Cooked for 3s" in out


# --- transcript_path ------------------------------------------------------

def test_transcript_path_resolves_real_agent():
    p = tb.transcript_path("2")
    assert p is not None and p.endswith(".jsonl") and "/projects/" in p


# --- _incoming_text: typed text + replied-to / forwarded content ----------

class _Msg:
    """Minimal stand-in for a telegram Message."""
    def __init__(self, text=None, caption=None, reply_to_message=None):
        self.text = text
        self.caption = caption
        self.reply_to_message = reply_to_message


def test_incoming_text_plain():
    assert tb._incoming_text(_Msg(text="hello there")) == "hello there"


def test_incoming_text_uses_caption_when_no_text():
    # a forwarded photo/doc carries its words in .caption, not .text
    assert tb._incoming_text(_Msg(caption="look at this")) == "look at this"


def test_incoming_text_prepends_replied_to_message():
    # replying to a message must deliver THAT message's content to the terminal too
    quoted = _Msg(text="the original task description")
    out = tb._incoming_text(_Msg(text="do this", reply_to_message=quoted))
    assert "the original task description" in out and "do this" in out
    assert out.index("the original task description") < out.index("do this")


def test_incoming_text_reply_with_only_quote():
    # forwarding/replying with no new words → just the quoted content
    quoted = _Msg(text="forwarded content")
    assert tb._incoming_text(_Msg(text="", reply_to_message=quoted)) == "forwarded content"


def test_incoming_text_reply_to_captioned_message():
    quoted = _Msg(caption="caption of the quoted media")
    out = tb._incoming_text(_Msg(text="see above", reply_to_message=quoted))
    assert "caption of the quoted media" in out and "see above" in out


def test_incoming_text_empty_returns_blank():
    assert tb._incoming_text(_Msg()) == ""


# --- on_text: lock held ONLY around the send, released before streaming ----

def test_on_text_releases_lock_before_streaming(monkeypatch):
    """A message sent while Claude works must reach the terminal immediately, so
    on_text must drop the per-session lock once the keystrokes are sent and only
    THEN stream — otherwise the next message blocks until the stream finishes."""
    import asyncio

    session = "claude-terminal-6"
    seen = {}

    monkeypatch.setattr(tb, "get_current", lambda c: "6")
    monkeypatch.setattr(tb, "SESSIONS", {**tb.SESSIONS, "6": session})
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    monkeypatch.setattr(tb, "visible", lambda s: "")
    monkeypatch.setattr(tb, "parse_menu", lambda v: None)
    monkeypatch.setattr(tb, "baseline_uuids", lambda p: set())
    monkeypatch.setattr(tb, "transcript_path", lambda a: "/tmp/x.jsonl")

    def fake_send(s, t):
        seen["locked_during_send"] = tb.lock_for(session).locked()
    monkeypatch.setattr(tb, "send_text", fake_send)

    async def fake_stream(message, s, path, baseline, aid, user_text):
        seen["locked_during_stream"] = tb.lock_for(session).locked()
    monkeypatch.setattr(tb, "stream_live", fake_stream)

    class Placeholder:
        async def edit_text(self, *a, **k): pass

    class Message:
        text = "steer the agent now"
        caption = None
        reply_to_message = None
        async def reply_text(self, *a, **k): return Placeholder()

    class Upd:
        message = Message()
        effective_chat = type("C", (), {"id": 999})()
        effective_user = type("U", (), {"id": 1})()

    asyncio.run(tb.on_text(Upd(), None))
    assert seen["locked_during_send"] is True      # held while keys are sent
    assert seen["locked_during_stream"] is False   # released before streaming


# --- file storage: name sanitization + public reimake.com URL --------------

def test_stored_name_with_readable_original():
    assert tb._stored_name("Quarterly Report.pdf", ".pdf", "abcd1234") == "abcd1234_Quarterly_Report.pdf"


def test_stored_name_non_ascii_falls_back_to_token():
    # an all-non-ASCII stem sanitizes to empty → token + ext only (ext preserved)
    assert tb._stored_name("报告.pdf", ".pdf", "deadbeef") == "deadbeef.pdf"


def test_stored_name_no_original_uses_token_and_ext():
    assert tb._stored_name("", ".jpg", "0011aabb") == "0011aabb.jpg"


def test_stored_name_sanitizes_unsafe_chars():
    n = tb._stored_name("../../etc/pa ss;rm -rf.txt", ".txt", "tok")
    assert "/" not in n and " " not in n and ";" not in n
    assert n.startswith("tok_") and n.endswith(".txt")


def test_public_url_is_reimake_never_yanhs():
    u = tb._public_url("tok_file.pdf")
    assert u == "https://reimake.com/tgfiles/tok_file.pdf"
    assert "yanhs.stream" not in u           # the deprecated host must never appear


# --- media detection -------------------------------------------------------

class _Media:
    def __init__(self, **kw):
        self.file_name = kw.get("file_name")
        self.mime_type = kw.get("mime_type")
        self.file_size = kw.get("file_size")
        self.file_unique_id = kw.get("file_unique_id", "uniq")


class _FileMsg:
    def __init__(self, document=None, photo=None, video=None, audio=None,
                 voice=None, caption=None, chat_id=999):
        self.document = document; self.photo = photo; self.video = video
        self.audio = audio; self.voice = voice; self.caption = caption
        self.chat_id = chat_id


def test_media_info_document_uses_filename_ext():
    d = _Media(file_name="data.csv", mime_type="text/csv", file_size=10)
    media, kind, original, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and original == "data.csv" and ext == ".csv" and media is d


def test_media_info_document_ext_from_mime_when_no_name():
    d = _Media(file_name=None, mime_type="application/pdf")
    _, kind, _orig, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and ext == ".pdf"


def test_media_info_document_falls_back_to_bin():
    # no name-extension AND an unrecognized mime → ".bin", not an extensionless file
    d = _Media(file_name="report", mime_type="application/x-unknown-xyz")
    _, kind, _orig, ext = tb._media_info(_FileMsg(document=d))
    assert kind == "document" and ext == ".bin"


def test_media_info_photo_is_jpg_largest():
    p = [_Media(file_size=1), _Media(file_size=99)]      # Telegram lists sizes ascending
    media, kind, _orig, ext = tb._media_info(_FileMsg(photo=p))
    assert kind == "photo" and ext == ".jpg" and media is p[-1]


def test_media_info_none_for_textonly():
    assert tb._media_info(_FileMsg()) is None


# --- voice transcription (subprocess to the venv python) -------------------

def test_transcribe_voice_invokes_venv_python_and_parses(monkeypatch):
    captured = {}

    class R:
        returncode = 0; stdout = "  hello world  \n"; stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd; captured["kw"] = kw; return R()
    monkeypatch.setattr(tb.subprocess, "run", fake_run)

    out = tb.transcribe_voice("/tmp/x.oga")
    assert out == "hello world"                      # stdout stripped
    assert captured["cmd"][0] == tb.WHISPER_PY        # the venv python that has faster_whisper
    assert captured["cmd"][1] == tb.WHISPER_SCRIPT
    assert captured["cmd"][2] == "/tmp/x.oga"
    assert captured["kw"].get("capture_output") and captured["kw"].get("text")


def test_transcribe_voice_raises_on_failure(monkeypatch):
    import pytest

    class R:
        returncode = 1; stdout = ""; stderr = "boom: model missing"
    monkeypatch.setattr(tb.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        tb.transcribe_voice("/tmp/x.oga")
    assert "boom" in str(e.value)


# --- on_file: save + reimake link; caption → also deliver to terminal ------

def test_on_file_saves_and_replies_with_link(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(tb, "TGFILES_DIR", str(tmp_path))
    monkeypatch.setattr(tb, "TGFILES_URL", "https://reimake.com/tgfiles")
    monkeypatch.setattr(tb._uuid, "uuid4", lambda: type("U", (), {"hex": "abcd1234ef"})())
    monkeypatch.setattr(tb, "get_current", lambda c: None)   # no terminal → just the link
    saved = {}

    class TGFile:
        async def download_to_drive(self, dest):
            saved["dest"] = dest
            open(dest, "w").write("x")

    class Doc:
        file_name = "report.txt"; mime_type = "text/plain"; file_size = 10
        async def get_file(self): return TGFile()

    replies = []

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = None; self.chat_id = 999
        async def reply_text(self, t, **k): replies.append(t); return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert saved["dest"].endswith("abcd1234_report.txt")     # uuid4().hex[:8] = "abcd1234"
    assert any("https://reimake.com/tgfiles/abcd1234_report.txt" in r for r in replies)
    assert all("yanhs.stream" not in r for r in replies)


def test_on_file_with_caption_delivers_path_to_terminal(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(tb, "TGFILES_DIR", str(tmp_path))
    monkeypatch.setattr(tb, "TGFILES_URL", "https://reimake.com/tgfiles")
    monkeypatch.setattr(tb._uuid, "uuid4", lambda: type("U", (), {"hex": "ffff0000aa"})())
    monkeypatch.setattr(tb, "get_current", lambda c: "6")
    monkeypatch.setattr(tb, "has_session", lambda s: True)
    monkeypatch.setattr(tb, "SESSIONS", {**tb.SESSIONS, "6": "claude-terminal-6"})
    delivered = {}

    async def fake_deliver(reply_to, chat_id, text):
        delivered["text"] = text
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Doc:
        file_name = "log.txt"; mime_type = "text/plain"; file_size = 10
        async def get_file(self): return TGFile()

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = "check this log"; self.chat_id = 999
        async def reply_text(self, t, **k): return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert "check this log" in delivered["text"]             # caption forwarded
    assert "ffff0000_log.txt" in delivered["text"]            # local path included for the agent
    assert "https://reimake.com/tgfiles/ffff0000_log.txt" in delivered["text"]


def test_on_file_rejects_oversize(monkeypatch):
    import asyncio
    replies = []

    class Doc:
        file_name = "big.zip"; mime_type = "application/zip"; file_size = 25 * 1024 * 1024
        async def get_file(self): raise AssertionError("must not download an oversize file")

    class Msg:
        def __init__(self):
            self.document = Doc(); self.photo = None; self.video = None
            self.audio = None; self.voice = None; self.caption = None; self.chat_id = 999
        async def reply_text(self, t, **k): replies.append(t); return None

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_file(Upd(), None))
    assert replies and "20" in replies[0]                     # told it's over the 20 MB cap


# --- on_voice: transcribe → show user + deliver transcript to terminal -----

def test_on_voice_transcribes_and_delivers(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "transcribe_voice", lambda p: "open the readme file")
    delivered = {}; edits = []

    async def fake_deliver(reply_to, chat_id, text):
        delivered["text"] = text
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Voice:
        file_size = 1000; file_unique_id = "uq"
        async def get_file(self): return TGFile()

    class Note:
        async def edit_text(self, t, **k): edits.append(t)

    class Msg:
        def __init__(self): self.voice = Voice(); self.chat_id = 999
        async def reply_text(self, t, **k): return Note()

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_voice(Upd(), None))
    assert delivered["text"] == "open the readme file"        # transcript sent to the terminal
    assert any("open the readme file" in e for e in edits)    # user shown what was understood


def test_on_voice_handles_empty_transcript(monkeypatch):
    import asyncio
    monkeypatch.setattr(tb, "transcribe_voice", lambda p: "")
    called = {"deliver": False}

    async def fake_deliver(*a, **k): called["deliver"] = True
    monkeypatch.setattr(tb, "_deliver_to_terminal", fake_deliver)

    class TGFile:
        async def download_to_drive(self, dest): open(dest, "w").write("x")

    class Voice:
        file_size = 1000; file_unique_id = "uq"
        async def get_file(self): return TGFile()

    edits = []

    class Note:
        async def edit_text(self, t, **k): edits.append(t)

    class Msg:
        def __init__(self): self.voice = Voice(); self.chat_id = 999
        async def reply_text(self, t, **k): return Note()

    class Upd:
        def __init__(self): self.message = Msg(); self.effective_chat = type("C", (), {"id": 999})()

    asyncio.run(tb.on_voice(Upd(), None))
    assert called["deliver"] is False                         # nothing sent to the terminal
    assert edits                                              # user told it was empty
