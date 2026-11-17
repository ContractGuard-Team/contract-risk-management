"""Read only the current synthetic job from service volumes to verify handoffs.

Used by container_smoke with SMOKE_AUDIT_DIR and read-only named-volume mounts.
No debug API, contract payload logging or runtime service changes are needed.
"""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3


def rows_for(root, service, identity):
    path = Path(root) / service / (service + "-jobs.sqlite3")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT payload,result,status FROM jobs "
            "WHERE json_extract(payload,'$.identity.job_id')=? "
            "AND json_extract(payload,'$.identity.runtime_id')=? ORDER BY created",
            (identity["job_id"], identity["runtime_id"]),
        ).fetchall()
    assert rows, f"Missing {service} job records"
    parsed = []
    for payload, result, status in rows:
        assert status == "completed", f"Incomplete {service} job"
        payload, result = json.loads(payload), json.loads(result)
        assert payload["identity"] == identity, f"Wrong {service} correlation"
        parsed.append((payload, result))
    return parsed


def assert_model_received(payload, response):
    """Check the actual user message recorded by BaseAgent and the schema-shaped fixture answer."""
    turn = response["memory"]["turns"][-1]
    assert turn[0]["role"] == "user" and turn[-1]["role"] == "assistant"
    assert json.loads(turn[0]["content"]) == payload["request"], "Model input differs from RPC request"
    assert turn[-1]["content"] == response["result"]["text"]
    answer = json.loads(response["result"]["text"])
    workflow = payload["request"]["context"]["workflow"]
    if "chunk_id" in workflow:  # Agent 1 fixture echoes the chunk it was given: proves the model consumed it
        assert answer["chunk_id"] == workflow["chunk_id"], "Model consumed a different chunk"
        assert answer["clauses"][0]["text"] == payload["request"]["content"], "Model consumed different context"


