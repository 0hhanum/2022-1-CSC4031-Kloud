import os
import asyncio

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic.types import Optional
import boto3
import botocore.exceptions

from .boto3_wrappers.kloud_client import KloudClient
from .response_exceptions import UserNotInDBException, CeleryTimeOutError
from . import common_functions
from .auth import create_access_token, get_user_id, request_temp_cred_async, temp_session_create, security, revoke_token
from .config.cellery_app import da_app
from .redis_req import set_cred_to_redis, get_cred_from_redis, delete_cred_from_redis, get_cost_cache, set_cost_cache, \
    delete_cache_from_redis

from fastapi.responses import HTMLResponse
from fastapi import FastAPI, Request

app = FastAPI(
    title="Kloud API",
    contact={
        "name": "Team Kloud",
        "url": "https://github.com/CSID-DGU/2022-1-CSC4031-Kloud"
    }
)

# #### CORS #####
# 개발 편의를 위해 모든 origin 허용. 배포시 수정 필요
origins = [
    "*"
]

if os.environ.get('IS_PRODUCTION') == "true":
    origins = [os.environ.get('FRONT_END_URL'),  # https://something.com
               os.environ.get('API_URL')]  # https://api.something.com

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# #### CORS #####


async def get_user_client(user_id: str = Depends(get_user_id)) -> KloudClient:
    """
    redis에서 임시 자격증명을 가져와 객체를 생성함.
    """
    cred = await get_cred_from_redis(user_id)
    if cred is None:
        raise UserNotInDBException  # 없는 유저
    else:
        session_instance = temp_session_create(cred)
        kloud_client = KloudClient(user_id, session_instance)
        return kloud_client


class KloudLoginForm(BaseModel):
    access_key_public: str
    access_key_secret: str
    region: str


class AccessTokenResponse(BaseModel):
    access_token: str




LOOP_BREAKING_STATE = {'SUCCESS', 'REVOKED', 'FAILURE'}


async def wait_until_done(celery_task: da_app.AsyncResult, interval=0.3, timeout=10.0):
    """
    바람직한 방법은 아닌 것 같으나, 일단 작동은 합니다.
    asyncio.sleep(interval)로, interval(초) 만큼 이벤트 루프의 제어권을 넘깁니다.

    :param celery_task:  send_task 등으로 반환된 AsyncResult 객체
    :param interval:  결과 확인 빈도, 초 단위
    :param timeout:  초 단위
    :return: 셀러리 태스크의 return 값
    """
    time_passed = 0
    while celery_task.state not in LOOP_BREAKING_STATE:
        await asyncio.sleep(interval)
        time_passed += interval
        if time_passed >= timeout:
            celery_task.forget()  # forget() 혹은 get() 하지 않으면 메모리 누수 가능성 있음.
            da_app.control.revoke(celery_task.id)
            raise CeleryTimeOutError
    return celery_task.get()


@app.get("/")
async def health_check():
    return 'healthy_as_hell'


@app.post("/login", response_model=AccessTokenResponse)
async def login(login_form: KloudLoginForm):
    try:
        session_instance: boto3.Session = boto3.Session(aws_access_key_id=login_form.access_key_public,
                                                        aws_secret_access_key=login_form.access_key_secret,
                                                        region_name=login_form.region)
        temp_cred = await request_temp_cred_async(session_instance, login_form.region)

        await set_cred_to_redis(user_id=login_form.access_key_public, cred=temp_cred)  # await 하지 않을시 잠재적 에러 가능성
        token = create_access_token(login_form.access_key_public)
        return {"access_token": token}

    except botocore.exceptions.ClientError:
        raise HTTPException(status_code=401, detail="login_failed")

    except botocore.exceptions.InvalidRegionError:
        raise HTTPException(status_code=400, detail="invalid_region")


@app.post("/logout")
async def logout(user_id=Depends(get_user_id), token=Depends(security)):
    asyncio.create_task(revoke_token(token.credentials))  # 서버에서 발급한 jwt 무효화
    asyncio.create_task(delete_cred_from_redis(user_id))  # 서버에 저장된 aws sts 토큰 삭제
    asyncio.create_task(delete_cache_from_redis(user_id))  # 캐시 제거
    return 'logout success'


@app.get("/available-regions")  # 가능한 aws 지역 목록, 가장 기본적이고 보편적인 서비스인 ec2를 기본값으로 요청.
async def get_available_regions():
    return await common_functions.get_available_regions()


