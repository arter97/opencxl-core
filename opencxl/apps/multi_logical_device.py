"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from asyncio import gather, create_task
from typing import List

from opencxl.util.component import RunnableComponent
from opencxl.cxl.device.cxl_type3_device import CxlType3Device, CXL_T3_DEV_TYPE
from opencxl.cxl.component.switch_connection_client import SwitchConnectionClient
from opencxl.cxl.component.cxl_component import CXL_COMPONENT_TYPE
from opencxl.cxl.component.cxl_packet_processor import FifoGroup


class MultiLogicalDevice(RunnableComponent):
    def __init__(
        self,
        num_ld,
        port_index: int,
        memory_sizes: List[int],
        memory_files: List[str],
        host: str = "0.0.0.0",
        port: int = 8000,
        test_mode: bool = False,
        cxl_connections=None,
    ):
        label = f"Port{port_index}"
        super().__init__(label)

        self._cxl_type3_devices = []
        self._test_mode = test_mode

        assert (
            not test_mode or cxl_connections is not None
        ), "get_cxl_connection must be passed in test mode"
        assert (
            test_mode or cxl_connections is None
        ), "get_cxl_connection must not be passed in non-test mode"

        if cxl_connections is not None:
            self._get_cxl_connection = cxl_connections
        else:
            self._sw_conn_client = SwitchConnectionClient(
                port_index, CXL_COMPONENT_TYPE.LD, num_ld=num_ld, host=host, port=port
            )
            self._get_cxl_connection = self._sw_conn_client.get_cxl_connection()

        base_outgoing = FifoGroup(
            self._get_cxl_connection[0].cfg_fifo.target_to_host,
            self._get_cxl_connection[0].mmio_fifo.target_to_host,
            self._get_cxl_connection[0].cxl_mem_fifo.target_to_host,
            self._get_cxl_connection[0].cxl_cache_fifo.target_to_host,
        )

        # Share the outgoing queue across multiple LDs
        # TODO: avoid creation at all
        if num_ld > 1:
            for i in range(1, num_ld):
                connection = self._get_cxl_connection[i]
                connection.cfg_fifo.target_to_host = base_outgoing.cfg_space
                connection.mmio_fifo.target_to_host = base_outgoing.mmio
                connection.cxl_mem_fifo.target_to_host = base_outgoing.cxl_mem
                connection.cxl_cache_fifo.target_to_host = base_outgoing.cxl_cache

        for ld in range(num_ld):
            cxl_type3_device = CxlType3Device(
                transport_connection=self._get_cxl_connection[ld],
                memory_size=memory_sizes[ld],
                memory_file=memory_files[ld],
                dev_type=CXL_T3_DEV_TYPE.MLD,
                label=label,
                ld_id=ld,
            )
            self._cxl_type3_devices.append(cxl_type3_device)

    async def _run(self):
        run_tasks = [create_task(device.run()) for device in self._cxl_type3_devices]
        wait_tasks = [create_task(device.wait_for_ready()) for device in self._cxl_type3_devices]
        if not self._test_mode:
            run_tasks += [create_task(self._sw_conn_client.run())]
            wait_tasks += [create_task(self._sw_conn_client.wait_for_ready())]

        await gather(*wait_tasks)
        await self._change_status_to_running()
        await gather(*run_tasks)

    async def _stop(self):
        stop_tasks = [create_task(device.stop()) for device in self._cxl_type3_devices]
        if not self._test_mode:
            stop_tasks += [create_task(self._sw_conn_client.stop())]

        await gather(*stop_tasks)