def verify_handoffs(root, identity, file_bytes, filename, persisted_output, probe):
    processing = rows_for(root, "input-processing", identity)
    graphs = rows_for(root, "langgraph", identity)
    assert len(processing) == len(graphs) == 1
    pp, processed = processing[0]
    gp, graph = graphs[0]

    # 1. Exact downloaded bytes arrive at extraction; OCR leaves extraction provenance.
    assert base64.b64decode(pp["file"]["data_base64"], validate=True) == file_bytes
    assert pp["file"]["filename"] == filename
    contract = processed["contract"]
    preprocessing = contract["context"]["preprocessing"]
    assert preprocessing["document"]["key"] == filename
    assert preprocessing["document"]["bucket"] == "smoke-input"
    assert preprocessing["document"]["format"] == Path(filename).suffix[1:]
    chunks = contract["chunks"]
    assert len(chunks) == contract["expected_chunk_count"] > 0
    if filename.endswith(".png"):
        assert preprocessing["extraction"]["ocr_segments"] > 0, "OCR was bypassed"
        assert any(s["method"] == "ocr" and s["page"] == 1 for s in preprocessing["extraction"]["segments"])
        extracted = " ".join(c["text"] for c in chunks).upper()
        assert "PAYMENT" in extracted and "DAYS" in extracted, "OCR fixture text not recognized"
    else:
        assert len(chunks) >= 2, "Text fixture must exercise multi-chunk handoff"
        assert preprocessing["source_hash"] == hashlib.sha256(file_bytes.decode("utf-8").encode()).hexdigest()
    print("PASS handoff 1: worker bytes -> Text Extraction/OCR; source metadata retained", flush=True)

    # 2. Processing manifest survives worker + graph and reaches each Agent 1 input.
    assert gp["contract"] == contract, "Worker changed preprocessing manifest"
    a1 = [r for r in rows_for(root, "agent-1", identity) if r[0].get("operation") == "run"]
    a1.sort(key=lambda r: r[0]["request"]["context"]["workflow"]["chunk_index"])
    assert len(a1) == len(chunks)
    previous_memory = None
    for chunk, (payload, response) in zip(chunks, a1):
        request, workflow = payload["request"], payload["request"]["context"]["workflow"]
        assert request["content"] == chunk["text"]
        assert workflow["chunk_id"] == chunk["chunk_id"] and workflow["chunk_index"] == chunk["index"]
        assert workflow["chunk_count"] == len(chunks)
        assert workflow["source_metadata"] == chunk["metadata"]
        assert request["context"]["preprocessing"] == preprocessing
        assert request["context"]["handoff_probe"] == probe
        if previous_memory is not None:
            assert payload["memory"] == previous_memory, "Agent 1 prior chunk memory lost"
        previous_memory = response["memory"]
        assert_model_received(payload, response)
    print(f"PASS handoff 2: preprocessing -> Agent 1 context; {len(chunks)} ordered chunks and memory", flush=True)

    # 3. Every Agent 1 output is in Agent 2's actual model context in order.
    a2 = [r for r in rows_for(root, "agent-2", identity) if r[0].get("operation") == "run"]
    assert len(a2) == 1
    p2, r2 = a2[0]
    aggregate = graph["results"]["agent_1_classification"]
    assert p2["request"]["context"]["workflow"]["classification"] == aggregate
    assert len(aggregate["chunks"]) == len(chunks)
    for item, chunk, (_, response) in zip(aggregate["chunks"], chunks, a1):
        assert item["chunk_id"] == chunk["chunk_id"] and item["index"] == chunk["index"]
        assert item["metadata"] == chunk["metadata"]
        assert item["result"] == response["result"], "Agent 1 output changed during aggregation"
    assert p2["request"]["context"]["preprocessing"] == preprocessing
    assert p2["request"]["context"]["handoff_probe"] == probe
    assert_model_received(p2, r2)
    cache_db = Path(root) / "langgraph" / "workflow-cache.sqlite3"
    with sqlite3.connect(cache_db.as_uri() + "?mode=ro", uri=True) as db:
        saved = [json.loads(v) for (v,) in db.execute(
            "SELECT value FROM entries WHERE run_key=? AND stage='classification'", (graph["cache_key"],))]
    assert len(saved) == len(chunks), "Agent 1 chunk outputs missing from cache"
    assert all(s["scope"]["contract_id"] == contract["contract_id"] for s in saved), "Agent 1 cache mixed contracts"
    assert all("memory" not in s for s in saved), "Agent 1 memory not released after aggregate"
    print("PASS handoff 3: all Agent 1 outputs -> Agent 2 context.workflow.classification; Agent 1 memory released", flush=True)

    # 4. Agent 2 output is unchanged in Agent 3's actual model context.
    a3 = [r for r in rows_for(root, "agent-3", identity) if r[0].get("operation") == "run"]
    assert len(a3) == 1
    p3, r3 = a3[0]
    assert p3["request"]["context"]["workflow"]["risk_scoring"] == r2["result"]
    assert graph["results"]["agent_2_risk_scoring"] == r2["result"]
    assert p3["request"]["context"]["preprocessing"] == preprocessing
    assert p3["request"]["context"]["handoff_probe"] == probe
    assert_model_received(p3, r3)
    print("PASS handoff 4: Agent 2 output -> Agent 3 context.workflow.risk_scoring", flush=True)

    # 5. Agent 3 -> graph extraction.v2 -> worker's persisted S3 artifact, not just status.
    assert graph["results"]["agent_3_recommendation"] == r3["result"]
    assert persisted_output["result"] == graph["extraction"], "Worker saved a different extraction"
    assert [c["text"] for c in graph["extraction"]["clauses"]] == [c["text"] for c in chunks], "Clauses lost text"
    assert persisted_output["job_id"] == identity["job_id"]
    assert persisted_output["runtime_id"] == identity["runtime_id"]
    assert persisted_output["contract_id"] == identity["contract_id"]
    print("PASS handoff 5: agents -> LangGraph extraction.v2 -> worker -> identical S3 result", flush=True)
