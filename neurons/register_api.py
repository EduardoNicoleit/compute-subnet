# The MIT License (MIT)
# Copyright © 2023 Crazydevlegend
# Copyright © 2023 Rapiiidooo
# Copyright @ 2024 Skynet
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# Step 1: Import necessary libraries and modules

import argparse
import base64
import os
import json
import bittensor as bt
from compute.utils.socket import check_port
import torch
import time
from datetime import datetime, timezone
import asyncio
import multiprocessing
import uuid
import requests
import socket
from urllib3.exceptions import InsecureRequestWarning
import urllib3

from neurons.Validator.database.pog import get_pog_specs
urllib3.disable_warnings(InsecureRequestWarning)
from dotenv import load_dotenv
import math
from ipwhois import IPWhois
# Import Compute Subnet Libraries
import RSAEncryption as rsa
from compute.axon import ComputeSubnetSubtensor
from compute.protocol import Allocate
from compute.utils.db import ComputeDb
from compute.utils.parser import ComputeArgPaser
from compute.wandb.wandb import ComputeWandb
from neurons.Validator.database.allocate import (
    select_allocate_miners_hotkey,
    update_allocation_db,
    get_miner_details,
)

# Import FastAPI Libraries
import uvicorn
from fastapi import (
    FastAPI,
    status,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Optional, Union, List
from compute import (TRUSTED_VALIDATORS_HOTKEYS)

# Constants
DEFAULT_SSL_MODE = 2         # 1 for client CERT optional, 2 for client CERT_REQUIRED
DEFAULT_API_PORT = 8903      # default port for the API
DATA_SYNC_PERIOD = 600       # metagraph resync time
ALLOCATE_CHECK_PERIOD = 180  # timeout check period
ALLOCATE_CHECK_COUNT = 20     # maximum timeout count
MAX_NOTIFY_RETRY = 3         # maximum notify count
NOTIFY_RETRY_PERIOD = 15     # notify retry interval
MAX_ALLOCATION_RETRY = 8
PUBLIC_WANDB_NAME = "opencompute"
PUBLIC_WANDB_ENTITY = "neuralinternet"


class UserConfig(BaseModel):
    netuid: str = Field(default="15")
    subtensor_network: str = Field(default="test", alias="subtensor.network")
    subtensor_chain_endpoint: Union[str, None] = Field(
        default="", alias="subtensor.chain_endpoint"
    )
    wallet_name: str = Field(default="validator", alias="wallet.name")
    wallet_hotkey: str = Field(default="default", alias="wallet.hotkey")
    logging_debug: Union[str, None] = Field(default="", alias="logging.debug")


class DeviceRequirement(BaseModel):
    cpu_count: int = Field(default=1, description="CPU count")
    gpu_type: str = Field(default="gpu", description="GPU Name")
    gpu_size: int = Field(default=3, description="GPU size in GB")
    ram: int = Field(default=1, description="RAM size in GB")
    hard_disk: int = Field(default=1, description="Hard disk size in GB")
    timeline: int = Field(default=90, description="Rent Timeline in day")  # timeline=90 day by spec, 30 day by hotkey


class Allocation(BaseModel):
    resource: str = ""
    hotkey: str = ""
    regkey: str = ""
    ssh_ip: str = ""
    ssh_port: int = 4444
    ssh_username: str = ""
    ssh_password: str = ""
    ssh_command: str = ""
    status: str = ""
    ssh_key: str = ""
    uuid_key: str = ""


class DockerRequirement(BaseModel):
    base_image: str = "ubuntu"
    ssh_key: str = ""
    volume_path: str = "/tmp"
    dockerfile: str = ""


class UserInfo(BaseModel):
    user_id: str = ""  # wallet.hokey.ss58address
    user_pass: str = ""  # wallet.public_key hashed value
    jwt_token: str = ""  # jwt token


class ResourceGPU(BaseModel):
    gpu_name: str = ""
    gpu_capacity: int = 0
    gpu_count: int = 1


class Resource(BaseModel):
    hotkey: str = ""
    cpu_count: int = 1
    gpu_name: str = ""
    gpu_capacity: str = ""
    gpu_count: int = 1
    ip: str = ""
    geo: str = ""
    ram: str = "0"
    hard_disk: str = "0"
    allocate_status: str = ""  # "Avail." or "Res."


class Specs(BaseModel):
    details: str = ""


class ResourceQuery(BaseModel):
    gpu_name: Optional[str] = None
    cpu_count_min: Optional[int] = None
    cpu_count_max: Optional[int] = None
    gpu_capacity_min: Optional[float] = None
    gpu_capacity_max: Optional[float] = None
    hard_disk_total_min: Optional[float] = None
    hard_disk_total_max: Optional[float] = None
    ram_total_min: Optional[float] = None
    ram_total_max: Optional[float] = None


# Response Models
class SuccessResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict] = None


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    err_detail: Optional[str] = None


