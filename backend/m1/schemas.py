from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import ConfigDict, Field

from backend.shared.models import Id, InputModel, IsoTime, OutputModel, PositiveInt


ReservationStatus = Literal[
    "RESERVED", "CHECKED_IN", "CANCELLED_NO_SHOW", "CANCELLED_MAINTENANCE", "ENDED"
]
Source = Literal["M1", "M2", "M3"]
ScopeType = Literal["GLOBAL", "DEPARTMENT", "SPACE"]


class Scope(InputModel):
    type: ScopeType
    id: str | None


class SpaceOut(OutputModel):
    spaceId: Id
    name: str
    location: str
    isStudyRoom: bool
    ownerKind: Literal["SCHOOL", "DEPARTMENT"]
    departmentId: str | None
    registered: bool
    enabled: bool
    version: PositiveInt


class SeatOut(OutputModel):
    seatId: Id
    seatCode: Id
    spaceId: Id
    registered: bool
    enabled: bool
    socketTags: list[Literal["FIXED", "RAIL"]]
    version: PositiveInt


class TimeRange(InputModel):
    startTime: IsoTime
    endTime: IsoTime


class ReservationOut(OutputModel):
    reservationId: Id
    userId: Id
    spaceId: Id
    seatId: Id
    startTime: IsoTime
    endTime: IsoTime
    status: ReservationStatus
    checkInTime: IsoTime | None
    version: PositiveInt


class SearchSeatsInput(InputModel):
    spaceId: str | None = None
    socketTag: Literal["FIXED", "RAIL"] | None = None
    startTime: IsoTime | None = None
    endTime: IsoTime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class SearchSeatItem(OutputModel):
    space: SpaceOut
    seat: SeatOut
    available: bool | None
    reason: str


T = TypeVar("T")


class Page(OutputModel, Generic[T]):
    items: list[T]
    nextCursor: str | None


class ReserveSeatInput(InputModel):
    seatId: Id
    startTime: IsoTime
    endTime: IsoTime
    requestId: Id


class GetCheckInCodeInput(InputModel):
    spaceId: Id


class CheckInCodeOut(OutputModel):
    spaceId: Id
    date: str
    token: Id
    expiresAt: IsoTime


class CheckInInput(InputModel):
    reservationId: Id
    token: Id
    expectedVersion: PositiveInt
    requestId: Id


class GetMyReservationsInput(InputModel):
    status: ReservationStatus | None = None
    startTime: IsoTime | None = None
    endTime: IsoTime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class GetMyReservationInput(InputModel):
    reservationId: Id


class LimitOut(OutputModel):
    source: Source
    businessId: Id
    spaceId: Id
    seatId: str | None
    startTime: IsoTime | None
    endTime: IsoTime | None
    state: Literal["ACTIVE"]
    sourceVersion: int | None


class GetUsageInput(InputModel):
    startTime: IsoTime
    endTime: IsoTime
    spaceId: str | None = None
    seatId: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class UsageOut(OutputModel):
    startTime: IsoTime
    endTime: IsoTime
    checkedAt: IsoTime
    reservations: Page[ReservationOut]
    reservationCount: int
    checkedInCount: int
    limits: list[LimitOut]


class GetRoomAvailabilityInput(InputModel):
    spaceId: Id
    startTime: IsoTime
    endTime: IsoTime


class RoomAvailabilityOut(OutputModel):
    spaceId: Id
    startTime: IsoTime
    endTime: IsoTime
    canOccupy: bool
    blockedBy: list[Literal["BASE", "OPENING", "M1", "M2", "M3", "NO_SEATS"]]
    reasons: list[str]
    checkedAt: IsoTime


class ChangeRoomLimitInput(InputModel):
    source: Literal["M2", "M3"]
    businessId: Id
    spaceId: Id
    action: Literal["OCCUPY", "REPAIR", "RELEASE"]
    businessState: Literal["APPROVED", "REJECTED", "TIMEOUT_CANCELLED", "CANCELLED", "REPAIRING", "ACCEPTED"]
    startTime: IsoTime | None
    endTime: IsoTime | None
    sourceVersion: PositiveInt
    requestId: Id


class CurrentLimitOut(OutputModel):
    source: Literal["M2", "M3"]
    businessId: Id
    spaceId: Id
    startTime: IsoTime | None
    endTime: IsoTime | None
    state: Literal["ACTIVE", "RELEASED"]
    sourceVersion: PositiveInt


class RoomLimitOut(OutputModel):
    source: Literal["M2", "M3"]
    businessId: Id
    spaceId: str | None
    maxProcessedVersion: int | None
    currentLimit: CurrentLimitOut | None
    queriedRequestId: str | None
    requestStatus: Literal["SUCCESS", "REJECTED", "UNKNOWN"] | None
    message: str


class GetRoomLimitInput(InputModel):
    source: Literal["M2", "M3"]
    businessId: Id
    requestId: str | None = None


class GetConfigInput(InputModel):
    owner: Literal["M1", "M2", "M3", "D"]


class GetConfigOut(OutputModel):
    owner: Literal["M1", "M2", "M3", "D"]
    config: dict[str, Any]


class UpdateConfigInput(InputModel):
    owner: Literal["M1", "M2", "M3", "D"]
    config: dict[str, Any]
    expectedVersion: PositiveInt
    requestId: Id


class ProxyInput(InputModel):
    model_config = ConfigDict(extra="allow", strict=True)


class ProxyOutput(OutputModel):
    model_config = ConfigDict(extra="allow", strict=True)


class GetOpeningTimesInput(InputModel):
    spaceId: Id


class OpeningTimesOut(OutputModel):
    spaceId: Id
    timeRanges: list[TimeRange]
    version: PositiveInt


class SetOpeningTimesInput(InputModel):
    spaceId: Id
    timeRanges: list[TimeRange]
    expectedVersion: PositiveInt
    requestId: Id
