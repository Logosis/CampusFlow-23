"""D-S12：固定认证与通知渠道，仅允许调整额外重试次数。"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from backend.d.master_schemas import MasterId
from backend.shared.models import InputModel, OutputModel, PositiveInt


class GetPublicConfigInput(InputModel):
    operation: Literal["GET"]


class UpdatePublicConfigInput(InputModel):
    operation: Literal["UPDATE"]
    authMethod: Literal["PASSWORD"]
    notificationChannel: Literal["EMAIL"]
    notificationRetryLimit: Annotated[int, Field(ge=0, le=5)]
    expectedVersion: PositiveInt
    requestId: MasterId


PublicConfigInput = Annotated[
    GetPublicConfigInput | UpdatePublicConfigInput, Field(discriminator="operation"),
]
CONFIG_INPUT = TypeAdapter(PublicConfigInput)


class PublicConfigOutput(OutputModel):
    authMethod: Literal["PASSWORD"]
    notificationChannel: Literal["EMAIL"]
    notificationRetryLimit: Annotated[int, Field(ge=0, le=5)]
    version: PositiveInt
