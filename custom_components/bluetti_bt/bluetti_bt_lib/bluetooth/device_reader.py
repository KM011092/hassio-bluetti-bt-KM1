"""Device reader."""

import asyncio
import logging
from typing import Any, Callable, List, cast
import async_timeout
from bleak import BleakClient, BleakError

from ..base_devices.BluettiDevice import BluettiDevice
from ..const import NOTIFY_UUID, RESPONSE_TIMEOUT, WRITE_UUID
from ..exceptions import BadConnectionError, ModbusError, ParseError
from ..utils.commands import ReadHoldingRegisters

_LOGGER = logging.getLogger(__name__)


class DeviceReader:
    def __init__(
        self,
        bleak_client: BleakClient,
        bluetti_device: BluettiDevice,
        future_builder_method: Callable[[], asyncio.Future[Any]],
        persistent_conn: bool = False,
        polling_timeout: int = 45,
        max_retries: int = 5,
    ) -> None:
        self.client = bleak_client
        self.bluetti_device = bluetti_device
        self.create_future = future_builder_method
        self.persistent_conn = persistent_conn
        self.polling_timeout = polling_timeout
        self.max_retries = max_retries

        self.has_notifier = False
        self.notify_future: asyncio.Future[Any] | None = None
        self.current_command = None
        self.notify_response = bytearray()

        # polling mutex to guard against switches
        self.polling_lock = asyncio.Lock()

    async def read_data(
        self, filter_registers: List[ReadHoldingRegisters] | None = None
    ) -> dict | None:
        _LOGGER.info("Reading data")

        if self.bluetti_device is None:
            _LOGGER.error("Device is None")
            return None

        polling_commands = self.bluetti_device.polling_commands
        pack_commands = self.bluetti_device.pack_polling_commands
        if filter_registers is not None:
            polling_commands = filter_registers
            pack_commands = []

        parsed_data: dict = {}

        async with self.polling_lock:
            try:
                async with async_timeout.timeout(self.polling_timeout):
                    # Reconnect if not connected
                    for attempt in range(1, self.max_retries + 1):
                        try:
                            if not self.client.is_connected:
                                await self.client.connect()
                            break
                        except Exception as e:
                            if attempt == self.max_retries or attempt == 1:
                                raise  # pass exception on max_retries attempt
                            else:
                                _LOGGER.warning(
                                    f"Connect unsucessful (attempt {attempt}): {e}. Retrying..."
                                )
                                await asyncio.sleep(2)

                    # Attach notifier if needed
                    if not self.has_notifier:
                        await self.client.start_notify(
                            NOTIFY_UUID, self._notification_handler
                        )
                        self.has_notifier = True

                    # Execute polling commands
                    for command in polling_commands:
                        try:
                            body = command.parse_response(
                                await self._async_send_command(command)
                            )
                            _LOGGER.debug("Raw data: %s", body)
                            parsed = self.bluetti_device.parse(
                                command.starting_address, body
                            )
                            _LOGGER.debug("Parsed data: %s", parsed)
                            parsed_data.update(parsed)
                        except ParseError:
                            _LOGGER.warning("Got a parse exception")

                    # Execute pack polling commands
                    if len(pack_commands) > 0:
                        _LOGGER.debug("Polling battery packs")
                        for pack in range(1, self.bluetti_device.pack_num_max + 1):
                            # Set current pack number
                            await self._async_send_command(
                                self.bluetti_device.build_setter_command(
                                    "pack_num", pack
                                )
                            )

                            # We need to wait after switching packs for the data to be available
                            await asyncio.sleep(5)

                            for command in pack_commands:
                                # Request & parse result for each pack
                                try:
                                    body = command.parse_response(
                                        await self._async_send_command(command)
                                    )
                                    parsed = self.bluetti_device.parse(
                                        command.starting_address, body
                                    )
                                    _LOGGER.debug("Parsed data: %s", parsed)

                                    pack_number = parsed.get("pack_num")
                                    if (
                                        not isinstance(pack_number, int)
                                        or pack_number != pack
                                    ):
                                        _LOGGER.debug(
                                            "Parsed pack_num(%s) does not match expected '%s'",
                                            pack_number,
                                            pack,
                                        )
                                        continue

                                    for key, value in parsed.items():
                                        parsed_data.update({key + str(pack): value})

                                except ParseError:
                                    _LOGGER.warning("Got a parse exception...")

            except TimeoutError:
                _LOGGER.error("Polling timed out")
                return None
            except BleakError as err:
                _LOGGER.error("Bleak error: %s", err)
                return None
            finally:
                # Disconnect if connection not persistant
                if not self.persistent_conn:
                    if self.has_notifier:
                        await self.client.stop_notify(NOTIFY_UUID)
                        self.has_notifier = False
                    await self.client.disconnect()

            return parsed_data

    async def _async_send_command(self, command: ReadHoldingRegisters) -> bytes:
        """Send command and return response"""
        try:
            # Prepare to make request
            self.current_command = command
            self.notify_future = self.create_future()
            self.notify_response = bytearray()

            # Make request
            _LOGGER.debug("Requesting %s", command)
            await self.client.write_gatt_char(WRITE_UUID, bytes(command))

            # Wait for response
            res = await asyncio.wait_for(self.notify_future, timeout=RESPONSE_TIMEOUT)

            # Process data
            _LOGGER.debug("Got %s bytes", len(res))
            return cast(bytes, res)

        except TimeoutError:
            _LOGGER.warning("Polling single command timed out")
        except ModbusError as err:
            _LOGGER.warning(
                "Got an invalid request error for %s: %s",
                command,
                err,
            )
        except (BadConnectionError, BleakError) as err:
            _LOGGER.warning(
                "Needed to disconnect due to error: %s (This can also be the case if you used device controls)",
                err,
            )

        # caught an exception, return empty bytes object
        return bytes()

    def _notification_handler(self, _sender: int, data: bytearray):
        """Handle bt data."""

        # Ignore notifications we don't expect
        if self.notify_future is None or self.notify_future.done():
            _LOGGER.warning("Unexpected notification")
            return

        # If something went wrong, we might get weird data.
        if data == b"AT+NAME?\r" or data == b"AT+ADV?\r":
            err = BadConnectionError("Got AT+ notification")
            self.notify_future.set_exception(err)
            return

        # Save data
        self.notify_response.extend(data)

        if len(self.notify_response) == self.current_command.response_size():
            if self.current_command.is_valid_response(self.notify_response):
                self.notify_future.set_result(self.notify_response)
            else:
                self.notify_future.set_exception(ParseError("Failed checksum"))
        elif self.current_command.is_exception_response(self.notify_response):
            # We got a MODBUS command exception
            msg = f"MODBUS Exception {self.current_command}: {self.notify_response[2]}"
            self.notify_future.set_exception(ModbusError(msg))
