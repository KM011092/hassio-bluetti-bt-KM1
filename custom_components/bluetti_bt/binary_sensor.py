"""Bluetti BT sensors."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_NAME,
    EntityCategory,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .bluetti_bt_lib.field_attributes import FIELD_ATTRIBUTES, FieldType
from .bluetti_bt_lib.utils.device_builder import build_device

from . import device_info as dev_info, get_unique_id
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import PollingCoordinator
from .utils import unique_id_loggable

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup binary_sensor entities."""

    device_name = entry.data.get(CONF_NAME)
    address = entry.data.get(CONF_ADDRESS)
    if address is None:
        _LOGGER.error("Device has no address")

    # Generate device info
    _LOGGER.info("Creating binary_sensors for device with address %s", address)
    device_info = dev_info(entry)

    # Add sensors according to device_info
    bluetti_device = build_device(address, device_name)

    sensors_to_add = []
    all_fields = FIELD_ATTRIBUTES
    for field_key, field_config in all_fields.items():
        if bluetti_device.has_field(field_key):
            if field_config.type == FieldType.BOOL:
                category = None
                if field_config.setter is True:
                    category = EntityCategory.DIAGNOSTIC

                sensors_to_add.append(
                    BluettiBinarySensor(
                        hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR],
                        device_info,
                        address,
                        field_key,
                        field_config.name,
                        category=category,
                    )
                )

    async_add_entities(sensors_to_add)


class BluettiBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Bluetti universal binary sensor."""

    def __init__(
        self,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        address,
        response_key: str,
        name: str,
        category: EntityCategory | None = None,
    ):
        """Init battery entity."""
        super().__init__(coordinator)

        self._attr_has_entity_name = True
        e_name = f"{device_info.get('name')} {name}"
        self._address = address
        self._response_key = response_key

        self._attr_device_info = device_info
        self._attr_name = name
        self._attr_available = False
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_entity_category = category

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.reader.persistent_conn and not self.coordinator.reader.client.is_connected:
            return

        _LOGGER.debug("Updating state of %s", unique_id_loggable(self._attr_unique_id))
        if not isinstance(self.coordinator.data, dict):
            _LOGGER.debug(
                "Invalid data from coordinator (binary_sensor.%s)", unique_id_loggable(self._attr_unique_id)
            )
            self._attr_available = False
            return

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._attr_available = False
            return

        if not isinstance(response_data, bool):
            _LOGGER.warning(
                "Invalid response data type from coordinator (binary_sensor.%s): %s",
                unique_id_loggable(self._attr_unique_id),
                response_data,
            )
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_is_on = self.coordinator.data[self._response_key] is True
        self.async_write_ha_state()
