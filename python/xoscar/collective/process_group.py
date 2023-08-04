# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from abc import ABC, abstractmethod
from typing import Any, List, Optional

from ..tests.core import lazy_import
from ..utils import is_linux
from . import xoscar_pygloo as xp
from .common import (
    RENDEZVOUS_MASTER_IP_ENV_KEY,
    RENDEZVOUS_MASTER_PORT_ENV_KEY,
    AllReduceAlgorithm,
    AllReduceAlgorithmMappingGloo,
    CollectiveReduceOp,
    ReduceOpMappingGloo,
    TypeMappingGloo,
)
from .nccl_backend import TCPStore, XoscarNCCLBackend
from .utils import convert_data_to_np_array

cupy = lazy_import("cupy")
if cupy != None:
    from .common import ReduceOpMappingNCCL, ReduceOpMappingNCCLStr, TypeMappingNCCL


class _World:
    def __init__(self):
        self._store = None
        self._device = None
        self._backend = None
        self._nccl_size = 0

    @property
    def store(self):
        return self._store

    @property
    def device(self):
        return self._device

    @store.setter  # type: ignore
    def store(self, store):
        self._store = store

    @device.setter  # type: ignore
    def device(self, device):
        self._device = device


_world = _World()


class ProcessGroup(ABC):
    class Options:
        master_ip: Optional[str] = None
        master_port: Optional[int] = None

    def __init__(
        self,
        rank: int,
        world_size: int,
        group_name: Optional[str] = None,
        pg_options: Optional[Options] = None,
    ):
        self._rank = rank
        self._world_size = world_size
        self._group_name = group_name
        self._option = pg_options

    @property
    def rank(self):
        return self._rank

    @property
    def world_size(self):
        return self._world_size

    @property
    def name(self):
        return self._group_name

    @property
    def options(self):
        return self._option

    @abstractmethod
    def allreduce(self, *args, **kwargs):
        """All reduce function"""

    @abstractmethod
    def reduce(self, *args, **kwargs):
        """Reduce function"""

    @abstractmethod
    def allgather(self, *args, **kwargs):
        """All gather function"""

    @abstractmethod
    def gather(self, *args, **kwargs):
        """Gather function"""

    @abstractmethod
    def scatter(self, *args, **kwargs):
        """Scatter function"""

    @abstractmethod
    def reduce_scatter(self, *args, **kwargs):
        """Reduce scatter function"""

    @abstractmethod
    def alltoall(self, *args, **kwargs):
        """All to all function"""

    @abstractmethod
    def broadcast(self, *args, **kwargs):
        """Broadcast function"""


