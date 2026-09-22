"""统一认证 / 授权错误：结构化返回 {"error": {"code", "message"}}。"""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class AuthError(HTTPException):
    """认证（401）/ 授权（403）异常，携带机器可读错误码。"""

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.error_code = code


async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """将 AuthError 渲染为统一错误结构。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.error_code, "message": str(exc.detail)}},
    )
