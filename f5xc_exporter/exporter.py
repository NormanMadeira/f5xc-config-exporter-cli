import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
    TaskProgressColumn, MofNCompleteColumn,
)
from rich.table import Table

from .api_client import XCAPIError
from .resource_registry import RESOURCE_TYPES

LOG = logging.getLogger("f5xc.export")


def safe_name(value):
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "unnamed")[:160]


def extract_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "objects", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def get_name(obj):
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata")
    if isinstance(meta, dict) and meta.get("name"):
        return str(meta["name"])
    return str(obj["name"]) if obj.get("name") else None


def get_declared_namespace(obj):
    """Return an explicitly declared namespace, if the list summary provides one.

    F5 XC summary shapes vary by resource. Missing namespace metadata is not
    treated as a mismatch; the selected namespace remains enforced in the URL.
    """
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    for container, key in ((meta, "namespace"), (meta, "namespace_name"),
                           (obj, "namespace"), (obj, "namespace_name")):
        value = container.get(key) if isinstance(container, dict) else None
        if isinstance(value, dict):
            value = value.get("name") or value.get("namespace")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def export_namespace(client, namespace, run_dir, run_id, console):
    """Inventory all catalog collections first, then export the inventory with a live progress display."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for folder in ("create", "replace", "raw", "reports", "logs", "archive"):
        (run_dir / folder).mkdir(parents=True, exist_ok=True)

    manifest = {
        "tool": "F5 XC Config Exporter CLI", "version": "1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id, "tenant": client.base_url, "namespace": namespace,
        "mode": "all objects in the built-in resource catalog",
        "catalog_entries_attempted": len(RESOURCE_TYPES), "collection_requests": 0,
        "objects_discovered": 0, "objects_exported": 0,
        "create_payloads_exported": 0, "replace_payloads_exported": 0,
        "collection_failures": [], "object_failures": [], "unavailable_objects": [],
        "skipped_objects": [], "namespace_scope_warnings": [],
        "format_failures": [], "objects": [], "inventory": [],
        "output_layout": {
            "create": "Create-request payloads grouped by object type",
            "replace": "Replace-request payloads grouped by object type",
            "raw": "Original default GET responses grouped by object type",
            "reports": "Manifest, skipped-object, scope-warning, and error reports",
            "logs": "Verbose run log",
            "archive": "ZIP archive of the run (archive folder excluded from its own contents)"
        },
        "limitations": [
            "The built-in resource catalog is not guaranteed to include every F5 XC configuration API resource.",
            "Create/replace payloads are requested from the F5 XC API using response_format; they are not inferred by rewriting raw GET responses.",
            "A create-compatible payload does not guarantee dependency availability, permission, feature/license availability, or acceptance by every console workflow.",
            "Review all failures and validate restoration in a test namespace before relying on this as a complete DR backup."
        ]
    }

    # Phase 1: preflight inventory. Reuse these exact list responses for export to avoid
    # making a second collection request and to keep displayed counts consistent.
    inventory = []
    LOG.info("PREFLIGHT | listing object counts for all %d catalog categories before downloading objects", len(RESOURCE_TYPES))
    for idx, resource in enumerate(RESOURCE_TYPES, 1):
        LOG.info("PREFLIGHT COLLECTION %d/%d | category=%s | type=%s | endpoint=%s",
                 idx, len(RESOURCE_TYPES), resource.category, resource.label, resource.collection)
        record = {"resource": resource, "items": [], "named_items": [], "count": None,
                  "listed_count": 0, "skipped_count": 0, "status": "pending", "error": None}
        manifest["collection_requests"] += 1
        try:
            response = client.list_collection(namespace, resource.collection, resource.api_root)
            items = extract_items(response)
            named_items = []
            unnamed_count = 0
            skipped_count = 0
            for item_idx, summary in enumerate(items, 1):
                name = get_name(summary)
                if not name:
                    unnamed_count += 1
                    LOG.warning("Preflight: item %d for %s has no name/metadata.name; it cannot be exported", item_idx, resource.label)
                    continue

                declared_namespace = get_declared_namespace(summary)
                if declared_namespace and declared_namespace != namespace:
                    skipped_count += 1
                    skipped = {
                        "namespace": namespace, "type_key": resource.key,
                        "type_label": resource.label, "collection": resource.collection,
                        "name": name, "reason": "summary_namespace_mismatch",
                        "reported_namespace": declared_namespace,
                        "note": "Skipped because the collection summary explicitly identifies a different namespace.",
                    }
                    manifest["skipped_objects"].append(skipped)
                    LOG.warning("SCOPE FILTER | skipped name=%s type=%s selected_namespace=%s reported_namespace=%s",
                                name, resource.key, namespace, declared_namespace)
                    continue

                if name in resource.skip_names:
                    skipped_count += 1
                    skipped = {
                        "namespace": namespace, "type_key": resource.key,
                        "type_label": resource.label, "collection": resource.collection,
                        "name": name, "reason": "known_builtin_object",
                        "note": resource.skip_reason,
                    }
                    manifest["skipped_objects"].append(skipped)
                    LOG.info("BUILT-IN SKIP | namespace=%s type=%s name=%s reason=%s",
                             namespace, resource.key, name, resource.skip_reason)
                    continue

                named_items.append((name, summary))
            record.update(items=items, named_items=named_items, count=len(named_items),
                          listed_count=len(items), skipped_count=skipped_count,
                          status="success", unnamed_count=unnamed_count)
            LOG.info("PREFLIGHT RESULT | %s | eligible=%d | listed=%d | skipped=%d | unnamed=%d",
                     resource.label, len(named_items), len(items), skipped_count, unnamed_count)
        except Exception as exc:
            record.update(status="failed", error=str(exc))
            LOG.exception("PREFLIGHT FAILURE | category=%s | endpoint=%s", resource.label, resource.collection)
            manifest["collection_failures"].append({
                "namespace": namespace, "type_key": resource.key, "type_label": resource.label,
                "collection": resource.collection, "status_code": getattr(exc, "status_code", None),
                "error": str(exc), "phase": "preflight"
            })
        inventory.append(record)
        manifest["inventory"].append({
            "category": resource.category, "type_key": resource.key, "type_label": resource.label,
            "collection": resource.collection, "listed_count": record.get("listed_count"),
            "count": record["count"], "skipped_count": record.get("skipped_count", 0),
            "status": record["status"], "unnamed_items": record.get("unnamed_count", 0),
            "error": record["error"]
        })

    table = Table(title=f"Namespace inventory: {namespace}", show_lines=False, expand=True)
    table.add_column("Category", style="cyan", no_wrap=True)
    table.add_column("Object type", style="white")
    table.add_column("Endpoint", style="dim")
    table.add_column("Eligible", justify="right", style="bold green")
    table.add_column("Skipped", justify="right", style="yellow")
    table.add_column("Status", justify="center")
    total = 0
    unknown = 0
    for item in inventory:
        resource = item["resource"]
        if item["count"] is not None:
            total += item["count"]
            count_text = str(item["count"])
            status_text = "[green]OK[/green]"
        else:
            unknown += 1
            count_text = "?"
            status_text = "[red]FAILED[/red]"
        table.add_row(resource.category, resource.label, resource.collection, count_text,
                      str(item.get("skipped_count", 0)), status_text)
    console.print()
    console.print(table)
    console.print(f"[bold]Eligible objects queued:[/bold] [green]{total}[/green] across {len(RESOURCE_TYPES)} catalog categories")
    console.print(f"[bold]Objects skipped by scope/built-in filters:[/bold] [yellow]{len(manifest['skipped_objects'])}[/yellow]")
    if unknown:
        console.print(f"[yellow]Warning:[/yellow] counts are unknown for {unknown} category/categories because their inventory request failed. See the debug log.")
    console.print("[dim]The counts are based on the built-in resource catalog, not a guarantee that every F5 XC API object type is covered.[/dim]\n")
    LOG.info("PREFLIGHT SUMMARY | known_objects=%d | categories=%d | categories_failed=%d", total, len(RESOURCE_TYPES), unknown)

    # Phase 2: export, keeping a single live progress bar anchored at the bottom of the terminal.
    task_total = sum(len(item["named_items"]) for item in inventory)
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold cyan]Export progress[/bold cyan]"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]elapsed[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        refresh_per_second=8,
        expand=True,
    ) as progress:
        task_id = progress.add_task("Exporting", total=task_total)
        for collection_idx, record in enumerate(inventory, 1):
            resource = record["resource"]
            LOG.info("%s", "=" * 78)
            LOG.info("COLLECTION %d/%d | category=%s | type=%s | endpoint=%s | preflight_count=%s",
                     collection_idx, len(inventory), resource.category, resource.label, resource.collection,
                     record["count"] if record["count"] is not None else "unknown")
            start = time.monotonic()
            if record["status"] != "success":
                LOG.warning("Skipping export for %s because preflight inventory failed", resource.label)
                continue
            LOG.info("Collection result: %d item(s) for %s", record["count"], resource.label)
            for name_idx, (name, summary) in enumerate(record["named_items"], 1):
                manifest["objects_discovered"] += 1
                LOG.info("OBJECT %d/%d | namespace=%s | category=%s | type=%s | name=%s",
                         manifest["objects_discovered"], task_total, namespace, resource.category, resource.key, name)
                obj_start = time.monotonic()
                obj_record = {
                    "namespace": namespace, "type_key": resource.key, "type_label": resource.label,
                    "collection": resource.collection, "name": name,
                    "status": "exported", "formats": {},
                }
                base_rel = Path(safe_name(resource.key)) / f"{safe_name(name)}.json"
                try:
                    raw_data = client.get_object(namespace, resource.collection, name, resource.api_root)
                except XCAPIError as exc:
                    if exc.status_code == 404:
                        # Some collection responses can include names whose detail endpoint no longer
                        # resolves (deleted/stale/system-managed entries). Do not make two more requests.
                        obj_record["status"] = "unavailable_in_detail_api"
                        obj_record["formats"]["raw"] = "not_found"
                        obj_record["elapsed_seconds"] = round(time.monotonic() - obj_start, 3)
                        unavailable = {
                            "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                            "name": name, "status_code": 404, "error": str(exc),
                            "note": "The collection listed this name, but the detail endpoint returned HTTP 404 for the selected namespace. No request was made to another namespace."
                        }
                        manifest["unavailable_objects"].append(unavailable)
                        scope_warning = {
                            "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                            "name": name, "status_code": 404,
                            "note": "Collection/detail scope inconsistency: the selected namespace's collection listed the name, but its detail endpoint did not resolve it. Verify the object's namespace in the F5 XC console. The exporter intentionally does not probe other namespaces."
                        }
                        manifest["namespace_scope_warnings"].append(scope_warning)
                        manifest["objects"].append(obj_record)
                        LOG.warning("OBJECT SKIPPED | %s | detail endpoint returned 404 after collection listing; create/replace requests skipped", name)
                        progress.advance(task_id)
                        continue
                    LOG.exception("RAW FORMAT FAILURE | type=%s namespace=%s name=%s", resource.key, namespace, name)
                    obj_record["formats"]["raw"] = "failed"
                    manifest["object_failures"].append({
                        "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                        "name": name, "format": "raw", "error": str(exc)
                    })
                except Exception as exc:
                    LOG.exception("RAW FORMAT FAILURE | type=%s namespace=%s name=%s", resource.key, namespace, name)
                    obj_record["formats"]["raw"] = "failed"
                    manifest["object_failures"].append({
                        "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                        "name": name, "format": "raw", "error": str(exc)
                    })
                else:
                    raw_rel = Path("raw") / base_rel
                    write_json(run_dir / raw_rel, raw_data)
                    obj_record["raw_file"] = raw_rel.as_posix()
                    obj_record["formats"]["raw"] = "success"
                    LOG.info("RAW FORMAT SUCCESS | %s | file=%s", name, raw_rel.as_posix())

                try:
                    create_data = client.get_object(
                        namespace, resource.collection, name, resource.api_root,
                        response_format="GET_RSP_FORMAT_FOR_CREATE"
                    )
                    create_rel = Path("create") / base_rel
                    write_json(run_dir / create_rel, create_data)
                    obj_record["create_file"] = create_rel.as_posix()
                    obj_record["formats"]["create"] = "success"
                    manifest["create_payloads_exported"] += 1
                    LOG.info("CREATE FORMAT SUCCESS | %s | file=%s", name, create_rel.as_posix())
                except Exception as exc:
                    LOG.exception("CREATE FORMAT FAILURE | type=%s namespace=%s name=%s", resource.key, namespace, name)
                    obj_record["formats"]["create"] = "failed"
                    manifest["format_failures"].append({
                        "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                        "name": name, "format": "create", "error": str(exc)
                    })

                try:
                    replace_data = client.get_object(
                        namespace, resource.collection, name, resource.api_root,
                        response_format="GET_RSP_FORMAT_FOR_REPLACE"
                    )
                    replace_rel = Path("replace") / base_rel
                    write_json(run_dir / replace_rel, replace_data)
                    obj_record["replace_file"] = replace_rel.as_posix()
                    obj_record["formats"]["replace"] = "success"
                    manifest["replace_payloads_exported"] += 1
                    LOG.info("REPLACE FORMAT SUCCESS | %s | file=%s", name, replace_rel.as_posix())
                except Exception as exc:
                    LOG.exception("REPLACE FORMAT FAILURE | type=%s namespace=%s name=%s", resource.key, namespace, name)
                    obj_record["formats"]["replace"] = "failed"
                    manifest["format_failures"].append({
                        "namespace": namespace, "type_key": resource.key, "collection": resource.collection,
                        "name": name, "format": "replace", "error": str(exc)
                    })

                if any(obj_record["formats"].get(fmt) != "success" for fmt in ("raw", "create", "replace")):
                    obj_record["status"] = "partial"
                obj_record["elapsed_seconds"] = round(time.monotonic() - obj_start, 3)
                manifest["objects"].append(obj_record)
                if obj_record["formats"].get("raw") == "success":
                    manifest["objects_exported"] += 1
                LOG.info("OBJECT COMPLETE | %s | status=%s | formats=%s | elapsed=%.3fs",
                         name, obj_record["status"], obj_record["formats"], obj_record["elapsed_seconds"])
                progress.advance(task_id)
            LOG.info("Collection completed | type=%s | elapsed=%.3fs", resource.key, time.monotonic() - start)

    reports_dir = run_dir / "reports"
    write_json(reports_dir / "collection-errors.json", manifest["collection_failures"])
    write_json(reports_dir / "object-errors.json", manifest["object_failures"])
    write_json(reports_dir / "unavailable-objects.json", manifest["unavailable_objects"])
    write_json(reports_dir / "skipped-objects.json", manifest["skipped_objects"])
    write_json(reports_dir / "namespace-scope-warnings.json", manifest["namespace_scope_warnings"])
    write_json(reports_dir / "format-errors.json", manifest["format_failures"])
    manifest_path = reports_dir / "manifest.json"
    write_json(manifest_path, manifest)

    zip_path = run_dir / "archive" / f"F5XC-{safe_name(namespace)}-Export-{run_id}.zip"
    manifest["zip_file"] = str(zip_path.resolve())
    for handler in logging.getLogger().handlers:
        handler.flush()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if not path.is_file() or "archive" in path.relative_to(run_dir).parts:
                continue
            zf.write(path, path.relative_to(run_dir).as_posix())
    manifest["zip_size_bytes"] = zip_path.stat().st_size
    write_json(manifest_path, manifest)
    for handler in logging.getLogger().handlers:
        handler.flush()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if not path.is_file() or "archive" in path.relative_to(run_dir).parts:
                continue
            zf.write(path, path.relative_to(run_dir).as_posix())

    LOG.info("%s", "=" * 78)
    LOG.info("SUMMARY | namespace=%s discovered=%d skipped=%d raw_exported=%d create_payloads=%d replace_payloads=%d collection_failures=%d object_failures=%d unavailable_objects=%d scope_warnings=%d format_failures=%d zip_bytes=%d",
             namespace, manifest["objects_discovered"], len(manifest["skipped_objects"]), manifest["objects_exported"],
             manifest["create_payloads_exported"], manifest["replace_payloads_exported"],
             len(manifest["collection_failures"]), len(manifest["object_failures"]),
             len(manifest["unavailable_objects"]), len(manifest["namespace_scope_warnings"]),
             len(manifest["format_failures"]), manifest["zip_size_bytes"])
    return manifest, run_dir, zip_path
