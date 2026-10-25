import json
import sqlite3
from unittest.mock import patch
from pathlib import Path

from capture_app.check.llm import parse_json, provider_from_env
from capture_app.check.llm import CallLimit
from capture_app.check.pipeline import digest_chunks, load_items, render_evidence, run
from capture_app.storage.vault import VaultWriter


def fixture_vault(tmp_path):
    VaultWriter(tmp_path).conn.close()
    db = sqlite3.connect(tmp_path / ".capture_index" / "index.sqlite")
    db.execute("""insert into apis(method,url_path,path_template,domain,full_url,capture_count,headers_json,
        payload_json,response_json,entry_points_json) values(?,?,?,?,?,?,?,?,?,?)""",
        ("POST", "/orders", "/orders", "shop.test", "https://shop.test/orders", 1,
         json.dumps({"Authorization": "Bearer secret", "Cookie": "c", "accept": "application/json"}),
         '{"password":"hunter2","name":"x"}', '{"orderId": 4, "status": "ok"}', '["Checkout"]'))
    db.execute("insert into params(name,resource_key,example_values_json,api_refs_json,definition) values(?,?,?,?,?)",
               ("password", "shop.test:orders", json.dumps({"request": ["hunter2"]}),
                json.dumps({"POST_orders": ["request"]}), "User password"))
    db.commit(); db.close()
    (tmp_path / "BRDs").mkdir()
    (tmp_path / "BRDs" / "Orders.md").write_text("# BR-12 Orders\n\nOrders must be submitted.", encoding="utf-8")
    return tmp_path


class FakeLLM:
    def __init__(self, replies, max_calls=0): self.replies=list(replies); self.calls=0; self.cache_hits=0; self.max_calls=max_calls
    def complete_json(self, *args):
        if self.max_calls and self.calls>=self.max_calls: raise CallLimit
        self.calls += 1; return self.replies.pop(0)


def test_render_evidence_masks_secrets_and_only_sends_schema():
    evidence=render_evidence({"method":"GET","path":"/x","page":"p","statuses":[200],
        "headers":{"Authorization":"secret","Cookie":"cookie","accept":"application/json"},
        "params":[{"name":"api_token","role":"request","type":"string","examples":["x"*120]},
                  {"name":"title","role":"request","type":"string","examples":["y"*120]},
                  {"name":"data","role":"request","type":"object","examples":[{"access_token":"nested-secret","label":"ok"}]}],
        "response_fields":[{"name":"secret","type":"string"}]})
    assert "***" in evidence and "x"*81 not in evidence
    assert "hunter2" not in evidence and "Authorization" not in evidence and "Cookie" not in evidence
    assert "Header accept" in evidence
    assert "y"*80 in evidence and "y"*81 not in evidence
    assert "nested-secret" not in evidence


