"""Persistent memory store — file-backed, survives restarts."""

from __future__ import annotations

from agentmgr.memory import MemoryStore


def test_add_and_list(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    e = m.add("brokerage is Paradise Realty")
    assert e.id.startswith("mem")
    assert [x.text for x in m.all()] == ["brokerage is Paradise Realty"]


def test_persists_across_instances(tmp_path):
    path = tmp_path / "mem.json"
    MemoryStore(path).add("prefers morning showings")
    # a brand-new store reading the same file sees it — i.e. survives a restart
    assert [x.text for x in MemoryStore(path).all()] == ["prefers morning showings"]


def test_duplicate_text_not_added_twice(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    a = m.add("works in Stuart FL")
    b = m.add("Works In Stuart FL")     # case-insensitive duplicate
    assert a.id == b.id
    assert len(m.all()) == 1


def test_empty_text_rejected(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    try:
        m.add("   ")
    except ValueError:
        return
    raise AssertionError("expected ValueError on empty memory")


def test_search(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    m.add("daughter Jasmine is a photographer")
    m.add("son Oliver plays soccer")
    hits = [e.text for e in m.search("jasmine")]
    assert hits == ["daughter Jasmine is a photographer"]


def test_delete_and_clear(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    e = m.add("one")
    m.add("two")
    assert m.delete(e.id) is True
    assert m.delete("nope") is False
    assert m.clear() == 1
    assert m.all() == []


def test_delete_matching(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    m.add("prefers morning calls")
    m.add("brokerage is Paradise Realty")
    removed = m.delete_matching("morning")
    assert [e.text for e in removed] == ["prefers morning calls"]
    assert len(m.all()) == 1


def test_prompt_block_empty_then_filled(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    assert m.prompt_block() == ""
    m.add("brokerage is Paradise Realty")
    block = m.prompt_block()
    assert "Paradise Realty" in block
    assert block.startswith("Here is what you remember")
