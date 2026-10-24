"""pages — full-database SQLite snapshot (.amsdb) export / import routes.

This is the recommended full-data path (replaces the legacy ALLEXPORT XLSX
full raw import/export, which is a display format and a fragile transport for
a whole accounting database):

    GET  /import_export/full_db_export
        Downloads a portable SQLite snapshot of the entire live database
        (schema + all rows, users included).  The live data is not changed.

    POST /import_export/full_db_import
        Uploads a snapshot (.amsdb / .db) and performs a full sync:
        the current database is backed up automatically, EVERY data row of the
        current database is deleted first ("clean all data in AMS"),
        then every row of the uploaded file is inserted with its original
        primary keys.  Mode 'append' instead keeps existing rows and only
        inserts new primary keys.

    GET  /import_export/full_db_import_report/<report_name>
        Downloads the JSON report of a previous full-db import.

Only admin/root may use these routes (same rule as the XLSX full raw import).
The whole flow is guarded by the safety toggle FULL_DB_SYNC_ENABLED
(default: enabled).
"""
from __future__ import annotations

import json
import os
import tempfile
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from ._common import *  # noqa: F401,F403  (re-exports import_export_bp etc.)

from full_db_sync.engine import (
    FullDbSyncError,
    export_snapshot,
    import_snapshot,
)


def _full_db_sync_enabled() -> bool:
    val = os.environ.get(
        "FULL_DB_SYNC_ENABLED",
        str(current_app.config.get("FULL_DB_SYNC_ENABLED", "1")),
    )
    return str(val).strip().lower() in ["1", "true", "on", "yes"]


def _target_db_path() -> str:
    return str(current_app.config.get("APP_DB_PATH") or "")


def _snapshot_dir() -> Path:
    base = Path(current_app.instance_path) / "migration"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _full_db_report_dir() -> Path:
    from app.services.import_artifacts import reports_dir

    d = Path(reports_dir())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_admin_root():
    if current_app.config.get("LOGIN_DISABLED"):
        return None
    if getattr(current_user, "role", None) not in ["admin", "root"]:
        return "Only admin or root can run full database export/import.", 403
    return None


@import_export_bp.route("/full_db_export", methods=["GET"])
@login_required
def full_db_export():
    err = _require_admin_root()
    if err:
        return err
    if not _full_db_sync_enabled():
        flash("Full database snapshot export is disabled by the server safety "
              "setting. Set FULL_DB_SYNC_ENABLED=1 to enable it.", "warning")
        return redirect(url_for("import_export.import_export_page"))

    db_path = _target_db_path()
    if not db_path or not Path(db_path).exists():
        flash("No live database file found for this app instance.", "danger")
        return redirect(url_for("import_export.import_export_page"))

    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = _snapshot_dir() / f"AMS_FULL_{stamp}.amsdb"
        report = export_snapshot(
            db_path,
            out_path=out_path,
            meta_extra={
                "requested_by": getattr(current_user, "username", None) or "admin",
                "host": request.host,
            },
        )
    except Exception as e:
        flash(f"Full database export failed: {e}", "danger")
        return redirect(url_for("import_export.import_export_page"))

    try:
        audit_log(
            current_user,
            "export.full_db",
            f"file={Path(report['out_path']).name} "
            f"tables={len(report['tables'])} rows={report['total_rows']} "
            f"bytes={report['bytes']}",
        )
    except Exception:
        pass

    download_name = f"AMS_FULL_{stamp}.amsdb"
    return send_file(
        report["out_path"],
        as_attachment=True,
        download_name=download_name,
        mimetype="application/vnd.sqlite3",
        max_age=0,
    )


