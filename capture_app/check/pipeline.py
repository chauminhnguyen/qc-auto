from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from capture_app.chat.ingest import _parse_frontmatter, _sections
from capture_app.storage.vault import NOISE_PATH_HINTS, canonical_param_name, slugify, template_url_path
from . import prompts
from .llm import CallLimit, LLMClient, parse_json

SENSITIVE = re.compile(r"pass|pwd|token|secret|auth|key|otp|cookie|session|card", re.I)
TOKEN_LIKE = re.compile(r"(?i)^(?:bearer\s+)?(?:eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_.-]+|[A-F0-9]{40,}|[A-Za-z0-9_+/=-]{48,})$")
REQ_ID = re.compile(r"\b(?:BR|REQ|FR)-\d+(?:\.\d+)*\b", re.I)
NUMBERED_HEADING = re.compile(r"^\s*#{1,3}\s+(\d+(?:\.\d+)*)(?:[.)])?\s+", re.M)
MAX_DIGEST_TOKENS = 12000


def _type(value):
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, (int, float)): return "number"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    return "string"


def render_evidence(item, max_fields=None):
    """Render the only explored-side text allowed into prompts."""
    cap = max_fields or int(os.environ.get("CHECK_MAX_FIELDS_PER_API", "100"))
    def scrub(v):
        if isinstance(v, dict):
            return {str(k): ("***" if SENSITIVE.search(str(k)) else scrub(value)) for k,value in v.items()}
        if isinstance(v, list): return [scrub(x) for x in v]
        if isinstance(v, str) and TOKEN_LIKE.fullmatch(v) and len(set(v)) >= 8: return "***"
        return v
    def safe(v):
        cleaned=scrub(v)
        value=json.dumps(cleaned,ensure_ascii=False,separators=(",",":")) if isinstance(cleaned,(dict,list)) else str(cleaned)
        return value[:80]
    lines = [f"Method: {item.get('method','')}", f"Path: {item.get('path','')}",
             f"Page / entry point: {item.get('page','')}",
             f"Status codes: {', '.join(map(str, item.get('statuses', [])))}"]
    params = sorted(item.get("params", []), key=lambda p: (p.get("role", ""), p.get("name", "")))[:cap]
    lines.append("Params:")
    for p in params:
        examples = p.get("examples", [])
        if p.get("role") == "request":
            ex = "***" if SENSITIVE.search(p.get("name", "")) else (safe(examples[0]) if examples else "(none)")
            lines.append(f"- request {p.get('name','')}: {p.get('type','unknown')}; example={ex}; definition={p.get('definition','')}")
        else:
            lines.append(f"- response {p.get('name','')}: {p.get('type','unknown')}; definition={p.get('definition','')}")
    fields = item.get("response_fields", [])[:max(0, cap-len(params))]
    lines.append("Response schema fields (no values):")
    lines.extend(f"- {f['name']}: {f['type']}" for f in fields)
    for h in ("content-type", "accept"):
        if h in item.get("headers", {}): lines.append(f"Header {h}: {safe(item['headers'][h])}")
    return "\n".join(lines)


