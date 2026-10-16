"""Refresh-flow checks with real source and isolated JWT/session substitutes.

SQLAlchemy constructs queries when installed; no engine or database is created.
JWT/bcrypt library checks are separate and skipped if those libraries are absent.
Run from Backend: python -B -m unittest discover -s tests -v
"""
import ast
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from test_aws_surface import definitions, FakeHTTPException

try:
    from sqlalchemy import Boolean, Column, DateTime, Integer, String, select, update
    from sqlalchemy.orm import declarative_base
    from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, False_, True_
    from sqlalchemy.sql.selectable import Join
    from sqlalchemy.sql.dml import Update
except ImportError:
    HAS_SQLALCHEMY = False
else:
    HAS_SQLALCHEMY = True
    Base = declarative_base()

    class AccountColumns:
        id = Column(Integer, primary_key=True)
        email = Column(String)
        is_active = Column(Boolean)

        def verify_password(self, password):
            # Test substitute only; actual password methods are checked separately.
            return password == "test-password"

    class User(AccountColumns, Base):
        __tablename__ = "users"

    class Admin(AccountColumns, Base):
        __tablename__ = "admins"

    class TokenColumns:
        id = Column(Integer, primary_key=True)
        token = Column(String(255))
        is_active = Column(Boolean)
        expires_at = Column(DateTime(timezone=True))
        last_used_at = Column(DateTime(timezone=True))

    class UserToken(TokenColumns, Base):
        __tablename__ = "tokens"
        user_id = Column(Integer)

    class AdminToken(TokenColumns, Base):
        __tablename__ = "admin_tokens"
        admin_id = Column(Integer)


class TestJWTError(Exception):
    pass


class JWTSubstitute:
    """Opaque test tokens; does not implement or prove cryptographic signing."""
    def __init__(self):
        self.claims = {}

    def encode(self, payload, key, algorithm):
        token = "test-token-" + uuid.uuid4().hex
        self.claims[token] = dict(payload)
        return token

    def decode(self, token, key, algorithms):
        payload = self.claims.get(token)
        if not payload or payload["exp"] <= datetime.now(UTC):
            raise TestJWTError("Invalid or expired test token")
        return dict(payload)


SETTINGS = SimpleNamespace(SECRET_KEY="test-only-key", ALGORITHM="HS256", ACCESS_TOKEN_EXPIRE_MINUTES=30)


def security(jwt=None, error=TestJWTError, password_context=None):
    return definitions(
        "app/core/security.py", {"AuthBase"}, jwt=jwt or JWTSubstitute(), JWTError=error,
        settings=SETTINGS, pwd_context=password_context or Mock(),
        hashlib=hashlib, uuid=uuid, datetime=datetime, timedelta=timedelta, UTC=UTC,
    )["AuthBase"]


