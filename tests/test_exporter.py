import json
import tempfile
import unittest
from pathlib import Path
from io import StringIO

from rich.console import Console

from f5xc_exporter.api_client import XCAPIError
from f5xc_exporter.exporter import export_namespace


class FakeClient:
    base_url = "https://tenant.example.invalid"

    def __init__(self):
        self.list_calls = []
        self.detail_calls = []

    def list_collection(self, namespace, collection, api_root):
        self.list_calls.append((namespace, collection, api_root))
        if collection == "service_policys":
            return {"items": [
                {"metadata": {"name": "ves-io-allow-all", "namespace": "default"}},
                {"metadata": {"name": "ves-io-next-policy-set", "namespace": "default"}},
                {"metadata": {"name": "customer-policy", "namespace": "default"}},
                {"metadata": {"name": "summary-from-shared", "namespace": "shared"}},
            ]}
        if collection == "malicious_user_mitigations":
            return {"items": [
                {"metadata": {"name": "ves-io-default-malicious-user-mitigation", "namespace": "default"}}
            ]}
        if collection == "global_log_receivers":
            # Deliberately lacks namespace metadata, like a list summary may do.
            return {"items": [{"metadata": {"name": "shared-log-receiver"}}]}
        return {"items": []}

    def get_object(self, namespace, collection, name, api_root, response_format=None):
        self.detail_calls.append((namespace, collection, name, response_format))
        if name == "shared-log-receiver":
            raise XCAPIError("HTTP 404: object not found in selected namespace", 404)
        return {"metadata": {"name": name, "namespace": namespace}, "spec": {"demo": True},
                "requested_format": response_format or "raw"}


class ExporterNamespaceScopeTests(unittest.TestCase):
    def test_builtin_skip_namespace_filter_and_no_cross_namespace_probe(self):
        client = FakeClient()
        console = Console(file=StringIO(), force_terminal=False, no_color=True, width=140)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            manifest, _, zip_path = export_namespace(client, "default", run_dir, "test-run", console)

            skipped = {(item["name"], item["reason"]) for item in manifest["skipped_objects"]}
            self.assertIn(("ves-io-allow-all", "known_builtin_object"), skipped)
            self.assertIn(("ves-io-next-policy-set", "known_builtin_object"), skipped)
            self.assertIn(("ves-io-default-malicious-user-mitigation", "known_builtin_object"), skipped)
            self.assertIn(("summary-from-shared", "summary_namespace_mismatch"), skipped)

            self.assertEqual(manifest["objects_discovered"], 2)
            self.assertEqual(manifest["objects_exported"], 1)
            self.assertEqual(len(manifest["unavailable_objects"]), 1)
            self.assertEqual(len(manifest["namespace_scope_warnings"]), 1)
            self.assertTrue(zip_path.exists())
            self.assertTrue((run_dir / "reports" / "skipped-objects.json").exists())
            self.assertTrue((run_dir / "reports" / "namespace-scope-warnings.json").exists())

            # The exporter must never silently query another namespace.
            self.assertTrue(all(call[0] == "default" for call in client.list_calls))
            self.assertTrue(all(call[0] == "default" for call in client.detail_calls))

            report = json.loads((run_dir / "reports" / "skipped-objects.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(report), 4)


if __name__ == "__main__":
    unittest.main()