def _db(vault):
    path = Path(vault) / ".capture_index" / "index.sqlite"
    if not path.is_file(): raise ValueError(f"Capture database not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _json(s, default):
    try: return json.loads(s or "")
    except (ValueError, TypeError): return default


def load_items(vault, scopes=()):
    conn = _db(vault)
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        apis = conn.execute("select * from apis").fetchall()
        if not apis: return []
        totals = Counter()
        for row in apis: totals[row["domain"]] += row["capture_count"] or 1
        primary = totals.most_common(1)[0][0]
        statuses, pages = defaultdict(set), defaultdict(set)
        if "session_api_events" in tables:
            cols = {r[1] for r in conn.execute("pragma table_info(session_api_events)")}
            select = [x for x in ("ref_stem", "status", "entry_point") if x in cols]
            if "ref_stem" in select:
                for r in conn.execute("select " + ",".join(select) + " from session_api_events"):
                    if "status" in select and r["status"] is not None: statuses[r["ref_stem"]].add(r["status"])
                    if "entry_point" in select and r["entry_point"]: pages[r["ref_stem"]].add(r["entry_point"])
        params = conn.execute("select name,resource_key,example_values_json,api_refs_json,definition from params").fetchall()
        output = []
        for api in apis:
            path = api["path_template"] or template_url_path(api["url_path"])
            if api["domain"] != primary or any(h in path.casefold() for h in NOISE_PATH_HINTS): continue
            stem = f"{api['method']}_{slugify(api['url_path'])}"
            entry_points = _json(api["entry_points_json"], []) + list(pages[stem])
            page = str(entry_points[0]) if entry_points else "/".join(path.split("/")[:3])
            if scopes and not any(s.casefold() in page.casefold() or s.casefold() in path.casefold() for s in scopes): continue
            item_id = f"api:{api['id']}"
            api_note = Path(vault) / "APIs" / slugify(api["domain"]) / f"{api['method']}_{slugify(path)}.md"
            row = {"id": item_id, "api_id": api["id"], "method": api["method"], "path": path, "page": page,
                   "statuses": sorted(statuses[stem]), "params": [], "response_fields": [], "headers": _json(api["headers_json"], {}),
                   "domain": api["domain"], "resource_key": "", "vault_root": str(Path(vault).resolve()), "api_note": str(api_note), "param_notes": []}
            for p in params:
                refs = _json(p["api_refs_json"], {})
                roles = refs.get(stem, [])
                if isinstance(roles, str): roles = [roles]
                if not roles: continue
                ex = _json(p["example_values_json"], {})
                pnote = Path(vault) / "Params" / f"{slugify(p['resource_key'])}__{slugify(canonical_param_name(p['name']))}.md"
                if pnote not in [Path(x) for x in row["param_notes"]]: row["param_notes"].append(str(pnote))
                for role in roles:
                    vals = ex.get(role, []) if isinstance(ex, dict) else []
                    sample = vals[0] if vals else None
                    row["params"].append({"name": p["name"], "role": role, "type": _type(sample), "examples": vals,
                                          "definition": p["definition"] or ""})
            # Derive a response schema from the stored sample, retaining only field names and types.
            body = _json(api["response_json"], None)
            def schema(value, prefix=""):
                result = []
                if isinstance(value, dict):
                    for k, v in value.items(): result.append({"name": f"{prefix}{k}", "type": _type(v)})
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    result.extend(schema(value[0]))
                return result
            row["response_fields"] = schema(body)[:int(os.environ.get("CHECK_MAX_FIELDS_PER_API", "100"))]
            row["evidence"] = render_evidence(row)
            output.append(row)
            for param in row["params"]:
                param_item = dict(row)
                param_item["id"] = f"param:{api['id']}:{param['name']}"
                param_item["param_name"] = param["name"]
                param_item["params"] = [param]
                param_item["param_notes"] = [p for p in row["param_notes"] if canonical_param_name(param["name"]) in Path(p).stem.casefold()]
                param_item["evidence"] = render_evidence(param_item)
                output.append(param_item)
        return output
    finally: conn.close()


def find_brd_paths(vault, requested=()):
    root = Path(vault).resolve()
    if requested:
        paths = []
        for raw in requested:
            p = Path(raw); p = (root / p).resolve() if not p.is_absolute() else p.resolve()
            if not p.is_relative_to(root): raise ValueError(f"BRD path must be inside the vault: {raw}")
            if p.is_dir(): paths.extend(p.rglob("*.md"))
            elif p.is_file() and p.suffix.lower() == ".md": paths.append(p)
            else: raise ValueError(f"BRD path not found: {raw}")
        return sorted(p for p in set(paths) if ".check_cache" not in {x.casefold() for x in p.parts})
    candidates = [p for p in root.iterdir() if p.is_dir() and p.name.casefold() in {"brd", "brds", "requirements"}]
    if len(candidates) != 1:
        listed = ", ".join(p.name for p in candidates) or "(none)"
        raise ValueError(f"Expected exactly one top-level BRD folder; candidates: {listed}. Pass --brd.")
    return sorted(p for p in candidates[0].rglob("*.md") if ".check_cache" not in {x.casefold() for x in p.parts})


def digest_chunks(paths):
    chunks = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        _, body = _parse_frontmatter(text)
        for ordinal, (heading, chunk) in enumerate(_sections(body, path.stem), 1):
            source = f"{heading}\n\n{chunk}" if heading else chunk
            found = REQ_ID.findall(source)
            numbered = NUMBERED_HEADING.search(source)
            reqid = found[0] if found else numbered.group(1) if numbered else f"{path.stem}#{ordinal}"
            chunks.append({"path": path, "rel": path.as_posix(), "heading": heading, "text": source, "id": reqid, "ord": ordinal})
    return chunks


class Cache:
    def __init__(self, vault, enabled=True):
        self.conn = None
        if enabled:
            p = Path(vault) / ".check_cache" / "cache.sqlite"
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(p, check_same_thread=False)
            self.conn.execute("create table if not exists cache (key text primary key, value text not null)")
            self.conn.commit()
        self.lock = threading.Lock()
    def key(self, stage, model, text): return hashlib.sha256((prompts.PROMPT_VERSION[stage]+model+text).encode()).hexdigest()
    def get(self, key):
        if not self.conn: return None
        with self.lock: row = self.conn.execute("select value from cache where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self, key, value):
        if self.conn:
            with self.lock:
                self.conn.execute("insert or replace into cache values(?,?)", (key, json.dumps(value, ensure_ascii=False))); self.conn.commit()


def run(vault, brd_paths, *, provider, match_model, judge_model, min_confidence="medium", scopes=(), workers=2,
        max_calls=0, dry_run=False, no_cache=False, out=None, digest_only=False, llm=None):
    root = Path(vault).resolve(); paths = find_brd_paths(root, brd_paths); chunks = digest_chunks(paths)
    cache = Cache(root, not no_cache)
    llm = llm or LLMClient(provider, max_calls=max_calls, dry_run=dry_run)
    counts = Counter(); calls_by_stage=Counter(); failed_stages=Counter(); requirements = []; errors = []; errored_items=set(); stopped = False; prompts_written = []
    def invoke(stage, model, system, user, key_text, schema_check):
        nonlocal stopped
        key = cache.key(stage, model, key_text)
        value = cache.get(key)
        if value is not None: llm.cache_hits += 1; return value
        if dry_run:
            folder = root / ".check_cache" / "dry_run"; folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{stage}-{hashlib.sha256((system+user).encode()).hexdigest()[:12]}.json"
            target.write_text(json.dumps({"stage": stage, "model": model, "system": system, "user": user}, ensure_ascii=False, indent=2), encoding="utf-8")
            prompts_written.append((stage, len(system)+len(user))); return None
        try:
            for repair in range(2):
                before=llm.calls
                try:
                    raw = llm.complete_json(system, user if not repair else user + "\nValidation error: " + error, model, 3000)
                finally:
                    calls_by_stage[stage] += llm.calls-before
                try:
                    val = parse_json(raw.get("_text", raw.get("_value", raw)) if isinstance(raw, dict) else raw)
                    schema_check(val); cache.put(key, val); return val
                except Exception as e:
                    error = str(e)
                    if repair: raise
            return None
        except CallLimit:
            stopped = True; return None
        except Exception as e:
            # Provider exceptions can contain response excerpts; keep report errors free of prompt data.
            errors.append(f"{stage}: {type(e).__name__}"); failed_stages[stage] += 1; return None
    def list_check(v):
        if not isinstance(v, list): raise ValueError("expected JSON list")
        for row in v:
            if not isinstance(row, dict) or not isinstance(row.get("req_id"), str) or not isinstance(row.get("summary_en"), str):
                raise ValueError("each requirement needs req_id and summary_en strings")
            if not isinstance(row.get("entities"), dict) or not isinstance(row.get("constraints"), list):
                raise ValueError("each requirement needs entities object and constraints list")
    for i, chunk in enumerate(chunks, 1):
        print(f"[digest] {i}/{len(chunks)} {chunk['rel']}")
        user = f"<brd>\n{chunk['text']}\n</brd>\nID hint: {chunk['id']}"
        reqs = invoke("digest", match_model, prompts.DIGEST_SYSTEM, user, chunk["text"], list_check)
        if reqs:
            for req in reqs:
                if not isinstance(req, dict) or not req.get("req_id"): continue
                req["source_text"] = chunk["text"]; req["path"] = chunk["rel"]; req["note_stem"] = chunk["path"].stem
                requirements.append(req)
        if stopped: break
    if digest_only:
        print(json.dumps([{k:v for k,v in r.items() if k not in {"source_text"}} for r in requirements], ensure_ascii=False, indent=2))
        return {"requirements": requirements, "calls": llm.calls, "cache_hits": llm.cache_hits}
    items = load_items(root, scopes=scopes)
    groups = defaultdict(list)
    for item in items: groups[item["page"]].append(item)
    candidates = defaultdict(dict); discarded = 0
    req_digest = [{k:r.get(k) for k in ("req_id","summary_en","entities")} for r in requirements]
    batches=[]; current=[]
    for req in req_digest:
        if current and (sum(len(json.dumps(x,ensure_ascii=False)) for x in current+[req])//4)>MAX_DIGEST_TOKENS:
            batches.append(current); current=[]
        current.append(req)
    if current or not batches: batches.append(current)
    def match_group(group, members):
        local={}; local_discarded=0
        print(f"[match] {group}")
        if len(batches)>5: print(f"[check] warning: {len(batches)} digest batches; use --scope to narrow the run")
        for batch in batches:
            if stopped: break
            ev = [{"item_id":x["id"],"evidence":"<observed>\n"+x["evidence"]+"\n</observed>"} for x in members]
            safe_batch=[{**req,"summary_en":"<brd>"+str(req.get("summary_en", ""))+"</brd>"} for req in batch]
            user=json.dumps({"items":ev,"requirements":safe_batch},ensure_ascii=False)
            def match_check(v):
                if not isinstance(v,dict) or not isinstance(v.get("items"),list): raise ValueError("expected items list")
                for row in v["items"]:
                    if not isinstance(row,dict) or not isinstance(row.get("item_id"),str) or not isinstance(row.get("candidates"),list): raise ValueError("invalid match item")
                    for c in row["candidates"]:
                        if not isinstance(c,dict) or c.get("confidence") not in {"low","medium","high"} or not isinstance(c.get("req_id"),str): raise ValueError("invalid candidate")
            val=invoke("match",match_model,prompts.MATCH_SYSTEM,user,user+json.dumps(batch,ensure_ascii=False),match_check)
            if val is None and errors and not stopped and not dry_run:
                errored_items.update(x["id"] for x in members)
            if val:
                ids={x["id"] for x in members}; allowed={r["req_id"] for r in batch}
                for it in val["items"]:
                    if it.get("item_id") not in ids: continue
                    for cand in it.get("candidates",[]):
                        if cand.get("req_id") not in allowed: local_discarded+=1; continue
                        if cand.get("confidence") in {"low","medium","high"}: local.setdefault(it["item_id"],{})[cand["req_id"]]=cand
        return local, local_discarded
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures={pool.submit(match_group,group,members): group for group,members in groups.items()}
        for future in as_completed(futures):
            local, bad_ids=future.result(); discarded += bad_ids
            for item_id, rows in local.items(): candidates[item_id].update(rows)
    if dry_run:
        # Matching is intentionally not executed. Save judge templates too, so users can inspect
        # the complete quote-verification payload shape and exactly which evidence/BRD text is eligible.
        for item in items:
            judge_user=json.dumps({"item_id":item["id"],"observed":"<observed>\n"+item["evidence"]+"\n</observed>",
                                   "requirements":[{"req_id":r.get("req_id"),"text":"<brd>\n"+r.get("source_text","")+"\n</brd>"} for r in requirements]},ensure_ascii=False)
            target=root/".check_cache"/"dry_run"/("judge-template-"+hashlib.sha256(judge_user.encode()).hexdigest()[:12]+".json")
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps({"stage":"judge-template","model":judge_model,"system":prompts.JUDGE_SYSTEM,"user":judge_user},ensure_ascii=False,indent=2),encoding="utf-8")
            prompts_written.append(("judge-template",len(prompts.JUDGE_SYSTEM)+len(judge_user)))
    by_item={x["id"]:x for x in items}; findings=[]; unverified=[]
    minrank={"low":0,"medium":1,"high":2}[min_confidence]
    for idx,(item_id,cands) in enumerate(candidates.items(),1):
        item=by_item[item_id]; accepted=[(rid,c) for rid,c in cands.items() if {"low":0,"medium":1,"high":2}[c["confidence"]]>=minrank]
        if not accepted: continue
        print(f"[judge] {idx}/{len(candidates)} {item_id}")
        reqs=[r for r in requirements if any(r["req_id"]==rid for rid,_ in accepted)]
        user=json.dumps({"item_id":item_id,"observed":"<observed>\n"+item["evidence"]+"\n</observed>","requirements":[{"req_id":r["req_id"],"text":"<brd>\n"+r["source_text"]+"\n</brd>"} for r in reqs]},ensure_ascii=False)
        def judge_check(v):
            if not isinstance(v,dict) or not isinstance(v.get("results"),list): raise ValueError("expected results list")
            for f in v["results"]:
                if not isinstance(f,dict) or f.get("verdict") not in {"consistent","conflict","unclear"}: raise ValueError("invalid judge verdict")
                for key in ("req_id","brd_quote","observed_quote","explanation"):
                    if not isinstance(f.get(key),str): raise ValueError(f"{key} must be a string")
                if f["verdict"]=="conflict" and f.get("severity") not in {"high","medium","low"}: raise ValueError("conflict severity is required")
        val=invoke("judge",judge_model,prompts.JUDGE_SYSTEM,user,user,judge_check)
        if val is None and errors and not stopped and not dry_run:
            errored_items.add(item_id)
        if val:
            req_map={r["req_id"]:r for r in reqs}
            bad_quotes=[]
            for finding in val["results"]:
                req=req_map.get(finding.get("req_id"))
                if not req: continue
                if " ".join(finding.get("brd_quote","").split()) not in " ".join(req["source_text"].split()) or " ".join(finding.get("observed_quote","").split()) not in " ".join(item["evidence"].split()):
                    bad_quotes.append(finding)
            if bad_quotes and not stopped:
                feedback=json.dumps({"failed_quote_checks":bad_quotes,"instruction":"Repair only these results. Return the complete judge schema and copy exact substrings."},ensure_ascii=False)
                retry=invoke("judge",judge_model,prompts.JUDGE_SYSTEM,user+"\n<observed>\n"+feedback+"\n</observed>",user+"\nquote-repair\n"+feedback,judge_check)
                if retry:
                    fixed={f.get("req_id"):f for f in retry.get("results", [])}
                    merged={f.get("req_id"):f for f in val.get("results", [])}
                    merged.update(fixed)
                    val={"results":list(merged.values())}
            for finding in val["results"]:
                req=req_map.get(finding.get("req_id"))
                if not req: continue
                good_brd=" ".join(finding.get("brd_quote","").split()) in " ".join(req["source_text"].split())
                good_obs=" ".join(finding.get("observed_quote","").split()) in " ".join(item["evidence"].split())
                finding.update({"item":item,"requirement":req})
                if not good_brd or not good_obs: unverified.append(finding)
                else: findings.append(finding)
        if stopped: break
    result={"items":items,"findings":findings,"unverified":unverified,"requirements":requirements,"errors":errors,"errored_items":errored_items,
            "candidates":candidates,"discarded":discarded,"stopped":stopped,"calls":llm.calls,"cache_hits":llm.cache_hits,
            "provider":provider,"match_model":match_model,"judge_model":judge_model,"prompts_written":prompts_written,"calls_by_stage":dict(calls_by_stage),"failed_stages":dict(failed_stages),
            "min_confidence":min_confidence}
    out=Path(out) if out else root/"Reports"/f"BRD-conflicts-{time.strftime('%Y%m%d-%H%M')} .md".replace(" .md",".md")
    if dry_run:
        stages=Counter(stage for stage,_ in prompts_written); chars=Counter()
        for stage,size in prompts_written: chars[stage]+=size
        print(f"[dry-run] estimated calls: {sum(stages.values())}; rough tokens: " + ", ".join(f"{stage}={chars[stage]//4}" for stage in sorted(chars)))
    out.parent.mkdir(parents=True,exist_ok=True); out.write_text(render_report(result),encoding="utf-8")
    print(f"[check] items={len(items)} findings={len(findings)} errors={len(errors)} report={out}")
    return result


def render_report(r):
    findings=r["findings"]; sections={v:[] for v in ("conflict","unclear","consistent")}
    for f in findings: sections.setdefault(f.get("verdict"),[]).append(f)
    minrank={"low":0,"medium":1,"high":2}.get(r.get("min_confidence","medium"),1)
    unmatched=[i for i in r["items"] if i["id"] not in r.get("errored_items", set()) and not any({"low":0,"medium":1,"high":2}.get(c.get("confidence"),-1)>=minrank for c in r["candidates"].get(i["id"],{}).values())]
    errored_count=len(r.get("errored_items", []))+r.get("failed_stages", {}).get("digest", 0)
    lines=["# BRD conflict check", "", "## Summary", "", f"Items checked: {len(r['items'])}; consistent: {len(sections['consistent'])}; conflicts: {len(sections['conflict'])}; unclear: {len(sections['unclear'])}; unmatched: {len(unmatched)}; unverified: {len(r['unverified'])}; errored: {errored_count}.", "", "## Conflicts", ""]
    order={"high":0,"medium":1,"low":2}
    def item_links(item):
        root=Path(item.get("vault_root", ".")); result=[]
        api=Path(item.get("api_note", ""))
        if api.is_file(): result.append(f"[[{api.relative_to(root).with_suffix('').as_posix()}]]")
        for pp in item.get("param_notes", []):
            p=Path(pp)
            if p.is_file(): result.append(f"[[{p.relative_to(root).with_suffix('').as_posix()}]]")
        return " · ".join(result)
    def brd_link(f):
        p=Path(f.get("requirement", {}).get("path", "")); root=Path(f.get("item", {}).get("vault_root", "."))
        return f"[[{p.relative_to(root).with_suffix('').as_posix()}]]" if p.is_file() and p.is_relative_to(root) else ""
    for f in sorted(sections["conflict"],key=lambda x:order.get(x.get("severity"),3)):
        item=f['item']; brd=f["requirement"].get("path", "")
        links=[]
        if linked:=item_links(item): links.append(linked)
        if link:=brd_link(f): links.append(link)
        label=f"{item['method']} {item['path']}" + (f" · param {item['param_name']}" if item.get("param_name") else "")
        lines += [f"### {label} — {f['req_id']} ({f.get('severity','')})", " · ".join(links), f"> BRD: {f.get('brd_quote','')}", f"> Observed: {f.get('observed_quote','')}", f.get("explanation", ""), ""]
    for title,key in (("Unclear","unclear"),("No BRD coverage","unmatched"),("Unverified","unverified")):
        lines += [f"## {title}", ""]
        if key=="unmatched": lines += [f"- {item_links(i) or i['id']} {i['method']} {i['path']}" + (f" · param {i['param_name']}" if i.get("param_name") else "") + " — needs manual review: undocumented behavior or a missed match" for i in unmatched]
        elif key=="unverified": lines += [f"- {item_links(f['item'])} {brd_link(f)} {f.get('req_id')}: quote verification failed" for f in r[key]]
        else: lines += [f"- {item_links(f['item'])} {brd_link(f)} {f['item']['method']} {f['item']['path']} — {f['req_id']}: {f.get('explanation','')}" for f in sections[key]]
        lines.append("")
    lines += ["## Consistent", "", "| Item | Requirement |", "|---|---|"]
    lines += [f"| {item_links(f['item'])} {brd_link(f)} {f['item']['method']} {f['item']['path']} | {f['req_id']} |" for f in sections["consistent"]]
    lines += ["", "## Errors", ""] + [f"- {x}" for x in r["errors"]]
    lines += ["", "## Run info", "", f"Provider: {r['provider']}; match model: {r['match_model']}; judge model: {r['judge_model']}; date: {time.strftime('%Y-%m-%d %H:%M')}; calls: {r['calls']} ({', '.join(f'{k}={v}' for k,v in sorted(r.get('calls_by_stage', {}).items()))}); cache hits: {r['cache_hits']}; discarded IDs: {r['discarded']}; stopped at --max-calls: {r['stopped']}.", ""]
    return "\n".join(lines)
