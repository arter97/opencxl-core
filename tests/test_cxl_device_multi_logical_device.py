"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from asyncio import gather, create_task
import asyncio
import struct
from typing import cast, Optional, Dict, Union, List
import pytest

from opencxl.apps.multi_logical_device import MultiLogicalDevice
from opencxl.cxl.component.common import CXL_COMPONENT_TYPE
from opencxl.cxl.component.cxl_packet_processor import CxlPacketProcessor
from opencxl.cxl.component.hdm_decoder import DecoderInfo
from opencxl.cxl.component.packet_reader import PacketReader
from opencxl.cxl.device.cxl_type3_device import CxlType3Device, CXL_T3_DEV_TYPE
from opencxl.cxl.device.root_port_device import CxlRootPortDevice
from opencxl.cxl.component.cxl_connection import CxlConnection
from opencxl.util.number_const import MB
from opencxl.util.logger import logger
from opencxl.util.pci import create_bdf
from opencxl.cxl.transport.transaction import (
    CxlIoMemRdPacket,
    CxlIoMemWrPacket,
    CxlIoCompletionWithDataPacket,
    is_cxl_io_completion_status_sc,
)
from opencxl.cxl.transport.transaction import (
    CxlIoCfgRdPacket,
    CxlIoCfgWrPacket,
    CxlIoMemRdPacket,
    CxlIoMemWrPacket,
    CxlIoCompletionWithDataPacket,
    CxlMemMemRdPacket,
    CxlMemMemWrPacket,
    CxlMemMemDataPacket,
    CxlMemBIRspPacket,
    CxlMemBISnpPacket,
    CxlMemCmpPacket,
    CXL_IO_CPL_STATUS,
    CXL_MEM_M2SBIRSP_OPCODE,
    CXL_MEM_S2MBISNP_OPCODE,
)


def test_multi_logical_device():
    memory_size = 256 * MB
    memory_file = "mem.bin"
    transport_connection = CxlConnection()
    CxlType3Device(
        transport_connection,
        memory_size=memory_size,
        memory_file=memory_file,
        dev_type=CXL_T3_DEV_TYPE.MLD,
    )


@pytest.mark.asyncio
async def test_multi_logical_device_run_stop(get_gold_std_reg_vals):
    memory_size = 256 * MB
    memory_file = "mem.bin"
    transport_connection = CxlConnection()
    device = CxlType3Device(
        transport_connection,
        memory_size=memory_size,
        memory_file=memory_file,
        dev_type=CXL_T3_DEV_TYPE.MLD,
    )

    # No register values for MLD yet
    # # check register values after initialization
    # reg_vals = str(device.get_reg_vals())
    # reg_vals_expected = get_gold_std_reg_vals("SLD")
    # assert reg_vals == reg_vals_expected

    async def wait_and_stop():
        await device.wait_for_ready()
        await device.stop()

    tasks = [create_task(device.run()), create_task(wait_and_stop())]
    await gather(*tasks)


@pytest.mark.asyncio
async def test_multi_logical_device_enumeration():
    memory_size = 256 * MB
    memory_file = "mem.bin"
    transport_connection = CxlConnection()
    root_port_device = CxlRootPortDevice(downstream_connection=transport_connection, label="Port0")
    device = CxlType3Device(
        transport_connection,
        memory_size=memory_size,
        memory_file=memory_file,
        dev_type=CXL_T3_DEV_TYPE.MLD,
    )
    memory_base_address = 0xFE000000

    async def wait_and_stop():
        await device.wait_for_ready()
        await root_port_device.enumerate(memory_base_address)
        await device.stop()

    tasks = [create_task(device.run()), create_task(wait_and_stop())]
    await gather(*tasks)


