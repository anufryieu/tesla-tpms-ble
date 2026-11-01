"""Binary sensor platform for Tesla / Autel BLE TPMS."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info
from sensor_state_data import SensorUpdate

from .coordinator import TeslaTpmsConfigEntry
from .device import device_key_to_bluetooth_entity_key
from .entity import TeslaTPMSEntity
from .parser import TPMSBinarySensor

PARALLEL_UPDATES = 0

BINARY_SENSOR_DESCRIPTIONS: dict[str, BinarySensorEntityDescription] = {
    # The device class gives the on/off wording; the translation key keeps the
    # name "Awake", which says more here than "Running".
    TPMSBinarySensor.AWAKE: BinarySensorEntityDescription(
        key=TPMSBinarySensor.AWAKE,
        translation_key=TPMSBinarySensor.AWAKE,
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate:
    """Convert a sensor update to a Bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(
                device_key
            ): BINARY_SENSOR_DESCRIPTIONS[device_key.key]
            for device_key in sensor_update.binary_entity_descriptions
            if device_key.key in BINARY_SENSOR_DESCRIPTIONS
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.binary_entity_values.items()
        },
        # Nameless on purpose -- see the sensor platform.
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): None
            for device_key in sensor_update.binary_entity_values
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaTpmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the TPMS binary sensors."""
    coordinator = entry.runtime_data.coordinator
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            TeslaTPMSBinarySensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(
            processor, BinarySensorEntityDescription
        )
    )


class TeslaTPMSBinarySensorEntity(TeslaTPMSEntity, BinarySensorEntity):
    """A binary value from a Tesla-protocol TPMS sensor."""

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.processor.entity_data.get(self.entity_key)