class RegisterAPI:
    def __init__(
            self,
            config: Optional[bt.config] = None,
            wallet: Optional[bt.wallet] = None,
            subtensor: Optional[bt.subtensor] = None,
            dendrite: Optional[bt.dendrite] = None,
            metagraph: Optional[bt.metagraph] = None, # type: ignore
            wandb: Optional[ComputeWandb] = None,
    ):

        # Compose User Config Data with bittensor config
        # Get the config from the user config
        if config is None:
            # Step 1: Parse the bittensor and compute subnet config
            self.config = self._init_config()

            # Set up logging with the provided configuration and directory.
            bt.logging.set_debug(self.config.logging.debug)
            bt.logging.set_trace(self.config.logging.trace)
            bt.logging(config=self.config, logging_dir=self.config.full_path)
            bt.logging.info(
                f"Running validator register for subnet: {self.config.netuid} "
                f"on network: {self.config.subtensor.chain_endpoint} with config:")

            # Log the configuration for reference.
            bt.logging.info(self.config)
            bt.logging.info("Setting up bittensor objects.")

            # The wallet holds the cryptographic key pairs for the validator.
            self.wallet = bt.wallet(config=self.config)
            bt.logging.info(f"Wallet: {self.wallet}")

            self.wandb = ComputeWandb(self.config, self.wallet, "validator.py")

            # The subtensor is our connection to the Bittensor blockchain.
            self.subtensor = ComputeSubnetSubtensor(config=self.config)
            bt.logging.info(f"Subtensor: {self.subtensor}")

            # Dendrite is the RPC client; it lets us send messages to other nodes (axons) in the network.
            self.dendrite = bt.dendrite(wallet=self.wallet)
            bt.logging.info(f"Dendrite: {self.dendrite}")

            # The metagraph holds the state of the network, letting us know about other miners.
            self.metagraph = self.subtensor.metagraph(self.config.netuid)
            bt.logging.info(f"Metagraph: {self.metagraph}")

            # Set the IP address and port for the API server
            if self.config.axon.ip == "[::]":
                self.ip_addr = "0.0.0.0"
            else:
                self.ip_addr = self.config.axon.ip

            if self.config.axon.port is None:
                self.port = DEFAULT_API_PORT
            else:
                self.port = self.config.axon.port

        else:
            self.config = config.copy()
            # Wallet is the keypair that lets us sign messages to the blockchain.
            self.wallet = wallet
            # The subtensor is our connection to the Bittensor blockchain.
            self.subtensor = subtensor
            # Dendrite is the RPC client; it lets us send messages to other nodes (axons) in the network.
            self.dendrite = dendrite
            # The metagraph holds the state of the network, letting us know about other miners.
            self.metagraph = metagraph
            # Initialize the W&B logging
            self.wandb = wandb

            if self.config.axon.ip == "[::]":
                self.ip_addr = "0.0.0.0"
            else:
                self.ip_addr = self.config.axon.ip

            if self.config.axon.port is None:
                self.port = DEFAULT_API_PORT
            else:
                self.port = self.config.axon.port

        if self.config.logging.trace:
            self.app = FastAPI(debug=False)
        else:
            self.app = FastAPI(debug=False, docs_url=None, redoc_url=None)

        load_dotenv()
        self._setup_routes()
        self.process = None
        self.websocket_connection = None
        self.allocation_table = []
        self.checking_allocated = []
        self.notify_retry_table = []
        self.deallocation_notify_url = os.getenv("DEALLOCATION_NOTIFY_URL")
        self.status_notify_url = os.getenv("STATUS_NOTIFY_URL")        

    def _setup_routes(self):
        # Define a custom validation error handler
        @self.app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError):
            # Customize the error response
            errors = exc.errors()
            custom_errors = [{"field": err['loc'][-1], "message": err['msg']} for err in errors]
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "success": False,
                    "message": "Validation Error, Please check the request body.",
                    "err_detail": custom_errors,
                },
            )

        @self.app.on_event("startup")
        async def startup_event():
            """
            This function is called when the application starts. <br>
            It initializes the database connection and other necessary components. <br>
            """
            # Setup the repeated task
            self.metagraph_task = asyncio.create_task(self._refresh_metagraph())
            self.allocate_check_task = asyncio.create_task(self._check_allocation())
            bt.logging.info(f"Register API server is started on https://{self.ip_addr}:{self.port}")

        @self.app.on_event("shutdown")
        async def shutdown_event():
            """
            This function is called when the application stops. <br>
            """
            pass

        # Entry point for the API
        @self.app.get("/", tags=["Root"])
        async def read_root():
            return {
                "message": "Welcome to Compute Subnet Allocation API, Please access the API via endpoint."
            }

        @self.app.websocket(path="/connect", name="websocket")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.websocket_connection = websocket
            bt.logging.info("API: Websocket connection established")
            while True:
                try:
                    # data = await websocket.receive_text()
                    msg = {
                        "type": "keepalive",
                        "payload": {
                            "time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                        }
                    }
                    await websocket.send_text(json.dumps(msg))
                    await asyncio.sleep(30)
                except WebSocketDisconnect:
                    bt.logging.info(f"API: Websocket connection closed")
                    await self.websocket_connection.close()
                    self.websocket_connection = None
                    break

        @self.app.post(
            "/service/allocate_spec",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "Resource was successfully allocated",
                },
                400: {
                    "model": ErrorResponse,
                    "description": "Invalid allocation request",
                },
                401: {
                    "model": ErrorResponse,
                    "description": "Missing authorization",
                },
                404: {
                    "model": ErrorResponse,
                    "description": "Fail to allocate resource",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def allocate_spec(requirements: DeviceRequirement, docker_requirement: DockerRequirement) -> JSONResponse:
            """
            The GPU resource allocate API endpoint. <br>
            requirements: The GPU resource requirements which contain the GPU type, GPU size, ram, hard_disk
            and booking timeline. <br>
            """
            # client_host = request.client.host
            if requirements:
                device_requirement = {
                    "cpu": {"count": requirements.cpu_count},
                    "gpu": {},
                    "hard_disk": {"capacity": requirements.hard_disk * 1024.0 ** 3},
                    "ram": {"capacity": requirements.ram * 1024.0 ** 3},
                }
                if requirements.gpu_type != "" and int(requirements.gpu_size) != 0:
                    device_requirement["gpu"] = {
                        "count": 1,
                        "capacity": int(requirements.gpu_size) * 1000,
                        "type": requirements.gpu_type,
                    }

                # Generate UUID
                uuid_key = str(uuid.uuid1())

                timeline = int(requirements.timeline)
                private_key, public_key = rsa.generate_key_pair()
                run_start = time.time()
                result = await run_in_threadpool(self._allocate_container, device_requirement,
                                                 timeline, public_key, docker_requirement.dict())

                if result["status"] is False:
                    bt.logging.info(f"API: Allocation Failed : {result['msg']}")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "Fail to allocate resource",
                            "err_detail": result["msg"],
                        },
                    )

                run_end = time.time()
                time_eval = run_end - run_start
                # bt.logging.info(f"API: Create docker container in: {run_end - run_start:.2f} seconds")

                result_hotkey = result["hotkey"]
                result_info = result["info"]
                private_key = private_key.encode("utf-8")
                decrypted_info_str = rsa.decrypt_data(
                    private_key, base64.b64decode(result_info)
                )

                # Iterate through the miner specs details to get gpu_name
                db = ComputeDb()
                specs_details = await run_in_threadpool(get_miner_details, db)
                db.close()

                for key, details in specs_details.items():
                    if str(key) == str(result_hotkey) and details:
                        try:
                            gpu_miner = details["gpu"]
                            gpu_name = str(
                                gpu_miner["details"][0]["name"]
                            ).lower()
                            break
                        except (KeyError, IndexError, TypeError):
                            gpu_name = "Invalid details"
                    else:
                        gpu_name = "No details available"

                info = json.loads(decrypted_info_str)
                info["ip"] = result["ip"]
                info["resource"] = gpu_name
                info["regkey"] = public_key
                info["ssh_key"] = docker_requirement.ssh_key
                info["uuid"] = uuid_key

                await asyncio.sleep(1)

                allocated = Allocation()
                allocated.resource = info["resource"]
                allocated.hotkey = result_hotkey
                # allocated.regkey = info["regkey"]
                allocated.ssh_key = info["ssh_key"]
                allocated.ssh_ip = info["ip"]
                allocated.ssh_port = info["port"]
                allocated.ssh_username = info["username"]
                allocated.ssh_password = info["password"]
                allocated.uuid_key = info["uuid"]
                allocated.ssh_command = f"ssh {info['username']}@{result['ip']} -p {str(info['port'])}"

                update_allocation_db(result_hotkey, info, True)
                await self._update_allocation_wandb()
                bt.logging.info(f"API: Resource {result_hotkey} was successfully allocated")

                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "success": True,
                        "message": "Resource was successfully allocated",
                        "data": jsonable_encoder(allocated),
                    },
                )

            else:
                bt.logging.error(f"API: Invalid allocation request")
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "message": "Invalid allocation request",
                        "err_detail": "Invalid requirement, please check the requirements",
                    },
                )

        @self.app.post(
            "/service/allocate_hotkey",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "Resource was successfully allocated",
                },
                400: {
                    "model": ErrorResponse,
                    "description": "Invalid allocation request",
                },
                401: {
                    "model": ErrorResponse,
                    "description": "Missing authorization",
                },
                404: {
                    "model": ErrorResponse,
                    "description": "Fail to allocate resource",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def allocate_hotkey(hotkey: str, ssh_key: Optional[str] = None,
                                  docker_requirement: Optional[DockerRequirement] = None) -> JSONResponse:
            """
            The GPU allocate by hotkey API endpoint. <br>
            User use this API to book a specific miner. <br>
            hotkey: The miner hotkey to allocate the gpu resource. <br>
            """
            if hotkey:
                # client_host = request.client.host
                requirements = DeviceRequirement()
                requirements.gpu_type = ""
                requirements.gpu_size = 0
                requirements.timeline = 30

                # Generate UUID
                uuid_key = str(uuid.uuid1())

                private_key, public_key = rsa.generate_key_pair()
                if ssh_key:
                    if docker_requirement is None:
                        docker_requirement = DockerRequirement()
                        docker_requirement.ssh_key = ssh_key
                    else:
                        docker_requirement.ssh_key = ssh_key

                run_start = time.time()
                result = await run_in_threadpool(self._allocate_container_hotkey, requirements, hotkey,
                                                 requirements.timeline, public_key, docker_requirement.dict())
                if result["status"] is False:
                    bt.logging.error(f"API: Allocation {hotkey} Failed : {result['msg']}")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "Fail to allocate resource",
                            "err_detail": result["msg"],
                        },
                    )

                run_end = time.time()
                time_eval = run_end - run_start
                # bt.logging.info(f"API: Create docker container in: {run_end - run_start:.2f} seconds")

                # Iterate through the miner specs details to get gpu_name
                db = ComputeDb()
                specs_details = await run_in_threadpool(get_miner_details, db)
                for key, details in specs_details.items():
                    if str(key) == str(hotkey) and details:
                        try:
                            gpu_miner = details["gpu"]
                            gpu_name = str(gpu_miner["details"][0]["name"]).lower()
                            break
                        except (KeyError, IndexError, TypeError):
                            gpu_name = "Invalid details"
                    else:
                        gpu_name = "No details available"

                result_hotkey = result["hotkey"]
                result_info = result["info"]
                private_key = private_key.encode("utf-8")
                decrypted_info_str = rsa.decrypt_data(
                    private_key, base64.b64decode(result_info)
                )

                info = json.loads(decrypted_info_str)
                info["ip"] = result["ip"]
                info["resource"] = gpu_name
                info["regkey"] = public_key
                info["ssh_key"] = docker_requirement.ssh_key
                info["uuid"] = uuid_key

                await asyncio.sleep(1)
                allocated = Allocation()
                allocated.resource = info["resource"]
                allocated.hotkey = result_hotkey
                allocated.ssh_key = info["ssh_key"]
                # allocated.regkey = info["regkey"]
                allocated.ssh_ip = info["ip"]
                allocated.ssh_port = info["port"]
                allocated.ssh_username = info["username"]
                allocated.ssh_password = info["password"]
                allocated.uuid_key = info["uuid"]
                allocated.ssh_command = f"ssh {info['username']}@{result['ip']} -p {str(info['port'])}"

                update_allocation_db(result_hotkey, info, True)
                await self._update_allocation_wandb()

                bt.logging.info(f"API: Resource {allocated.hotkey} was successfully allocated")
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "success": True,
                        "message": "Resource was successfully allocated",
                        "data": jsonable_encoder(allocated),
                    },
                )
            else:
                bt.logging.error(f"API: Invalid allocation request")
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "message": "Invalid allocation request",
                        "err_detail": "Invalid hotkey, please check the hotkey",
                    },
                )

        @self.app.post(
            "/service/deallocate",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "Resource deallocated successfully.",
                },
                400: {
                    "model": ErrorResponse,
                    "description": "Deallocation not successfully, please try again.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                403: {
                    "model": ErrorResponse,
                    "description": "An error occurred during de-allocation",
                },
                404: {
                    "model": ErrorResponse,
                    "description": "No allocation details found for the provided hotkey.",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def deallocate(hotkey: str, uuid_key: str, request: Request, notify_flag: bool = False) -> JSONResponse:
            """
            The GPU deallocate API endpoint. <br>
            hotkey: The miner hotkey to deallocate the gpu resource. <br>
            """
            client_host = request.client.host
            # Instantiate the connection to the db
            db = ComputeDb()
            cursor = db.get_cursor()

            try:
                # Retrieve the allocation details for the given hotkey
                cursor.execute(
                    "SELECT details, hotkey FROM allocation WHERE hotkey = ?",
                    (hotkey,),
                )
                row = cursor.fetchone()

                if row:
                    # Parse the JSON string in the 'details' column
                    info = json.loads(row[0])
                    result_hotkey = row[1]

                    username = info["username"]
                    password = info["password"]
                    port = info["port"]
                    ip = info["ip"]
                    regkey = info["regkey"]
                    uuid_key_db = info["uuid"]

                    if uuid_key_db == uuid_key:
                        if hotkey in self.metagraph.hotkeys:
                            index = self.metagraph.hotkeys.index(hotkey)
                            axon = self.metagraph.axons[index]
                            run_start = time.time()
                            retry_count = 0

                            while retry_count < MAX_NOTIFY_RETRY:
                                allocate_class = Allocate(timeline=0, device_requirement={}, checking=False, public_key=regkey)
                                deregister_response = await run_in_threadpool(
                                    self.dendrite.query, axon, allocate_class, timeout=60
                                )
                                run_end = time.time()
                                time_eval = run_end - run_start
                                # bt.logging.info(f"API: Stop docker container in: {run_end - run_start:.2f} seconds")

                                if deregister_response and deregister_response["status"] is True:
                                    bt.logging.info(f"API: Resource {hotkey} deallocated successfully")
                                    break
                                else:
                                    retry_count += 1
                                    bt.logging.info(f"API: Resource {hotkey} no response to deallocated signal - retry {retry_count}")
                                    await asyncio.sleep(1)

                            if retry_count == MAX_NOTIFY_RETRY:
                                bt.logging.error(f"API: Resource {hotkey} deallocated successfully without response.")

                        deallocated_at = datetime.now(timezone.utc)
                        update_allocation_db(result_hotkey, info, False)
                        await self._update_allocation_wandb()

                        # Notify the deallocation event when the client is localhost
                        if notify_flag:
                            response = await self._notify_allocation_status(
                                event_time=deallocated_at,
                                hotkey=hotkey,
                                uuid=uuid_key,
                                event="DEALLOCATION",
                                details=f"deallocate trigger via API interface"
                            )

                            if response:
                                bt.logging.info(f"API: Notify deallocation event is success on {hotkey} ")
                            else:
                                bt.logging.info(f"API: Notify deallocation event is failed on {hotkey} ")
                                self.notify_retry_table.append(
                                    {
                                        "deallocated_at": deallocated_at,
                                        "hotkey": hotkey,
                                        "uuid": uuid_key,
                                        "event": "DEALLOCATION",
                                        "details": "deallocate trigger via API interface"
                                    }
                                )

                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource deallocated successfully.",
                            },
                        )
                    else:
                        bt.logging.error(f"API: Invalid UUID key for {hotkey}")
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "success": False,
                                "message": "Deallocation not successfully, please try again.",
                                "err_detail": "Invalid UUID key",
                            },
                        )

                else:
                    bt.logging.info(f"API: No allocation details found for the provided hotkey")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No allocation details found for the provided hotkey.",
                            "err_detail": "No allocation details found for the provided hotkey.",
                        },
                    )
            except Exception as e:
                bt.logging.error(f"API: An error occurred during deallocation {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred during deallocation.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()

        @self.app.post(
            path="/service/check_miner_status",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "Resource deallocated successfully.",
                },
                403: {
                    "model": ErrorResponse,
                    "description": "An error occurred while retrieving hotkey status.",
                },
            }
        )
        async def check_miner_status(hotkey_list: List[str]) -> JSONResponse:
            checking_list = []
            for hotkey in hotkey_list:
                checking_result = {
                    "hotkey": hotkey,
                    "status": "Not Found"
                }
                for axon in self.metagraph.axons:
                    if axon.hotkey == hotkey:
                        try:
                            register_response = await run_in_threadpool(self.dendrite.query,
                                                                        axon, Allocate(timeline=1, checking=True, ),
                                                                        timeout=60)
                            if register_response:
                                if register_response["status"] is True:
                                    checking_result = {"hotkey": hotkey, "status": "Docker OFFLINE"}
                                else:
                                    checking_result = {"hotkey": hotkey, "status": "Docker ONLINE"}
                            else:
                                checking_result = {"hotkey": hotkey, "status": "Miner NO_RESPONSE"}
                        except Exception as e:
                            bt.logging.error(
                                f"API: An error occur during the : {e}"
                            )
                            checking_result = {"hotkey": hotkey, "status": "Unknown"}
                checking_list.append(checking_result)

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "List hotkey status successfully.",
                    "data": jsonable_encoder(checking_list),
                },
            )

        @self.app.post(path="/service/restart_docker",
                       tags=["Allocation"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "Resource restart successfully.",
                           },
                           403: {
                               "model": ErrorResponse,
                               "description": "An error occurred while restarting docker.",
                           },
                       })
        async def restart_docker(hotkey: str, uuid_key: str) -> JSONResponse:
            # Instantiate the connection to the db
            db = ComputeDb()
            cursor = db.get_cursor()

            try:
                # Retrieve the allocation details for the given hotkey
                cursor.execute(
                    "SELECT details, hotkey FROM allocation WHERE hotkey = ?",
                    (hotkey,),
                )
                row = cursor.fetchone()

                if row:
                    # Parse the JSON string in the 'details' column
                    info = json.loads(row[0])
                    result_hotkey = row[1]

                    username = info["username"]
                    password = info["password"]
                    port = info["port"]
                    ip = info["ip"]
                    regkey = info["regkey"]
                    uuid_key_db = info["uuid"]

                    docker_action = {
                        "action": "restart",
                        "ssh_key": "",
                    }

                    if uuid_key_db == uuid_key:
                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]
                        run_start = time.time()
                        allocate_class = Allocate(timeline=0, device_requirement={}, checking=False, public_key=regkey,
                                                  docker_change=True, docker_action=docker_action)
                        response = await run_in_threadpool(
                            self.dendrite.query, axon, allocate_class, timeout=60
                        )
                        run_end = time.time()
                        time_eval = run_end - run_start
                        # bt.logging.info(f"API: Stop docker container in: {run_end - run_start:.2f} seconds")

                        if response and response["status"] is True:
                            bt.logging.info(f"API: Resource {hotkey} docker restart successfully")
                        else:
                            bt.logging.error(f"API: Resource {hotkey} docker restart without response.")

                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource restarted successfully.",
                            },
                        )
                    else:
                        bt.logging.error(f"API: Invalid UUID key for {hotkey}")
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "success": False,
                                "message": "Restart not successfully, please try again.",
                                "err_detail": "Invalid UUID key",
                            },
                        )

                else:
                    bt.logging.info(f"API: No allocation details found for the provided hotkey")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No allocation details found for the provided hotkey.",
                            "err_detail": "No allocation details found for the provided hotkey.",
                        },
                    )
            except Exception as e:
                bt.logging.error(f"API: An error occurred during restart operation {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred during restart operation.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()

        @self.app.post(path="/service/pause_docker",
                          tags=["Allocation"],
                          response_model=SuccessResponse | ErrorResponse,
                          responses={
                            200: {
                                 "model": SuccessResponse,
                                 "description": "Resource pause successfully.",
                            },
                            403: {
                                 "model": ErrorResponse,
                                 "description": "An error occurred while pausing docker.",
                            },
                          })
        async def pause_docker(hotkey: str, uuid_key: str) -> JSONResponse:
            # Instantiate the connection to the db
            db = ComputeDb()
            cursor = db.get_cursor()

            try:
                # Retrieve the allocation details for the given hotkey
                cursor.execute(
                    "SELECT details, hotkey FROM allocation WHERE hotkey = ?",
                    (hotkey,),
                )
                row = cursor.fetchone()

                if row:
                    # Parse the JSON string in the 'details' column
                    info = json.loads(row[0])
                    result_hotkey = row[1]

                    username = info["username"]
                    password = info["password"]
                    port = info["port"]
                    ip = info["ip"]
                    regkey = info["regkey"]
                    uuid_key_db = info["uuid"]

                    docker_action = {
                        "action": "pause",
                        "ssh_key": "",
                    }

                    if uuid_key_db == uuid_key:
                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]
                        run_start = time.time()
                        allocate_class = Allocate(timeline=0, device_requirement={}, checking=False, public_key=regkey,
                                                  docker_change=True, docker_action=docker_action)
                        response = await run_in_threadpool(
                            self.dendrite.query, axon, allocate_class, timeout=60
                        )
                        run_end = time.time()
                        time_eval = run_end - run_start
                        # bt.logging.info(f"API: Stop docker container in: {run_end - run_start:.2f} seconds")

                        if response and response["status"] is True:
                            bt.logging.info(f"API: Resource {hotkey} docker pause successfully")
                        else:
                            bt.logging.error(f"API: Resource {hotkey} docker pause without response.")

                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource paused successfully.",
                            },
                        )
                    else:
                        bt.logging.error(f"API: Invalid UUID key for {hotkey}")
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "success": False,
                                "message": "Pause not successfully, please try again.",
                                "err_detail": "Invalid UUID key",
                            },
                        )

                else:
                    bt.logging.info(f"API: No allocation details found for the provided hotkey")
                    return JSONResponse(
                        status_code
                        =status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No allocation details found for the provided hotkey.",
                            "err_detail": "No allocation details found for the provided hotkey.",
                        },
                    )
            except Exception as e:
                bt.logging.error(f"API: An error occurred during pause operation {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred during pause operation.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()

        @self.app.post(path="/service/unpause_docker",
                            tags=["Allocation"],
                            response_model=SuccessResponse | ErrorResponse,
                            responses={
                                200: {
                                    "model": SuccessResponse,
                                    "description": "Resource unpause successfully.",
                                },
                                403: {
                                    "model": ErrorResponse,
                                    "description": "An error occurred while unpausing docker.",
                                },
                            })
        async def unpause_docker(hotkey: str, uuid_key: str) -> JSONResponse:
            # Instantiate the connection to the db
            db = ComputeDb()
            cursor = db.get_cursor()

            try:
                # Retrieve the allocation details for the given hotkey
                cursor.execute(
                    "SELECT details, hotkey FROM allocation WHERE hotkey = ?",
                    (hotkey,),
                )
                row = cursor.fetchone()

                if row:
                    # Parse the JSON string in the 'details' column
                    info = json.loads(row[0])
                    result_hotkey = row[1]

                    username = info["username"]
                    password = info["password"]
                    port = info["port"]
                    ip = info["ip"]
                    regkey = info["regkey"]
                    uuid_key_db = info["uuid"]

                    docker_action = {
                        "action": "unpause",
                        "ssh_key": "",
                    }

                    if uuid_key_db == uuid_key:
                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]
                        run_start = time.time()
                        allocate_class = Allocate(timeline=0, device_requirement={}, checking=False, public_key=regkey,
                                                  docker_change=True, docker_action=docker_action)
                        response = await run_in_threadpool(
                            self.dendrite.query, axon, allocate_class, timeout=60
                        )
                        run_end = time.time()
                        time_eval = run_end - run_start
                        # bt.logging.info(f"API: Stop docker container in: {run_end - run_start:.2f} seconds")

                        if response and response["status"] is True:
                            bt.logging.info(f"API: Resource {hotkey} docker unpause successfully")
                        else:
                            bt.logging.error(f"API: Resource {hotkey} docker unpause without response.")

                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource unpaused successfully.",
                            },
                        )
                    else:
                        bt.logging.error(f"API: Invalid UUID key for {hotkey}")
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "success": False,
                                "message": "Unpause not successfully, please try again.",
                                "err_detail": "Invalid UUID key",
                            },
                        )

                else:
                    bt.logging.info(f"API: No allocation details found for the provided hotkey")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No allocation details found for the provided hotkey.",
                            "err_detail": "No allocation details found for the provided hotkey.",
                        },
                    )
            except Exception as e:
                bt.logging.error(f"API: An error occurred during unpause operation {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred during unpause operation.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()

        @self.app.post("/service/exchange_docker_key",
                       tags=["Allocation"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "Resource ssh_key was changed successfully.",
                           },
                           403: {
                               "model": ErrorResponse,
                               "description": "An error occurred while exchanging docker key.",
                           },
                       })
        async def exchange_docker_key(hotkey: str, uuid_key: str, ssh_key: str) -> JSONResponse:
            # Instantiate the connection to the db
            db = ComputeDb()
            cursor = db.get_cursor()

            try:
                # Retrieve the allocation details for the given hotkey
                cursor.execute(
                    "SELECT details, hotkey FROM allocation WHERE hotkey = ?",
                    (hotkey,),
                )
                row = cursor.fetchone()

                if row:
                    # Parse the JSON string in the 'details' column
                    info = json.loads(row[0])
                    result_hotkey = row[1]

                    username = info["username"]
                    password = info["password"]
                    port = info["port"]
                    ip = info["ip"]
                    regkey = info["regkey"]
                    uuid_key_db = info["uuid"]

                    docker_action = {
                        "action": "exchange_key",
                        "ssh_key": ssh_key,
                    }

                    if uuid_key_db == uuid_key:
                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]
                        run_start = time.time()
                        allocate_class = Allocate(timeline=0, device_requirement={}, checking=False, public_key=regkey,
                                                  docker_change=True, docker_action=docker_action)
                        response = await run_in_threadpool(
                            self.dendrite.query, axon, allocate_class, timeout=60
                        )
                        run_end = time.time()
                        time_eval = run_end - run_start
                        # bt.logging.info(f"API: Stop docker container in: {run_end - run_start:.2f} seconds")

                        if response and response["status"] is True:
                            bt.logging.info(f"API: Resource {hotkey} docker ssh_key exchange successfully")
                        else:
                            bt.logging.error(f"API: Resource {hotkey} docker ssh_key exchange without response.")

                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource ssh_key is exchanged successfully.",
                            },
                        )
                    else:
                        bt.logging.error(f"API: Invalid UUID key for {hotkey}")
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "success": False,
                                "message": "Exchange ssh_key not successfully, please try again.",
                                "err_detail": "Invalid UUID key",
                            },
                        )

                else:
                    bt.logging.info(f"API: No allocation details found for the provided hotkey")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No allocation details found for the provided hotkey.",
                            "err_detail": "No allocation details found for the provided hotkey.",
                        },
                    )
            except Exception as e:
                bt.logging.error(f"API: An error occurred during exchange ssh_key operation {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred during exchange ssh_key operation.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()
                
        @self.app.post(
            "/list/allocations_sql",
            tags=["SQLite"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List allocations successfully.",
                },
                401: {
                    "model": ErrorResponse,
                    "description": "Missing authorization token",
                },
                403: {
                    "model": ErrorResponse,
                    "description": "An error occurred while retrieving allocation details",
                },
                404: {
                    "model": ErrorResponse,
                    "description": "There is no allocation available",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def list_allocations() -> JSONResponse:
            """
            The list allocation API endpoint. <br>
            The API will return the current allocation on the validator. <br>
            """
            db = ComputeDb()
            cursor = db.get_cursor()
            allocation_list = []

            try:
                # Retrieve all records from the allocation table
                cursor.execute("SELECT id, hotkey, details FROM allocation")
                rows = cursor.fetchall()

                bt.logging.info(f"API: List Allocation Resources")

                if not rows:
                    bt.logging.info(
                        f"API: No resources allocated. Allocate a resource with validator"
                    )
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No resources found.",
                            "data": "No allocated resources found. Allocate a resource with validator.",
                        },
                    )

                for row in rows:
                    id, hotkey, details = row
                    info = json.loads(details)
                    entry = Allocation()

                    entry.hotkey = hotkey
                    # entry.regkey = info["regkey"]
                    entry.resource = info["resource"]
                    entry.ssh_username = info["username"]
                    entry.ssh_password = info["password"]
                    entry.ssh_port = info["port"]
                    entry.ssh_ip = info["ip"]
                    entry.ssh_command = (
                        f"ssh {info['username']}@{info['ip']} -p {info['port']}"
                    )
                    entry.uuid_key = info["uuid"]
                    entry.ssh_key = info["ssh_key"]
                    # check the online status in self.checking_allocated
                    entry.status = "online"
                    for item in self.checking_allocated:
                        if item.get("hotkey") == hotkey:
                            entry.status = "offline"
                            break
                    allocation_list.append(entry)

            except Exception as e:
                bt.logging.error(
                    f"API: An error occurred while retrieving allocation details: {e}"
                )
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred while retrieving allocation details.",
                        "err_detail": e.__repr__(),
                    },
                )
            finally:
                cursor.close()
                db.close()

            bt.logging.info(f"API: List allocations successfully")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "List allocations successfully.",
                    "data": jsonable_encoder(allocation_list),
                },
            )
        @self.app.post(
            "/list/resources_sql",
            tags=["SQLite"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List resources successfully.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "There is no resource available",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def list_resources(query: ResourceQuery = None,
                                 stats: bool = False,
                                 page_size: Optional[int] = None,
                                 page_number: Optional[int] = None) -> JSONResponse:
            """
            The list resources API endpoint. <br>
            The API will return the current miner resource and their detail specs on the validator. <br>
            query: The query parameter to filter the resources. <br>
            """
            db = ComputeDb()
            specs_details = await run_in_threadpool(get_miner_details, db)
            bt.logging.info(f"API: List resources on compute subnet")

            # check wandb for available hotkeys
            # self.wandb.api.flush()
            running_hotkey = []
            filter_rule = {
                "$and": [
                    {"config.config.netuid": self.config.netuid},
                    {"config.role": "miner"},
                    {"state": "running"},
                ]
            }
            runs = await run_in_threadpool(self.wandb.api.runs,
                                           f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)
            for run in runs:
                run_config = run.config
                run_hotkey = run_config.get("hotkey")
                running_hotkey.append(run_hotkey)

            # Initialize a dictionary to keep track of GPU instances
            resource_list = []
            gpu_instances = {}
            total_gpu_counts = {}

            # Get the allocated hotkeys from wandb
            allocated_hotkeys = await run_in_threadpool(self.wandb.get_allocated_hotkeys, [], False)

            if specs_details:
                # Iterate through the miner specs details and print the table
                for hotkey, details in specs_details.items():
                    if hotkey in running_hotkey:
                        if details:  # Check if details are not empty
                            resource = Resource()
                            try:
                                # Extract GPU details
                                gpu_miner = details["gpu"]
                                gpu_capacity = "{:.2f}".format(
                                    (gpu_miner["capacity"] / 1024)
                                )
                                gpu_name = str(gpu_miner["details"][0]["name"]).lower()
                                gpu_count = gpu_miner["count"]

                                # Extract CPU details
                                cpu_miner = details["cpu"]
                                cpu_count = cpu_miner["count"]

                                # Extract RAM details
                                ram_miner = details["ram"]
                                ram = "{:.2f}".format(
                                    ram_miner["available"] / 1024.0 ** 3
                                )

                                # Extract Hard Disk details
                                hard_disk_miner = details["hard_disk"]
                                hard_disk = "{:.2f}".format(
                                    hard_disk_miner["free"] / 1024.0 ** 3
                                )

                                # Update the GPU instances count
                                gpu_key = (gpu_name, gpu_count)
                                gpu_instances[gpu_key] = (
                                        gpu_instances.get(gpu_key, 0) + 1
                                )
                                total_gpu_counts[gpu_name] = (
                                        total_gpu_counts.get(gpu_name, 0) + gpu_count
                                )

                            except (KeyError, IndexError, TypeError):
                                gpu_name = "Invalid details"
                                gpu_capacity = "N/A"
                                gpu_count = "N/A"
                                cpu_count = "N/A"
                                ram = "N/A"
                                hard_disk = "N/A"
                        else:
                            gpu_name = "No details available"
                            gpu_capacity = "N/A"
                            gpu_count = "N/A"
                            cpu_count = "N/A"
                            ram = "N/A"
                            hard_disk = "N/A"

                        # Allocation status
                        # allocate_status = "N/A"

                        if hotkey in allocated_hotkeys:
                            allocate_status = "reserved"
                        else:
                            allocate_status = "available"

                        add_resource = False
                        # Print the row with column separators
                        resource.hotkey = hotkey

                        try:
                            if gpu_name != "Invalid details" and gpu_name != "No details available":
                                if query is None or query == {}:
                                    add_resource = True
                                else:
                                    if query.gpu_name is not None and query.gpu_name.lower() not in gpu_name:
                                        continue
                                    if (query.gpu_capacity_max is not None and
                                            float(gpu_capacity) > query.gpu_capacity_max):
                                        continue
                                    if (query.gpu_capacity_min is not None and
                                            float(gpu_capacity) < query.gpu_capacity_min):
                                        continue
                                    if (query.cpu_count_max is not None and
                                            int(cpu_count) > query.cpu_count_max):
                                        continue
                                    if (query.cpu_count_min is not None and
                                            int(cpu_count) < query.cpu_count_min):
                                        continue
                                    if (query.ram_total_max is not None and
                                            float(ram) > query.ram_total_max):
                                        continue
                                    if (query.ram_total_min is not None and
                                            float(ram) < query.ram_total_min):
                                        continue
                                    if (query.hard_disk_total_max is not None and
                                            float(hard_disk) > query.hard_disk_total_max):
                                        continue
                                    if (query.hard_disk_total_min is not None and
                                            float(hard_disk) < query.hard_disk_total_min):
                                        continue
                                    add_resource = True

                                if add_resource:
                                    resource.cpu_count = int(cpu_count)
                                    resource.gpu_name = gpu_name
                                    resource.gpu_capacity = float(gpu_capacity)
                                    resource.gpu_count = int(gpu_count)
                                    resource.ram = float(ram)
                                    resource.hard_disk = float(hard_disk)
                                    resource.allocate_status = allocate_status
                                    resource_list.append(resource)
                            resource_list = await map_axon_ip_to_resources(resource_list)
                        except (KeyError, IndexError, TypeError, ValueError) as e:
                            bt.logging.error(f"API: Error occurred while filtering resources: {e}")
                            continue

                if stats:
                    status_counts = {"available": 0, "reserved": 0, "total": 0}
                    try:
                        for item in resource_list:
                            status_code = item.dict()["allocate_status"]
                            if status_code in status_counts:
                                status_counts[status_code] += 1
                                status_counts["total"] += 1
                    except Exception as e:
                        bt.logging.error(f"API: Error occurred while counting status: {e}")
                        status_counts = {"available": 0, "reserved": 0, "total": 0}

                    bt.logging.info(f"API: List resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List resources successfully",
                            "data": jsonable_encoder({"stats": status_counts}),
                        },
                    )
                else:
                    if page_number:
                        page_size = page_size if page_size else 50
                        result = self._paginate_list(resource_list, page_number, page_size)
                    else:
                        result = {
                            "page_items": resource_list,
                            "page_number": 1,
                            "page_size": len(resource_list),
                            "next_page_number": None,
                        }

                    bt.logging.info(f"API: List resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List resources successfully",
                            "data": jsonable_encoder(result),
                        },
                    )

            else:
                bt.logging.info(f"API: There is no resource available")
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "There is no resource available",
                        "err_detail": "No resources found.",
                    },
                )
        async def map_axon_ip_to_resources(resources):
            """
            Map axon IPs to the list of resources based on hotkeys.
            """
            for resource in resources:
                hotkey = resource.hotkey
                if hotkey:
                    for axon in self.metagraph.axons:
                        if axon.hotkey == hotkey:
                            resource.ip = axon.ip
                            obj = IPWhois(resource.ip)
                            result = obj.lookup_rdap()
                            resource.geo = result.get("asn_country_code","Unknown")
                            break
            return resources
        async def get_wandb_running_miners():
            """
            Get the running miners from wandb
            """

            filter_rule = {
                "$and": [
                    {"config.config.netuid": self.config.netuid},
                    {"config.role": "miner"},
                    {"state": "running"},
                ]
            }
            try:
                specs_details = {}
                running_hotkey = []
                runs =  await run_in_threadpool(
                    self.wandb.api.runs,
                    f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}",
                    filter_rule,
                )
                penalized_hotkeys = await run_in_threadpool(
                    self.wandb.get_penalized_hotkeys_checklist, [], False
                )

                # bt.logging.info(penalized_hotkeys)


                for run in runs:
                    run_config = run.config
                    run_hotkey = run_config.get("hotkey")
                    specs = run_config.get("specs")
                    configs = run_config.get("config")
                    is_active = any(axon.hotkey == run_hotkey for axon in self.metagraph.axons)

                    #if is_active:
                        #bt.logging.info(f"DEBUG - This hotkey is active - {run_hotkey}")
                    # check the signature
                    is_penalized = run_hotkey in penalized_hotkeys

                    if (
                        run_hotkey
                        and configs
                        and not is_penalized
                        and is_active
                    ):
                        # bt.logging.info(f"DEBUG - This hotkey is OK - {run_hotkey}")
                        running_hotkey.append(run_hotkey)
                        if specs:
                            specs_details[run_hotkey] = specs
                        else:
                            specs_details[run_hotkey] = {}
                return specs_details , running_hotkey
            except Exception as e:
                bt.logging.error(
                    f"API: An error occurred while retrieving runs from wandb: {e}"
                )
                return {} , []

        @self.app.post(
            "/list/count_all_gpus",
            tags=["WandB"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List resources successfully.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "There is no resource available",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def count_all_gpus() -> JSONResponse:
            """
            Count all GPUs on the compute subnet
            """
            bt.logging.info(f"API: Count Gpus(wandb) on compute subnet")            
            GPU_COUNTS = 0 
            specs_details , running_hotkey = await get_wandb_running_miners()
            try:
                if specs_details:
                    # Iterate through the miner specs details and print the table
                    for hotkey, details in specs_details.items():
                        if details :
                            gpu_miner = details["gpu"]
                            gpu_capacity = "{:.2f}".format(
                                (gpu_miner["capacity"] / 1024)
                            )
                            gpu_name = str(gpu_miner["details"][0]["name"]).lower()
                            gpu_count = gpu_miner["count"]
                            GPU_COUNTS += gpu_count
                    bt.logging.info(f"API: List resources successfully")
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "count": GPU_COUNTS,
                    },
                )
            except Exception as e:
                bt.logging.error(f"API: An error occurred while counting GPUs: {e}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "success": False,
                        "message": "An error occurred while counting GPUs.",
                        "err_detail": e.__repr__(),
                    },
                )
        @self.app.post(
            "/list/count_all_by_model",
            tags=["WandB"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List resources successfully.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "There is no resource available",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def count_all_model(model: str , cpu_count: Optional[int] = None, ram_size: Optional[float] = None) -> JSONResponse:
            """
            Count all GPUs on the compute subnet
            """
            bt.logging.info(f"API: Count Gpus by model(wandb) on compute subnet")            
            counter = 0
            specs_details , running_hotkey = await get_wandb_running_miners()
            try:
                if specs_details:
                    # Iterate through the miner specs details and print the table
                    for hotkey, details in specs_details.items():
                        flag = 0
                        if details :
                            gpu_miner = details["gpu"]
                            gpu_details = gpu_miner.get("details", [])

                            # Check if details exist and is non-empty
                            if gpu_details and isinstance(gpu_details, list) and len(gpu_details) > 0:
                                    gpu_name = str(gpu_details[0].get("name", "")).lower()
                            if model.lower() == gpu_name:
                                if cpu_count is not None:
                                    cpu_miner = details["cpu"]
                                    if cpu_miner.get("count") == cpu_count:
                                        flag += 1
                                elif ram_size is not None:
                                    ram_miner = details.get("ram", {})
                                    ram = ram_miner.get("total", 0) / 1024.0 ** 3
                                    if int(math.ceil(ram)) == int(ram_size):
                                        flag += 1
                                else:
                                    flag += 1
                            if flag:
                                counter+=1
                    bt.logging.info(f"API: List resources successfully")
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                    "count" : counter,
                    },
                )
            except Exception as e:
                bt.logging.error(f"API: An error occurred while counting GPUs: {e}")
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                    "success": False,
                    "message": "An error occurred while counting GPUs.",
                    "err_detail": e.__repr__(),
                    },
                )

        @self.app.post(
            "/list/resources_wandb",
            tags=["WandB"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List resources successfully.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "There is no resource available",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def list_resources_wandb(query: ResourceQuery = None,
                                 stats: bool = False,
                                 page_size: Optional[int] = None,
                                 page_number: Optional[int] = None) -> JSONResponse:
            """
            The list resources API endpoint. <br>
            The API will return the current miner resource and their detail specs on the validator. <br>
            query: The query parameter to filter the resources. <br>
            """

            bt.logging.info(f"API: List resources(wandb) on compute subnet")            
            self.wandb.api.flush()

            specs_details,running_hotkey = await get_wandb_running_miners()

            bt.logging.info(f"Number of running miners: {len(running_hotkey)}")

            # Initialize a dictionary to keep track of GPU instances
            resource_list = []
            gpu_instances = {}
            total_gpu_counts = {}

            # Get the allocated hotkeys from wandb
            allocated_hotkeys = await run_in_threadpool(self.wandb.get_allocated_hotkeys, TRUSTED_VALIDATORS_HOTKEYS, True)
            bt.logging.info(f"Allocated hotkeys: {allocated_hotkeys}")
            bt.logging.info(f"Number of allocated hotkeys: {len(allocated_hotkeys)}")

            db = ComputeDb()

            # penalized_hotkeys = await run_in_threadpool(self.get_penalized_hotkeys_checklist, valid_validator_hotkeys=[], flag=False)

            if specs_details:
                # Iterate through the miner specs details and print the table
                for hotkey, details in specs_details.items():

                    miner_older_than = self.miner_is_older_than(db, 48, hotkey)
                    miner_pog_ok = self.miner_pog_ok((db, 48, hotkey))

                    if hotkey in running_hotkey and miner_pog_ok:
                        if details:  # Check if details are not empty
                            resource = Resource()
                            try:
                                # Extract GPU details
                                gpu_miner = details["gpu"]
                                gpu_capacity = "{:.2f}".format(
                                    (gpu_miner["capacity"] / 1024)
                                )
                                gpu_name = str(gpu_miner["details"][0]["name"]).lower()
                                gpu_count = gpu_miner["count"]

                                # Extract CPU details
                                cpu_miner = details["cpu"]
                                cpu_count = cpu_miner["count"]

                                # Extract RAM details
                                ram_miner = details["ram"]
                                ram = "{:.2f}".format(
                                    ram_miner["total"] / 1024.0 ** 3
                                )

                                # Extract Hard Disk details
                                hard_disk_miner = details["hard_disk"]
                                hard_disk = "{:.2f}".format(
                                    hard_disk_miner["free"] / 1024.0 ** 3
                                )

                                # Update the GPU instances count
                                gpu_key = (gpu_name, gpu_count)
                                gpu_instances[gpu_key] = (
                                        gpu_instances.get(gpu_key, 0) + 1
                                )
                                total_gpu_counts[gpu_name] = (
                                        total_gpu_counts.get(gpu_name, 0) + gpu_count
                                )

                            except (KeyError, IndexError, TypeError):
                                gpu_name = "Invalid details"
                                gpu_capacity = "N/A"
                                gpu_count = "N/A"
                                cpu_count = "N/A"
                                ram = "N/A"
                                hard_disk = "N/A"
                        else:
                            gpu_name = "No details available"
                            gpu_capacity = "N/A"
                            gpu_count = "N/A"
                            cpu_count = "N/A"
                            ram = "N/A"
                            hard_disk = "N/A"

                        # Allocation status
                        # allocate_status = "N/A"

                        if hotkey in allocated_hotkeys:
                            allocate_status = "reserved"
                            if not stats:
                                continue
                        else:
                            allocate_status = "available"

                        add_resource = False
                        # Print the row with column separators
                        resource.hotkey = hotkey

                        try:
                            if gpu_name != "Invalid details" and gpu_name != "No details available":
                                if query is None or query == {}:
                                    add_resource = True
                                else:
                                    if query.gpu_name is not None and query.gpu_name.lower() not in gpu_name:
                                        continue
                                    if (query.gpu_capacity_max is not None and
                                            float(gpu_capacity) > query.gpu_capacity_max):
                                        continue
                                    if (query.gpu_capacity_min is not None and
                                            float(gpu_capacity) < query.gpu_capacity_min):
                                        continue
                                    if (query.cpu_count_max is not None and
                                            int(cpu_count) > query.cpu_count_max):
                                        continue
                                    if (query.cpu_count_min is not None and
                                            int(cpu_count) < query.cpu_count_min):
                                        continue
                                    if (query.ram_total_max is not None and
                                            float(ram) > query.ram_total_max):
                                        continue
                                    if (query.ram_total_min is not None and
                                            float(ram) < query.ram_total_min):
                                        continue
                                    if (query.hard_disk_total_max is not None and
                                            float(hard_disk) > query.hard_disk_total_max):
                                        continue
                                    if (query.hard_disk_total_min is not None and
                                            float(hard_disk) < query.hard_disk_total_min):
                                        continue
                                    add_resource = True

                                if add_resource:
                                    resource.cpu_count = int(cpu_count)
                                    resource.gpu_name = gpu_name
                                    resource.gpu_capacity = float(gpu_capacity)
                                    resource.gpu_count = int(gpu_count)
                                    resource.ram = float(ram)
                                    resource.hard_disk = float(hard_disk)
                                    resource.allocate_status = allocate_status
                                    resource_list.append(resource)
                            resource_list = await map_axon_ip_to_resources(resource_list)
                        except (KeyError, IndexError, TypeError, ValueError) as e:
                            bt.logging.error(f"API: Error occurred while filtering resources: {e}")
                            continue

                if stats:
                    status_counts = {"available": 0, "reserved": 0, "total": 0}
                    try:
                        for item in resource_list:
                            status_code = item.dict()["allocate_status"]
                            if status_code in status_counts:
                                status_counts[status_code] += 1
                                status_counts["total"] += 1
                    except Exception as e:
                        bt.logging.error(f"API: Error occurred while counting status: {e}")
                        status_counts = {"available": 0, "reserved": 0, "total": 0}

                    bt.logging.info(f"API: List resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List resources successfully",
                            "data": jsonable_encoder({"stats": status_counts}),
                        },
                    )
                else:
                    bt.logging.info(f"Number of resources returned: {len(resource_list)}")
                    bt.logging.trace("Resource List Contents:")
                    for resource in resource_list:
                        bt.logging.trace(vars(resource))
                    if page_number:
                        page_size = page_size if page_size else 50
                        result = self._paginate_list(resource_list, page_number, page_size)
                    else:
                        result = {
                            "page_items": resource_list,
                            "page_number": 1,
                            "page_size": len(resource_list),
                            "next_page_number": None,
                        }

                    bt.logging.info(f"API: List resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List resources successfully",
                            "data": jsonable_encoder(result),
                        },
                    )

            else:
                bt.logging.info(f"API: There is no resource available")
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "There is no resource available",
                        "err_detail": "No resources found.",
                    },
                )

        @self.app.post("/list/all_runs",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "List run resources successfully.",
                           },
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while getting runs from wandb",
                           },
                           422: {
                               "model": ErrorResponse,
                               "description": "Validation Error, Please check the request body.",
                           },
                       }
                       )
        async def list_all_runs(hotkey: Optional[str] = None, page_size: Optional[int] = None,
                                page_number: Optional[int] = None) -> JSONResponse:
            """
            This function gets all run resources.
            """
            db_list = []
            try:
                # self.wandb.api.flush()
                if hotkey:
                    filter_rule = {
                        "$and": [
                            {"config.config.netuid": self.config.netuid},
                            {"config.hotkey": hotkey},
                            {"state": "running"},
                        ]
                    }
                else:
                    filter_rule = {
                        "$and": [
                            {"config.config.netuid": self.config.netuid},
                            {"state": "running"},
                        ]
                    }
                runs = await run_in_threadpool(self.wandb.api.runs,
                                               f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_id = run.id
                        run_name = run.name
                        run_description = run.description
                        run_config = run.config
                        run_state = run.state
                        # run_start_at = datetime.strptime(run.created_at, '%Y-%m-%dT%H:%M:%S')
                        configs = run_config.get("config")
                        append_entry = True

                        # append the data to the db_list
                        if configs and append_entry:
                            db_specs_dict = {index: {
                                "id": run_id,
                                "name": run_name,
                                "description": run_description,
                                "configs": configs,
                                "state": run_state,
                                "start_at": run.created_at
                            }}
                            db_list.append(db_specs_dict)

                    if page_number:
                        page_size = page_size if page_size else 50
                        result = self._paginate_list(db_list, page_number, page_size)
                    else:
                        result = {
                            "page_items": db_list,
                            "page_number": 1,
                            "page_size": len(db_list),
                            "next_page_number": None,
                        }

                    bt.logging.info(f"API: List run resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List run resources successfully.",
                            "data": jsonable_encoder(result),
                        },
                    )

                else:
                    bt.logging.info(f"API: no runs available")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "No runs available",
                            "data": {},
                        },
                    )

            except Exception as e:
                # Handle the exception by logging an error message
                bt.logging.error(f"API: An error occurred while getting specs from wandb: {e}")
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "Error occurred while getting runs from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post(
            "/list/specs",
            tags=["WandB"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                200: {
                    "model": SuccessResponse,
                    "description": "List spec resources successfully.",
                },
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "Error occurred while getting specs from wandb",
                },
                422: {
                    "model": ErrorResponse,
                    "description": "Validation Error, Please check the request body.",
                },
            },
        )
        async def list_specs(hotkey: Optional[str] = None,
                             page_size: Optional[int] = None,
                             page_number: Optional[int] = None) -> JSONResponse:
            """
            The list specs API endpoint. <br>
            """
            db_list = []

            try:
                # self.wandb.api.flush()
                if hotkey:
                    filter_rule = {
                        "$and": [
                            {"config.role": "miner"},
                            {"config.config.netuid": self.config.netuid},
                            {"state": "running"},
                            {"config.hotkey": hotkey},
                            {"config.specs": {"$exists": True}},
                        ]
                    }
                else:
                    filter_rule = {
                        "$and": [
                            {"config.role": "miner"},
                            {"config.config.netuid": self.config.netuid},
                            {"config.specs": {"$exists": True}},
                            {"state": "running"},
                        ]
                    }

                runs = await run_in_threadpool(self.wandb.api.runs,
                                               f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_config = run.config
                        run_state = run.state
                        hotkey = run_config.get("hotkey")
                        specs = run_config.get("specs")
                        configs = run_config.get("config")

                        # check the signature
                        if hotkey and specs:
                            db_specs_dict = {index: {"hotkey": hotkey, "configs": configs,
                                                     "specs": specs, "state": run_state}}
                            db_list.append(db_specs_dict)

                    if page_number:
                        page_size = page_size if page_size else 50
                        result = self._paginate_list(db_list, page_number, page_size)
                    else:
                        result = {
                            "page_items": db_list,
                            "page_number": 1,
                            "page_size": len(db_list),
                            "next_page_number": None,
                        }

                    # Return the db_specs_dict for further use or inspection
                    bt.logging.info(f"API: List specs successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List specs successfully",
                            "data": jsonable_encoder(result),
                        },
                    )

                else:
                    bt.logging.info(f"API: no specs available")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "No specs available",
                            "data": {},
                        },
                    )

            except Exception as e:
                # Handle the exception by logging an error message
                bt.logging.error(
                    f"API: An error occurred while getting specs from wandb: {e}"
                )
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "Error occurred while getting specs from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post("/list/run_by_name",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "List run resources successfully.",
                           },
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while getting run from wandb",
                           },
                           422: {
                               "model": ErrorResponse,
                               "description": "Validation Error, Please check the request body.",
                           },
                       }
                       )
        async def list_run_name(run_name: str) -> JSONResponse:
            """
            This function gets runs by name.
            """
            db_specs_dict = {}
            try:
                # self.wandb.api.flush()
                filter_rule = {
                    "$and": [
                        {"config.config.netuid": self.config.netuid},
                        {"display_name": run_name},
                        {"state": "running"},
                    ]
                }

                runs = await run_in_threadpool(self.wandb.api.runs,
                                               f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_id = run.id
                        run_name = run.name
                        run_description = run.description
                        run_config = run.config
                        hotkey = run_config.get("hotkey")
                        configs = run_config.get("config")

                        # check the signature
                        if hotkey and configs:
                            db_specs_dict[index] = {"id": run_id, "name": run_name, "description": run_description,
                                                    "config": configs}

                    bt.logging.info(f"API: list run by name is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List run by name",
                            "data": jsonable_encoder(db_specs_dict),
                        },
                    )

                else:
                    bt.logging.info(f"API: no run available")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "No run available",
                            "data": {},
                        },
                    )

            except Exception as e:
                # Handle the exception by logging an error message
                bt.logging.error(f"API: An error occurred while getting specs from wandb: {e}")

                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "Error occurred while run from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post("/list/available",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "List available resources successfully.",
                           },
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while fetch available miner from wandb",
                           },
                           422: {
                               "model": ErrorResponse,
                               "description": "Validation Error, Please check the request body.",
                           },
                       }
                       )
        async def list_available_miner(rent_status: bool = False,
                                       page_size: Optional[int] = None,
                                       page_number: Optional[int] = None) -> JSONResponse:
            """
            This function gets all available miners.
            """
            db_list = []
            try:
                self.wandb.api.flush()
                if rent_status:
                    filter_rule = {
                        "config.allocated": {"$regex": "\\d.*"},
                        "config.config.netuid": self.config.netuid,
                        "config.role": "miner",
                        "state": "running",
                    }
                else:
                    filter_rule = {
                        "$or": [
                            {"config.allocated": {"$regex": "null"}},
                            {"config.allocated": {"$exists": False}},
                        ],
                        "config.config.netuid": self.config.netuid,
                        "config.role": "miner",
                        "state": "running",
                    }

                runs = await run_in_threadpool(self.wandb.api.runs,
                                               f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_config = run.config
                        hotkey = run_config.get("hotkey")
                        specs = run.config.get("specs")
                        configs = run_config.get("config")

                        # check the signature
                        if hotkey and configs:
                            db_specs_dict = {index: {"hotkey": hotkey, "details": specs}}
                            db_list.append(db_specs_dict)

                    if page_number:
                        page_size = page_size if page_size else 50
                        result = self._paginate_list(db_list, page_number, page_size)
                    else:
                        result = {
                            "page_items": db_list,
                            "page_number": 1,
                            "page_size": len(db_list),
                            "next_page_number": None,
                        }
                else:
                    bt.logging.info(f"API: No available miners")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "No available miner",
                            "data": {},
                        },
                    )

                if rent_status:
                    bt.logging.info(f"API: List rented miners is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List rented miners",
                            "data": jsonable_encoder(result),
                        },
                    )
                else:
                    bt.logging.info(f"API: List available miners is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List available miners",
                            "data": jsonable_encoder(result),
                        },
                    )

            except Exception as e:
                # Handle the exception by logging an error message
                bt.logging.error(f"API: An error occurred while fetching available miner from wandb: {e}")
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "Error occurred while fetching available miner from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post("/list/allocated_hotkeys",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "List available resources successfully.",
                           },
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while fetch allocated hotkey from wandb",
                           },
                           422: {
                               "model": ErrorResponse,
                               "description": "Validation Error, Please check the request body.",
                           },
                       }
                       )
        async def list_allocated_hotkeys() -> JSONResponse:
            """
            This function gets all allocated hotkeys from all validators.
            Only relevant for validators.
            """
            try:
                self.wandb.api.flush()
                filter_rule = {
                    "$and": [
                        {"config.role": "validator"},
                        {"config.config.netuid": self.config.netuid},
                        {"config.allocated_hotkeys": {"$regex": "\\d.*"}},
                        {"state": "running"},
                    ]
                }

                # Query all runs in the project and Filter runs where the role is 'validator'
                validator_runs = await run_in_threadpool(self.wandb.api.runs,
                                                         f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filter_rule)

                # Check if the runs list is empty
                if not validator_runs:
                    bt.logging.info(f"API: No validator with allocated info in the project opencompute.")
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "success": False,
                            "message": "No validator with allocated info in the project opencompute.",
                            "data": {},
                        },
                    )

            except Exception as e:
                bt.logging.error(f"API: list_allocated_hotkeys error with {e.__repr__()}")
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "success": False,
                        "message": "Error occurred while fetching allocated hotkey from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

            # Initialize an empty list to store allocated keys from runs with a valid signature
            allocated_keys_list = []

            # Verify the signature for each validator run
            for run in validator_runs:
                try:
                    # Access the run's configuration
                    run_config = run.config
                    # hotkey = run_config.get("hotkey")
                    allocated_keys = run_config.get("allocated_hotkeys")
                    # id = run_config.get("id")
                    # name = run_config.get("name")

                    # valid_validator_hotkey = hotkey in valid_validator_hotkeys
                    # Allow all validator hotkeys for data retrieval only
                    # if verify_run(id,name, hotkey, allocated_keys) and allocated_keys and valid_validator_hotkey:
                    allocated_keys_list.extend(allocated_keys)  # Add the keys to the list

                except Exception as e:
                    bt.logging.error(f"API: Run ID: {run.id}, Name: {run.name}, Error: {e}")

            bt.logging.info(f"API: List allocated hotkeys is success")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "List allocated hotkeys",
                    "data": jsonable_encoder(allocated_keys_list),
                },
            )

        @self.app.post("/test/notify",
                       tags=["Testing"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           200: {
                               "model": SuccessResponse,
                               "description": "Notify allocation event testing is success",
                           },
                           400: {
                               "model": ErrorResponse,
                               "description": "Notify allocation event testing is failed",
                           },
                           422: {
                               "model": ErrorResponse,
                               "description": "Validation Error, Please check the request body.",
                           },
                       }
                       )
        async def test_notify(hotkey: str = None, uuid_key: str = None, event: str = None) -> JSONResponse:
            """
            This function is used to test the notification system.
            """
            try:
                if not hotkey:
                    hotkey = "test_hotkey"
                if not uuid_key:
                    uuid_key = str(uuid.uuid1())
                if not event:
                    event = "DEALLOCATION"
                # Notify the allocation event
                response = await self._notify_allocation_status(
                    event_time=datetime.now(timezone.utc),
                    hotkey=hotkey,
                    uuid=uuid_key,
                    event=event,
                    details="test notify event message",
                )

                if response:
                    bt.logging.info(f"API: Notify allocation event testing is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "Notify allocation event testing is success",
                        },
                    )
                else:
                    bt.logging.error(f"API: Notify allocation event testing is failed")
                    return JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={
                            "success": False,
                            "message": "Notify allocation event testing is failed",
                        },
                    )

            except Exception as e:
                bt.logging.error(f"API: An error occurred while testing notify: {e}")
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "success": False,
                        "message": "Error occurred while testing notify",
                        "err_detail": e.__repr__(),
                    },
                )

    @staticmethod
    def _init_config():
        """
        This function is responsible for setting up and parsing command-line arguments.
        :return: config
        """
        parser = ComputeArgPaser(description="This script aims to help allocation with the compute subnet.")
        config = parser.config

        # Step 3: Set up logging directory
        # Logging is crucial for monitoring and debugging purposes.
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/{}/{}/".format(
                config.logging.logging_dir,
                config.wallet.name,
                config.wallet.hotkey,
                config.netuid,
                "validator",
                "register"
            )
        )
        # Ensure the logging directory exists.
        if not os.path.exists(config.full_path):
            os.makedirs(config.full_path, exist_ok=True)

        # Return the parsed config.
        return config

    @staticmethod
    def _get_config(user_config: UserConfig, requirements: Union[DeviceRequirement, None] = None):
        """
        Get the config from user config and spec requirement for the API. <br>
        user_config: The user configuration which contain the validator's hotkey and wallet information. <br>
        requirements: The device requirements. <br>
        """
        parser = argparse.ArgumentParser()
        # Adds bittensor specific arguments
        parser.add_argument(
            "--netuid", type=int, default=27, help="The chain subnet uid."
        )
        # parser.add_argument("--gpu_type", type=str, help="The GPU type.")
        # parser.add_argument("--gpu_size", type=int, help="The GPU memory in MB.")
        # parser.add_argument("--timeline", type=int, help="The allocation timeline.")
        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)

        if not user_config.subtensor_chain_endpoint:
            if user_config.subtensor_network == "finney":
                user_config.subtensor_chain_endpoint = (
                    "wss://entrypoint-finney.opentensor.ai:443"
                )
            elif user_config.subtensor_network == "test":
                user_config.subtensor_chain_endpoint = (
                    "wss://test.finney.opentensor.ai:443"
                )

        # Add user configuration and requirement to list for the bt config parser
        args_list = []
        for entry in [user_config, requirements]:
            if entry:
                for k, v in entry.__fields__.items():
                    args_list.append(f"--{v.alias}")
                    args_list.append(getattr(entry, k))

        # Parse the initial config to check for provided arguments
        config = bt.config(parser=parser, args=args_list)

        # Set up logging directory
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/{}".format(
                config.logging.logging_dir,
                config.wallet.name,
                config.wallet.hotkey,
                config.netuid,
                "validator",
            )
        )
        if not os.path.exists(config.full_path):
            os.makedirs(config.full_path, exist_ok=True)

        return config

    def _allocate_container(self, device_requirement, timeline, public_key, docker_requirement: dict):
        """
        Allocate the container with the given device requirement. <br>
        """
        # Generate ssh connection for given device requirements and timeline
        # Instantiate the connection to the db
        db = ComputeDb()

        # Find out the candidates
        candidates_hotkey = select_allocate_miners_hotkey(db, device_requirement)

        axon_candidates = []
        for axon in self.metagraph.axons:
            if axon.hotkey in candidates_hotkey:
                axon_candidates.append(axon)

        responses = self.dendrite.query(
            axon_candidates,
            Allocate(
                timeline=timeline, device_requirement=device_requirement, checking=True
            ),
        )

        final_candidates_hotkey = []

        for index, response in enumerate(responses):
            hotkey = axon_candidates[index].hotkey
            if response and response["status"] is True:
                final_candidates_hotkey.append(hotkey)

        # Check if there are candidates
        if len(final_candidates_hotkey) <= 0:
            return {"status": False, "msg": "Requested resource is not available."}

        # Sort the candidates with their score
        scores = torch.ones_like(self.metagraph.S, dtype=torch.float32)

        score_dict = {
            hotkey: score
            for hotkey, score in zip(
                [axon.hotkey for axon in self.metagraph.axons], scores
            )
        }
        sorted_hotkeys = sorted(
            final_candidates_hotkey,
            key=lambda hotkey: score_dict.get(hotkey, 0),
            reverse=True,
        )

        # Loop the sorted candidates and check if one can allocate the device
        for hotkey in sorted_hotkeys:
            index = self.metagraph.hotkeys.index(hotkey)
            axon = self.metagraph.axons[index]
            register_response = self.dendrite.query(
                axon,
                Allocate(
                    timeline=timeline,
                    device_requirement=device_requirement,
                    checking=False,
                    public_key=public_key,
                    docker_requirement=docker_requirement,
                ),
                timeout=100,
            )
            if register_response and register_response["status"] is True:
                register_response["ip"] = axon.ip
                register_response["hotkey"] = axon.hotkey
                return register_response

        # Close the db connection
        db.close()

        return {"status": False, "msg": "Requested resource is not available."}

    def _allocate_container_hotkey(self, requirements, hotkey, timeline, public_key, docker_requirement: dict):
        """
        Allocate the container with the given hotkey. <br>
        Generate ssh connection for given device requirements and timeline. <br>
        """
        device_requirement = {"cpu": {"count": 1}, "gpu": {
            "count": 1,
            "capacity": int(requirements.gpu_size) * 1000,
            "type": requirements.gpu_type,
        }, "hard_disk": {"capacity": 1073741824}, "ram": {"capacity": 1073741824}}

        # Start of allocation process
        bt.logging.info(f"API: Starting container allocation with hotkey: {hotkey}")

        bt.logging.trace(f"Docker Requirement: {docker_requirement}")

        # Instantiate the connection to the db
        for axon in self.metagraph.axons:
            if axon.hotkey == hotkey:
                attempt = 0
                # Retry allocation up to max_retries times

                while attempt < MAX_ALLOCATION_RETRY:
                    attempt += 1
                    check_allocation = self.dendrite.query(
                        axon,
                        Allocate(
                            timeline=timeline,
                            device_requirement=device_requirement,
                            checking=True,
                        ),
                        timeout=30,
                    )

                    if not check_allocation or check_allocation.get("status") is not True:
                        bt.logging.warning(f"API: Allocation check failed for hotkey: {hotkey}")
                        continue  # Move to the next axon if allocation check failed

                    bt.logging.info(f"API: Allocation check passed for hotkey: {hotkey}")

                    if check_allocation and check_allocation["status"] is True:
                        register_response = self.dendrite.query(
                            axon,
                            Allocate(
                                timeline=60,
                                device_requirement=device_requirement,
                                checking=False,
                                public_key=public_key,
                                docker_requirement=docker_requirement,
                            ),
                            timeout=60,
                        )
                        if register_response and register_response["status"] is True:
                            register_response["ip"] = axon.ip
                            register_response["hotkey"] = axon.hotkey
                            return register_response

                    # Log or print retry attempt (optional)
                    bt.logging.trace(f"Attempt {attempt} failed for hotkey {hotkey}, retrying...") if attempt < MAX_ALLOCATION_RETRY else None
                    time.sleep(10)  # Sleep before the next retry attempt

        return {"status": False, "msg": "Requested resource is not available."}

    async def _update_allocation_wandb(self, ):
        """
        Update the allocated hotkeys in wandb. <br>
        """
        hotkey_list = []

        # Instantiate the connection to the db
        db = ComputeDb()
        cursor = db.get_cursor()

        try:
            # Retrieve all records from the allocation table
            cursor.execute("SELECT id, hotkey, details FROM allocation")
            rows = cursor.fetchall()

            for row in rows:
                id, hotkey, details = row
                hotkey_list.append(hotkey)

        except Exception as e:
            print(f"An error occurred while retrieving allocation details: {e}")
        finally:
            cursor.close()
            db.close()
        try:
            await run_in_threadpool(self.wandb.update_allocated_hotkeys, hotkey_list)
        except Exception as e:
            bt.logging.info(f"API: Error updating wandb : {e}")
            return

    async def _refresh_metagraph(self):
        """
        Refresh the metagraph by resync_period. <br>
        """
        while True:
            if self.metagraph:
                self.metagraph.sync(lite=True, subtensor=self.subtensor)
                # bt.logging.info(f"API: Metagraph refreshed")
                await asyncio.sleep(DATA_SYNC_PERIOD)

    async def _refresh_allocation(self):
        """
        Refresh the allocation by resync_period. <br>
        """
        while True:
            self.allocation_table = self.wandb.get_allocated_hotkeys([], False)
            bt.logging.info(f"API: Allocation refreshed: {self.allocation_table}")
            await asyncio.sleep(DATA_SYNC_PERIOD)

    async def _notify_allocation_status(self, event_time: datetime, hotkey: str,
                                        uuid: str, event: str, details: str | None = ""):
        """
        Notify the allocation by hotkey and status. <br>
        """
        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json',
        }
        if event == "DEALLOCATION":
            msg = {
                "time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                "deallocated_at": event_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                "hotkey": hotkey,
                "status": event,
                "uuid": uuid,
            }
            notify_url = self.deallocation_notify_url
        elif event == "OFFLINE" or event == "ONLINE":
            msg = {
                "time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                "status_change_at": event_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                "hotkey": hotkey,
                "status": event,
                "uuid": uuid,
            }
            notify_url = self.status_notify_url

        retries = 0
        while retries < MAX_NOTIFY_RETRY:
            try:
                # Send the POST request
                data = json.dumps(msg)
                response = await run_in_threadpool(
                    requests.post, notify_url, headers=headers, data=data, timeout=3, json=True, verify=False,
                    cert=("cert/server.cer", "cert/server.key"),
                )
                # Check for the expected ACK in the response
                if response.status_code == 200 or response.status_code == 201:
                    response_data = response.json()
                    # if response_data.get("status") == "success":  # ACK response
                    #     return response
                    # else:
                    #     print(f"ACK not received, response: {response_data}")
                    # bt.logging.info(f"API: Notify success with {hotkey} status code: "
                    #                 {response.status_code}, response: {response.text}")
                    return response_data
                else:
                    bt.logging.info(f"API: Notify failed with {hotkey} status code: "
                                    f"{response.status_code}, response: {response.text}")
                    return None

            except requests.exceptions.RequestException as e:
                bt.logging.info(f"API: Notify {hotkey} failed: {e}")

            # Increment the retry counter and wait before retrying
            retries += 1
            await asyncio.sleep(NOTIFY_RETRY_PERIOD)

        return None

    async def _check_allocation(self):
        """
        Check the allocation by resync_period. <br>
        """
        while True:
            db = ComputeDb()
            cursor = db.get_cursor()
            try:
                # Retrieve all records from the allocation table
                cursor.execute("SELECT id, hotkey, details FROM allocation")
                rows = cursor.fetchall()
                for row in rows:
                    id, hotkey, details = row
                    info = json.loads(details)
                    uuid_key = info.get("uuid")

                    # Check if hotkey exists in self.metagraph.hotkeys and uuid_key is valid
                    if hotkey in self.metagraph.hotkeys and uuid_key:
                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]

                        register_response = await run_in_threadpool(self.dendrite.query, axon,
                                                          Allocate(timeline=1, checking=True, ), timeout=60)
                        if register_response and register_response["status"] is False:

                            if hotkey in self.checking_allocated:
                                response = await self._notify_allocation_status(
                                    event_time=deallocated_at,
                                    hotkey=hotkey,
                                    uuid=uuid_key,
                                    event="ONLINE",
                                    details=f"GPU Resume for {ALLOCATE_CHECK_PERIOD} seconds"
                                )
                                bt.logging.info(f"API: Allocation ONLINE notification for hotkey: {hotkey}")
                            self.checking_allocated = [x for x in self.checking_allocated if x != hotkey]

                        else:
                            # handle the case when no response is received or the docker is not running
                            self.checking_allocated.append(hotkey)
                            # bt.logging.info(f"API: No response timeout is triggered for hotkey: {hotkey}")
                            deallocated_at = datetime.now(timezone.utc)
                            response = await self._notify_allocation_status(
                                event_time=deallocated_at,
                                hotkey=hotkey,
                                uuid=uuid_key,
                                event="OFFLINE",
                                details=f"No response timeout for {ALLOCATE_CHECK_PERIOD} seconds"
                            )
                            bt.logging.info(f"API: Allocation OFFLINE notification for hotkey: {hotkey}")
                            if not response:
                                pass

                        if self.checking_allocated.count(hotkey) >= ALLOCATE_CHECK_COUNT:
                            deallocated_at = datetime.now(timezone.utc)
                            # update the allocation table
                            update_allocation_db(hotkey, info, False)
                            await self._update_allocation_wandb()
                            response = await self._notify_allocation_status(
                                event_time=deallocated_at,
                                hotkey=hotkey,
                                uuid=uuid_key,
                                event="DEALLOCATION",
                                details=f"No response timeout for {ALLOCATE_CHECK_COUNT} times"
                            )
                            bt.logging.info(f"API: deallocate event triggered due to {hotkey} "
                                            f"is timeout for {ALLOCATE_CHECK_COUNT} times")

                            # remove the hotkey from checking table
                            if not response:
                                self.notify_retry_table.append({"event_time": deallocated_at,
                                                                "hotkey": hotkey,
                                                                "uuid": uuid_key,
                                                                "event": "DEALLOCATION",
                                                                "details": "Retry deallocation notify event triggered"})

                for entry in self.notify_retry_table:
                    response = await self._notify_allocation_status(event_time=entry["event_time"],
                                                                    hotkey=entry["hotkey"],
                                                                    uuid=entry["uuid"],
                                                                    event=entry["event"],
                                                                    details=entry["details"])
                    if response:
                        self.notify_retry_table.remove(entry)
                        bt.logging.info(f"API: Notify {entry['event']} retry event is success on {entry['hotkey']} ")
                    else:
                        bt.logging.info(f"API: Notify {entry['event']} retry event is failed on {entry['hotkey']} ")

            except Exception as e:
                bt.logging.error(f"API: Error occurred while checking allocation: {e}")
            finally:
                # bt.logging.info(f"API: Allocation checking triggered")
                await asyncio.sleep(ALLOCATE_CHECK_PERIOD)

    @staticmethod
    def _paginate_list(items, page_number, page_size):
        # Calculate the start and end indices of the items on the current page
        start_index = (page_number - 1) * page_size
        end_index = start_index + page_size

        # Get the items on the current page
        page_items = items[start_index:end_index]

        # Determine if there are more pages
        has_next_page = end_index < len(items)
        next_page_number = page_number + 1 if has_next_page else None

        return {
            "page_items": page_items,
            "page_number": page_number,
            "page_size": page_size,
            "next_page_number": next_page_number
        }

    @staticmethod
    def check_port_open(host, port, hotkey):
        result = check_port(host, port)
        if result is True:
            bt.logging.info(f"API: Port {port} on {host} is open for {hotkey}")
            return True
        elif result is False:
            bt.logging.info(f"API: Port {port} on {host} is closed for {hotkey}")
            return False
        else:
            bt.logging.warning(f"API: Could not determine status of port {port} on {host} for {hotkey}")
            return False

    def miner_is_older_than(self, db: ComputeDb, hours: int, ss58_address: str) -> bool:
        cursor = db.get_cursor()
        try:
            cursor.execute("SELECT MIN(created_at) FROM challenge_details WHERE ss58_address = ?", (ss58_address,))
            oldest_timestamp = cursor.fetchone()[0]
            if oldest_timestamp:
                if (datetime.now() - datetime.fromisoformat(oldest_timestamp)).total_seconds() <= hours * 3600:
                    print(f"Hotkey not old enough: {ss58_address}")
                    return False
                return True
            return False
        except Exception as e:
            bt.logging.info(f"Error occurred: {e}")
            return False
        finally:
            cursor.close()

    def miner_pog_ok(self, db: ComputeDb, hours: int, ss58_address: str) -> bool:
        gpu_specs = get_pog_specs(self.db, ss58_address)
        if gpu_specs is not None:
            return True
        else:
            return False

    def run(self):
        """
        Run the FastAPI app. <br>
        """
        if os.path.exists("cert/ca.cer") and os.path.exists("cert/server.key") and os.path.exists("cert/server.cer"):
            uvicorn.run(
                self.app,
                host=self.ip_addr,
                port=self.port,
                log_level="critical",
                ssl_keyfile="cert/server.key",
                ssl_certfile="cert/server.cer",
                ssl_cert_reqs=DEFAULT_SSL_MODE,  # 1 for client CERT optional, 2 for client CERT_REQUIRED
                ssl_ca_certs="cert/ca.cer",
            )
        else:
            bt.logging.error(f"API: No SSL certificate found, please generate one with /cert/gen_ca.sh")
            exit(1)

    def start(self):
        """
        Start the FastAPI app in the process. <br>
        """
        self.process = multiprocessing.Process(
            target=self.run, args=(), daemon=True
        ).start()

    def stop(self):
        """
        Stop the FastAPI app in the process. <br>
        """
        if self.process:
            self.process.terminate()
            self.process.join()


# Run the FastAPI app
if __name__ == "__main__":
    os.environ["WANDB_SILENT"] = "true"
    register_app = RegisterAPI()
    register_app.run()