class RefreshSecurityTests(unittest.TestCase):
    def test_full_token_sha256_and_existing_password_delegation(self):
        passwords = Mock()
        auth = security(password_context=passwords)
        self.assertEqual(auth.refresh_token_fingerprint("abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertNotEqual(auth.refresh_token_fingerprint("x" * 72 + "first"),
                            auth.refresh_token_fingerprint("x" * 72 + "second"))
        auth.hash_token("test-password")
        passwords.hash.assert_called_once_with("test-password")
        auth.verify_token_hash("test-password", "stored-password-hash")
        passwords.verify.assert_called_once_with("test-password", "stored-password-hash")

    def test_password_sources_and_token_column_capacity(self):
        root = Path(__file__).resolve().parents[1]
        for path in ("app/core/security.py", "app/models/admin.py", "app/models/user.py"):
            calls = {ast.unparse(n) for n in ast.walk(ast.parse((root / path).read_text(encoding="utf-8-sig")))
                     if isinstance(n, ast.Call)}
            self.assertIn("CryptContext(schemes=['bcrypt'], deprecated='auto')", calls)
        for model, field in (("admin", "password"), ("user", "hashed_password")):
            source = ast.parse((root / f"app/models/{model}.py").read_text(encoding="utf-8-sig"))
            methods = {n.name: n for n in ast.walk(source) if isinstance(n, ast.FunctionDef)}
            self.assertEqual(ast.unparse(methods["get_password_hash"].body[0]), "return pwd_context.hash(password)")
            self.assertEqual(ast.unparse(methods["verify_password"].body[0]),
                             f"return pwd_context.verify(plain_password, self.{field})")
        source = ast.parse((root / "app/models/token.py").read_text(encoding="utf-8-sig"))
        for node in source.body:
            if isinstance(node, ast.ClassDef):
                column = next(n for n in node.body if isinstance(n, ast.Assign)
                              and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "token")
                self.assertEqual(ast.literal_eval(column.value.args[0].args[0]), 255)

    def test_refresh_scopes_and_access_scopes(self):
        auth = security()
        for realm in ("client", "backoffice"):
            token = auth.create_refresh_token("1", scope=realm + "_refresh")
            self.assertIsNotNone(auth.verify_token(token, scope=realm + "_refresh"))
            self.assertIsNone(auth.verify_token(token, scope=realm))
            access = auth.create_access_token("1", scope=realm)
            self.assertIsNotNone(auth.verify_token(access, scope=realm))
            self.assertIsNone(auth.verify_token(access, scope=realm + "_refresh"))
        with self.assertRaises(ValueError):
            auth.create_refresh_token("1", scope="refresh")

    def test_auth_routes_share_services_in_main_and_mounted_apps(self):
        root = Path(__file__).resolve().parents[1]
        registry = definitions("app/route/router_registry.py", {"RouteConfig", "CLIENT_ROUTES", "BACKOFFICE_ROUTES"},
                               settings=SimpleNamespace(API_V1_STR="/api/v1"))
        for realm, service in (("client", "client_auth_service"), ("backoffice", "backoffice_auth_service")):
            self.assertIn(f"app.api.{realm}.v1.auth", [config.module_path for config in registry[realm.upper() + "_ROUTES"]])
            calls = {ast.unparse(n) for n in ast.walk(ast.parse((root / f"app/api/{realm}/v1/auth.py").read_text(encoding="utf-8-sig")))
                     if isinstance(n, ast.Call)}
            self.assertIn(f"{service}.refresh_token(db, request.refresh_token)", calls)
            self.assertIn(f"{service}.logout(db, request.refresh_token)", calls)
        # Existing AWS route-surface tests check the shared registry and both mounts.

    @unittest.skipUnless(importlib.util.find_spec("jose"), "python-jose unavailable; real JWT verification unverified")
    def test_real_jose_signature_expiration_and_scope(self):
        from jose import jwt, JWTError
        auth = security(jwt=jwt, error=JWTError)
        token = auth.create_refresh_token("1", scope="client_refresh")
        self.assertIsNotNone(auth.verify_token(token, scope="client_refresh"))
        self.assertIsNone(auth.verify_token(token, scope="backoffice_refresh"))
        header, payload, signature = token.split(".")
        tampered = header + "." + ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
        self.assertIsNone(auth.verify_token(tampered, scope="client_refresh"))
        expired = auth.create_refresh_token("1", scope="client_refresh", expires_delta=-timedelta(seconds=1))
        self.assertIsNone(auth.verify_token(expired, scope="client_refresh"))

    @unittest.skipUnless(importlib.util.find_spec("passlib") and importlib.util.find_spec("bcrypt"),
                         "passlib/bcrypt unavailable; real password round-trip unverified")
    def test_real_bcrypt_password_round_trip(self):
        from passlib.context import CryptContext
        auth = security(password_context=CryptContext(schemes=["bcrypt"], deprecated="auto"))
        hashed = auth.hash_token("test-password")
        self.assertTrue(hashed.startswith("$2"))
        self.assertTrue(auth.verify_token_hash("test-password", hashed))
        self.assertFalse(auth.verify_token_hash("wrong-password", hashed))


if HAS_SQLALCHEMY:
    class SessionSubstitute:
        """Evaluate these queries against synthetic rows, without SQL execution."""
        def __init__(self):
            self.rows = {User: [User(id=1, email="patient@example.invalid", is_active=True)],
                         Admin: [Admin(id=1, email="admin@example.invalid", is_active=True)],
                         UserToken: [], AdminToken: []}
            self.commit, self.rollback, self.flush = AsyncMock(), AsyncMock(), AsyncMock()
            self.executed = []

        def context(self, model, row):
            context = {model.__tablename__: row}
            if model in (UserToken, AdminToken):
                account, owner = (User, "user_id") if model is UserToken else (Admin, "admin_id")
                context[account.__tablename__] = next((r for r in self.rows[account] if r.id == getattr(row, owner)), None)
            return context

        def value(self, node, context):
            if isinstance(node, BindParameter):
                return node.value
            if isinstance(node, (True_, False_)):
                return isinstance(node, True_)
            row = context[node.table.name]
            return getattr(row, node.key) if row is not None else None

        def matches(self, clause, context):
            if isinstance(clause, BooleanClauseList):
                return all(self.matches(c, context) for c in clause.clauses)
            return clause.operator(self.value(clause.left, context), self.value(clause.right, context))

        async def execute(self, statement):
            self.executed.append(statement)
            is_update = isinstance(statement, Update)
            model = next(m for m in self.rows if m.__tablename__ == statement.table.name) if is_update else statement.column_descriptions[0]["entity"]
            found = []
            for row in self.rows[model]:
                context = self.context(model, row)
                if not all(self.matches(c, context) for c in statement._where_criteria):
                    continue
                if not is_update:
                    if not all(not isinstance(f, Join) or self.matches(f.onclause, context) for f in statement.get_final_froms()):
                        continue
                found.append(row)
                if is_update:
                    for key, value in statement._values.items():
                        setattr(row, key if isinstance(key, str) else key.key, self.value(value, context))
            def scalar():
                if len(found) > 1:
                    raise AssertionError("Query matched multiple sessions")
                return found[0] if found else None
            return SimpleNamespace(scalar_one_or_none=scalar)

        def add(self, row):
            row.id = len(self.rows[type(row)]) + 1
            self.rows[type(row)].append(row)


@unittest.skipUnless(HAS_SQLALCHEMY, "SQLAlchemy unavailable; service/query substitute checks skipped")
class RefreshFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SessionSubstitute()
        self.jwt = JWTSubstitute()
        self.auth = security(jwt=self.jwt)
        self.error = definitions("app/exceptions/http_exceptions.py", {"APIException"},
                                 HTTPException=FakeHTTPException, get_message=lambda message, language: message)["APIException"]
        transaction = asynccontextmanager(definitions("app/db/session.py", {"transaction"})["transaction"])
        common = dict(AuthBase=self.auth, settings=SETTINGS, APIException=self.error, transaction=transaction,
                      select=select, update=update, datetime=datetime, timedelta=timedelta, UTC=UTC)
        client = definitions("app/services/client/auth.py", {"ClientAuthService"}, User=User, UserToken=UserToken, **common)["ClientAuthService"]
        admin = definitions("app/services/backoffice/auth.py", {"BackofficeAuthService"}, Admin=Admin, AdminToken=AdminToken, **common)["BackofficeAuthService"]
        self.realms = [(client, "client", User, UserToken), (admin, "backoffice", Admin, AdminToken)]

    async def login(self, realm):
        service, name, account, model = realm
        return await service.login(self.db, self.db.rows[account][0].email, "test-password")

    async def assert_rejected(self, service, token):
        with self.assertRaises(self.error) as caught:
            await service.refresh_token(self.db, token)
        self.assertEqual(caught.exception.status_code, 401)

    async def test_login_refresh_logout_and_idempotent_logout(self):
        for realm in self.realms:
            service, name, account, model = realm
            with self.subTest(realm=name):
                tokens = await self.login(realm)
                row = self.db.rows[model][-1]
                self.assertEqual(row.token, self.auth.refresh_token_fingerprint(tokens["refresh_token"]))
                self.assertEqual(len(row.token), 64)
                self.assertEqual(self.jwt.claims[tokens["refresh_token"]]["scope"], name + "_refresh")
                refreshed = await service.refresh_token(self.db, tokens["refresh_token"])
                self.assertEqual(self.jwt.claims[refreshed["access_token"]]["scope"], name)
                await service.logout(self.db, tokens["refresh_token"])
                self.assertFalse(row.is_active)
                await self.assert_rejected(service, tokens["refresh_token"])
                commits = self.db.commit.await_count
                await service.logout(self.db, tokens["refresh_token"])
                self.assertEqual(self.db.commit.await_count, commits)

    async def test_cross_realm_and_access_tokens_cannot_refresh_or_revoke(self):
        tokens = [await self.login(realm) for realm in self.realms]
        for i, (service, name, account, model) in enumerate(self.realms):
            for invalid in (tokens[1 - i]["refresh_token"], tokens[i]["access_token"]):
                with self.subTest(realm=name):
                    await self.assert_rejected(service, invalid)
                    await service.logout(self.db, invalid)
                    self.assertTrue(self.db.rows[model][-1].is_active)

    async def test_failed_login_does_not_replace_existing_session(self):
        for realm in self.realms:
            service, name, account, model = realm
            await self.login(realm)
            user = self.db.rows[account][0]
            for reason in ("wrong_password", "disabled_account"):
                with self.subTest(realm=name, reason=reason):
                    user.is_active = reason != "disabled_account"
                    with self.assertRaises(self.error):
                        await service.login(self.db, user.email,
                                            "wrong-password" if reason == "wrong_password" else "test-password")
                    self.assertTrue(self.db.rows[model][-1].is_active)
                    self.assertEqual(len(self.db.rows[model]), 1)

    async def test_expired_revoked_tampered_and_legacy_tokens_rejected(self):
        for realm in self.realms:
            service, name, account, model = realm
            for reason in ("jwt_expired", "db_expired", "revoked", "tampered", "legacy_scope", "legacy_hash"):
                with self.subTest(realm=name, reason=reason):
                    tokens = await self.login(realm)
                    token, row = tokens["refresh_token"], self.db.rows[model][-1]
                    if reason == "jwt_expired":
                        self.jwt.claims[token]["exp"] = datetime.now(UTC) - timedelta(seconds=1)
                    elif reason == "db_expired":
                        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    elif reason == "revoked":
                        row.is_active = False
                    elif reason == "tampered":
                        token += "tampered"
                    elif reason == "legacy_scope":
                        self.jwt.claims[token]["scope"] = "refresh"
                    else:
                        row.token = "old-bcrypt-hash-substitute"
                    await self.assert_rejected(service, token)
                    commits = self.db.commit.await_count
                    await service.logout(self.db, token)
                    self.assertEqual(self.db.commit.await_count, commits)

    async def test_disabled_or_missing_accounts_cannot_refresh(self):
        for realm in self.realms:
            service, name, account, model = realm
            tokens = await self.login(realm)
            self.db.rows[account][0].is_active = False
            await self.assert_rejected(service, tokens["refresh_token"])
            self.db.rows[account].clear()
            await self.assert_rejected(service, tokens["refresh_token"])

    async def test_relogin_invalidates_old_token_and_old_logout_preserves_new(self):
        for realm in self.realms:
            service, name, account, model = realm
            old = await self.login(realm)
            new = await self.login(realm)
            self.assertFalse(self.db.rows[model][-2].is_active)
            await self.assert_rejected(service, old["refresh_token"])
            await service.logout(self.db, old["refresh_token"])
            self.assertTrue(self.db.rows[model][-1].is_active)
            await service.refresh_token(self.db, new["refresh_token"])

    async def test_exact_session_and_subject_matching(self):
        for realm in self.realms:
            service, name, account, model = realm
            tokens = await self.login(realm)
            extra = self.auth.create_refresh_token("1", scope=name + "_refresh")
            await self.assert_rejected(service, extra)
            await service.logout(self.db, extra)
            self.assertTrue(self.db.rows[model][-1].is_active)
            for subject in (None, "invalid", "0", "-1", "2147483648", "2"):
                with self.subTest(realm=name, subject=subject):
                    self.jwt.claims[tokens["refresh_token"]]["sub"] = subject
                    await self.assert_rejected(service, tokens["refresh_token"])


if __name__ == "__main__":
    unittest.main()
