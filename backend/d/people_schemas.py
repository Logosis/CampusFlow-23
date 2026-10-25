"""D-S08：候选分页与按编号核验/展示的严格输入输出。"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from backend.d.master_schemas import Cursor, MasterId as Identifier
from backend.d.schemas import ScopeInput
from backend.shared.models import InputModel, OutputModel


QualificationPurpose = Literal["APPROVAL", "DISPATCH", "ESCALATION", "ACCEPTANCE"]


class PeopleQueryBase(InputModel):
    purpose: Literal["APPROVAL", "DISPATCH", "ESCALATION", "ACCEPTANCE", "DISPLAY"]
    permissionCodes: list[Identifier]
    targetScope: ScopeInput

    @model_validator(mode="after")
    def validate_qualification(self):
        if self.purpose == "DISPLAY" and self.permissionCodes:
            raise ValueError("DISPLAY requires empty permissionCodes")
        if self.purpose != "DISPLAY" and not self.permissionCodes:
            raise ValueError("Qualification queries require permissionCodes")
        return self


class PeopleCandidatesInput(PeopleQueryBase):
    mode: Literal["CANDIDATES"]
    purpose: QualificationPurpose
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor = Field(default=None)


class PeopleByIdsInput(PeopleQueryBase):
    mode: Literal["BY_IDS"]
    userIds: list[Identifier]


QueryPeopleInput = Annotated[PeopleCandidatesInput | PeopleByIdsInput, Field(discriminator="mode")]
PEOPLE_INPUT = TypeAdapter(QueryPeopleInput)


class PersonOutput(OutputModel):
    userId: Identifier
    displayName: str
    departmentId: Identifier | None
    accountStatus: Literal["ACTIVE", "DISABLED"]
    eligible: bool | None
    matchedPermissionCodes: list[Identifier]


class PeoplePage(OutputModel):
    items: list[PersonOutput]
    nextCursor: Cursor | None


class PeopleByIdsOutput(OutputModel):
    people: list[PersonOutput]


QueryPeopleOutput = PeoplePage | PeopleByIdsOutput