def test_pipeline_finds_only_verified_quote(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM([
        [{"req_id":"BR-12","summary_en":"Submit orders","entities":{"fields":[],"endpoints":["/orders"],"actions":[],"screens":[]},"constraints":[]}],
        {"items":[{"item_id":"api:1","candidates":[{"req_id":"BR-12","confidence":"high","reason":"orders endpoint"},{"req_id":"FAKE","confidence":"high","reason":"bad id"}]}]},
        {"results":[{"req_id":"BR-12","verdict":"conflict","severity":"high","brd_quote":"made up quote","observed_quote":"Path: /orders","explanation":"bad quote"}]},
    ])
    result=run(vault, [], provider="fake", match_model="m", judge_model="j", no_cache=True, llm=fake)
    assert result["discarded"]==1
    assert len(result["unverified"])==1 and result["findings"]==[]
    assert "Unverified" in (vault / "Reports").glob("*.md").__iter__().__next__().read_text()


def test_chunk_ids_and_cache_round_trip(tmp_path):
    p=tmp_path/"x.md"; p.write_text("---\ntitle: X\n---\n# Req\n\nREQ-3.1 must be retained.",encoding="utf-8")
    assert digest_chunks([p])[0]["id"]=="REQ-3.1"
    vault=fixture_vault(tmp_path/"vault")
    fake=FakeLLM([[]])
    run(vault, [], provider="fake", match_model="m", judge_model="j", digest_only=True, llm=fake)
    # Digest cache is persisted for the same chunk and model.
    fake2=FakeLLM([])
    run(vault, [], provider="fake", match_model="m", judge_model="j", digest_only=True, llm=fake2)
    assert fake2.calls==0
    (vault/"BRDs"/"Orders.md").write_text("# BR-12 Orders\n\nOrders must now be cancelled.", encoding="utf-8")
    fake3=FakeLLM([[{"req_id":"BR-12","summary_en":"Changed","entities":{},"constraints":[]}]])
    run(vault, [], provider="fake", match_model="m", judge_model="j", digest_only=True, llm=fake3)
    assert fake3.calls==1


def test_dry_run_writes_prompts_without_calls(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM([])
    run(vault, [], provider="fake", match_model="m", judge_model="j", dry_run=True, no_cache=True, llm=fake)
    prompts=list((vault/".check_cache"/"dry_run").glob("*.json"))
    assert prompts and fake.calls==0
    assert any('"stage": "judge-template"' in p.read_text(encoding="utf-8") for p in prompts)


def test_quote_correct_finding_and_no_coverage(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM([
        [{"req_id":"BR-12","summary_en":"Submit orders","entities":{},"constraints":[]}],
        {"items":[{"item_id":"api:1","candidates":[{"req_id":"BR-12","confidence":"high","reason":"match"}]}]},
        {"results":[{"req_id":"BR-12","verdict":"conflict","severity":"medium","brd_quote":"Orders must be submitted.","observed_quote":"Path: /orders","explanation":"Directly checked."}]},
    ])
    result=run(vault,[],provider="fake",match_model="m",judge_model="j",no_cache=True,llm=fake)
    assert len(result["findings"])==1 and not result["unverified"]
    assert "Orders must be submitted." in next((vault/"Reports").glob("*.md")).read_text(encoding="utf-8")


def test_provider_selection_and_fenced_json():
    with patch("capture_app.check.llm.load_project_env", return_value=None), patch.dict("os.environ", {}, clear=True):
        try: provider_from_env()
        except ValueError as error: assert "ANTHROPIC_API_KEY" in str(error) and "OPENROUTER_API_KEY" in str(error)
        else: assert False
    with patch("capture_app.check.llm.load_project_env", return_value=None), patch.dict("os.environ", {"ANTHROPIC_API_KEY":"x","OPENROUTER_API_KEY":"y"}, clear=True):
        assert provider_from_env()=="claude"
        assert provider_from_env("openrouter")=="openrouter"
    assert parse_json("```json\n{\"ok\": true}\n```")=={"ok":True}
    assert parse_json('[{"req_id":"BR-1"}]')==[{"req_id":"BR-1"}]


def test_invalid_json_gets_one_repair_call(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM(["not json", []])
    result=run(vault,[],provider="fake",match_model="m",judge_model="j",digest_only=True,no_cache=True,llm=fake)
    assert fake.calls==2 and not result["requirements"]


def test_max_calls_stops_with_partial_report(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM([[{"req_id":"BR-12","summary_en":"Orders","entities":{},"constraints":[]}]],max_calls=1)
    result=run(vault,[],provider="fake",match_model="m",judge_model="j",max_calls=1,no_cache=True,llm=fake)
    assert result["stopped"] and result["calls"]==1
    assert "stopped at --max-calls: True" in next((vault/"Reports").glob("*.md")).read_text(encoding="utf-8")


def test_unmatched_is_never_a_conflict(tmp_path):
    vault=fixture_vault(tmp_path)
    fake=FakeLLM([
        [{"req_id":"BR-12","summary_en":"Submit orders","entities":{},"constraints":[]}],
        {"items":[{"item_id":"api:1","candidates":[]},{"item_id":"param:1:password","candidates":[]}]},
    ])
    result=run(vault,[],provider="fake",match_model="m",judge_model="j",no_cache=True,llm=fake)
    text=next((vault/"Reports").glob("*.md")).read_text(encoding="utf-8")
    assert not result["findings"] and "No BRD coverage" in text and "needs manual review" in text


def test_scope_and_fallback_requirement_ids(tmp_path):
    vault=fixture_vault(tmp_path)
    assert len(load_items(vault, scopes=["Checkout"]))==2
    assert load_items(vault, scopes=["not-a-page"])==[]
    p=vault/"BRDs"/"Fallback.md"
    p.write_text("A requirement without an ID.",encoding="utf-8")
    fallback=[c for c in digest_chunks([p]) if c["id"].startswith("Fallback#")]
    assert fallback and fallback[0]["id"]=="Fallback#1"

