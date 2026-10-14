import getpass
import logging
import sys
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.logging import RichHandler
from f5xc_exporter.api_client import XCClient
from f5xc_exporter.exporter import export_namespace

APP_VERSION = "1.0"
ROOT = Path.cwd()
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = ROOT / "exports"

class RedactToken(logging.Filter):
    def __init__(self):
        super().__init__()
        self.token = ""
    def filter(self, record):
        try:
            msg = record.getMessage()
            if self.token:
                msg = msg.replace(self.token, "[REDACTED_API_TOKEN]")
            record.msg, record.args = msg, ()
        except Exception:
            pass
        return True

def setup_logging(run_dir, console):
    log_dir = Path(run_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    redactor = RedactToken()
    console_handler = RichHandler(console=console, level=logging.DEBUG, show_time=True, show_level=True, show_path=False, markup=False, rich_tracebacks=False, log_time_format="[%Y-%m-%d %H:%M:%S]")
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    console_handler.addFilter(redactor)
    log_path = log_dir / "f5xc-export.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return redactor, log_path

def ask_required(prompt):
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("A value is required.")

def choose_namespace():
    print("\nChoose namespace:")
    print("  1. default  - standard default namespace")
    print("  2. shared   - literal namespace named 'shared'")
    print("  3. custom   - enter the exact namespace name")
    while True:
        choice = input("Select 1, 2, or 3 [1]: ").strip() or "1"
        if choice == "1": return "default"
        if choice == "2": return "shared"
        if choice == "3":
            value = ask_required("Custom namespace: ")
            if any(c in value for c in "/\\?#"):
                print("Namespace cannot contain /, backslash, ?, or #.")
                continue
            return value
        print("Invalid selection. Choose 1, 2, or 3.")

def main():
    # Timestamp is chosen at startup but the run folder is created only after the
    # user confirms the export, immediately before the first request to F5 XC.
    run_id = None
    console = Console()
    console.print("[bold cyan]" + "=" * 78 + "[/bold cyan]")
    console.print(f"[bold bright_cyan]F5 XC Config Exporter CLI v{APP_VERSION}[/bold bright_cyan]")
    console.print("[white]Interactive read-only configuration export[/white]")
    console.print("[bold cyan]" + "=" * 78 + "[/bold cyan]")
    console.print("[bold yellow]SECURITY:[/bold yellow] Your API token is entered locally, hidden while typing, and is not saved to a config file or log.")
    console.print("The token is sent over HTTPS directly to the F5 XC tenant URL you enter to authenticate API calls.")
    console.print("This tool does not send the token to a third-party service. Use a least-privilege read-only token.")
    console.print("[bold cyan]" + "=" * 78 + "[/bold cyan]")
    try:
        tenant = ask_required("F5 XC tenant URL: ")
        if not tenant.startswith(("https://", "http://")):
            tenant = "https://" + tenant
        if not tenant.startswith("https://"):
            print("WARNING: HTTP is not secure; the token could be exposed in transit.")
            if input("Type YES to continue without HTTPS: ").strip() != "YES":
                print("Cancelled. Use HTTPS.")
                return 2
        print("\nEnter API token (input is hidden).")
        token = getpass.getpass("API token: ").strip()
        if not token:
            print("Token cannot be empty.")
            return 2
        namespace = choose_namespace()
        output_base = Path.cwd() / "exports"
        run_dir_preview = output_base / "F5XC-Export-<timestamp>"
        print("\nExport plan")
        print(f"  Tenant:    {tenant}")
        print(f"  Namespace: {namespace}")
        print("  Scope:     all resource types in the built-in catalog")
        print("  Output:    timestamped folder with create, replace, raw, reports, logs, and archive subfolders")
        print(f"  Folder:    {run_dir_preview.resolve()} (timestamp assigned when export starts)")
        if input("Start export? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
            print("Cancelled. No export folder was created and no XC requests were made.")
            return 0

        # Assign the timestamp and create the output tree only after confirmation,
        # immediately before the first request to F5 XC.
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = output_base / f"F5XC-Export-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=False)
        redactor, log_path = setup_logging(run_dir, console)
        redactor.token = token
        logging.info("START | version=%s | python=%s | platform=%s", APP_VERSION, sys.version.split()[0], sys.platform)
        logging.info("Working directory=%s", Path.cwd().resolve())
        logging.info("Run output directory=%s", run_dir.resolve())
        logging.info("Verbose log file=%s", log_path.resolve())
        logging.info("EXPORT PLAN | tenant=%s | namespace=%s | scope=built-in resource catalog", tenant, namespace)
        logging.info("Creating run folder and starting read-only GET requests. No create/update/delete calls will be made.")

        client = XCClient(tenant, token)
        token = None
        manifest, run_dir, zip_path = export_namespace(client, namespace, run_dir, run_id, console)
        console.print("\n[bold cyan]" + "=" * 78 + "[/bold cyan]")
        console.print("[bold green]EXPORT FINISHED[/bold green]")
        console.print(f"[bold]Namespace:[/bold]             {namespace}")
        console.print(f"[bold]Objects queued:[/bold]        {manifest['objects_discovered']}")
        console.print(f"[green]Raw objects exported:[/green]  {manifest['objects_exported']}")
        console.print(f"[green]Create payloads:[/green]       {manifest['create_payloads_exported']}")
        console.print(f"[green]Replace payloads:[/green]      {manifest['replace_payloads_exported']}")
        console.print(f"[yellow]Skipped objects:[/yellow]      {len(manifest['skipped_objects'])}")
        console.print(f"[yellow]Unavailable objects:[/yellow]  {len(manifest['unavailable_objects'])}")
        console.print(f"[yellow]Namespace warnings:[/yellow]   {len(manifest['namespace_scope_warnings'])}")
        console.print(f"[red]Collection failures:[/red]    {len(manifest['collection_failures'])}")
        console.print(f"[red]Object failures:[/red]        {len(manifest['object_failures'])}")
        console.print(f"[red]Format failures:[/red]        {len(manifest['format_failures'])}")
        console.print(f"[dim]Timestamped run folder:[/dim] {run_dir.resolve()}")
        console.print(f"[dim]ZIP archive:[/dim]             {zip_path.resolve()}")
        console.print(f"[dim]Detailed log:[/dim]            {log_path.resolve()}")
        if manifest["collection_failures"] or manifest["object_failures"] or manifest["unavailable_objects"] or manifest["format_failures"] or manifest["namespace_scope_warnings"]:
            console.print("[bold yellow]WARNING:[/bold yellow] Review the reports for unavailable objects, namespace-scope inconsistencies, or request failures.")
            logging.warning("Export completed with warnings or errors.")
        else:
            console.print("[green]All eligible catalog objects were processed; this does not prove the catalog covers every tenant resource.[/green]")
        console.print("[bold cyan]" + "=" * 78 + "[/bold cyan]")
        logging.info("END | export completed")
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user (Ctrl+C).")
        print("\nInterrupted. Check the run log if the export had started.")
        return 130
    except Exception:
        logging.exception("Fatal application error")
        print("Fatal error. Review the run log if the timestamped output folder was created.")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
