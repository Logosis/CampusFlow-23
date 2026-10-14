from typing import Annotated, Generic, Literal, TypeVar
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from backend.shared.clock import parse_time

def valid_time(value: str) -> str:
    parse_time(value)
    return value

Id = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
IsoTime = Annotated[str, AfterValidator(valid_time)]

class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class OutputModel(InputModel):
    pass

T = TypeVar("T")

class SuccessResponse(InputModel, Generic[T]):
    ok: Literal[True]
    data: T
    error: None