# Test ld_id
@pytest.mark.asyncio
async def test_multi_logical_device_ld_id():
    # Test 4 LDs
    num_ld = 4
    # Test routing to LD-ID 2
    target_ld_id = 2

    ld_size = 256 * MB

    logger.info(f"[PyTest] Creating {num_ld} LDs, testing LD-ID routing to {target_ld_id}")

    # Create MLD instance
    cxl_connections = [CxlConnection() for _ in range(num_ld)]
    mld = MultiLogicalDevice(
        num_ld=num_ld,
        port_index=1,
        memory_sizes=[ld_size] * num_ld,
        memory_files=[f"mld_mem{i}.bin" for i in range(num_ld)],
        test_mode=True,
        cxl_connections=cxl_connections,
    )

    # Setup HDM decoder
    for device in mld._cxl_type3_devices:
        # Use HPA = DPA
        device._cxl_memory_device_component._hdm_decoder_manager.commit(
            0, DecoderInfo(size=ld_size, base=0x0)
        )

    # Start MLD pseudo server
    async def handle_client(reader, writer):
        global mld_pseudo_server_reader, mld_pseudo_server_packet_reader, mld_pseudo_server_writer
        mld_pseudo_server_reader = reader
        mld_pseudo_server_packet_reader = PacketReader(reader, label="test_mmio")
        mld_pseudo_server_writer = writer
        assert mld_pseudo_server_writer is not None, "mld_pseudo_server_writer is NoneType"

    server = await asyncio.start_server(handle_client, "127.0.0.1", 8000)
    # This is cleaned up via 'server.wait_closed()' below
    asyncio.create_task(server.serve_forever())

    await server.start_serving()

    # Setup CxlPacketProcessor for MLD - connect to 127.0.0.1:8000
    mld_packet_processor_reader, mld_packet_processor_writer = await asyncio.open_connection(
        "127.0.0.1", 8000
    )
    mld_packet_processor = CxlPacketProcessor(
        mld_packet_processor_reader,
        mld_packet_processor_writer,
        cxl_connections,
        CXL_COMPONENT_TYPE.LD,
        label=f"ClientPortMld",
    )
    mld_packet_processor_task = create_task(mld_packet_processor.run())

    memory_base_address = 0xFE000000
    bar_size = 65920  # 4096 is for PCI, 65920 is for CXL

    async def configure_bar(
        target_ld_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        packet_reader = PacketReader(reader, label="test_mmio")
        packet_writer = writer

        logger.info("[PyTest] Settting Bar Address")
        # NOTE: Test Config Space Type0 Write - BAR WRITE
        packet = CxlIoCfgWrPacket.create(
            create_bdf(0, 0, 0),
            0x10,
            4,
            value=memory_base_address,
            is_type0=True,
            ld_id=target_ld_id,
        )
        packet_writer.write(bytes(packet))
        await packet_writer.drain()
        # ACK
        packet = await packet_reader.get_packet()
        assert is_cxl_io_completion_status_sc(packet)

    async def test_mmio(
        target_ld_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        packet_reader = PacketReader(reader, label="test_mmio")
        packet_writer = writer

        logger.info("[PyTest] Accessing MMIO register")

        # NOTE: Write 0xDEADBEEF
        data = 0xDEADBEEF
        packet = CxlIoMemWrPacket.create(memory_base_address, 4, data=data, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()

        # NOTE: Confirm 0xDEADBEEF is written
        packet = CxlIoMemRdPacket.create(memory_base_address, 4, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()
        packet = await packet_reader.get_packet()
        assert is_cxl_io_completion_status_sc(packet)
        cpld_packet = cast(CxlIoCompletionWithDataPacket, packet)
        logger.info(f"[PyTest] Received CXL.io packet: {cpld_packet}")
        assert cpld_packet.data == data

        # NOTE: Write OOB (Upper Boundary), Expect No Error
        packet = CxlIoMemWrPacket.create(
            memory_base_address + bar_size, 4, data=data, ld_id=target_ld_id
        )
        packet_writer.write(bytes(packet))
        await packet_writer.drain()

        # NOTE: Write OOB (Lower Boundary), Expect No Error
        packet = CxlIoMemWrPacket.create(memory_base_address - 4, 4, data=data, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()

        # NOTE: Read OOB (Upper Boundary), Expect 0
        packet = CxlIoMemRdPacket.create(memory_base_address + bar_size, 4, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()
        packet = await packet_reader.get_packet()
        assert is_cxl_io_completion_status_sc(packet)
        cpld_packet = cast(CxlIoCompletionWithDataPacket, packet)
        assert cpld_packet.data == 0

        # NOTE: Read OOB (Lower Boundary), Expect 0
        packet = CxlIoMemRdPacket.create(memory_base_address - 4, 4, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()
        packet = await packet_reader.get_packet()
        assert is_cxl_io_completion_status_sc(packet)
        cpld_packet = cast(CxlIoCompletionWithDataPacket, packet)
        assert cpld_packet.data == 0

    async def send_packets(
        target_ld_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        packet_reader = PacketReader(reader, label="send_packets")
        packet_writer = writer

        target_address = 0x80
        target_data = 0xDEADBEEF

        logger.info("[PyTest] Sending CXL.mem request packets from client")
        packet = CxlMemMemWrPacket.create(target_address, target_data, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()

        # Get ACK
        packet = await packet_reader.get_packet()
        # TODO: Check if the ACK is correctly formed

        packet = CxlMemMemRdPacket.create(target_address, ld_id=target_ld_id)
        packet_writer.write(bytes(packet))
        await packet_writer.drain()

        logger.info("[PyTest] Checking CXL.mem request packets received from server")
        packet = await packet_reader.get_packet()
        mem_packet = cast(CxlMemMemRdPacket, packet)
        logger.info(f"[PyTest] Received CXL.mem packet: {hex(mem_packet.data)}")
        assert mem_packet.data == target_data

        # Check MLD bin file
        logger.info("[PyTest] Checking MLD bin file")
        with open(f"mld_mem{target_ld_id}.bin", "rb") as f:
            f.seek(target_address)
            data = f.read(4)
            value = struct.unpack("<I", data)[0]
            assert value == target_data

    async def wait_test_stop():
        await gather(*(device.wait_for_ready() for device in mld._cxl_type3_devices))
        # Test MLD LD-ID handling
        await configure_bar(target_ld_id, mld_pseudo_server_reader, mld_pseudo_server_writer)
        await test_mmio(target_ld_id, mld_pseudo_server_reader, mld_pseudo_server_writer)
        await send_packets(target_ld_id, mld_pseudo_server_reader, mld_pseudo_server_writer)
        # Stop all devices
        await mld_packet_processor.stop()
        await mld_packet_processor_task
        await mld._stop()

    test_task = create_task(wait_test_stop())
    # Start MLD
    await mld._run()

    await test_task

    # Stop pseudo server
    server.close()
    await server.wait_closed()
