from __future__ import annotations

from dataclasses import dataclass
from typing import Any


APS_VIEWER_OAUTH_INTEGRATION = "aps-automation-webinar-alejandro"
APS_AUTOMATION_OAUTH_INTEGRATION = "aps-integration-automation-v2"


@dataclass(frozen=True)
class ModelContext:
    token: str
    region: str
    version_urn: str


@dataclass(frozen=True)
class AccAutomationContext:
    token3lo: str
    project_id: str
    input_item_urn: str
    output_folder_id: str
    version_urn: str


def get_token(oauth2_integration: str = APS_VIEWER_OAUTH_INTEGRATION) -> str:
    import viktor as vkt

    integration = vkt.external.OAuth2Integration(oauth2_integration)
    return integration.get_access_token()


def get_model_context(autodesk_file: Any) -> ModelContext:
    if autodesk_file is None:
        raise ValueError("No Autodesk model selected.")

    token = get_token(APS_VIEWER_OAUTH_INTEGRATION)
    region = autodesk_file.get_region(token)
    version = autodesk_file.get_latest_version(token)

    return ModelContext(
        token=token,
        region=region,
        version_urn=version.urn,
    )


def get_acc_automation_context(autodesk_file: Any) -> AccAutomationContext:
    if autodesk_file is None:
        raise ValueError("No Autodesk model selected.")

    from aps_automation_sdk.acc import parent_folder_from_item

    token3lo = get_token(APS_AUTOMATION_OAUTH_INTEGRATION)
    project_id = getattr(autodesk_file, "project_id", None)
    input_item_urn = getattr(autodesk_file, "urn", None)

    if not project_id or not input_item_urn:
        raise ValueError("Missing project id or URN for the selected Autodesk file.")

    version = autodesk_file.get_latest_version(token3lo)
    output_folder_id = parent_folder_from_item(
        project_id=project_id,
        item_id=input_item_urn,
        token=token3lo,
    )

    return AccAutomationContext(
        token3lo=token3lo,
        project_id=project_id,
        input_item_urn=input_item_urn,
        output_folder_id=output_folder_id,
        version_urn=version.urn,
    )
