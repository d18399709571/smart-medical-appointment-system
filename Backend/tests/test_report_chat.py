"""Real source definitions with synthetic DB/Redis/RAG/LLM; no app imports.

SQLAlchemy only builds queries. Pydantic checks use its real request model when
installed. No configuration, database engine, Redis connection or HTTP is loaded.
Run from Backend: python -B -m unittest discover -s tests -v
"""
import ast
import importlib.util
import json
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from test_aws_surface import definitions, FakeHTTPException, tree

try:
    from sqlalchemy import Column, Integer, select
    from sqlalchemy.orm import declarative_base
except ImportError:
    HAS_SQLALCHEMY = False
else:
    HAS_SQLALCHEMY = True
    Base = declarative_base()

    class Report(Base):
        __tablename__ = "reports"
        id = Column(Integer, primary_key=True)
        patient_id = Column(Integer)


SERVICE = "app/services/client/report_chat.py"


class RedisSubstitute:
    def __init__(self):
        self.rows, self.ttls, self.calls = {}, {}, []

    async def lrange(self, key, start, stop):
        self.calls.append(("lrange", key, start, stop))
        return self.rows.get(key, [])[start:stop + 1]

    async def lpush(self, key, message):
        self.calls.append(("lpush", key, message))
        self.rows.setdefault(key, []).insert(0, message)

    async def ltrim(self, key, start, stop):
        self.calls.append(("ltrim", key, start, stop))
        self.rows[key] = self.rows[key][start:stop + 1]

    async def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        self.ttls[key] = seconds  # Records the request; does not simulate a real clock.