class ProcessGroupGloo(ProcessGroup):
    def __init__(
        self,
        ip: str,
        rank: int,
        world_size: int,
        group_name: Optional[str] = None,
        pg_options: Optional[ProcessGroup.Options] = None,
    ):
        super().__init__(rank, world_size, group_name, pg_options)
        if _world.store is None:
            master_ip = (
                pg_options.master_ip
                if pg_options is not None
                else os.environ.get(RENDEZVOUS_MASTER_IP_ENV_KEY, None)
            )
            master_port = (
                pg_options.master_port
                if pg_options is not None
                else os.environ.get(RENDEZVOUS_MASTER_PORT_ENV_KEY, None)
            )
            if master_ip is None or master_port is None:
                raise ValueError("Cannot find master ip or port for rendezvous")

            opt = xp.rendezvous.TCPStoreOptions()
            opt.port = int(master_port)
            opt.numWorkers = world_size
            opt.isServer = rank == 0

            store = xp.rendezvous.TCPStore(master_ip, opt)
            if not is_linux():
                attr = xp.transport.uv.attr(ip)  # type: ignore
                dev = xp.transport.uv.CreateDevice(attr)  # type: ignore
            else:
                attr = xp.transport.tcp.attr(ip)
                dev = xp.transport.tcp.CreateDevice(attr)  # type: ignore
            _world.store = store  # type: ignore
            _world.device = dev  # type: ignore
        else:
            store = _world.store
            dev = _world.device

        prefix_store = xp.rendezvous.PrefixStore(group_name or str(world_size), store)  # type: ignore
        context = xp.rendezvous.Context(rank, world_size)
        context.connectFullMesh(prefix_store, dev)
        self._context = context

    def reduce(
        self,
        send_data: Any,
        recv_data: Any,
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
        root: Optional[int] = 0,
        tag: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        size = send_buf.size
        dtype = send_buf.dtype
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.reduce(
            self._context,
            sendptr,
            recvptr,
            size,
            gloo_type,
            ReduceOpMappingGloo[op],
            root,
            tag,
        )

    def allreduce(
        self,
        send_data: Any,
        recv_data: Any,
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
        algorithm: AllReduceAlgorithm = AllReduceAlgorithm.RING,
        tag: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        size = send_buf.size
        dtype = send_buf.dtype
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.allreduce(
            self._context,
            sendptr,
            recvptr,
            size,
            gloo_type,
            ReduceOpMappingGloo[op],
            AllReduceAlgorithmMappingGloo[algorithm],
            tag,  # type: ignore
        )

    def gather(
        self,
        send_data: Any,
        recv_data: Any,
        root: Optional[int] = 0,
        tag: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        size = send_buf.size
        dtype = send_buf.dtype
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.gather(self._context, sendptr, recvptr, size, gloo_type, root, tag)

    def allgather(self, send_data: Any, recv_data: Any, tag: Optional[int] = 0):
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        size = send_buf.size
        dtype = send_buf.dtype
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.allgather(self._context, sendptr, recvptr, size, gloo_type, tag)

    def scatter(
        self,
        send_data: List[Any],
        recv_data: Any,
        root: Optional[int] = 0,
        tag: Optional[int] = 0,
    ):
        send_bufs = [convert_data_to_np_array(d) for d in send_data]
        recv_buf = convert_data_to_np_array(recv_data)
        size = sum([d.size for d in send_bufs])
        dtype = recv_buf.dtype
        sendptrs = [d.ctypes.data for d in send_bufs]
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.scatter(self._context, sendptrs, recvptr, size, gloo_type, root, tag)  # type: ignore

    def reduce_scatter(
        self,
        send_data: Any,
        recv_data: Any,
        recv_elems: List[int],
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
    ):  # pragma: no cover
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        size = send_buf.size
        dtype = send_buf.dtype
        gloo_type = TypeMappingGloo[dtype.type]
        xp.reduce_scatter(
            self._context,
            sendptr,
            recvptr,
            size,
            recv_elems,
            gloo_type,
            ReduceOpMappingGloo[op],
        )

    def alltoall(self, send_data: Any, recv_data: Any, tag: Optional[int] = 0):
        send_buf = convert_data_to_np_array(send_data)
        recv_buf = convert_data_to_np_array(recv_data)
        size = send_buf.size
        dtype = send_buf.dtype
        sendptr = send_buf.ctypes.data
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.all_to_all(self._context, sendptr, recvptr, size, gloo_type, tag)

    def broadcast(
        self,
        send_data: Any,
        recv_data: Any,
        root: Optional[int] = 0,
        tag: Optional[int] = 0,
    ):
        if send_data is not None:
            send_buf = convert_data_to_np_array(send_data)
            sendptr = send_buf.ctypes.data
        else:
            sendptr = None
        recv_buf = convert_data_to_np_array(recv_data)
        size = recv_buf.size
        dtype = recv_buf.dtype
        recvptr = recv_buf.ctypes.data
        gloo_type = TypeMappingGloo[dtype.type]
        xp.broadcast(
            self._context,
            recvptr if sendptr is None else sendptr,
            recvptr,
            size,
            gloo_type,
            root,
            tag,
        )


class ProcessGroupNCCL(ProcessGroup):
    def __init__(
        self,
        ip: str,
        rank: int,
        device_id: int,
        world_size: int,
        group_name: Optional[str] = None,
        pg_options: Optional[ProcessGroup.Options] = None,
    ):
        assert (
            cupy != None
        ), "cupy is required when creating a group using nccl as backend."
        from cupy.cuda import nccl

        super().__init__(rank, world_size, group_name, pg_options)
        cupy.cuda.Device(device_id).use()
        if _world._backend is None:
            master_ip = (
                pg_options.master_ip
                if pg_options is not None
                else os.environ.get(RENDEZVOUS_MASTER_IP_ENV_KEY, None)
            )
            master_port = (
                pg_options.master_port
                if pg_options is not None
                else os.environ.get(RENDEZVOUS_MASTER_PORT_ENV_KEY, None)
            )
            if master_ip is None or master_port is None:
                raise ValueError("Cannot find master ip or port for rendezvous")
            store = TCPStore(world_size)
            backend = XoscarNCCLBackend(
                world_size, rank, store, master_ip, int(master_port)
            )
            _world._backend = backend
            _world._nccl_size = world_size
            self._is_world = True
            self._backend = backend
        else:
            self._is_world = False
            if rank == 0:
                commId = nccl.get_unique_id()
                ccid = cupy.array(commId)
                for i in range(1, world_size):
                    _world._backend.send(ccid, i, None)
            else:
                commId = (int(i) for i in range(128))
                commId = tuple(commId)
                ccid = cupy.array(commId, dtype="int64")
                _world._backend.recv(ccid, 0, None)
                commId = tuple(ccid.tolist())
            self._backend = nccl.NcclCommunicator(world_size, commId, rank)

    def reduce(
        self,
        send_data: Any,
        recv_data: Any,
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
        root: Optional[int] = 0,
        stream: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            self._backend.reduce(
                send_buf, recv_buf_cp, root, ReduceOpMappingNCCLStr[op], stream
            )
        else:
            self._backend.reduce(
                send_buf.data.ptr,
                recv_buf_cp.data.ptr,
                send_buf.size,
                TypeMappingNCCL[dtype.type],
                ReduceOpMappingNCCL[op],
                root,
                stream,
            )
        recv_buf[:] = cupy.asnumpy(recv_buf_cp)

    def allreduce(
        self,
        send_data: Any,
        recv_data: Any,
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
        stream: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            self._backend.all_reduce(
                send_buf, recv_buf_cp, ReduceOpMappingNCCLStr[op], stream
            )
        else:
            self._backend.allReduce(
                send_buf.data.ptr,
                recv_buf_cp.data.ptr,
                send_buf.size,
                TypeMappingNCCL[dtype.type],
                ReduceOpMappingNCCL[op],
                stream,
            )
        recv_buf[:] = cupy.asnumpy(recv_buf_cp)

    def gather(
        self,
        send_data: Any,
        recv_data: Any,
        root: Optional[int] = 0,
        stream: Optional[int] = 0,
    ):
        assert (
            send_data.size * self.world_size == recv_data.size
        ), "Send_size * world_number must be equal to recv_size"
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf).reshape((1, -1))
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            self._backend.gather(send_buf, recv_buf_cp, root, stream)
            recv_buf[:] = cupy.asnumpy(recv_buf_cp)
        else:
            if self.rank == root:
                buffs = []
                for peer in range(self.world_size):
                    if peer == self.rank:
                        buffs.append(send_buf)
                    else:
                        temp_recv = send_buf.copy()
                        self._backend.recv(
                            temp_recv.data.ptr,
                            temp_recv.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
                        buffs.append(temp_recv)

                cupy_buff = cupy.concatenate(buffs)
                recv_buf[:] = cupy.asnumpy(cupy_buff.reshape(recv_buf.shape))
            else:
                self._backend.send(
                    send_buf.data.ptr,
                    send_buf.size,
                    TypeMappingNCCL[dtype.type],
                    root,
                    stream,
                )

    def allgather(self, send_data: Any, recv_data: Any, stream: Optional[int] = 0):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            self._backend.all_gather(send_buf, recv_buf_cp, send_buf.size, stream=None)
        else:
            self._backend.allGather(
                send_buf.data.ptr,
                recv_buf_cp.data.ptr,
                send_buf.size,
                TypeMappingNCCL[dtype.type],
                stream,
            )
        recv_buf[:] = cupy.asnumpy(recv_buf_cp)

    def scatter(
        self,
        send_data: List[Any],
        recv_data: Any,
        root: Optional[int] = 0,
        stream: Optional[int] = 0,
    ):
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            import numpy as np

            send_buf = convert_data_to_np_array(
                np.concatenate(send_data).reshape(self.world_size, -1)
            )
            send_buf = cupy.asarray(send_buf)
            stream = stream if stream != 0 else None
            self._backend.scatter(send_buf, recv_buf_cp, root, stream)
            recv_buf[:] = cupy.asnumpy(recv_buf_cp)
        else:
            if self.rank == root:
                assert (
                    len(send_data) == self.world_size
                ), "Scatter size must be equal to the size of group"
                for peer in range(self.world_size):
                    send_buf = convert_data_to_np_array(send_data[peer])
                    if peer == root:
                        recv_buf[:] = send_buf
                    else:
                        dtype = send_buf.dtype
                        send_buf = cupy.asarray(send_buf).reshape((1, -1))
                        self._backend.send(
                            send_buf.data.ptr,
                            send_buf.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
            else:
                dtype = recv_buf.dtype
                self._backend.recv(
                    recv_buf_cp.data.ptr,
                    recv_buf_cp.size,
                    TypeMappingNCCL[dtype.type],
                    root,
                    stream,
                )
                recv_buf[:] = cupy.asnumpy(recv_buf_cp)

    def reduce_scatter(
        self,
        send_data: Any,
        recv_data: Any,
        recv_elems: List[int],
        op: CollectiveReduceOp = CollectiveReduceOp.SUM,
        stream: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            self._backend.reduce_scatter(
                send_buf,
                recv_buf_cp,
                recv_elems[self.rank],
                ReduceOpMappingNCCLStr[op],
                stream,
            )
        else:
            self._backend.reduceScatter(
                send_buf.data.ptr,
                recv_buf_cp.data.ptr,
                recv_elems[self.rank],
                TypeMappingNCCL[dtype.type],
                ReduceOpMappingNCCL[op],
                stream,
            )
        recv_buf[:] = cupy.asnumpy(recv_buf_cp)

    def alltoall(self, send_data: Any, recv_data: Any, stream: Optional[int] = 0):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        assert (
            self.world_size == send_buf.shape[0]
        ), "The first dim of send data must be equal to world size."
        assert (
            recv_buf.size == send_buf.size
        ), "The size of send data must be equal to the size of recv data."
        buffs = []
        if self._is_world:
            stream = stream if stream != 0 else None
            recv_buf_cp = cupy.asarray(recv_buf)
            self._backend.all_to_all(send_buf, recv_buf_cp, stream)
            recv_buf[:] = cupy.asnumpy(recv_buf_cp)
        else:
            for peer in range(self.world_size):
                if peer == self.rank:
                    buffs.append(send_buf[peer : peer + 1])
                else:
                    temp_send = send_buf[peer : peer + 1].copy()
                    temp_recv = cupy.asarray(recv_buf[peer : peer + 1].copy())
                    if self.rank > peer:
                        self._backend.recv(
                            temp_recv.data.ptr,
                            temp_recv.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
                        self._backend.send(
                            temp_send.data.ptr,
                            temp_send.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
                    else:
                        self._backend.send(
                            temp_send.data.ptr,
                            temp_send.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
                        self._backend.recv(
                            temp_recv.data.ptr,
                            temp_recv.size,
                            TypeMappingNCCL[dtype.type],
                            peer,
                            stream,
                        )
                    buffs.append(temp_recv)
            recv_buf[:] = cupy.asnumpy(cupy.concatenate(buffs))

    def broadcast(
        self,
        send_data: Any,
        recv_data: Any,
        root: Optional[int] = 0,
        stream: Optional[int] = 0,
    ):
        send_buf = convert_data_to_np_array(send_data)
        dtype = send_buf.dtype
        send_buf = cupy.asarray(send_buf)
        recv_buf = convert_data_to_np_array(recv_data)
        recv_buf_cp = cupy.asarray(recv_buf)
        if self._is_world:
            stream = stream if stream != 0 else None
            if self._rank == root:
                self._backend.broadcast(send_buf, root, stream)
                recv_buf_cp[:] = send_buf
            else:
                self._backend.broadcast(recv_buf_cp, root, stream=None)
        else:
            self._backend.broadcast(
                send_buf.data.ptr,
                recv_buf_cp.data.ptr,
                send_buf.size,
                TypeMappingNCCL[dtype.type],
                root,
                stream,
            )
        recv_data[:] = cupy.asnumpy(recv_buf_cp)
