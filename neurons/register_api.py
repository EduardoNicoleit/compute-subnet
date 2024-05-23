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
import copy

# Constants
API_DEFAULT_PORT = 8903
DATA_SYNC_PERIOD = 180
PUBLIC_WANDB_NAME = "opencompute"
PUBLIC_WANDB_ENTITY = "neuralinternet"

# Import Common Libraries
import argparse
import base64
import os
import json
import bittensor as bt
import torch
import time
from datetime import datetime, timedelta, timezone
import asyncio
import multiprocessing

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
    Depends,
    HTTPException,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import typing, BaseModel, Field
from typing import List, Optional, Type, Union, Any, Annotated

# Security configuration
oauth2_token_scheme = Annotated[str, Depends(OAuth2PasswordBearer(tokenUrl="login"))]



class UserConfig(BaseModel):
    netuid: str = Field(default="15")
    subtensor_network: str = Field(default="test", alias="subtensor.network")
    subtensor_chain_endpoint: Union[str, None] = Field(
        default="", alias="subtensor.chain_endpoint"
    )
    wallet_name: str = Field(default="validator", alias="wallet.name")
    wallet_hotkey: str = Field(default="default", alias="wallet.hotkey")
    logging_debug: Union[str, None] = Field(default="", alias="logging.debug")


class Requirement(BaseModel):
    gpu_type: str = "a6000"
    gpu_size: int = 3  # in GB
    timeline: int = "90"  # timeline=90 day by spec, 30 day by hotkey


class Allocation(BaseModel):
    resource: str = ""
    hotkey: str = ""
    regkey: str = ""
    ssh_ip: str = ""
    ssh_port: int = 4444
    ssh_username: str = ""
    ssh_password: str = ""
    ssh_command: str = ""


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


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str | None = None