@unittest.skipUnless(HAS_SQLALCHEMY, "SQLAlchemy unavailable; report service substitute checks skipped")
class ReportChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = RedisSubstitute()
        self.reports = {
            rid: SimpleNamespace(id=rid, patient_id=pid, interpretation_status="completed",
                                 type="synthetic report", content={"test_value": rid}, ai_interpretation="test baseline")
            for rid, pid in ((11, 1), (12, 1), (21, 2))
        }

        async def execute(statement):
            # Inspect real query predicates; no SQL is executed.
            conditions = {c.left.key: c.right.value for c in statement._where_criteria}
            self.assertEqual(set(conditions), {"id", "patient_id"})
            report = self.reports.get(conditions["id"])
            if report and not all(c.operator(getattr(report, c.left.key), c.right.value)
                                  for c in statement._where_criteria):
                report = None
            return SimpleNamespace(scalar_one_or_none=lambda: report)

        self.db = SimpleNamespace(execute=AsyncMock(side_effect=execute))
        self.rag = SimpleNamespace(search=AsyncMock(return_value=[SimpleNamespace(content="test reference")]))

        async def stream(*args, **kwargs):
            yield "answer "
            yield "stream"

        self.llm = SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(content="answer")),
                                   stream=Mock(side_effect=stream))
        self.prompt = Mock(side_effect=lambda name, **kwargs: {"name": name, **kwargs})
        self.review = Mock(return_value=(True, ""))
        self.error = definitions("app/exceptions/http_exceptions.py", {"APIException"},
                                 HTTPException=FakeHTTPException, get_message=lambda message, language: message)["APIException"]
        self.service = definitions(SERVICE, {
            "CONV_KEY_PREFIX", "CONV_MAX_MESSAGES", "CONV_EXPIRE", "conversation_key",
            "get_conversation_history", "save_turn", "chat", "chat_stream",
        }, json=json, uuid=uuid, Report=Report, select=select,
            redis_client=SimpleNamespace(redis=self.redis), rag_service=self.rag, llm_service=self.llm,
            load_prompt=self.prompt, review_content=self.review, logger=Mock(), APIException=self.error,
            status=SimpleNamespace(HTTP_404_NOT_FOUND=404, HTTP_400_BAD_REQUEST=400))

    async def turn(self, mode, patient=1, report=11, message="question", session=None):
        kwargs = dict(db=self.db, patient_id=patient, report_id=report, user_message=message, session_id=session)
        if mode == "chat":
            sid, reply, count = await self.service[mode](**kwargs)
            return sid, count
        events = [event async for event in self.service[mode](**kwargs)]
        return events[0]["session_id"], events[-1]["turn_count"]

    def history(self, patient, report, session):
        key = self.service["conversation_key"](patient, report, session)
        return [json.loads(message) for message in self.redis.rows[key]]

    async def test_same_patient_report_and_session_continue_history(self):
        await self.turn("chat", message="first", session="same-session")
        sid, count = await self.turn("chat", message="second", session="same-session")
        self.assertEqual((sid, count), ("same-session", 2))
        # Preserve the existing LPUSH order and prompt reversal exactly.
        self.assertEqual(self.prompt.call_args.kwargs["conversation_history"],
                         [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "first"}])
        self.assertEqual(len(self.history(1, 11, sid)), 4)

    async def test_patient_isolation_with_identical_client_session_id(self):
        key = self.service["conversation_key"]
        self.assertNotEqual(key(1, 11, "shared"), key(2, 11, "shared"))
        for mode in ("chat", "chat_stream"):
            with self.subTest(mode=mode):
                sid = "1:11:pretend-identity-" + mode
                await self.turn(mode, patient=1, report=11, message="patient-one", session=sid)
                before = list(self.redis.rows[key(1, 11, sid)])
                await self.turn(mode, patient=2, report=21, message="patient-two", session=sid)
                self.assertEqual(self.prompt.call_args.kwargs["conversation_history"], [])
                self.assertEqual(self.redis.rows[key(1, 11, sid)], before)
                self.assertEqual(self.history(2, 21, sid)[0]["content"], "patient-two")

    async def test_report_isolation_with_identical_client_session_id(self):
        for mode in ("chat", "chat_stream"):
            with self.subTest(mode=mode):
                sid = "shared-" + mode
                await self.turn(mode, report=11, message="report-one", session=sid)
                before = self.history(1, 11, sid)
                await self.turn(mode, report=12, message="report-two", session=sid)
                self.assertEqual(self.prompt.call_args.kwargs["conversation_history"], [])
                self.assertEqual(self.history(1, 11, sid), before)
                self.assertEqual(self.history(1, 12, sid)[0]["content"], "report-two")

    async def test_chat_and_stream_share_history_in_both_directions(self):
        sid, _ = await self.turn("chat", message="ordinary", session="shared")
        before = list(reversed(self.history(1, 11, sid)))
        self.assertEqual(await self.turn("chat_stream", message="streamed", session=sid), (sid, 2))
        self.assertEqual(self.prompt.call_args.kwargs["conversation_history"], before)
        before = list(reversed(self.history(1, 11, sid)))
        self.assertEqual(await self.turn("chat", message="ordinary-again", session=sid), (sid, 3))
        self.assertEqual(self.prompt.call_args.kwargs["conversation_history"], before)
        self.assertEqual({call[1] for call in self.redis.calls}, {"report_chat:v2:conversation:1:11:shared"})

    async def test_denied_missing_and_unfinished_reports_never_reach_redis_or_model(self):
        for mode in ("chat", "chat_stream"):
            for report, patient, status, expected in ((21, 1, "completed", 404), (99, 1, None, 404),
                                                      (11, 1, "pending", 400), (11, 1, "failed", 400)):
                with self.subTest(mode=mode, report=report, status=status):
                    if report in self.reports:
                        self.reports[report].interpretation_status = status
                    with self.assertRaises(self.error) as caught:
                        await self.turn(mode, patient=patient, report=report, session="21:2:spoofed")
                    self.assertEqual(caught.exception.status_code, expected)
        self.assertEqual(self.redis.calls, [])
        self.rag.search.assert_not_awaited()
        self.llm.call.assert_not_awaited()
        self.llm.stream.assert_not_called()
        self.prompt.assert_not_called()
        self.review.assert_not_called()

    async def test_first_turn_returns_complete_uuid4_and_followup_reuses_it(self):
        seen = set()
        for mode in ("chat", "chat_stream"):
            for initial in (None, ""):
                with self.subTest(mode=mode, initial=initial):
                    sid, count = await self.turn(mode, session=initial)
                    self.assertEqual(len(sid), 36)
                    self.assertEqual(str(uuid.UUID(sid)), sid)
                    self.assertEqual(uuid.UUID(sid).version, 4)
                    self.assertNotIn(sid, seen)
                    seen.add(sid)
                    self.assertEqual(count, 1)
                    self.assertEqual(await self.turn(mode, session=sid), (sid, 2))

    async def test_ttl_message_limit_lpush_order_and_turn_count_preserved(self):
        for i in range(10):
            self.llm.call.return_value = SimpleNamespace(content=f"answer-{i}")
            sid, count = await self.turn("chat", message=f"question-{i}", session="bounded")
            self.assertEqual(count, min(i + 1, 9))  # Existing count uses the bounded history window.
        key = "report_chat:v2:conversation:1:11:bounded"
        expected = [message for i in reversed(range(2, 10)) for message in (
            {"role": "user", "content": f"question-{i}"},
            {"role": "assistant", "content": f"answer-{i}"},
        )]
        self.assertEqual(self.history(1, 11, sid), expected)
        self.assertEqual(self.redis.ttls, {key: 3600})
        pushes = [json.loads(call[2])["role"] for call in self.redis.calls if call[0] == "lpush"]
        self.assertEqual(pushes, ["assistant", "user"] * 10)
        for call in self.redis.calls:
            if call[0] in ("lrange", "ltrim"):
                self.assertEqual(call[2:], (0, 15))

    async def test_legacy_keys_are_not_read_migrated_deleted_or_expired(self):
        sid = "1:11:12345678"
        old = "report_chat:conversation:" + sid
        self.redis.rows[old] = [json.dumps({"role": "user", "content": "old private history"})]
        before = list(self.redis.rows[old])
        for mode in ("chat", "chat_stream"):
            await self.turn(mode, session=sid)
        self.assertEqual(self.redis.rows[old], before)
        self.assertNotIn(old, self.redis.ttls)
        self.assertTrue(all(call[1] != old for call in self.redis.calls))

    async def test_rag_prompt_llm_and_review_inputs_preserved(self):
        for mode in ("chat", "chat_stream"):
            await self.turn(mode, message="test question", session=mode)
            self.rag.search.assert_awaited_with(self.db, query="synthetic report test question", top_k=5,
                                               source_type="医学参考资料", agent_type="report_followup")
            prompt = self.prompt.call_args.kwargs
            self.assertEqual(prompt["report_type"], "synthetic report")
            self.assertEqual(json.loads(prompt["report_content"]), {"test_value": 11})
            self.assertEqual(prompt["ai_interpretation"], "test baseline")
            self.assertEqual(prompt["reference_chunks"], ["test reference"])
            self.assertEqual(prompt["user_message"], "test question")
            call = self.llm.call if mode == "chat" else self.llm.stream
            self.assertEqual(call.call_args.kwargs, {"agent_type": "report_followup"})
            self.review.assert_called_with("answer" if mode == "chat" else "answer stream")

    async def test_stream_events_and_review_replacement_preserved(self):
        for passed in (True, False):
            self.review.return_value = (passed, "test reason")
            sid = "protocol-" + str(passed)
            events = [event async for event in self.service["chat_stream"](
                self.db, 1, 11, "question", sid)]
            self.assertEqual(events[0], {"type": "start", "session_id": sid, "turn_count": 1})
            self.assertEqual(events[1:3], [{"type": "delta", "content": "answer "},
                                         {"type": "delta", "content": "stream"}])
            self.assertEqual(events[-1], {"type": "done", "session_id": sid, "turn_count": 1})
            if passed:
                self.assertEqual([e["type"] for e in events], ["start", "delta", "delta", "done"])
            else:
                self.assertEqual([e["type"] for e in events], ["start", "delta", "delta", "replace", "done"])
                self.assertIn("未能通过内容审核", events[3]["content"])
                self.assertEqual(self.history(1, 11, sid)[1]["content"], events[3]["content"])
                await self.turn("chat", session="ordinary-review")
                self.assertEqual(self.history(1, 11, "ordinary-review")[1]["content"], events[3]["content"])