@import_export_bp.route("/full_db_import", methods=["POST"])
@login_required
def full_db_import():
    err = _require_admin_root()
    if err:
        return err
    if not _full_db_sync_enabled():
        flash("Full database snapshot import is disabled by the server safety "
              "setting. Set FULL_DB_SYNC_ENABLED=1 to enable it.", "warning")
        return redirect(url_for("import_export.import_export_page"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file uploaded for full database import.", "danger")
        return redirect(url_for("import_export.import_export_page"))
    name = (file.filename or "").lower()
    if not name.endswith((".amsdb", ".db", ".sqlite", ".sqlite3")):
        flash("Please upload a SQLite snapshot file (.amsdb / .db).", "danger")
        return redirect(url_for("import_export.import_export_page"))

    mode = (request.form.get("mode") or "clean_replace").strip().lower()
    if mode not in ("clean_replace", "append"):
        mode = "clean_replace"

    db_path = _target_db_path()
    if not db_path or not Path(db_path).exists():
        flash("No live database file found for this app instance.", "danger")
        return redirect(url_for("import_export.import_export_page"))

    # Persist the upload to disk so the engine can open it with sqlite3.
    upload_dir = Path(current_app.config.get("IMPORT_UPLOADS_DIR")
                      or current_app.instance_path)
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_path = upload_dir / f"full_db_upload_{uuid.uuid4().hex}{Path(file.filename).suffix}"
    file.save(upload_path)

    report_name = None
    report_meta = {}
    try:
        # require_sidecar=False: a browser upload is a single file by nature,
        # so it cannot carry the .report.txt sidecar.  The UI path keeps its
        # own protections (admin-only, engine integrity/FK/parity verification,
        # automatic backup, tamper detection) — the sidecar gate guards the
        # CLI/explicit-path flow where a quarantined *.INCOMPLETE file could
        # otherwise be named directly.
        report = import_snapshot(
            upload_path,
            db_path,
            mode=mode,
            backup_target=True,
            backup_dir=_snapshot_dir(),
            include_users=True,
            require_sidecar=False,
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_name = f"full_db_import_report_{stamp}.json"
        meta = {
            "name": report_name,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": report.get("mode"),
            "status": report.get("verification"),
            "integrity": report.get("integrity"),
            "fk_violations": report.get("fk_violations"),
            "rows_deleted": report.get("rows_deleted_total"),
            "rows_inserted": report.get("rows_inserted_total"),
            "tables_shared": report.get("tables_shared"),
            "source_file": file.filename,
            "backup_path": report.get("backup_path"),
            "count_mismatches": report.get("count_mismatches"),
        }
        report_meta = meta
        # The import itself is already committed here — saving the
        # JSON report is bookkeeping and must never turn a completed
        # import into an error banner.
        report_saved = True
        try:
            report_path = _full_db_report_dir() / report_name
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2,
                          default=str)
        except Exception:
            report_saved = False
            report_name = None
            traceback.print_exc()
        if report_name:
            try:
                session["full_db_import_report"] = report_name
                session["full_db_import_report_meta"] = meta
            except Exception:
                pass
        try:
            audit_log(
                current_user,
                "import.full_db",
                f"mode={mode} deleted={meta['rows_deleted']} "
                f"inserted={meta['rows_inserted']} "
                f"verification={meta['status']} source={file.filename}",
            )
        except Exception:
            pass

        if report["verification"] == "PASS":
            # Re-baseline the startup data-loss guard: a migration/import
            # legitimately changes row counts, and the guard would otherwise
            # refuse to start the app on the next boot (and again on every
            # boot after that) because counts dropped vs the old snapshot.
            try:
                from app.services import health as health_service
                rebased = health_service.rebaseline_after_full_import(
                    f"full_db_import mode={mode} source={file.filename}"
                )
            except Exception:
                rebased = False
            flash(
                f"Full database import complete ({mode}). Cleaned "
                f"{meta['rows_deleted']} rows, inserted {meta['rows_inserted']} rows "
                f"across {meta['tables_shared']} tables. Integrity ok, 0 FK "
                "violations. A backup of the previous database was saved first."
                + ("" if rebased else
                   " NOTE: the startup health baseline could not be refreshed — "
                   "delete instance/health_snapshot.json before the next start, "
                   "otherwise the app may refuse to boot (ALLOW_DB_DROP=1 "
                   "overrides).")
                + ("" if report_saved else
                   " NOTE: the JSON report file could not be written, but the "
                   "import itself completed and was verified."),
                "success",
            )
        else:
            problems = []
            if report.get("integrity") != "ok":
                problems.append("integrity check failed")
            if report.get("fk_violations"):
                problems.append(f"{report['fk_violations']} FK violations")
            if report.get("count_mismatches"):
                problems.append(f"{len(report['count_mismatches'])} table count mismatches")
            flash(
                f"Full database import finished with verification problems "
                f"({'; '.join(problems) or 'see report'}). Download the JSON "
                "report for details.", "danger",
            )
    except FullDbSyncError as e:
        flash(f"Full database import refused: {e}", "danger")
    except Exception:
        traceback.print_exc()
        flash(f"Full database import failed unexpectedly: "
              f"{traceback.format_exc().splitlines()[-1]}", "danger")
    finally:
        try:
            upload_path.unlink(missing_ok=True)
        except OSError:
            pass

    return redirect(
        url_for("import_export.import_export_page")
        if not report_name
        else url_for("import_export.import_export_page", full_db_import_report=report_name)
    )


@import_export_bp.route("/full_db_import_report/<report_name>", methods=["GET"])
@login_required
def full_db_import_report(report_name):
    # The report describes a full database import (table names + row counts)
    # and its two sibling routes are admin/root only.  Without the same guard
    # any signed-in LAN user could download it by guessing the filename.
    err = _require_admin_root()
    if err:
        return err
    safe = Path(report_name).name
    if safe != report_name or not safe.endswith(".json"):
        return "Bad report name", 400
    path = _full_db_report_dir() / safe
    if not path.exists():
        return "Report not found", 404
    return send_file(
        path,
        as_attachment=True,
        download_name=safe,
        mimetype="application/json",
        max_age=0,
    )