class RegisterAPI:
    def __init__(
        self,
        config: Optional[bt.config] = None,
        wallet: Optional[bt.wallet] = None,
        subtensor: Optional[bt.subtensor] = None,
        dendrite: Optional[bt.dendrite] = None,
        metagraph: Optional[bt.metagraph] = None,
        wandb: Optional[ComputeWandb] = None,
    ):
        # Check ACCESS and REFRESH Key, disable JWT and use SSL
        # env_file = ".env"
        # load_dotenv()
        # self.access_api_key = os.getenv("ACCESS_API_KEY")
        # self.refresh_api_key = os.getenv("REFRESH_API_KEY")
        # if not self.access_api_key:
        #    self.access_api_key = secrets.token_urlsafe(32)
        #    self.refresh_api_key = secrets.token_urlsafe(32)
        #    set_key(dotenv_path=env_file, key_to_set="ACCESS_API_KEY", value_to_set=self.access_api_key)
        #    set_key(dotenv_path=env_file, key_to_set="REFRESH_API_KEY", value_to_set=self.refresh_api_key)

        self.app = FastAPI()
        self._setup_routes()
        self.process = None

        # Compose User Config Data with bittensor config
        # Get the config from the user config
        # self.config = self._get_config(user_config=user_config)
        if config is None:
            # Step 1: Parse the bittensor and compute subnet config
            self.config = self._init_config()

            # Set up logging with the provided configuration and directory.
            bt.logging(config=self.config, logging_dir=self.config.full_path)
            bt.logging.info(f"Running validator register for subnet: {self.config.netuid} on network: {self.config.subtensor.chain_endpoint} with config:")
            
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

            if self.config.axon.ip=="[::]":
                self.ip_addr = "0.0.0.0"
            else:
                self.ip_addr = self.config.axon.ip

            if self.config.axon.port is None:
                self.port = API_DEFAULT_PORT
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

            if self.config.axon.ip=="[::]":
                self.ip_addr = "0.0.0.0"
            else:
                self.ip_addr = self.config.axon.ip

            if self.config.axon.port is None:
                self.port = API_DEFAULT_PORT
            else:
                self.port = self.config.axon.port

        self.allocation_table = []

    def _setup_routes(self):
        @self.app.on_event("startup")
        async def startup_event():
            """
            This function is called when the application starts. <br>
            It initializes the database connection and other necessary components. <br>
            """
            # Setup the repeated task
            self.metagraph_task = asyncio.create_task(self._refresh_metagraph())
            #self.allocation_task = asyncio.create_task(self._refresh_allocation())
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
                "message": "Welcome to Compute Subnet Allocation API, Please login to access the API."
            }

        # disable login route for Token authentication
        # @self.app.post(
        #     "/login",
        #     response_model=Token,
        #     responses={
        #         401: {"description": "Invalid authentication credentials"},
        #         200: {"description": "User login successful"},
        #     },
        # )
        async def login_for_access_token(form_data: Annotated[OAuth2PasswordRequestForm, Depends()],) -> Token:
            user = self._authenticate_user(form_data.username, form_data.password)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Incorrect username or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            access_token = self._create_access_token(
                data={"sub": user}, expires_delta=access_token_expires
            )
            return Token(access_token=access_token, token_type="bearer")

        @self.app.post(
            "/service/allocate_spec",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
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
                200: {
                    "model": SuccessResponse,
                    "description": "Resource was successfully allocated",
                },
            },
        )
        async def allocate_spec(requirements: Requirement,) -> JSONResponse | HTTPException:
            """
            The GPU resource allocate API endpoint. <br>
            token: The user token for the authorization. <br>
            user_config: The user configuration which contain the validator's hotkey and wallet information. <br>
            requirements: The GPU resource requirements which contain the GPU type, GPU size, and booking timeline. <br>
            """
            if True:
                if requirements:
                    device_requirement = {
                        "cpu": {"count": 1},
                        "gpu": {},
                        "hard_disk": {"capacity": 1073741824},
                        "ram": {"capacity": 1073741824},
                    }
                    if requirements.gpu_type != "" and int(requirements.gpu_size) != 0:
                        device_requirement["gpu"] = {
                            "count": 1,
                            "capacity": int(requirements.gpu_size) * 1024,
                            "type": requirements.gpu_type,
                        }

                    timeline = int(requirements.timeline)
                    private_key, public_key = rsa.generate_key_pair()
                    result = self._allocate_container(
                        device_requirement, timeline, public_key
                    )

                    if result["status"] is True:
                        result_hotkey = result["hotkey"]
                        result_info = result["info"]
                        private_key = private_key.encode("utf-8")
                        decrypted_info_str = rsa.decrypt_data(
                            private_key, base64.b64decode(result_info)
                        )
                        bt.logging.info(
                            f"API: Registered successfully : {decrypted_info_str}, 'ip':{result['ip']}"
                        )

                        # Iterate through the miner specs details to get gpu_name
                        db = ComputeDb()
                        specs_details = get_miner_details(db)
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

                        await asyncio.sleep(1)

                        allocated = Allocation()
                        allocated.resource = info["resource"]
                        allocated.hotkey = result_hotkey
                        # allocated.regkey = info["regkey"]
                        allocated.ssh_ip = info["ip"]
                        allocated.ssh_port = info["port"]
                        allocated.ssh_username = info["username"]
                        allocated.ssh_password = info["password"]
                        allocated.ssh_command = f"ssh {info['username']}@{result['ip']} -p {str(info['port'])}"

                        update_allocation_db(result_hotkey, info, True)
                        self._update_allocation_wandb()
                        self.allocation_table = self.wandb.get_allocated_hotkeys([], False)

                        bt.logging.info(f"Resource {result_hotkey} was successfully allocated")
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "success": True,
                                "message": "Resource was successfully allocated",
                                "data": jsonable_encoder(allocated),
                            },
                        )
                    else:
                        bt.logging.info(f"API: Allocation Failed : {result['msg']}")
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "success": False,
                                "message": "Fail to allocate resource",
                                "err_detail": result["msg"],
                            },
                        )
                else:
                    bt.logging.error(f"API: Invalid allocation request")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "success": False,
                            "message": "Invalid allocation request",
                            "err_detail": "Invalid requirement, please check the requirements",
                        },
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "success": False,
                        "message": "Missing authorization",
                        "err_detail": "Please provide the authorization token",
                    },
                )

        @self.app.post(
            "/service/allocate_hotkey",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
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
                200: {
                    "model": SuccessResponse,
                    "description": "Resource was successfully allocated",
                },
            },
        )
        async def allocate_hotkey(hotkey: str,) -> JSONResponse | HTTPException:
            """
            The GPU allocate by hotkey API endpoint. <br>
            User use this API to book a specific miner. <br>
            token: The user token for the authorization. <br>
            user_config: The user configuration which contain the validator's hotkey and wallet information. <br>
            hotkey: The miner hotkey to allocate the gpu resource. <br>
            """
            if True:
                if hotkey:
                    requirements = Requirement()
                    requirements.gpu_type = ""
                    requirements.gpu_size = 0
                    requirements.timeline = 30

                    private_key, public_key = rsa.generate_key_pair()
                    result = self._allocate_container_hotkey(
                        requirements, hotkey, requirements.timeline, public_key
                    )

                    # Iterate through the miner specs details to get gpu_name
                    db = ComputeDb()
                    specs_details = get_miner_details(db)
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

                    if result["status"] is True:
                        result_hotkey = result["hotkey"]
                        result_info = result["info"]
                        private_key = private_key.encode("utf-8")
                        decrypted_info_str = rsa.decrypt_data(
                            private_key, base64.b64decode(result_info)
                        )
                        bt.logging.info(
                            f"API: Allocation successfully : {decrypted_info_str}, 'ip':{result['ip']}"
                        )

                        info = json.loads(decrypted_info_str)
                        info["ip"] = result["ip"]
                        info["resource"] = gpu_name
                        info["regkey"] = public_key

                        time.sleep(1)
                        allocated = Allocation()
                        allocated.resource = info["resource"]
                        allocated.hotkey = result_hotkey
                        # allocated.regkey = info["regkey"]
                        allocated.ssh_ip = info["ip"]
                        allocated.ssh_port = info["port"]
                        allocated.ssh_username = info["username"]
                        allocated.ssh_password = info["password"]
                        allocated.ssh_command = f"ssh {info['username']}@{result['ip']} -p {str(info['port'])}"

                        update_allocation_db(result_hotkey, info, True)
                        self._update_allocation_wandb()
                        #self.allocation_table = self.wandb.get_allocated_hotkeys([], False)

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
                        bt.logging.info(f"API: Allocation Failed : {result['msg']}")
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "success": False,
                                "message": "Fail to allocate resource",
                                "err_detail": result["msg"],
                            },
                        )
                else:
                    bt.logging.error(f"API: Invalid allocation request")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "success": False,
                            "message": "Invalid allocation request",
                            "err_detail": "Invalid hotkey, please check the hotkey",
                        },
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "success": False,
                        "message": "Missing authorization",
                        "err_detail": "Please provide the authorization token",
                    },
                )

        @self.app.post(
            "/service/deallocate",
            tags=["Allocation"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                400: {
                    "model": ErrorResponse,
                    "description": "De-allocation not successfully, please try again.",
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
                200: {
                    "model": SuccessResponse,
                    "description": "Resource de-allocated successfully.",
                },
            },
        )
        async def deallocate(hotkey: str,) -> JSONResponse | HTTPException:
            """
            The GPU deallocate API endpoint. <br>
            token: The user token for the authorization. <br>
            user_config: The user configuration which contain the validator's hotkey and wallet information. <br>
            hotkey: The miner hotkey to deallocate the gpu resource. <br>
            """
            if True:
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

                        index = self.metagraph.hotkeys.index(hotkey)
                        axon = self.metagraph.axons[index]
                        deregister_response = self.dendrite.query(
                            axon,
                            Allocate(
                                timeline=0,
                                device_requirement={},
                                checking=False,
                                public_key=regkey,
                            ),
                            timeout=60,
                        )
                        if (
                            deregister_response
                            and deregister_response["status"] is True
                        ):
                            update_allocation_db(result_hotkey, info, False)
                            self._update_allocation_wandb()
                            #self.allocation_table = self.wandb.get_allocated_hotkeys([], False)

                            bt.logging.info(f"API: Resource {hotkey} de-allocated successfully")
                            return JSONResponse(
                                status_code=status.HTTP_200_OK,
                                content={
                                    "success": True,
                                    "message": "Resource de-allocated successfully.",
                                },
                            )
                        else:
                            bt.logging.error(f"API: Invalid {hotkey} de-allocation request")
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail={
                                    "success": False,
                                    "message": "Invalid de-allocation request",
                                    "err_detail": "De-allocation not successfully, please try again",
                                },
                            )
                    else:
                        bt.logging.info(f"API: No allocation details found for the provided hotkey")
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "success": False,
                                "message": "No allocation details found for the provided hotkey.",
                                "err_detail": "No allocation details found for the provided hotkey.",
                            },
                        )
                except Exception as e:
                    bt.logging.error(f"API: An error occurred during de-allocation {e.__repr__()}")
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "success": False,
                            "message": "An error occurred during de-allocation.",
                            "err_detail": e.__repr__(),
                        },
                    )
                finally:
                    cursor.close()
                    db.close()
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "success": False,
                        "message": "Missing authorization",
                        "err_detail": "Please provide the authorization token",
                    },
                )

        @self.app.post(
            "/list/allocations_sql",
            tags=["SQLite"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                401: {
                    "model": ErrorResponse,
                    "description": "Missing authorization token",
                },
                403: {
                    "model": ErrorResponse,
                    "description": "An error occurred while retrieving allocation details",
                },
                404: {
                    "model": SuccessResponse,
                    "description": "There is no allocation available",
                },
                200: {
                    "model": SuccessResponse,
                    "description": "List allocations successfully.",
                },
            },
        )
        async def list_allocations() -> JSONResponse | HTTPException:
            """
            The list allocation API endpoint. <br>
            The API will return the current allocation on the validator. <br>
            token: The user token for the authorization. <br>
            """
            if True:
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
                                "success": True,
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
                        allocation_list.append(entry)

                except Exception as e:
                    bt.logging.error(
                        f"API: An error occurred while retrieving allocation details: {e}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
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
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "success": False,
                        "message": "Missing authorization",
                        "err_detail": "Please provide the authorization token",
                    },
                )

        @self.app.post(
            "/list/resources_sql",
            tags=["SQLite"],
            response_model=SuccessResponse | ErrorResponse,
            responses={
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "There is no resource available",
                },
                200: {
                    "model": SuccessResponse,
                    "description": "List resources successfully.",
                },
            },
        )
        async def list_resources(query: ResourceQuery = None,) -> JSONResponse | HTTPException:
            """
            The list resources API endpoint. <br>
            The API will return the current miner resource and their detail specs on the validator. <br>
            token: The user token for the authorization. <br>
            """
            if True:
                db = ComputeDb()
                specs_details = get_miner_details(db)

                bt.logging.info(f"API: List resources on compute subnet")

                # Initialize a dictionary to keep track of GPU instances
                resource_list = []
                gpu_instances = {}
                total_gpu_counts = {}

                # Get the allocated hotkeys from wandb
                #if not self.allocation_table:
                allocated_hotkeys = self.wandb.get_allocated_hotkeys([], False)
                #self.allocation_table = allocated_hotkeys
                #else:
                #allocated_hotkeys = self.allocation_table

                if specs_details:
                    # Iterate through the miner specs details and print the table
                    for hotkey, details in specs_details.items():
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
                                    ram_miner["available"] / 1024.0**3
                                )

                                # Extract Hard Disk details
                                hard_disk_miner = details["hard_disk"]
                                hard_disk = "{:.2f}".format(
                                    hard_disk_miner["free"] / 1024.0**3
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
                        allocate_status = "N/A"

                        if hotkey in allocated_hotkeys:
                            allocate_status = "Res."
                        else:
                            allocate_status = "Avail."

                        add_resource = False
                        # Print the row with column separators
                        resource.hotkey = hotkey

                        if query is None or query == {}:
                            add_resource = True
                        else:
                            if query.gpu_name is not None and query.gpu_name not in gpu_name:
                                continue
                            if query.gpu_capacity_max is not None and float(gpu_capacity) > query.gpu_capacity_max:
                                continue
                            if query.gpu_capacity_min is not None and float(gpu_capacity) < query.gpu_capacity_min:
                                continue
                            if query.cpu_count_max is not None and int(cpu_count) > query.cpu_count_max:
                                continue
                            if query.cpu_count_min is not None and int(cpu_count) < query.cpu_count_min:
                                continue
                            if query.ram_total_max is not None and float(ram) > query.ram_total_max:
                                continue
                            if query.ram_total_min is not None and float(ram) < query.ram_total_min:
                                continue
                            if query.hard_disk_total_max is not None and float(hard_disk) > query.hard_disk_total_max:
                                continue
                            if query.hard_disk_total_min is not None and float(hard_disk) < query.hard_disk_total_min:
                                continue
                            add_resource = True

                        if add_resource:
                            resource.cpu_count = cpu_count
                            resource.gpu_name = gpu_name
                            resource.gpu_capacity = gpu_capacity
                            resource.gpu_count = gpu_count
                            resource.ram = ram
                            resource.hard_disk = hard_disk
                            resource.allocate_status = allocate_status
                            resource_list.append(resource)

                    bt.logging.info(f"API: List resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List resources successfully",
                            "data": jsonable_encoder(resource_list),
                        },
                    )

                else:
                    bt.logging.info(f"API: There is no resource available")
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "success": False,
                            "message": "There is no resource available",
                            "err_detail": "No resources found.",
                        },
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "success": False,
                        "message": "Missing authorization",
                        "err_detail": "Please provide the authorization token",
                    },
                )

        @self.app.post("/list/all_runs",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while getting runs from wandb",
                           },
                           200: {
                               "model": SuccessResponse,
                               "description": "List run resources successfully.",
                           },
                       }
        )
        def list_all_runs() -> JSONResponse | HTTPException:
            """
            This function gets all run resources.
            """
            db_specs_dict = {}
            try:
                self.wandb.api.flush()
                filter_rule = {
                    "$and": [
                        {"config.config.netuid": self.config.netuid},
                        {"state": "running"},
                    ]
                }

                runs = self.wandb.api.runs(f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filters=filter_rule)

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_id = run.id
                        run_name = run.name
                        run_description = run.description
                        run_config = run.config
                        configs = run_config.get("config")

                        # check the signature
                        if configs:
                            db_specs_dict[index] = {"id": run_id, "name": run_name, "description": run_description,
                                                    "configs": configs}

                    bt.logging.info(f"API: List run resources successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List run resources successfully.",
                            "data": jsonable_encoder(db_specs_dict),
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
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
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
                401: {"model": ErrorResponse, "description": "Missing authorization"},
                404: {
                    "model": ErrorResponse,
                    "description": "Error occurred while getting specs from wandb",
                },
                200: {
                    "model": SuccessResponse,
                    "description": "List spec resources successfully.",
                },
            },
        )
        async def list_specs() -> JSONResponse | HTTPException:
            """
            The list specs API endpoint. <br>
            """
            db_specs_dict = {}

            try:
                self.wandb.api.flush()
                runs = self.wandb.api.runs(
                    f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}",
                    filters={
                        "$and": [
                            {"config.role": "miner"},
                            {"config.config.netuid": self.config.netuid},
                            {"state": "running"},
                            {"config.specs": {"$exists": True}},
                        ]
                    },
                )

                if runs:
                    # Iterate over all runs in the opencompute project
                    for index, run in enumerate(runs, start=1):
                        # Access the run's configuration
                        run_config = run.config
                        hotkey = run_config.get("hotkey")
                        specs = run_config.get("specs")
                        configs = run_config.get("config")

                        # check the signature
                        if hotkey and specs:
                            db_specs_dict[index] = {"hotkey": hotkey, "configs": configs, "specs": specs}

                    # Return the db_specs_dict for further use or inspection
                    bt.logging.info(f"API: List specs successfully")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List specs successfully",
                            "data": jsonable_encoder(db_specs_dict),
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
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "success": False,
                        "message": "Error occurred while getting specs from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post("/list/run_by_name",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while getting run from wandb",
                           },
                           200: {
                               "model": SuccessResponse,
                               "description": "List run resources successfully.",
                           },
                       }
        )
        def list_run_name(run_name: str) -> JSONResponse | HTTPException:
            """
            This function gets runs by name.
            """
            db_specs_dict = {}
            try:
                self.wandb.api.flush()
                filter_rule = {
                    "$and": [
                        {"config.config.netuid": self.config.netuid},
                        {"display_name": run_name},
                        {"state": "running"},
                    ]
                }

                runs = self.wandb.api.runs(f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filters=filter_rule)

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

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "success": False,
                        "message": "Error occurred while run from wandb",
                        "err_detail": e.__repr__(),
                    },
                )

        @self.app.post("/list/available",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while fetch available miner from wandb",
                           },
                           200: {
                               "model": SuccessResponse,
                               "description": "List available resources successfully.",
                           },
                       }
        )

        def list_available_miner(rent_status: bool = False) -> JSONResponse | HTTPException:
            """
            This function gets all available miners.
            """
            db_specs_dict = {}
            try:
                self.wandb.api.flush()
                if rent_status:
                    filter_rule = {
                        "config.allocated": {"$regex": "\d.*"},
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

                runs = self.wandb.api.runs(f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filters=filter_rule)
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
                            db_specs_dict[index] = {"hotkey": hotkey, "details": specs}
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
                            "data": jsonable_encoder(db_specs_dict),
                        },
                    )
                else:
                    bt.logging.info(f"API: List available miners is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List available miners",
                            "data": jsonable_encoder(db_specs_dict),
                        },
                    )

            except Exception as e:
                # Handle the exception by logging an error message
                bt.logging.error(f"API: An error occurred while fetching available miner from wandb: {e}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "success": False,
                        "message": "Error occurred while fetching available miner from wandb",
                        "err_detail": e.__repr__(),
                    },
                )


        @self.app.post("/list/allocated_hotkeys",
                       tags=["WandB"],
                       response_model=SuccessResponse | ErrorResponse,
                       responses={
                           401: {"model": ErrorResponse, "description": "Missing authorization"},
                           404: {
                               "model": ErrorResponse,
                               "description": "Error occurred while fetch allocated hotkey from wandb",
                           },
                           200: {
                               "model": SuccessResponse,
                               "description": "List available resources successfully.",
                           },
                       }
        )
        async def list_allocated_hotkeys() -> JSONResponse | HTTPException:
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
                        {"config.allocated_hotkeys": {"$regex": "\d.*"}},
                    ]
                }

                # Query all runs in the project and Filter runs where the role is 'validator'
                validator_runs = self.wandb.api.runs(
                    path=f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}", filters=filter_rule
                )

                # Check if the runs list is empty
                if not validator_runs:
                    bt.logging.info(f"API: No validator with allocated info in the project opencompute.")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "No validator with allocated info in the project opencompute.",
                            "data": {},
                        },
                    )

            except Exception as e:
                bt.logging.error(f"API: list_allocated_hotkeys error with {e.__repr__()}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
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
                    hotkey = run_config.get("hotkey")
                    allocated_keys = run_config.get("allocated_hotkeys")
                    id = run_config.get("id")
                    name = run_config.get("name")

                    # valid_validator_hotkey = hotkey in valid_validator_hotkeys

                    # Allow all validator hotkeys for data retrieval only

                    # if verify_run(id,name, hotkey, allocated_keys) and allocated_keys and valid_validator_hotkey:
                    allocated_keys_list.extend(allocated_keys)  # Add the keys to the list

                    bt.logging.info(f"API: List allocated hotkeys is success")
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={
                            "success": True,
                            "message": "List allocated hotkeys",
                            "data": jsonable_encoder(allocated_keys_list),
                        },
                    )

                except Exception as e:
                    bt.logging.error(f"API: Run ID: {run.id}, Name: {run.name}, Error: {e}")

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
    def _get_config(user_config: UserConfig, requirements: Union[Requirement, None] = None):
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
        # args = [f"--{v.alias}";getattr(entry,k) for entry in [user_config, requirements] for k, v in entry.__fields__.items()]
        args_list = []
        for entry in [user_config, requirements]:
            if entry:
                for k, v in entry.__fields__.items():
                    args_list.append(f"--{(v.alias)}")
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


    def _allocate_container(self, device_requirement, timeline, public_key):
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
                ),
                timeout=60,
            )
            if register_response and register_response["status"] is True:
                register_response["ip"] = axon.ip
                register_response["hotkey"] = axon.hotkey
                return register_response

        # Close the db connection
        db.close()

        return {"status": False, "msg": "Requested resource is not available."}


    def _allocate_container_hotkey(self, requirements, hotkey, timeline, public_key):
        """
        Allocate the container with the given hotkey. <br>
        Generate ssh connection for given device requirements and timeline. <br>
        """
        device_requirement = {
            "cpu": {"count": 1},
            "gpu": {},
            "hard_disk": {"capacity": 1073741824},
            "ram": {"capacity": 1073741824},
        }
        device_requirement["gpu"] = {
            "count": 1,
            "capacity": int(requirements.gpu_size) * 1024,
            "type": requirements.gpu_type,
        }

        # Instantiate the connection to the db
        axon_candidate = []
        for axon in self.metagraph.axons:
            if axon.hotkey == hotkey:
                check_allocation = self.dendrite.query(
                    axon,
                    Allocate(
                        timeline=timeline,
                        device_requirement=device_requirement,
                        checking=True,
                    ),
                    timeout=60,
                )
                if check_allocation and check_allocation["status"] is True:
                    register_response = self.dendrite.query(
                        axon,
                        Allocate(
                            timeline=timeline,
                            device_requirement=device_requirement,
                            checking=False,
                            public_key=public_key,
                        ),
                        timeout=60,
                    )
                    if register_response and register_response["status"] is True:
                        register_response["ip"] = axon.ip
                        register_response["hotkey"] = axon.hotkey
                        return register_response

        return {"status": False, "msg": "Requested resource is not available."}

    def _update_allocation_wandb(self,):
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
            self.wandb.update_allocated_hotkeys(hotkey_list)
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
                bt.logging.info(f"API: Metagraph refreshed")
                await asyncio.sleep(DATA_SYNC_PERIOD)

    async def _refresh_allocation(self):
        """
        Refresh the allocation by resync_period. <br>
        """
        while True:
            self.allocation_table = self.wandb.get_allocated_hotkeys([], False)
            bt.logging.info(f"API: Allocation refreshed: {self.allocation_table}")
            await asyncio.sleep(DATA_SYNC_PERIOD)

    def _authenticate_user(self, user_id: str, user_password: str) -> Union[str, bool]:
        """
        Authenticate the user with wallet hotkey and public_key info. <br>
        """
        if not user_id or not user_password:
            return False
        if user_id == self.wallet.hotkey.ss58_address:
            if user_password == self.wallet.hotkey.public_key.hex():
                return user_id
            else:
                return False
        else:
            return False

    def _create_access_token(self, data: dict, expires_delta: timedelta | None = None) -> str:
        """
        Create the access token for the user. <br>
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(
                minutes=ACCESS_TOKEN_EXPIRE_MINUTES
            )
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, self.access_api_key, algorithm=ALGORITHM)
        return encoded_jwt

    def _verify_access_token(self, token: oauth2_token_scheme,) -> Union[Any, None]:
        """
        Verify the access token for the user. <br>
        """
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        try:
            payload = jwt.decode(token, self.access_api_key, algorithms=[ALGORITHM])
            username: str = payload.get("sub")
            if username is None:
                raise credentials_exception
            token_data = TokenData(username=username)
            return payload

        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credential is Expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
            # return {"error": "Invalid token"}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=e.__repr__(),
                headers={"WWW-Authenticate": "Bearer"},
            )

    def run(self):
        """
        Run the FastAPI app. <br>
        """
        uvicorn.run(
            self.app,
            host=self.ip_addr,
            port=self.port,
            log_level="error",
            ssl_keyfile="key.pem",
            ssl_certfile="cert.pem",
            ssl_cert_reqs=1,
            ssl_ca_certs="cert.pem",
        )

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