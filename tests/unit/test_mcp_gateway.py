import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from common.exceptions import IntegrationNotConfigured
from mcp_server.s3_reader import S3ReadOnly
from integrations.database.postgres_client import PostgresConnector
from mcp_server.qdrant_reader import QdrantReader
from integrations.external.mcp_client import McpToolClient, McpToolError, bind_mcp_tools
from mcp_server.tavily_search import TavilySearch
from mcp_server.service import FACTORIES, ToolGateway, create_app

TOKEN = "m" * 40


class FakeSearch:
    def search(self, query, max_results=5):
        return [{"title": "t:" + query, "url": "https://example.test", "snippet": "s"}][:max_results]


class McpGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"MCP_API_TOKEN": TOKEN, "MONITORING_URL": ""})
        cls.env.start()
        for name in ("QDRANT_URL", "POSTGRES_DSN", "MCP_S3_BUCKETS", "TAVILY_API_KEY"):
            os.environ.pop(name, None)
        cls.opened = []
        factories = {**FACTORIES, "web_search": lambda: cls.opened.append("web_search") or FakeSearch()}
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(create_app(ToolGateway(factories)), host="127.0.0.1",
                                                   port=port, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        cls.base = f"http://127.0.0.1:{port}"
        cls.client = McpToolClient(cls.base + "/mcp", TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.env.stop()

    def test_agent_requested_tool_is_opened_and_called(self):
        result = self.client.call("web_search", {"query": "điều 418", "max_results": 1})
        self.assertEqual(result["results"][0]["title"], "t:điều 418")
        self.assertEqual(self.opened, ["web_search"])  # other connectors stay closed

    def test_unconfigured_connector_reports_what_is_missing(self):
        with self.assertRaisesRegex(McpToolError, "QDRANT_URL"):
            self.client.call("qdrant_search", {"collection": "laws", "vector": [0.1]})
        with self.assertRaisesRegex(McpToolError, "POSTGRES_DSN"):
            self.client.call("postgres_query", {"sql": "select 1"})

    def test_wrong_token_is_rejected_and_health_is_public(self):
        self.assertEqual(httpx.post(self.base + "/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        ready = httpx.get(self.base + "/readyz").json()
        self.assertEqual(ready["tools"], {"legal_search": False, "web_search": False, "qdrant_search": False,
                                          "postgres_query": False, "s3_get_object": False,
                                          "s3_list_objects": False, "qdrant_collection_info": False,
                                          "supabase_select": False, "supabase_list_tables": False,
                                          "partner_document": False})

    def test_bind_only_agent_2_and_3(self):
        from agents.hermes.agent_factory import create_agent
        provider = object()
        names = {aid: bind_mcp_tools(create_agent(aid, provider=provider), self.client)
                 for aid in ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")}
        self.assertEqual(names["agent_1_classification"], [])
        self.assertEqual(set(names["agent_2_risk_scoring"]), {"legal_search", "web_search", "qdrant_search", "postgres_query", "s3_get_object", "s3_list_objects",
                                                                    "qdrant_collection_info",
                                                                    "supabase_select", "supabase_list_tables"})
        self.assertIn("legal_reference", names["agent_3_recommendation"])  # alias of the RAG tool legal_search
        self.assertIn("partner_document", names["agent_3_recommendation"])
        self.assertNotIn("partner_document", names["agent_2_risk_scoring"])  # only Agent 3 reads the original
        agent3 = create_agent("agent_3_recommendation", provider=provider)
        bind_mcp_tools(agent3, self.client)
        self.assertEqual(agent3.run_tool("web_search", {"query": "x"})["results"][0]["title"], "t:x")
        # Tool loop: specs come from the gateway's own tool list, alias renamed for the model.
        specs = {s["function"]["name"]: s["function"] for s in agent3.tools.specs()}
        self.assertEqual(set(specs), set(names["agent_3_recommendation"]))
        self.assertIn("query", specs["legal_reference"]["parameters"]["properties"])
        self.assertIn("ContractGuard", specs["partner_document"]["description"])


class ConnectorTests(unittest.TestCase):
    def test_qdrant_search_info_and_tenant_filter(self):
        from qdrant_client import QdrantClient, models
        client = QdrantClient(":memory:")
        client.create_collection("legal_documents", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
        client.create_payload_index("legal_documents", "contract", models.PayloadSchemaType.KEYWORD)
        client.upsert("legal_documents", [
            models.PointStruct(id=1, vector=[1, 0, 0, 0], payload={"contract": "a", "law": "BLDS 2015 d418",
                                                                   "current": True, "status": "in_force"}),
            models.PointStruct(id=2, vector=[0.9, 0.1, 0, 0], payload={"contract": "b", "law": "LTM 2005 d301",
                                                                       "current": True, "status": "in_force"}),
            models.PointStruct(id=3, vector=[1, 0, 0, 0], payload={"law": "old wording", "current": False,
                                                                   "status": "in_force"})])

        class FakeEmbedder:
            def __init__(self):
                self.queries = []

            def embed_query(self, text):
                self.queries.append(text)
                return [1, 0, 0, 0]
        embedder = FakeEmbedder()
        reader = QdrantReader(collection="legal_documents", client=client, embedder=embedder)
        self.assertEqual([p["id"] for p in reader.search(query="phạt vi phạm hợp đồng", limit=5)], [1, 2])  # no history
        self.assertEqual(embedder.queries, ["phạt vi phạm hợp đồng"])
        self.assertEqual(sorted(p["id"] for p in reader.search([1, 0, 0, 0], limit=5, query_filter={})), [1, 2, 3])
        only_b = reader.search([1, 0, 0, 0], query_filter={"must": [{"key": "contract", "match": {"value": "b"}}]})
        self.assertEqual([p["payload"]["law"] for p in only_b], ["LTM 2005 d301"])
        with self.assertRaises(ValueError):
            reader.search()
        info = reader.info()
        self.assertEqual((info["points_count"], info["vectors"][""]["size"], info["vectors"][""]["distance"]), (3, 4, "Cosine"))
        with self.assertRaises(ValueError):
            reader.search([1, 0, 0, 0], collection="../x")
        with self.assertRaises(IntegrationNotConfigured), patch.dict(os.environ, {"QDRANT_URL": ""}):
            QdrantReader()

    def test_local_embedder_prefixes_e5_passages_and_queries(self):
        from integrations.external.embedding_client import LocalEmbedder

        class Engine:
            def __init__(self):
                self.seen = []

            def embed(self, texts, batch_size=16):
                self.seen += texts
                return [[0.5, 0.5] for _ in texts]
        engine = Engine()
        embedder = LocalEmbedder("intfloat/multilingual-e5-large", dimensions=2, engine=engine)
        self.assertEqual(embedder.embed(["Điều 1."]), [[0.5, 0.5]])
        self.assertEqual(embedder.embed_query("phạt hợp đồng"), [0.5, 0.5])
        self.assertEqual(engine.seen, ["passage: Điều 1.", "query: phạt hợp đồng"])
        with self.assertRaises(ValueError):
            LocalEmbedder("intfloat/multilingual-e5-large", dimensions=3, engine=engine).embed(["x"])

    def test_supabase_read_only_select(self):
        from mcp_server.supabase_reader import SupabaseReader
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[{"id": 1, "law": "BLDS 2015"}])
        http = httpx.Client(transport=httpx.MockTransport(handler))
        reader = SupabaseReader("https://demo.supabase.co", "sb_publishable_x", http_client=http, tables=["laws"])
        result = reader.select("laws", "id,law", {"law": "ilike.*BLDS*", "id": "gte.1"}, "id.desc", 500)
        self.assertEqual(result["rows"], [{"id": 1, "law": "BLDS 2015"}])
        url = str(seen[0].url)
        self.assertTrue(url.startswith("https://demo.supabase.co/rest/v1/laws?"), url)
        for part in ("select=id%2Claw", "id=gte.1", "order=id.desc", "limit=100"):
            self.assertIn(part, url)
        self.assertEqual((seen[0].method, seen[0].headers["apikey"]), ("GET", "sb_publishable_x"))
        for write in (lambda: reader.client.table("laws").insert({"law": "x"}).execute(),
                      lambda: reader.client.table("laws").update({"law": "x"}).eq("id", 1).execute(),
                      lambda: reader.client.table("laws").delete().eq("id", 1).execute()):
            with self.assertRaisesRegex(PermissionError, "read-only"):
                write()
        self.assertEqual(len(seen), 1)  # no write ever reached the network
        with self.assertRaises(PermissionError):
            reader.select("users")
        with self.assertRaises(ValueError):
            reader.select("laws", filters={"law": "drop.x"})
        schema = httpx.MockTransport(lambda r: httpx.Response(200, json={"paths": {"/": {}, "/laws/": {}, "/users/": {}, "/rpc/f": {}}}))
        secret = SupabaseReader("https://demo.supabase.co", "sb_secret_x", http_client=httpx.Client(transport=schema), tables=["laws"])
        self.assertEqual(secret.list_tables(), {"tables": ["laws"], "restricted": True, "source": "schema"})
        self.assertEqual(reader.list_tables()["source"], "SUPABASE_TABLES")  # publishable key cannot read the schema
        with patch.dict(os.environ, {"SUPABASE_URL": "https://demo.supabase.co", "SUPABASE_SECRET_KEY": "sb_secret_y",
                                     "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_y"}):
            self.assertEqual(SupabaseReader(http_client=httpx.Client()).headers["apikey"], "sb_secret_y")

    def test_postgres_allows_one_statement_only(self):
        db = PostgresConnector("postgresql://x", connect=lambda *a, **k: self.fail("must not connect"))
        with self.assertRaises(ValueError):
            db.query("select 1; delete from laws")
        with self.assertRaises(IntegrationNotConfigured), patch.dict(os.environ, {"POSTGRES_DSN": ""}):
            PostgresConnector()

    def test_partner_document_reads_original_read_only_and_pages(self):
        import boto3
        from moto import mock_aws
        from mcp_server.partner_reader import PartnerDocuments
        bucket = "contractguard-dev-documents-699061129385"
        env = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_REGION": "ap-southeast-1"}

        class Processor:
            calls = []
            def run_job(self, payload):
                self.calls.append(payload)
                return {"contract": {"chunks": [{"index": 1, "text": "Điều 2. Phạt"}, {"index": 0, "text": "Điều 1. Giá"}],
                                     "context": {"preprocessing": {"document": {"page_count": 2}}}}}

        with patch.dict(os.environ, env), mock_aws():
            s3 = boto3.client("s3", region_name="ap-southeast-1")
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
            s3.put_object(Bucket=bucket, Key="quarantine/u1/hd.txt", Body="Điều 1. Thanh toán 30 ngày".encode())
            s3.put_object(Bucket=bucket, Key="quarantine/u1/hd.pdf", Body=b"%PDF-1.7\xff\xfe\x00binary")
            processor = Processor()
            reader = PartnerDocuments(bucket, processor=processor)
            mine = ["quarantine/u1/hd.txt", "quarantine/u1/hd.pdf"]  # this job's keys (request.json)
            first = reader.read("quarantine/u1/hd.txt", max_chars=8, allowed=mine)
            self.assertEqual((first["text"], first["next_offset"], first["extraction"]), ("Điều 1. ", 8, "utf-8"))
            rest = reader.read("quarantine/u1/hd.txt", offset=first["next_offset"], max_chars=1000, allowed=mine)
            self.assertEqual(first["text"] + rest["text"], "Điều 1. Thanh toán 30 ngày")
            self.assertIsNone(rest["next_offset"])
            pdf = reader.read("quarantine/u1/hd.pdf", allowed=mine)  # binary -> input-processing, chunks in order
            reader.read("quarantine/u1/hd.pdf", allowed=mine)        # cached: no second extraction/OCR
            s3.put_object(Bucket=bucket, Key="quarantine/u2/other.txt", Body=b"another tenant's contract")
            with self.assertRaisesRegex(PermissionError, "not a document of this job"):
                reader.read("quarantine/u2/other.txt", allowed=mine)  # readable by IAM, not by this job
            self.assertEqual((pdf["text"], pdf["page_count"]), ("Điều 1. Giá\nĐiều 2. Phạt", 2))
            self.assertEqual(len(processor.calls), 1)
            self.assertEqual(processor.calls[0]["identity"]["job_id"], processor.calls[0]["job"]["contract_id"])
            for key in ("results/x/request.json", "quarantine/../results/x", "other/a.txt"):
                with self.assertRaises(ValueError):
                    reader.read(key, allowed=[key])
            with self.assertRaisesRegex(PermissionError, "read-only"):
                reader.client.delete_object(Bucket=bucket, Key="quarantine/u1/hd.txt")
        with patch.dict(os.environ, {"PARTNER_S3_BUCKET": ""}), self.assertRaises(IntegrationNotConfigured):
            PartnerDocuments()

    def test_s3_read_only_list_and_read(self):
        import boto3
        from moto import mock_aws
        bucket = "contract-risk-database-unique-333596350642-ap-southeast-1-an"
        env = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_REGION": "ap-southeast-1"}
        with patch.dict(os.environ, env), mock_aws():
            s3 = boto3.client("s3", region_name="ap-southeast-1")
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
            s3.put_object(Bucket=bucket, Key="laws/blds-2015.txt", Body="Điều 418. Phạt vi phạm".encode(), ContentType="text/plain")
            s3.put_object(Bucket=bucket, Key="laws/scan.pdf", Body=b"%PDF-1.7\xff\xfe\x00binary")
            reader = S3ReadOnly([bucket], max_bytes=12)
            listed = reader.list_objects(prefix="laws/")  # bucket omitted: the only configured bucket
            self.assertEqual([o["key"] for o in listed["objects"]], ["laws/blds-2015.txt", "laws/scan.pdf"])
            doc = reader.get_text("", "laws/blds-2015.txt")
            self.assertTrue(doc["truncated"])
            self.assertTrue("Điều 418. Phạt vi phạm".startswith(doc["text"]))  # no broken char at the cut
            self.assertTrue(S3ReadOnly([bucket]).get_text(bucket, "laws/scan.pdf")["binary"])
            with self.assertRaises(PermissionError):
                reader.get_text("other-bucket", "x.txt")
            for write in (lambda: reader.client.put_object(Bucket=bucket, Key="x", Body=b"x"),
                          lambda: reader.client.delete_object(Bucket=bucket, Key="laws/scan.pdf")):
                with self.assertRaisesRegex(PermissionError, "read-only"):
                    write()
            self.assertEqual(len(s3.list_objects_v2(Bucket=bucket)["Contents"]), 2)  # nothing changed

    def test_web_search_uses_tavily_and_maps_results(self):
        calls = []

        class Client:
            def search(self, query, **kw):
                calls.append((query, kw))
                return {"results": [{"title": "T", "url": "U", "content": "B", "score": 0.9}]}
        with patch.dict(os.environ, {"TAVILY_COUNTRY": "vietnam", "TAVILY_SEARCH_DEPTH": "basic"}):
            client = TavilySearch(client=Client())
        self.assertEqual(client.search(" luật ", 50), [{"title": "T", "url": "U", "snippet": "B", "score": 0.9}])
        self.assertEqual(calls[0], ("luật", {"search_depth": "basic", "topic": "general", "country": "vietnam",
                                            "max_results": 20}))
        with self.assertRaises(ValueError):
            client.search("x" * 401)
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}), self.assertRaisesRegex(Exception, "TAVILY_API_KEY"):
            TavilySearch()

if __name__ == "__main__":
    unittest.main()
