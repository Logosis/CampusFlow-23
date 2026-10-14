from fastapi.responses import JSONResponse

STATUS = {
    "AUTH_FAILED": 401, "UNAUTHENTICATED": 401, "FORBIDDEN": 403,
    "NOT_FOUND": 404, "CONFLICT": 409, "REQUEST_ID_REUSED": 409,
    "INVALID_ARGUMENT": 422, "UNAVAILABLE": 503, "INTERNAL_ERROR": 500,
}

class ApiError(Exception):
    def __init__(self, code: str, message: str):
        if code not in STATUS:
            raise ValueError("Unregistered error code")
        self.code, self.message = code, message

def success(data):
    return {"ok": True, "data": data, "error": None}

def failure(code: str, message: str):
    return JSONResponse(status_code=STATUS[code], content={
        "ok": False, "data": None, "error": {"code": code, "message": message},
    })
