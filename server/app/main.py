import botocore.exceptions
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .client import KloudClient
from pathlib import Path
from . import sdk_handle
from .auth import create_access_token, get_user_id
import boto3

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
aws_info = boto3.Session()

clients = dict()  # 수정 필요

##### CORS #####
# 개발 편의를 위해 모든 origin 허용. 배포시 수정 필요

origins = [
    "*"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


##### CORS #####

class KloudLoginForm(BaseModel):
    access_key_public: str
    access_key_secret: str
    region: str


@app.post("/login")
async def login(login_form: KloudLoginForm):  # todo token revoke 목록 확인, refresh token
    try:
        session_instance: boto3.Session = sdk_handle.create_session(access_key_id=login_form.access_key_public,
                                                                    secret_access_key=login_form.access_key_secret,
                                                                    region=login_form.region)
        if await sdk_handle.is_valid_session(session_instance):
            kloud_client = KloudClient(access_key_id=login_form.access_key_public,
                                       session_instance=session_instance)
            clients[login_form.access_key_public] = kloud_client  # todo 현재 KloudClient 객체를 딕셔너리에 저장함. 추후 변동 가능
            token = await create_access_token(data={"user_id": login_form.access_key_public})
            return {"access_token": token}

    except botocore.exceptions.ClientError:
        raise HTTPException(status_code=401, detail="login_failed")
    except botocore.exceptions.InvalidRegionError:
        raise HTTPException(status_code=400, detail="invalid_region")


@app.get("/available_regions")  # 가능한 aws 지역 목록, 가장 기본적이고 보편적인 서비스인 ec2를 기본값으로 요청.
async def get_available_regions():
    return await sdk_handle.get_available_regions()


class InfraInfoReq(BaseModel):  # 보안 확인 필요
    access_token: str


@app.post("/infra_info")
async def infra_info(user_id=Depends(get_user_id)):
    try:
        client: KloudClient = clients[user_id]
    except KeyError:
        raise HTTPException(status_code=404, detail="kloud_client_not_found")
    return await client.get_current_infra_dict()


@app.post("/cost_history_default")
async def cost_history_default(user_id=Depends(get_user_id)):
    try:
        client: KloudClient = clients[user_id]
    except KeyError:
        raise HTTPException(status_code=404, detail="kloud_client_not_found")
    return await client.get_default_cost_history()

# class ResourceInfoReq(BaseModel):
#     id: str
#     resource_id: str


# @app.post("/infra_specific_info")
# async def resource_info(req: ResourceInfoReq):
#     try:
#         client: KloudClient = clients[req.id]
#     except KeyError:
#         raise HTTPException(status_code=404, detail="kloud_client_not_found")
#     return await client.get_resource_info(resource_id=req.resource_id)


class KloudLogoutForm(BaseModel):
    access_token: str


@app.post("/logout")
async def logout(user_id=Depends(get_user_id)):  # todo token revoke 목록
    try:
        clients.pop(user_id)
    except KeyError:
        pass
    finally:
        return "logout_success"
