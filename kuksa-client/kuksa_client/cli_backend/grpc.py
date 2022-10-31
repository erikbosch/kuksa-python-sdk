########################################################################
# Copyright (c) 2022 Robert Bosch GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0
########################################################################


import asyncio
import json
import pathlib
import queue
from typing import Callable
import uuid

from kuksa_client import cli_backend
import kuksa_client.grpc
from kuksa_client.grpc import VSSValue


def callback_wrapper(callback: Callable[[str], None]) -> Callable[[VSSValue], None]:
    def wrapper(value: VSSValue) -> None:
        callback(json.dumps({
            'path': value.path,
            'val': value.val,
            'timestamp': value.timestamp.isoformat() if value.timestamp else value.timestamp,
        }))
    return wrapper


class Backend(cli_backend.Backend):
    def __init__(self, config):
        super().__init__(config)
        self.cacertificate = pathlib.Path(self.cacertificate)
        self.keyfile = pathlib.Path(self.keyfile)
        self.certificate = pathlib.Path(self.certificate)
        self.tokenfile = pathlib.Path(
            config.get('token', str(self.default_cert_path / 'jwt/all-read-write.json.token')),
        )
        self.grpcConnected = False

        self.sendMsgQueue = queue.Queue()
        self.run = False

        self.AttrDict = {
            "value": kuksa_client.grpc.RequestType.CURRENT_VALUE,
            "targetValue": kuksa_client.grpc.RequestType.TARGET_VALUE,
            "metadata": kuksa_client.grpc.RequestType.METADATA,
        }

    # Function to check connection status
    def checkConnection(self):
        if self.grpcConnected:
            return True
        return False

    # Function to stop the communication
    def stop(self):
        self.run = False
        print("gRPC channel disconnected.")

    # Function to implement fetching of metadata
    def getMetaData(self, path, timeout=5):
        return self.getValue(path, "metadata", timeout)

    # Function to implement get
    def getValue(self, path, attribute="value", timeout=5):
        if attribute in self.AttrDict:
            requestArgs = {'type': self.AttrDict[attribute], 'paths': (path,)}
            return self._sendReceiveMsg(("get", requestArgs), timeout)

        return json.dumps({"error": "Invalid Attribute"})

    # Function to implement set
    def setValue(self, path, value, attribute="value", timeout=5):
        if attribute in self.AttrDict:
            requestArgs = {'type': self.AttrDict[attribute], 'values': (kuksa_client.grpc.VSSValue(path=path, val=value),)}
            return self._sendReceiveMsg(("set", requestArgs), timeout)

        return json.dumps({"error": "Invalid Attribute"})

    # Function for authorization
    def authorize(self, tokenfile=None, timeout=2):
        if tokenfile is None:
            tokenfile = self.tokenfile
        tokenfile = pathlib.Path(tokenfile)
        requestArgs = {'token': tokenfile.expanduser().read_text(encoding='utf-8')}

        return self._sendReceiveMsg(("authorize", requestArgs), timeout)

    # Subscribe value changes of to a given path.
    # The given callback function will be called then, if the given path is updated.
    def subscribe(self, path, callback, attribute = "value", timeout = 5):
        if attribute in self.AttrDict:
            requestArgs = {'path': path, 'type': self.AttrDict[attribute], 'callback': callback_wrapper(callback), 'start': True}
            return self._sendReceiveMsg(("subscribe", requestArgs), timeout)

        return json.dumps({"error": "Invalid Attribute"})

    # Unsubscribe value changes of to a given path.
    # The subscription id from the response of the corresponding subscription request will be required
    def unsubscribe(self, sub_id, timeout = 5):
        try:
            sub_id = uuid.UUID(sub_id)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        requestArgs = {'subscription_id': sub_id}
        return self._sendReceiveMsg(("unsubscribe", requestArgs), timeout)

    def _sendReceiveMsg(self, req, timeout):
        (call, requestArgs) = req
        recvQueue = queue.Queue(maxsize=1)
        self.sendMsgQueue.put((call, requestArgs, recvQueue))
        try:
            resp, error = recvQueue.get(timeout=timeout)
            if error:
                respJson = json.dumps({"error": error})
            elif resp:
                respJson = json.dumps(resp, cls=kuksa_client.grpc.VSSJSONEncoder, indent=4)
            else:
                respJson = "OK"
        except queue.Empty:
            respJson = json.dumps({"error": "Timeout"})
        return respJson

    # Async function to handle the gRPC calls
    async def _grpcHandler(self, vss_client):
        self.grpcConnected = True
        self.run = True
        while self.run:
            try:
                (call, requestArgs, responseQueue) = self.sendMsgQueue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            try:
                if call == "get":
                    respObj = await vss_client.get(**requestArgs)
                    respObj = respObj[0] if respObj else None
                elif call == "set":
                    respObj = await vss_client.set(**requestArgs)
                elif call == "authorize":
                    respObj = await vss_client.authorize(**requestArgs)
                elif call == "subscribe":
                    respObj = await vss_client.subscribe(**requestArgs)
                    respObj = {"subscriptionId": str(respObj)}
                elif call == "unsubscribe":
                    respObj = await vss_client.unsubscribe(**requestArgs)
                else:
                    raise Exception("Not Implemented.")

                responseQueue.put((respObj, None))
            except Exception as exc:  # pylint: disable=broad-except
                responseQueue.put((None, str(exc)))

        self.grpcConnected = False

    # Main loop for handling gRPC communication
    async def mainLoop(self):
        if self.insecure:
            async with kuksa_client.grpc.VSSClient(self.serverIP, self.serverPort) as vss_client:
                print("gRPC channel connected.")
                await self._grpcHandler(vss_client)
        else:
            async with kuksa_client.grpc.VSSClient(
                self.serverIP,
                self.serverPort,
                root_certificates=self.cacertificate,
                private_key=self.keyfile,
                certificate_chain=self.certificate,
            ) as vss_client:
                print("Secure gRPC channel connected.")
                await self._grpcHandler(vss_client)