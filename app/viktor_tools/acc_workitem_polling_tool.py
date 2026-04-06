"""Poll and finalize pending ACC automation jobs."""

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ANALYTICAL_JOB_STORAGE_KEY = "pending_acc_analytical_model_job"
FOOTING_JOB_STORAGE_KEY = "pending_acc_footing_job"
PILE_JOB_STORAGE_KEY = "pending_acc_pile_job"
TERMINAL_STATUSES = {"success", "failedUpload", "cancelled"}


class PendingAccJob(BaseModel):
    """Persisted ACC job state needed for later finalization."""

    job_type: Literal[
        "analytical_model_json",
        "footing_acc_automation",
        "pile_acc_automation",
    ]
    workitem_id: str
    project_id: str
    folder_id: str
    file_name: str
    output_storage_id: str
    status: str = "submitted"
    report_url: str | None = None
    output_item_urn: str | None = None
    storage_key: str | None = None
    result_stored: bool = False
    finalized: bool = False


class PollAnalyticalModelAccJobArgs(BaseModel):
    """Polling configuration for the analytical ACC job."""

    wait_seconds: int = Field(
        default=15,
        ge=0,
        le=60,
        description=(
            "Seconds to wait before checking the work item status once. "
            "Use the default 15 seconds for agentic polling loops."
        ),
    )


class PollFootingAccJobArgs(BaseModel):
    """Polling configuration for the footing ACC job."""

    wait_seconds: int = Field(
        default=15,
        ge=0,
        le=60,
        description=(
            "Seconds to wait before checking the work item status once. "
            "Use the default 15 seconds for agentic polling loops."
        ),
    )


class PollPileAccJobArgs(BaseModel):
    """Polling configuration for the pile ACC job."""

    wait_seconds: int = Field(
        default=15,
        ge=0,
        le=60,
        description=(
            "Seconds to wait before checking the work item status once. "
            "Use the default 15 seconds for agentic polling loops."
        ),
    )


def save_pending_job(vkt: Any, storage_key: str, job: PendingAccJob) -> None:
    vkt.Storage().set(
        storage_key,
        data=vkt.File.from_data(job.model_dump_json(indent=2)),
        scope="entity",
    )


def load_pending_job(vkt: Any, storage_key: str) -> PendingAccJob:
    stored_file = vkt.Storage().get(storage_key, scope="entity")
    if not stored_file:
        raise ValueError(f"Missing pending ACC job in Viktor Storage key '{storage_key}'.")
    raw = stored_file.getvalue_binary().decode("utf-8")
    return PendingAccJob.model_validate_json(raw)


def _build_output_parameter(job: PendingAccJob) -> Any:
    from aps_automation_sdk import ActivityOutputParameterAcc

    if job.job_type == "analytical_model_json":
        output_acc = ActivityOutputParameterAcc(
            name="exportJson",
            localName="analytical_export.json",
            verb="put",
            description="Analytical model JSON output",
            project_id=job.project_id,
            folder_id=job.folder_id,
            file_name=job.file_name,
        )
    elif job.job_type == "footing_acc_automation":
        output_acc = ActivityOutputParameterAcc(
            name="resultModel",
            localName="result.rvt",
            verb="put",
            description="Footing automation output model",
            project_id=job.project_id,
            folder_id=job.folder_id,
            file_name=job.file_name,
        )
    elif job.job_type == "pile_acc_automation":
        output_acc = ActivityOutputParameterAcc(
            name="resultModel",
            localName="result.rvt",
            verb="put",
            description="Output Revit model",
            project_id=job.project_id,
            folder_id=job.folder_id,
            file_name=job.file_name,
        )
    else:
        raise ValueError(f"Unsupported ACC job type '{job.job_type}'.")

    output_acc._storage_id = job.output_storage_id
    if job.output_item_urn:
        output_acc._item_lineage_urn = job.output_item_urn
    return output_acc


def _finalize_output_item(
    vkt: Any,
    storage_key: str,
    job: PendingAccJob,
    *,
    token3lo: str,
) -> PendingAccJob:
    output_acc = _build_output_parameter(job)

    if not job.output_item_urn:
        created_item = output_acc.create_acc_item(token3lo)
        job.output_item_urn = ((created_item or {}).get("data") or {}).get("id")
        save_pending_job(vkt, storage_key, job)

    if job.job_type == "analytical_model_json" and not job.result_stored:
        if not job.storage_key:
            raise ValueError("Missing Viktor Storage key for analytical ACC job.")

        with TemporaryDirectory() as tmpdir:
            local_output = Path(tmpdir) / job.file_name
            output_acc.download_to(str(local_output), token3lo)
            parsed_json = json.loads(local_output.read_text(encoding="utf-8"))

        vkt.Storage().set(
            job.storage_key,
            data=vkt.File.from_data(json.dumps(parsed_json, indent=2)),
            scope="entity",
        )
        job.result_stored = True

    job.finalized = True
    save_pending_job(vkt, storage_key, job)
    return job