class ReportChatSurfaceTests(unittest.TestCase):
    def test_authenticated_identity_and_shared_main_client_mount(self):
        api = tree("app/api/client/v1/report.py")
        handlers = {n.name: n for n in api.body if isinstance(n, ast.AsyncFunctionDef)}
        for name, service in (("chat_about_report", "chat"), ("chat_about_report_stream", "chat_stream")):
            handler = handlers[name]
            self.assertIn("Depends(get_current_user)", [ast.unparse(n) for n in handler.args.defaults])
            call = next(n for n in ast.walk(handler) if isinstance(n, ast.Call)
                        and ast.unparse(n.func) == "report_chat." + service)
            keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            self.assertEqual(keywords["patient_id"], "current_user.id")
            self.assertEqual(keywords["report_id"], "report_id")
            self.assertEqual(keywords["session_id"], "request.session_id")
        registry = definitions("app/route/router_registry.py", {"RouteConfig", "CLIENT_ROUTES"},
                               settings=SimpleNamespace(API_V1_STR="/api/v1"))
        self.assertIn("app.api.client.v1.report", [r.module_path for r in registry["CLIENT_ROUTES"]])
        for relative in ("app/route/route.py", "app/configs/docs_apps.py"):
            calls = {ast.unparse(n) for n in ast.walk(tree(relative)) if isinstance(n, ast.Call)}
            self.assertIn("register_routes(app, get_client_routes())", calls)
        calls = {ast.unparse(n) for n in ast.walk(tree("app/route/route.py")) if isinstance(n, ast.Call)}
        self.assertIn("app.mount('/client', client_docs_app)", calls)

    def test_request_length_constraint_declared(self):
        request = next(n for n in tree("app/schemas/client/report.py").body
                       if isinstance(n, ast.ClassDef) and n.name == "ReportChatRequest")
        field = next(n for n in request.body if isinstance(n, ast.AnnAssign) and n.target.id == "session_id")
        self.assertEqual(next(ast.literal_eval(k.value) for k in field.value.keywords if k.arg == "max_length"), 128)

    @unittest.skipUnless(importlib.util.find_spec("pydantic"), "Pydantic unavailable; real request validation unverified")
    def test_real_pydantic_request_accepts_bounded_ids_and_rejects_overlong_ids(self):
        from pydantic import BaseModel, ConfigDict, Field, ValidationError
        base = definitions("app/schemas/base.py", {"BaseSchema"}, BaseModel=BaseModel, ConfigDict=ConfigDict)["BaseSchema"]
        request = definitions("app/schemas/client/report.py", {"ReportChatRequest"},
                              BaseSchema=base, Field=Field, Optional=Optional)["ReportChatRequest"]
        request.model_rebuild(_types_namespace={"Optional": Optional})
        for sid in (None, "", str(uuid.uuid4()), "1:11:12345678", "x" * 128, "测" * 128):
            self.assertEqual(request(message="question", session_id=sid).session_id, sid)
        self.assertIsNone(request(message="question").session_id)
        for sid in ("x" * 129, "测" * 129):
            with self.assertRaises(ValidationError) as caught:
                request(message="question", session_id=sid)
            self.assertEqual(caught.exception.errors()[0]["loc"], ("session_id",))
            self.assertEqual(caught.exception.errors()[0]["type"], "string_too_long")


if __name__ == "__main__":
    unittest.main()
