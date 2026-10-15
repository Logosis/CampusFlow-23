from fastapi import APIRouter, Request
from backend.shared.models import SuccessResponse
from backend.shared.errors import success
from backend.m3 import schemas as s, service

router = APIRouter()


@router.post('/get_report_locations', response_model=SuccessResponse[s.Page[s.Space]])
async def get_report_locations(input: s.LocationsInput, request: Request):
    return success(await service.locations(request, input))


@router.post('/report_fault', response_model=SuccessResponse[s.MutationOutput])
async def report_fault(input: s.ReportInput, request: Request):
    return success(await service.report(request, input))


@router.post('/get_dispatch_candidates', response_model=SuccessResponse[s.Page[s.Person]])
async def get_dispatch_candidates(input: s.CandidatesInput, request: Request):
    return success(await service.candidates(request, input))


@router.post('/dispatch_order', response_model=SuccessResponse[s.MutationOutput])
async def dispatch_order(input: s.DispatchInput, request: Request):
    return success(await service.dispatch(request, input))


@router.post('/get_dispatch_tasks', response_model=SuccessResponse[s.Page[s.Detail]])
async def get_dispatch_tasks(input: s.DispatchTasksInput, request: Request):
    return success(await service.dispatch_tasks(request, input))
