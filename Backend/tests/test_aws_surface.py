"""AWS removal checks using only the standard library; no app imports or network.

Route checks are static. Handler checks execute source definitions with fake
authentication context, responses and AWS clients; they are not HTTP/E2E tests.
Run from Backend: python -B -m unittest discover -s tests -p test_aws_surface.py -v
"""
import ast
from datetime import datetime
import mimetypes
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4


BACKEND = Path(__file__).resolve().parents[1]
CLIENT_AWS = "app/api/client/v1/aws.py"


def tree(relative):
    return ast.parse((BACKEND / relative).read_text(encoding="utf-8-sig"))


def definitions(relative, names, **namespace):
    """Load selected real definitions without executing imports or decorators."""
    nodes = []
    for node in tree(relative).body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node.decorator_list = []
            nodes.append(node)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    code = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(code, str(BACKEND / relative), "exec"), namespace)
    return namespace


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


class AWSSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def test_sts_capability_removed(self):
        self.assertFalse((BACKEND / "app/api/backoffice/v1/aws.py").exists())
        for relative in (CLIENT_AWS, "app/utils/utils.py"):
            for node in ast.walk(tree(relative)):
                self.assertNotEqual(getattr(node, "name", None), "get_temporary_credentials")
                self.assertNotEqual(getattr(node, "attr", None), "get_session_token")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn("temporary-credentials", node.value)

    def test_main_and_mounted_route_surfaces(self):
        registry = definitions(
            "app/route/router_registry.py",
            {"RouteConfig", "CLIENT_ROUTES", "BACKOFFICE_ROUTES"},
            settings=SimpleNamespace(API_V1_STR="/api/v1"),
        )
        routes = {}
        for group in ("CLIENT_ROUTES", "BACKOFFICE_ROUTES"):
            paths = set()
            for config in registry[group]:
                self.assertNotEqual(config.module_path, "app.api.backoffice.v1.aws")
                for node in tree(config.module_path.replace(".", "/") + ".py").body:
                    for decorator in getattr(node, "decorator_list", []):
                        if (isinstance(decorator, ast.Call)
                                and isinstance(decorator.func, ast.Attribute)
                                and decorator.func.attr in {"get", "post", "put", "delete", "patch"}):
                            paths.add(config.prefix + ast.literal_eval(decorator.args[0]))
            routes[group] = paths

        main_calls = {ast.unparse(n) for n in ast.walk(tree("app/route/route.py")) if isinstance(n, ast.Call)}
        docs_calls = {ast.unparse(n) for n in ast.walk(tree("app/configs/docs_apps.py")) if isinstance(n, ast.Call)}
        for group in ("client", "backoffice"):
            call = f"register_routes(app, get_{group}_routes())"
            self.assertIn(call, main_calls)
            self.assertIn(call, docs_calls)
            self.assertIn(f"app.mount('/{group}', {group}_docs_app)", main_calls)

        client = routes["CLIENT_ROUTES"]
        backoffice = routes["BACKOFFICE_ROUTES"]
        exposed = client | backoffice | {"/client" + p for p in client} | {"/backoffice" + p for p in backoffice}
        for path in (
            "/api/v1/aws/temporary-credentials",
            "/client/api/v1/aws/temporary-credentials",
            "/api/v1/backoffice/aws/temporary-credentials",
            "/backoffice/api/v1/backoffice/aws/temporary-credentials",
        ):
            self.assertNotIn(path, exposed)
        for prefix in ("", "/client"):
            self.assertIn(prefix + "/api/v1/aws/presigned-upload-url", exposed)
            self.assertIn(prefix + "/api/v1/aws/presigned-download-url", exposed)

    def test_presigned_dependencies_and_s3_imports_preserved(self):
        handlers = [n for n in tree(CLIENT_AWS).body if isinstance(n, ast.AsyncFunctionDef)]
        self.assertEqual({n.name for n in handlers}, {"generate_presigned_upload_url", "generate_presigned_download_url"})
        for handler in handlers:
            defaults = dict(zip(handler.args.args[-len(handler.args.defaults):], handler.args.defaults))
            self.assertEqual(
                {arg.arg: ast.unparse(value) for arg, value in defaults.items()}["current_user"],
                "Depends(get_current_user)",
            )
        imports = {ast.unparse(n) for n in tree("app/utils/utils.py").body if isinstance(n, (ast.Import, ast.ImportFrom))}
        self.assertIn("import boto3", imports)
        self.assertIn("from botocore.exceptions import ClientError", imports)

    def handlers(self):
        settings = SimpleNamespace(AWS_ACCESS_KEY_ID="test-only", AWS_SECRET_ACCESS_KEY="test-only",
                                   AWS_REGION="us-east-1", AWS_BUCKET_NAME="test-bucket",
                                   AWS_ENDPOINT="https://example.invalid")
        sdk = Mock()
        sdk.generate_presigned_url.return_value = "https://example.invalid/presigned"
        boto3 = SimpleNamespace(client=Mock(return_value=sdk))
        error = definitions("app/exceptions/http_exceptions.py", {"APIException"},
                            HTTPException=FakeHTTPException, get_message=lambda message, language: message)["APIException"]
        common = dict(settings=settings, boto3=boto3, ClientError=RuntimeError,
                      APIException=error, mimetypes=mimetypes, uuid4=uuid4, datetime=datetime)
        service = definitions("app/services/common/s3_service.py", {"S3Service"}, **common)["S3Service"]()
        handlers = definitions(CLIENT_AWS, {"generate_presigned_upload_url", "generate_presigned_download_url"},
                               Depends=lambda dependency: dependency, get_current_user=object(),
                               s3_service=service, APIException=error,
                               ApiResponse=SimpleNamespace(success=lambda data: data))
        return handlers, sdk, common

    async def test_presigned_upload_and_download_with_fake_aws(self):
        handlers, sdk, _ = self.handlers()
        request = SimpleNamespace(file_name="report.pdf", file_type="application/pdf", file_size=1024,
                                  module=None, module_id=None, sub_path=None)
        result = await handlers["generate_presigned_upload_url"](request, SimpleNamespace(id=7))
        self.assertTrue(result["file_key"].startswith("users/7/"))
        self.assertEqual(result["presigned_url"], "https://example.invalid/presigned")
        sdk.generate_presigned_url.assert_called_once_with(
            "put_object", Params={"Bucket": "test-bucket", "Key": result["file_key"], "ContentType": "application/pdf"},
            ExpiresIn=900, HttpMethod="PUT")
        sdk.reset_mock()
        result = await handlers["generate_presigned_download_url"]("users/7/report.pdf", SimpleNamespace(id=7))
        self.assertEqual(result["download_url"], "https://example.invalid/presigned")
        sdk.generate_presigned_url.assert_called_once_with(
            "get_object", Params={"Bucket": "test-bucket", "Key": "users/7/report.pdf"}, ExpiresIn=3600)

    async def test_invalid_upload_and_foreign_download_never_reach_aws(self):
        handlers, sdk, common = self.handlers()
        for filename, size in (("report.exe", 1024), ("report.pdf", 10 * 1024 * 1024 + 1)):
            with self.subTest(filename=filename, size=size), self.assertRaises(common["APIException"]) as caught:
                await handlers["generate_presigned_upload_url"](
                    SimpleNamespace(file_name=filename, file_size=size), SimpleNamespace(id=7))
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(common["APIException"]) as caught:
            await handlers["generate_presigned_download_url"]("users/8/report.pdf", SimpleNamespace(id=7))
        self.assertEqual(caught.exception.status_code, 403)
        sdk.generate_presigned_url.assert_not_called()

    async def test_utils_s3_handler_still_uses_s3(self):
        _, sdk, common = self.handlers()
        handler = definitions("app/utils/utils.py", {"S3Handler"}, **common)["S3Handler"]("test-bucket")
        result = await handler.upload_file("unused-test-path", "report.pdf")
        self.assertTrue(result.startswith("https://example.invalid/uploads/"))
        sdk.upload_file.assert_called_once()
        self.assertEqual(sdk.upload_file.call_args.args[0], "unused-test-path")
        self.assertEqual(sdk.upload_file.call_args.args[1], "test-bucket")
        common["boto3"].client.assert_called_with(
            "s3", aws_access_key_id="test-only", aws_secret_access_key="test-only", region_name="us-east-1")


if __name__ == "__main__":
    unittest.main()