@app.get("/infra/info")
async def infra_info(user_client: KloudClient = Depends(get_user_client)):
    return await user_client.get_current_infra_dict()


@app.get("/infra/tree")
async def infra_tree(user_client: KloudClient = Depends(get_user_client)):
    return await user_client.get_infra_tree()


@app.get("/cost/history/param")
async def cost_history_param(user_id=Depends(get_user_id),
                             granularity: Optional[str] = 'DAILY',
                             days: Optional[int] = 90) -> dict:
    """
    :param user_id:
    :param granularity: HOURLY|DAILY|MONTHLY
    :param days: maximum 180
    :return: dict

    HOURLY 일시 최대 14일 가능
    """
    key = f'{user_id}_{granularity}_{days}'
    cost_history = await get_cost_cache(key)
    if cost_history is None:
        user_client = await get_user_client(user_id)
        cost_history: dict = await user_client.get_cost_history(days=days, granularity=granularity)
        asyncio.create_task(set_cost_cache(key, cost_history))
    else:
        print(f'cache hit {user_id=}')
    return cost_history


@app.get("/cost/history/default")
async def cost_history_default(user_id=Depends(get_user_id)):
    """
    deprecated
    cost_history_param 사용할것.
    """
    return await cost_history_param(user_id=user_id)


@app.get("/cost/history/by-resource")
async def cost_history_by_resource(user_id=Depends(get_user_id),
                                   specific: Optional[bool] = False,
                                   granularity: Optional[str] = 'MONTHLY'
                                   ):
    """
    :param user_id:
    :param specific: true|false, default false, usage type and quantity 나누어 세부적으로 출력
    :param granularity: MONTHLY|DAILY|HOURLY
    :return: dict
    """
    user_client = await get_user_client(user_id)
    return await user_client.get_cost_history_by_instances(show_usage_type_and_quantity=specific,
                                                           granularity=granularity)


@app.get("/cost/history/by-service")
async def cost_history_by_service(user_client: KloudClient = Depends(get_user_client), days: Optional[int] = 90):
    return await user_client.get_cost_history_by_service(days=days)


class InstanceStop(BaseModel):
    instance_id: str
    hibernate: bool
    force: bool


@app.post("/mod/instance/stop")
async def stop_instance(req_body: InstanceStop, user_client: KloudClient = Depends(get_user_client)):
    await asyncio.to_thread(user_client.stop_instance,
                            instance_id=req_body.instance_id,
                            hibernate=req_body.hibernate,
                            force=req_body.force)
    return 'request sent'  # todo 요청 결과 반환


class InstanceStart(BaseModel):
    instance_id: str


@app.post("/mod/instance/start")
async def start_instance(req_body: InstanceStart, user_client: KloudClient = Depends(get_user_client)):
    await asyncio.to_thread(user_client.start_instance, instance_id=req_body.instance_id)

    return 'request sent'


@app.get("/cost/trend/similarity")
async def pattern_finder(user_client=Depends(get_user_id), token=Depends(security)):
    task = da_app.send_task("/cost/trend/similarity", [token.credentials])
    return await wait_until_done(task)  # 비동기 실행, 결과값 체크 예시

@app.get("/cost/trend/prophet")
async def pattern_finder2(user_client=Depends(get_user_id), token=Depends(security),
                                yearly_seasonality :  Optional[bool] = True,
                                weekly_seasonality :  Optional[bool] = True,
                                daily_seasonality : Optional[bool] =True,
                                n_changepoints : Optional[int] = 10,
                                period : Optional[int] = 5):
    """
    yearly_seasonality : 연 계절성
    weekly_seasonality : 주 계절성 
    daily_seasonality : 일 계절성
    changepoint_prior_scale : changepoint(trend) 의 유연성 조절
    n_changepoints : 트렌드가 변하는 changepoint 의 개수(갑작스럽게 변하는 시점의 수)
    period : 예측 일수
    """
    task = da_app.send_task("/cost/trend/prophet", [token.credentials, yearly_seasonality, weekly_seasonality, daily_seasonality, n_changepoints, period])
    return await wait_until_done(task, timeout = 1000)  # 비동기 실행, 결과값 체크 예시


@app.get("/res/{job_id}")
async def check(job_id):
    return da_app.AsyncResult(job_id).get()  # 예시로, blocking io를 발생시킴.
