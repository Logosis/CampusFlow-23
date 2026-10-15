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


@router.post('/get_work_tasks', response_model=SuccessResponse[s.Page[s.Detail]])
async def get_work_tasks(input: s.WorkTasksInput, request: Request):
    return success(await service.work_tasks(request, input))


@router.post('/start_repair', response_model=SuccessResponse[s.MutationOutput])
async def start_repair(input: s.VersionInput, request: Request):
    return success(await service.mutate(request, input, 'start_repair'))


@router.post('/submit_completion', response_model=SuccessResponse[s.MutationOutput])
async def submit_completion(input: s.CompletionInput, request: Request):
    return success(await service.mutate(request, input, 'submit_completion'))


@router.post('/submit_acceptance', response_model=SuccessResponse[s.MutationOutput])
async def submit_acceptance(input: s.AcceptanceInput, request: Request):
    return success(await service.mutate(request, input, 'submit_acceptance'))


@router.post('/submit_evaluation', response_model=SuccessResponse[s.MutationOutput])
async def submit_evaluation(input: s.EvaluationInput, request: Request):
    return success(await service.mutate(request, input, 'submit_evaluation'))


@router.post('/cancel_work_order', response_model=SuccessResponse[s.MutationOutput])
async def cancel_work_order(input: s.CancelInput, request: Request):
    return success(await service.mutate(request, input, 'cancel_work_order'))


@router.post('/get_work_orders', response_model=SuccessResponse[s.Page[s.WorkOrderOutput]])
async def get_work_orders(input: s.OrdersInput, request: Request):
    return success(await service.orders(request, input))


@router.post('/get_work_order', response_model=SuccessResponse[s.DetailOutput])
async def get_work_order(input: s.OrderInput, request: Request):
    return success(await service.order_detail(request, input))


@router.post('/get_maintenance_stats', response_model=SuccessResponse[s.StatsOutput])
async def get_maintenance_stats(input: s.StatsInput, request: Request):
    return success(await service.stats(request, input))


@router.post('/get_timeout_config', response_model=SuccessResponse[s.ConfigOutput])
async def get_timeout_config(input: s.ConfigInput, request: Request):
    return success(await service.get_config(request))


@router.post('/update_timeout_config', response_model=SuccessResponse[s.ConfigOutput])
async def update_timeout_config(input: s.UpdateConfigInput, request: Request):
    return success(await service.update_config(request, input))
