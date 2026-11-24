"""对拍键契约 API：契约只读接口 + 切契约 + 契约装载/对齐。"""
from fastapi import APIRouter, HTTPException
from app.modules.对拍键契约 import 服务

路由 = APIRouter(prefix="/api/对拍键契约", tags=["对拍键契约"])


@路由.get("/契约")
def api_契约():
    return 服务.契约列表()


@路由.get("/账册")
def api_账册():
    return 服务.账册列表()


@路由.get("/状态")
def api_状态():
    return 服务.状态()


@路由.get("/策略")
def api_策略():
    return {"策略": 服务.策略目录()}


@路由.get("/左轨")
def api_左():
    return 服务.左轨列表()


@路由.get("/右轨")
def api_右():
    return 服务.右轨列表()


@路由.get("/对齐差")
def api_差():
    return 服务.对齐差列表()


@路由.get("/悬账")
def api_悬():
    return 服务.悬账列表()


@路由.post("/切契约")
def api_切(体: dict):
    r = 服务.切契约(体)
    if r.get("判定") in ("基期不符", "契约冲突"):
        raise HTTPException(409, r)
    if r.get("判定") == "参数错":
        raise HTTPException(400, r)
    return r


@路由.post("/左装载")
def api_左装(体: dict):
    return 服务.装左(体)


@路由.post("/右装载")
def api_右装(体: dict):
    return 服务.装右(体)


@路由.post("/对齐")
def api_对齐():
    return 服务.跑对齐()
