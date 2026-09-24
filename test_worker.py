"""Dependency-free regression checks for upload and conversion cleanup."""

import ast
import asyncio
import pathlib
import shutil
import tempfile
import unittest
import urllib.parse


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(detail)


class Upload:
    filename = "sample.pdf"
    content_type = "application/pdf"

    def __init__(self, data):
        self.data = data
        self.closed = False

    async def read(self, size):
        data, self.data = self.data[:size], self.data[size:]
        return data

    async def close(self):
        self.closed = True


def load_functions():
    tree = ast.parse(pathlib.Path(__file__).with_name("worker.py").read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"convert", "convert_file"}]
    # The FastAPI decorator is not needed when calling the endpoint directly.
    for node in selected:
        node.decorator_list = []
        if node.name == "convert":
            node.args.defaults = [ast.Constant(value=None)] * len(node.args.defaults)
    namespace = {"pathlib": pathlib, "tempfile": tempfile, "shutil": shutil,
                 "HTTPException": HTTPException, "MAX_BYTES": 25 * 1024 * 1024,
                 "ALLOWED": {".pdf"}, "run": lambda *a: (1, "failed"),
                 "authorize": lambda value: None}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "worker.py", "exec"), namespace)
    return namespace


class ConversionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_functions()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.roots = []
        self.real_mkdtemp = tempfile.mkdtemp

        def make_root(*args, **kwargs):
            root = self.real_mkdtemp(dir=self.temp.name)
            self.roots.append(pathlib.Path(root))
            return root

        self.worker["tempfile"] = type("Temp", (), {"mkdtemp": staticmethod(make_root)})

    def call(self, upload, target="pdfua2"):
        return asyncio.run(self.worker["convert"](upload, target, "unused"))

    def test_size_limit_removes_partial_upload(self):
        self.worker["MAX_BYTES"] = 3
        upload = Upload(b"1234")
        with self.assertRaises(HTTPException) as error:
            self.call(upload)
        self.assertEqual(error.exception.status_code, 413)
        self.assertFalse(self.roots[0].exists())
        self.assertTrue(upload.closed)

    def test_failed_conversion_removes_upload(self):
        upload = Upload(b"%PDF-1.4")
        self.worker["validate_ua2"] = lambda *args: (_ for _ in ()).throw(RuntimeError("validator failed"))
        with self.assertRaisesRegex(RuntimeError, "validator failed"):
            self.call(upload)
        self.assertFalse(self.roots[0].exists())
        self.assertTrue(upload.closed)

    def test_success_leaves_file_until_response_finishes(self):
        class Response:
            def __init__(self, result, **kwargs):
                self.result = result
                self.background = kwargs["background"]

        class Background:
            def __init__(self, callback, *args):
                self.callback, self.args = callback, args

        self.worker.update(FileResponse=Response, BackgroundTask=Background,
                           urllib=urllib)
        upload = Upload(b"%PDF-1.4")
        response = self.call(upload, "original")
        self.assertTrue(pathlib.Path(response.result).exists())
        response.background.callback(*response.background.args)
        self.assertFalse(self.roots[0].exists())
        self.assertTrue(upload.closed)


if __name__ == "__main__":
    unittest.main()