def _poll_pending_job_once(vkt: Any, storage_key: str, *, wait_seconds: int) -> str:
    from aps_automation_sdk.core import get_workitem_status

    from app.aec import APS_AUTOMATION_OAUTH_INTEGRATION, get_token

    job = load_pending_job(vkt, storage_key)
    token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)

    if wait_seconds:
        time.sleep(wait_seconds)

    status_payload = get_workitem_status(job.workitem_id, token3lo)
    job.status = status_payload.get("status", "unknown")
    job.report_url = status_payload.get("reportUrl")
    save_pending_job(vkt, storage_key, job)

    if job.status not in TERMINAL_STATUSES:
        return (
            f"ACC work item '{job.workitem_id}' is still '{job.status}'. "
            f"Report URL: {job.report_url or 'n/a'}."
        )

    if job.status != "success":
        job.finalized = True
        save_pending_job(vkt, storage_key, job)
        return (
            f"ACC work item '{job.workitem_id}' finished with status '{job.status}'. "
            f"Report URL: {job.report_url or 'n/a'}."
        )

    if not job.finalized:
        job = _finalize_output_item(vkt, storage_key, job, token3lo=token3lo)

    if job.job_type == "analytical_model_json":
        return (
            "ACC analytical model automation completed successfully. "
            f"Stored JSON in Viktor Storage with key '{job.storage_key}'. "
            f"Output ACC item URN: {job.output_item_urn or 'unknown'}. "
            f"Report URL: {job.report_url or 'n/a'}."
        )

    if job.job_type == "pile_acc_automation":
        return (
            "ACC pile automation completed successfully. "
            f"Output ACC item URN: {job.output_item_urn or 'unknown'}. "
            f"Report URL: {job.report_url or 'n/a'}."
        )

    return (
        "ACC footing automation completed successfully. "
        f"Output ACC item URN: {job.output_item_urn or 'unknown'}. "
        f"Report URL: {job.report_url or 'n/a'}."
    )


async def poll_pile_acc_job_func(_ctx: Any, args: str) -> str:
    payload = PollPileAccJobArgs.model_validate_json(args or "{}")

    try:
        import viktor as vkt
    except ImportError as e:
        return f"Error importing required modules: {e}."

    try:
        return _poll_pending_job_once(
            vkt,
            PILE_JOB_STORAGE_KEY,
            wait_seconds=payload.wait_seconds,
        )
    except Exception as e:
        logger.exception("Unexpected error in poll_pile_acc_job_func")
        return f"Error polling pile ACC job: {type(e).__name__}: {e}"


async def poll_analytical_model_acc_job_func(_ctx: Any, args: str) -> str:
    payload = PollAnalyticalModelAccJobArgs.model_validate_json(args or "{}")

    try:
        import viktor as vkt
    except ImportError as e:
        return f"Error importing required modules: {e}."

    try:
        return _poll_pending_job_once(
            vkt,
            ANALYTICAL_JOB_STORAGE_KEY,
            wait_seconds=payload.wait_seconds,
        )
    except Exception as e:
        logger.exception("Unexpected error in poll_analytical_model_acc_job_func")
        return f"Error polling analytical ACC job: {type(e).__name__}: {e}"


async def poll_footing_acc_job_func(_ctx: Any, args: str) -> str:
    payload = PollFootingAccJobArgs.model_validate_json(args or "{}")

    try:
        import viktor as vkt
    except ImportError as e:
        return f"Error importing required modules: {e}."

    try:
        return _poll_pending_job_once(
            vkt,
            FOOTING_JOB_STORAGE_KEY,
            wait_seconds=payload.wait_seconds,
        )
    except Exception as e:
        logger.exception("Unexpected error in poll_footing_acc_job_func")
        return f"Error polling footing ACC job: {type(e).__name__}: {e}"


def poll_analytical_model_acc_job_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="poll_analytical_model_acc_job",
        description=(
            "Wait about 15 seconds by default, then check the latest submitted ACC analytical model work item once. "
            "If it finished successfully, finalize the ACC output file, download the JSON, "
            "and store it in Viktor Storage. Designed for agentic polling loops. "
            "Before calling this tool in a loop, the agent should first emit a short user-facing "
            "assistant message starting with 'Progress:'."
        ),
        params_json_schema=PollAnalyticalModelAccJobArgs.model_json_schema(),
        on_invoke_tool=poll_analytical_model_acc_job_func,
    )


def poll_footing_acc_job_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="poll_footing_acc_job",
        description=(
            "Wait about 15 seconds by default, then check the latest submitted ACC footing work item once. "
            "If it finished successfully, finalize the ACC output file in ACC. "
            "Designed for agentic polling loops. Before calling this tool in a loop, the agent should "
            "first emit a short user-facing assistant message starting with 'Progress:'."
        ),
        params_json_schema=PollFootingAccJobArgs.model_json_schema(),
        on_invoke_tool=poll_footing_acc_job_func,
    )


def poll_pile_acc_job_tool() -> Any:
    from agents import FunctionTool

    return FunctionTool(
        name="poll_pile_acc_job",
        description=(
            "Wait about 15 seconds by default, then check the latest submitted ACC pile work item once. "
            "If it finished successfully, finalize the ACC output file in ACC. "
            "Designed for agentic polling loops. Before calling this tool in a loop, the agent should "
            "first emit a short user-facing assistant message starting with 'Progress:'."
        ),
        params_json_schema=PollPileAccJobArgs.model_json_schema(),
        on_invoke_tool=poll_pile_acc_job_func,
    )
